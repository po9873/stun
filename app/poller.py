"""后台轮询线程。

职责：
1. 每分钟：遍历 active 映射，用各自内网端口问 STUN；端口变化只改 wan_port；
   单映射连续失败 5 次 -> 挂起（删爱快规则、保留内网信息）。
2. 每小时：NAT 复检；NAT4/阻断 -> 全部挂起；恢复 -> 自动重建规则。
3. 每小时：扫描 STUN 服务器池，更新可用性与延迟。

原则：删的是爱快规则，本地内网信息始终保留，恢复时原样重建。
"""
import logging
import threading
import time

from . import config, db, ikuai_client, stun_probe, state

log = logging.getLogger("stun.poller")

_thread = None
_stop = threading.Event()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="stun-poller", daemon=True)
    _thread.start()
    log.info("轮询线程已启动")


def stop():
    _stop.set()


def _loop():
    client = ikuai_client.IKuaiClient()
    last_scan = 0.0
    last_nat = 0.0
    last_pool = 0.0
    last_reconcile = 0.0
    _check_nat(client)
    stun_probe.scan_servers()
    _reconcile(client)
    last_reconcile = time.time()
    while not _stop.is_set():
        now = time.time()
        if now - last_reconcile >= config.RECONCILE_INTERVAL:
            _reconcile(client)
            last_reconcile = now
        if config.STUN_POOL_ENABLED and now - last_pool >= config.STUN_POOL_REFRESH_INTERVAL:
            r = stun_probe.refresh_pool()
            log.info(f"STUN 池在线更新: ok={r['ok']} 新增={r['added']}/{r['total']}"
                     + (f" 失败原因={r['error']}" if not r["ok"] else ""))
            last_pool = now
        if now - last_nat >= config.NAT_CHECK_INTERVAL:
            _check_nat(client)
            last_nat = now
        if now - last_scan >= config.STUN_SCAN_INTERVAL:
            stun_probe.scan_servers()
            last_scan = now
        _poll_mappings(client)
        _stop.wait(config.POLL_INTERVAL)


def _poll_mappings(client):
    for m in db.list_mappings():
        if m["status"] != "active":
            continue
        server = db.get_best_stun_server()
        if not server:
            # 池子全被判 down：立刻扫一遍自救，而不是干等下个小时
            log.warning("无可用 STUN 服务器，立即重新扫描")
            stun_probe.scan_servers()
            server = db.get_best_stun_server()
            if not server:
                continue
        ext_ip, ext_port, exact = stun_probe.probe_mapping_port(
            m["lan_ip"], m["lan_port"], server
        )
        if ext_port is None:
            fc = (m["fail_count"] or 0) + 1
            if fc >= 5:
                log.warning(f"映射[{m['id']} {m['name']}] 连续 5 次失败，挂起")
                _suspend(client, m, reason="连续5次STUN失败")
            else:
                db.update_mapping(m["id"], fail_count=fc)
            continue
        # 探测成功
        db.update_mapping(m["id"], fail_count=0)
        if m.get("public_port") != ext_port:
            log.info(f"公网端口变化 [{m['name']}] {m.get('public_port')} -> {ext_port}"
                     f" ({'精确' if exact else '近似'})")
            db.update_mapping(m["id"], public_port=ext_port)
        # 每轮都对齐一次，这样切模式/重启后能自动纠正不一致
        _sync_wan_port(client, db.get_mapping(m["id"]), ext_port)


def _check_nat(client):
    label, ext_ip, ext_port = stun_probe.nat_detect()
    # 只有「对称型」和「完全无响应」才真正阻断映射；
    # unknown/error 只是本次没测准，不该把已有映射全部挂掉。
    blocked = label in ("nat4", "blocked")
    state.set(nat_type=label, nat_blocked=blocked,
              external_ip=ext_ip, last_nat_check=db.now())
    log.info(f"NAT 检测: {label} blocked={blocked} ext_ip={ext_ip}")
    if blocked:
        for m in db.list_mappings():
            if m["status"] == "active":
                _suspend(client, m, reason=f"NAT异常({label})")
    else:
        # 恢复：仅重建因 NAT 异常挂起的规则
        for m in db.list_mappings():
            if m["status"] == "suspended" and (m["note"] or "").startswith("NAT"):
                _resume(client, m)


def _sync_wan_port(client, m, public_port):
    """按模式决定是否把探测到的公网端口同步进爱快 DNAT 的 wan_port。

    stun     模式：wan_port = STUN 实测公网端口（无公网/CGNAT 常用）
    preserve 模式：wan_port = 内网端口（端口保留），公网端口只展示
    """
    from . import settings
    if not m:
        return
    if settings.get("wan_port_mode") != "stun":
        return
    if m.get("wan_port") == public_port:
        return
    old = m.get("wan_port")
    # 先推爱快，成功后再落库：万一推送失败，本地仍留旧值，下一轮会自然重试，
    # 不会变成「本地已改、路由没改，且此后永远不再推」的僵局。
    if m.get("rule_id"):
        fallback = {
            "lan_addr": m["lan_ip"],
            "lan_port": str(int(m["lan_port"])),
            "protocol": m["proto"],
            "interface": client.interface,
            "tagname": m["name"],
        }
        pushed = False
        # 刚创建的规则，爱快 GET 可能短暂取不完整，失败后等一下重试一次
        for attempt in range(2):
            try:
                client.update_dnat(m["rule_id"],
                                   {"wan_port": str(int(public_port))},
                                   fallback=fallback)
                pushed = True
                break
            except Exception as e:
                if attempt == 0:
                    log.warning(f"[{m['name']}] 更新爱快规则失败，重试一次: {e}")
                    time.sleep(1.5)
                else:
                    log.error(f"[{m['name']}] 更新爱快规则失败: {e}")
        if not pushed:
            return
    if old != public_port:
        log.info(f"[{m['name']}] 公网端口 {old} -> {public_port}，爱快外网端口已同步")
    db.update_mapping(m["id"], wan_port=public_port)


def _reconcile(client):
    """与爱快对账：拿路由上的真实规则，发现 wan_port 不一致就推回去。

    解决这类问题：本地记录改了但那次推送失败（规则刚创建时爱快 GET 取不完整等），
    之后数值再没变过，常规轮询就不会再推，两边长期不一致。
    """
    try:
        rules = client.list_dnat()
    except Exception as e:
        log.warning(f"对账失败，无法读取爱快规则: {e}")
        return
    from . import settings as _settings
    router = {}
    raw = {}
    for r in rules:
        try:
            rid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        router[rid] = str(r.get("wan_port"))
        raw[rid] = r
    # 把路由上的真实值公开给 WebUI，让「本地 vs 爱快」不一致能被直接看到
    state.set(router_wan_ports={str(k): v for k, v in router.items()},
              last_reconcile=db.now())

    stun_mode = _settings.get("wan_port_mode") == "stun"
    fixed = 0
    for m in db.list_mappings():
        if m["status"] != "active" or not m.get("rule_id"):
            continue
        rid = int(m["rule_id"])
        if rid not in router:
            continue
        manual = bool(m.get("wan_port_manual"))
        if stun_mode:
            # 实测公网端口为准：本地记录可能因历史问题落后于实测值
            want = str(int(m.get("public_port") or m["wan_port"] or m["lan_port"]))
        elif manual:
            # 用户手填的外网端口，尊重它
            want = str(int(m["wan_port"] or m["lan_port"]))
        else:
            # 端口保留：外网端口一律等于内网端口
            want = str(int(m["lan_port"]))
        # 本地记录跟模式不一致时，先纠正本地，再和路由比对
        if m.get("wan_port") != int(want):
            db.update_mapping(m["id"], wan_port=int(want))
        if router[rid] == want:
            continue
        r = raw[rid]
        fallback = {
            "lan_addr": (r.get("lan_addr") or "").strip()
                        or ikuai_client.IKuaiClient._int_to_ip(r.get("lan_addr_int"))
                        or m["lan_ip"],
            "lan_port": str(int(r.get("lan_port") or m["lan_port"])),
            "protocol": r.get("protocol") or m["proto"],
            "interface": r.get("interface") or client.interface,
            "tagname": r.get("tagname") or m["name"],
        }
        try:
            client.update_dnat(rid, {"wan_port": want}, fallback=fallback)
            fixed += 1
            log.info(f"对账修正 [{m['name']}] 爱快外网端口 {router[rid]} -> {want}")
            router[rid] = want
        except Exception as e:
            log.error(f"对账修正失败 [{m['name']}]: {e}")
    if fixed:
        log.info(f"对账完成，修正 {fixed} 条规则")
        state.set(router_wan_ports={str(k): v for k, v in router.items()})


def _suspend(client, m, reason):
    """挂起：删除爱快规则，保留本地内网信息，标记 suspended。"""
    if m["rule_id"]:
        try:
            client.delete_dnat(m["rule_id"])
        except Exception as e:
            log.error(f"挂起时删除爱快规则失败: {e}")
    db.update_mapping(m["id"], status="suspended", rule_id=None,
                      note=reason, fail_count=0)


def _resume(client, m):
    """恢复：按当前模式重建爱快规则。

    preserve 且非手填 → 外网端口 = 内网端口；手填 → 用填的值；stun 模式 → 用实测值。
    """
    from . import settings as _settings
    if _settings.get("wan_port_mode") == "stun" and m.get("public_port"):
        wan_port = m["public_port"]
    elif m.get("wan_port_manual") and m.get("wan_port"):
        wan_port = m["wan_port"]
    else:
        wan_port = m["lan_port"]
    try:
        rid = client.create_dnat({
            "name": m["name"], "lan_ip": m["lan_ip"],
            "lan_port": m["lan_port"], "proto": m["proto"],
            "wan_port": wan_port,
        })
        db.update_mapping(m["id"], status="active", rule_id=rid,
                          note="", fail_count=0)
        log.info(f"映射[{m['name']}] 已恢复，规则ID={rid}")
    except Exception as e:
        log.error(f"恢复映射失败: {e}")


def resume_mapping(id):
    """供 WebUI 手动恢复调用。"""
    m = db.get_mapping(id)
    if not m:
        return None
    if m["status"] == "active":
        return m
    client = ikuai_client.IKuaiClient()
    _resume(client, m)
    # 立即触发一次探测，刷新 wan_port
    threading.Thread(target=_provision_probe, args=(id,), daemon=True).start()
    return db.get_mapping(id)


def _provision_probe(id):
    """新增/恢复后即时探测一次，刷新「公网侧端口」（仅展示，不改爱快规则）。"""
    m = db.get_mapping(id)
    if not m or m["status"] != "active":
        return
    server = db.get_best_stun_server()
    if not server:
        return
    ext_ip, ext_port, exact = stun_probe.probe_mapping_port(
        m["lan_ip"], m["lan_port"], server
    )
    if ext_port is None:
        return
    db.update_mapping(id, public_port=ext_port, fail_count=0)
    _sync_wan_port(ikuai_client.IKuaiClient(), db.get_mapping(id), ext_port)
