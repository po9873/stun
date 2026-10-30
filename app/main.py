"""FastAPI 后端 + WebUI。

启动钩子：初始化数据库、种入预置 STUN 服务器、启动后台轮询线程。
"""
import logging
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import (client_ctl, config, db, ikuai_client, logbuf, notifier, poller,
               settings, state, stun_probe)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stun")
logbuf.install()
log = logging.getLogger("stun.api")   # 业务操作日志，会被 WebUI 日志页收集

app = FastAPI(title="stun", version="1.0.0")
_HERE = Path(__file__).parent


# ---------------- 请求模型 ----------------
class MappingIn(BaseModel):
    name: str
    lan_ip: str
    lan_port: int
    proto: str = "tcp"          # tcp / udp / tcp+udp / any
    wan_port: Optional[int] = None
    punch: Optional[int] = 0            # 1=穿透模式（公网端口写回客户端）
    client_kind: Optional[str] = None   # qb / tr
    client_url: Optional[str] = None    # http://192.168.6.12:8086
    client_user: Optional[str] = None
    client_pass: Optional[str] = None


class MappingPatch(BaseModel):
    """只改穿透那一组字段（名称/内网地址端口不动）。"""
    punch: Optional[int] = None
    client_kind: Optional[str] = None
    client_url: Optional[str] = None
    client_user: Optional[str] = None
    client_pass: Optional[str] = None


class StunIn(BaseModel):
    host: str
    port: int = 3478
    nat_capable: int = 0


class ImportIn(BaseModel):
    ids: list = []          # 要接管的爱快规则 ID


class BulkIn(BaseModel):
    """批量录入：直接粘贴从爱快「穿透规则」页 / Lucky 界面复制出来的多行文本。"""
    text: str = ""
    create_rules: bool = True


class SettingsIn(BaseModel):
    ikuai_base_url: Optional[str] = None
    ikuai_token: Optional[str] = None      # 留空 = 沿用已保存的令牌
    ikuai_interface: Optional[str] = None
    ikuai_verify_ssl: Optional[bool] = None
    wan_port_mode: Optional[str] = None    # stun | preserve
    stun_pool_url: Optional[str] = None
    notify_enabled: Optional[bool] = None
    notify_kind: Optional[str] = None      # custom/dingtalk/wecom/feishu/bark
    notify_url: Optional[str] = None
    notify_min_gap: Optional[str] = None
    notify_on: Optional[str] = None        # port,mapping,reconcile,error


def local_ips():
    """容器自己持有的 IPv4 地址集合。

    STUN 探测只能绑定到容器自己的地址。若映射的目标机是别的 IP，容器根本绑不上去，
    测出来的公网端口是容器那条 CGNAT 映射，与目标机无关 —— 这种就没必要探测。
    """
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))       # 只借路由表推出出口 IP，不发包
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return ips


# ---------------- 启动 ----------------
@app.on_event("startup")
def startup():
    db.init()
    if not db.list_stun_servers():
        stun_probe.seed_servers()
    poller.start()
    log.info("stun 启动完成")


# ---------------- 页面 ----------------
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(_HERE / "index.html")


# ---------------- 状态 ----------------
@app.get("/api/status")
def api_status():
    st = state.get()
    mappings = db.list_mappings()
    return {
        "nat_type": st["nat_type"],
        "nat_blocked": st["nat_blocked"],
        "external_ip": st["external_ip"],
        "last_nat_check": st["last_nat_check"],
        "last_scan": st["last_scan"],
        "mock": config.MOCK,
        "counts": {
            "total": len(mappings),
            "active": sum(1 for m in mappings if m["status"] == "active"),
            "suspended": sum(1 for m in mappings if m["status"] == "suspended"),
        },
        "ikuai_configured": bool(settings.get("ikuai_token") and not config.MOCK),
        # 对账读回的爱快真实外网端口 {rule_id: "5055"}，供前端标出本地与路由不一致
        "router_wan_ports": st.get("router_wan_ports") or {},
        "last_reconcile": st.get("last_reconcile"),
    }


# ---------------- 配置（WebUI 设置页） ----------------
@app.get("/api/settings")
def api_get_settings():
    return settings.public_view()


@app.put("/api/settings")
def api_put_settings(req: SettingsIn):
    data = {}
    if req.ikuai_base_url is not None:
        data["ikuai_base_url"] = req.ikuai_base_url
    if req.ikuai_token is not None:
        data["ikuai_token"] = req.ikuai_token
    if req.ikuai_interface is not None:
        data["ikuai_interface"] = req.ikuai_interface
    if req.ikuai_verify_ssl is not None:
        data["ikuai_verify_ssl"] = "true" if req.ikuai_verify_ssl else "false"
    if req.wan_port_mode in ("stun", "preserve"):
        data["wan_port_mode"] = req.wan_port_mode
    if req.stun_pool_url is not None:
        data["stun_pool_url"] = req.stun_pool_url
    if req.notify_enabled is not None:
        data["notify_enabled"] = "true" if req.notify_enabled else "false"
    for k in ("notify_kind", "notify_min_gap", "notify_on"):
        v = getattr(req, k)
        if v is not None:
            data[k] = v
    settings.set_values(data)
    # 推送地址允许被清空（其余设置项留空表示「不变」，这里反过来）
    if req.notify_url is not None:
        db.set_setting("notify_url", req.notify_url.strip())
    log.info(f"设置已保存（外网端口模式={settings.get('wan_port_mode')}，"
             f"通知={'开' if settings.get_bool('notify_enabled') else '关'}）")
    return settings.public_view()


@app.post("/api/notify/test")
def api_notify_test():
    """设置页「发送测试」：立即推一条，用于验证地址与平台格式对不对。"""
    ok, msg = notifier.test()
    return {"ok": ok, "message": msg}


@app.post("/api/settings/test")
def api_test_settings(req: SettingsIn):
    """保存配置并试连爱快（只读拉取 DNAT 规则数量）。"""
    data = {}
    if req.ikuai_base_url is not None:
        data["ikuai_base_url"] = req.ikuai_base_url
    if req.ikuai_token is not None:
        data["ikuai_token"] = req.ikuai_token
    if req.ikuai_interface is not None:
        data["ikuai_interface"] = req.ikuai_interface
    if req.ikuai_verify_ssl is not None:
        data["ikuai_verify_ssl"] = "true" if req.ikuai_verify_ssl else "false"
    if req.wan_port_mode in ("stun", "preserve"):
        data["wan_port_mode"] = req.wan_port_mode
    if req.stun_pool_url is not None:
        data["stun_pool_url"] = req.stun_pool_url
    if req.notify_enabled is not None:
        data["notify_enabled"] = "true" if req.notify_enabled else "false"
    for k in ("notify_kind", "notify_min_gap", "notify_on"):
        v = getattr(req, k)
        if v is not None:
            data[k] = v
    settings.set_values(data)
    if req.notify_url is not None:
        db.set_setting("notify_url", req.notify_url.strip())
    try:
        client = ikuai_client.IKuaiClient()
        rules = client.list_dnat()
        log.info(f"爱快连接测试成功 {client.base_url}，读取 {len(rules)} 条 DNAT 规则")
        return {"ok": True, "api_base": client.base_url,
                "dnat_rules": len(rules), "saved": True}
    except Exception as e:
        log.error(f"爱快连接测试失败: {e}")
        return {"ok": False, "error": str(e), "saved": True}


# ---------------- 日志 ----------------
@app.get("/api/logs")
def api_logs(limit: int = 200):
    return {"items": logbuf.recent(limit)}


@app.delete("/api/logs")
def api_clear_logs():
    logbuf.clear()
    return {"ok": True}


# ---------------- 映射管理 ----------------
@app.get("/api/mappings")
def api_list_mappings():
    return db.list_mappings()


def _rule_fields(m, client):
    """这条映射在爱快上应该长什么样：中转的指到容器的独占端口，其余指到目标机。"""
    return client.rule_fields(m)


def _push_rule(m):
    """把规则内容推到爱快（中转时指向容器，关闭后指回目标机）。"""
    if not m.get("rule_id"):
        return False
    client = ikuai_client.IKuaiClient()
    fields = _rule_fields(m, client)
    want = {k: fields[k] for k in ("lan_addr", "lan_port", "wan_port")}
    for attempt in range(2):
        try:
            client.update_dnat(m["rule_id"], want, fallback=fields)
            return True
        except Exception as e:
            if attempt == 0:
                time.sleep(1.5)      # 刚创建的规则爱快有时取不完整，等一下重试
            else:
                log.error(f"[{m['name']}] 更新爱快规则失败: {e}")
    return False


@app.get("/api/relay/status")
def api_relay_status():
    """借端口已废弃：改用直接爱快映射（容器从目标机那个端口发包）测公网端口。"""
    return {"ip": "", "ports": [], "dests": {}, "deprecated": True}


@app.post("/api/mappings")
def api_create_mapping(req: MappingIn):
    if req.proto not in ("tcp", "udp", "tcp+udp", "any"):
        raise HTTPException(400, "proto 必须为 tcp/udp/tcp+udp/any")
    # wan_port 是「爱快侧端口」：默认等于内网端口（端口保留）。
    # 用户手填了才记 manual=1，这样切模式时不会被自动覆盖。
    manual = 1 if req.wan_port else 0
    wan_port = int(req.wan_port or req.lan_port)
    # 目标机不是容器自己时无法绑定源地址，探测结果无意义，直接不探测
    can_probe = req.lan_ip in local_ips()
    m = db.create_mapping(
        name=req.name, lan_ip=req.lan_ip, lan_port=req.lan_port,
        proto=req.proto, wan_port=wan_port, wan_port_manual=manual,
        probe=1 if can_probe else 0,
        punch=req.punch or 0, client_kind=req.client_kind,
        client_url=req.client_url, client_user=req.client_user,
        client_pass=req.client_pass,
    )
    client = ikuai_client.IKuaiClient()
    try:
        rid = client.create_dnat({
            "name": req.name, "lan_ip": req.lan_ip,
            "lan_port": req.lan_port, "proto": req.proto,
            "wan_port": wan_port,
        })
        if rid:
            db.update_mapping(m["id"], rule_id=rid)
        else:
            db.update_mapping(m["id"], note="爱快未返回规则ID")
    except Exception as e:
        db.update_mapping(m["id"], note=f"创建爱快规则失败: {e}")
        log.error(f"创建爱快规则失败 [{req.name}]: {e}")
        notifier.rule_failed(req.name, f"创建爱快规则失败：{e}")
    m = db.get_mapping(m["id"])
    log.info(f"新增映射 [{m['name']}] {m['lan_ip']}:{m['lan_port']} "
             f"爱快侧端口={m['wan_port']} 规则ID={m['rule_id']}")
    if not can_probe:
        log.warning(f"[{m['name']}] 目标机 {req.lan_ip} 不是容器自身，"
                    f"无法绑定源地址，跳过 STUN 探测（公网访问地址不可用）")
    notifier.mapping_created(m)
    if can_probe:
        # 后台即时探测一次，刷新「公网侧端口」（仅用于展示访问地址，不写回爱快）
        threading.Thread(target=poller._provision_probe, args=(m["id"],), daemon=True).start()
    return m


def _punch_now(mid):
    """开/关穿透后立刻生效：先问一次公网端口，再同步到爱快与客户端。"""
    try:
        m = db.get_mapping(mid)
        if not m or m["status"] != "active":
            return
        if not m.get("punch"):
            poller.restore_punch(ikuai_client.IKuaiClient(), m)
            return
        ext_ip, ext_port, exact = stun_probe.probe_mapping_port(
            m["lan_ip"], m["lan_port"], proto=m["proto"])
        if ext_port:
            db.update_mapping(mid, public_port=ext_port, public_exact=1)
        poller._sync_punch(ikuai_client.IKuaiClient(), db.get_mapping(mid), force=True)
    except Exception as e:
        log.error(f"穿透同步失败: {e}")


@app.put("/api/mappings/{mid}")
def api_patch_mapping(mid: int, req: MappingPatch):
    """改穿透设置（名称、内网地址端口不动）。"""
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(404, "映射不存在")
    fields = {k: v for k, v in req.dict(exclude_none=True).items()}
    if not fields:
        return m
    url = fields.get("client_url", m.get("client_url"))
    if fields.get("punch") is not None and int(fields["punch"]) == 1 and not url:
        raise HTTPException(400, "开穿透要先填客户端地址")
    db.update_mapping(mid, **fields)
    log.info(f"[{m['name']}] 穿透设置已保存：{fields}")
    threading.Thread(target=_punch_now, args=(mid,), daemon=True).start()
    return db.get_mapping(mid)


@app.post("/api/mappings/{mid}/client/test")
def api_test_client(mid: int):
    """测一下客户端地址/账号对不对，返回它当前的监听端口。"""
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(404, "映射不存在")
    try:
        return {"ok": True, "port": client_ctl.test(m)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})$")
_IPPORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})[:：](\d{1,5})$")
_PROTO_ALIAS = {
    "tcp": "tcp", "tcp4": "tcp", "tcp6": "tcp",
    "udp": "udp", "udp4": "udp", "udp6": "udp",
    "tcp+udp": "tcp+udp", "tcpudp": "tcp+udp", "udp+tcp": "tcp+udp",
    "tcp4+udp4": "tcp+udp", "all": "any", "any": "any",
}


def parse_rule_line(line):
    """从一行文本里挖出「名称 / 内网 IP / 内网端口 / 协议」。

    容忍各种粘贴格式，例如爱快穿透规则页复制出来的：
        UDP4  :39213  EasyTier-udp  —  —  192.168.6.42:27834
    或手写：
        QB-RSS, 192.168.6.13, 46065, tcp+udp

    注意：行里的 :39213 这种公网端口一律丢弃 —— 按 CGNAT 端口保留模型，
    爱快 DNAT 的外网端口必须等于内网端口，把公网端口填进去规则就废了。
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None
    toks = [t for t in re.split(r"[\s,，;；|]+", s) if t and t not in ("—", "-", "–")]

    lan_ip = lan_port = None
    proto = None
    name = None
    for t in toks:
        m = _IPPORT_RE.match(t)             # 192.168.6.42:27834
        if m:
            lan_ip, lan_port = m.group(1), int(m.group(2))
            continue
        if _IP_RE.match(t):                 # 192.168.6.13（端口在下一个 token）
            lan_ip = t
            continue
        low = t.lower()
        if low in _PROTO_ALIAS:
            proto = _PROTO_ALIAS[low]
            continue
        m2 = re.fullmatch(r"(:?)(\d{1,5})", t)
        if m2:
            n = int(m2.group(2))
            # 带冒号前缀的（如 :39213）明确是公网端口，直接丢弃，别拿它当名字
            if m2.group(1):
                continue
            # 紧跟在 IP 后面且还没定端口 → 内网端口；否则是公网端口，丢弃
            if lan_ip and lan_port is None and 1 <= n <= 65535:
                lan_port = n
            continue
        if name is None:
            name = t
    if not lan_ip or not lan_port:
        return None
    if not (1 <= lan_port <= 65535):
        return None
    # 名称里混入的协议后缀如 EasyTier-udp 不算协议，别被它误导
    return {
        "name": name or f"{lan_ip}:{lan_port}",
        "lan_ip": lan_ip,
        "lan_port": lan_port,
        "proto": proto or "tcp",
    }


@app.post("/api/mappings/bulk")
def api_bulk_mappings(req: BulkIn):
    """批量录入映射。用来把 Lucky / 爱快穿透规则页上的规则搬过来。"""
    parsed, bad = [], []
    for ln in (req.text or "").splitlines():
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        r = parse_rule_line(ln)
        if r:
            parsed.append(r)
        else:
            bad.append(ln.strip()[:40])

    have = {(m["lan_ip"], int(m["lan_port"]), m["proto"]) for m in db.list_mappings()}
    mine = local_ips()
    created, skipped = [], []
    for r in parsed:
        key = (r["lan_ip"], r["lan_port"], r["proto"])
        if key in have:
            skipped.append({"name": r["name"], "reason": "本地已有相同映射"})
            continue
        can_probe = r["lan_ip"] in mine
        m = db.create_mapping(
            name=r["name"], lan_ip=r["lan_ip"], lan_port=r["lan_port"],
            proto=r["proto"], wan_port=r["lan_port"], wan_port_manual=0,
            probe=1 if can_probe else 0,
        )
        if req.create_rules:
            try:
                rid = ikuai_client.IKuaiClient().create_dnat({
                    "name": r["name"], "lan_ip": r["lan_ip"],
                    "lan_port": r["lan_port"], "proto": r["proto"],
                    "wan_port": r["lan_port"],
                })
                if rid:
                    db.update_mapping(m["id"], rule_id=rid)
            except Exception as e:
                db.update_mapping(m["id"], note=f"创建爱快规则失败: {e}")
                log.error(f"批量创建爱快规则失败 [{r['name']}]: {e}")
                notifier.rule_failed(r["name"], f"创建爱快规则失败：{e}")
        have.add(key)
        created.append(db.get_mapping(m["id"]))
        log.info(f"批量新增 [{r['name']}] {r['lan_ip']}:{r['lan_port']} ({r['proto']})")
        notifier.mapping_created(db.get_mapping(m["id"]))
    return {"created": created, "skipped": skipped, "unparsed": bad}


@app.delete("/api/mappings/{mid}")
def api_delete_mapping(mid: int):
    m = db.get_mapping(mid)
    if not m:
        raise HTTPException(404, "映射不存在")
    if m["rule_id"]:
        try:
            ikuai_client.IKuaiClient().delete_dnat(m["rule_id"])
            log.info(f"删除映射 [{m['name']}] 并移除爱快规则 {m['rule_id']}")
        except Exception as e:
            log.error(f"删除爱快规则失败: {e}")
            notifier.rule_failed(m["name"], f"删除爱快规则失败：{e}")
    db.delete_mapping(mid)
    notifier.mapping_deleted(m)
    return {"ok": True}


@app.post("/api/mappings/{mid}/resume")
def api_resume_mapping(mid: int):
    m = poller.resume_mapping(mid)
    if not m:
        raise HTTPException(404, "映射不存在")
    log.info(f"手动恢复映射 [{m['name']}]")
    return m


# ---------------- 从爱快发现 / 接管已有规则 ----------------
@app.get("/api/ikuai/rules")
def api_ikuai_rules():
    """列出爱快上现有的 DNAT 规则，并标出哪些已经被本工具接管。"""
    try:
        rules = ikuai_client.IKuaiClient().read_rules()
    except Exception as e:
        log.error(f"读取爱快规则失败: {e}")
        return {"ok": False, "error": str(e), "rules": []}
    taken = {m["rule_id"] for m in db.list_mappings() if m.get("rule_id")}
    for r in rules:
        r["taken"] = r["id"] in taken
    return {"ok": True, "error": None, "rules": rules}


@app.post("/api/ikuai/import")
def api_ikuai_import(req: ImportIn):
    """接管爱快上已有的规则：只在本地登记，不改动路由上的任何设置。

    - 沿用爱快现有的内网地址/端口与外网端口，外网端口记为「用户手填」，
      这样切换端口模式时不会被自动改写。
    - 爱快上已禁用的规则接管为 suspended，对账不会去碰它。
    """
    ids = [int(i) for i in (req.ids or [])]
    if not ids:
        return {"imported": 0, "skipped": [], "mappings": db.list_mappings()}
    try:
        rules = {r["id"]: r for r in ikuai_client.IKuaiClient().read_rules()}
    except Exception as e:
        log.error(f"导入失败，无法读取爱快规则: {e}")
        raise HTTPException(502, f"读取爱快规则失败: {e}")

    have_rule = {m["rule_id"] for m in db.list_mappings() if m.get("rule_id")}
    have_key = {(m["lan_ip"], int(m["lan_port"]), m["proto"])
                for m in db.list_mappings()}

    imported, skipped = 0, []
    mine = local_ips()
    for rid in ids:
        r = rules.get(rid)
        if not r:
            skipped.append({"id": rid, "reason": "爱快上已不存在"})
            continue
        if not r["adoptable"]:
            skipped.append({"id": rid, "reason": r["reason"] or "不支持接管"})
            continue
        if rid in have_rule:
            skipped.append({"id": rid, "reason": "已接管"})
            continue
        if (r["lan_addr"], r["lan_port"], r["proto"]) in have_key:
            skipped.append({"id": rid, "reason": "本地已有相同内网地址+端口的映射"})
            continue
        # 目标机不是容器自己时测不出有意义的公网端口，直接不探测
        can_probe = r["lan_addr"] in mine
        m = db.create_mapping(
            name=r["name"], lan_ip=r["lan_addr"], lan_port=r["lan_port"],
            proto=r["proto"], wan_port=r["wan_port"] or r["lan_port"],
            rule_id=rid, wan_port_manual=1, probe=1 if can_probe else 0,
        )
        if not r["enabled"]:
            db.update_mapping(m["id"], status="suspended",
                              note="接管时该规则在爱快上是禁用状态")
        have_rule.add(rid)
        have_key.add((r["lan_addr"], r["lan_port"], r["proto"]))
        imported += 1
        log.info(f"接管爱快规则 {rid} [{r['name']}] {r['lan_addr']}:{r['lan_port']} "
                 f"外网端口={r['wan_port']}"
                 + ("" if can_probe else "（非本机，不探测公网端口）"))
        if can_probe:
            # 后台即时探测一次，把「公网访问地址」测出来
            threading.Thread(target=poller._provision_probe,
                             args=(m["id"],), daemon=True).start()
    if imported:
        log.info(f"从爱快导入完成：接管 {imported} 条，跳过 {len(skipped)} 条")
    return {"imported": imported, "skipped": skipped, "mappings": db.list_mappings()}


# ---------------- STUN 服务器 ----------------
@app.get("/api/stun")
def api_list_stun():
    return db.list_stun_servers()


@app.get("/api/stun/servers")
def api_active_stun_servers():
    """给探针客户端用：返回当前可用的服务器清单。"""
    return [s for s in db.list_stun_servers() if s.get("status") == "active"]


@app.post("/api/stun/scan")
def api_scan_stun():
    res = stun_probe.scan_servers()
    ok = sum(1 for r in res if r["ok"])
    log.info(f"STUN 扫描完成：{ok}/{len(res)} 台可用")
    return {"results": res}


@app.post("/api/stun/refresh")
def api_refresh_pool():
    """从 always-online-stun 在线拉取最新的可用服务器清单并入池。

    只更新清单，不连带扫描（扫描由前端单独触发，或等下次定时扫描），
    避免一次请求卡住几十秒。
    """
    r = stun_probe.refresh_pool(force=True)
    if r["ok"]:
        log.info(f"STUN 清单已更新：{r['total']} 条，新增 {r['added']} 条")
    else:
        log.warning(f"STUN 清单更新失败：{r['error']}（沿用内置清单）")
    return r


@app.post("/api/mappings/reconcile")
def api_reconcile():
    """手动与爱快对账一次：把本地记录的公网端口推到路由上，纠正两边不一致。"""
    log.info("收到手动对账请求")
    poller._reconcile(ikuai_client.IKuaiClient())
    return db.list_mappings()


@app.post("/api/stun")
def api_add_stun(req: StunIn):
    db.upsert_stun_server(req.host, req.port, status="active", manual=1,
                          fail_count=0, nat_capable=req.nat_capable)
    return db.list_stun_servers()


@app.delete("/api/stun/{sid}")
def api_delete_stun(sid: int):
    db.delete_stun_server(sid)


# ---------------- 探针客户端（跑在内网的目标机上） ----------------
@app.get("/probe.py", response_class=PlainTextResponse)
def api_probe_script(request: Request):
    """下载探针脚本。地址已自动填好，拿过去直接 `python3 probe.py <端口>`。"""
    src = (_HERE.parent / "probe_client.py").read_text(encoding="utf-8")
    base = str(request.base_url).rstrip("/")
    src = src.replace('__import__("os").environ.get("STUN_SERVER")',
                      '"%s"' % base)
    return src


class ProbeReportIn(BaseModel):
    lan_ip: str
    lan_port: int
    public_ip: Optional[str] = None
    public_port: Optional[int] = None


def _apply_report(lan_ip, results):
    """把目标机自己测出来的 (lan_ip, lan_port, 公网端口) 写进库。

    容器测别的机器是测不准的：爱快 NAT 发现那个外部端口已被目标机占着，
    会给容器另分一个，测到的是容器自己那条 CGNAT 映射（实测目标机 39213、
    容器测出 2048）。所以只有目标机自己发包的测量结果才是权威值，存的时候
    标 public_exact=1，并保留 probe=0（容器不必再白测）。
    """
    # 目标机是容器自己时，容器本来就能测准，保留轮询让它持续保活；
    # 只有别人报上来的（容器测不了的那些）才关掉轮询，免得拿垃圾值盖掉权威值
    keep_probe = lan_ip in local_ips()
    seen = int(time.time())
    matched = []
    for r in results or []:
        if not r.get("ok") or not r.get("public_port"):
            continue
        port = int(r["lan_port"])
        for m in db.list_mappings():
            if m["lan_ip"] != lan_ip or int(m["lan_port"]) != port:
                continue
            old = m.get("public_port")
            db.update_mapping(m["id"], public_port=r["public_port"],
                              public_exact=1, probe=1 if keep_probe else 0,
                              agent_seen=seen)
            matched.append(m["name"])
            if old != r["public_port"]:
                log.info(f"探针回报 [{m['name']}] 公网端口 {old} -> {r['public_port']}")
                try:
                    notifier.public_port_changed(m, old, r["public_port"],
                                                 ext_ip=r.get("public_ip"))
                except Exception:
                    pass
        if r.get("public_ip"):
            state.set(external_ip=r["public_ip"])
    return matched


@app.post("/api/probe/report")
def api_probe_report(req: ProbeReportIn):
    matched = _apply_report(req.lan_ip, [{
        "lan_port": req.lan_port, "public_ip": req.public_ip,
        "public_port": req.public_port, "ok": 1 if req.public_port else 0,
    }])
    return {"ok": True, "matched": matched or "本机没有端口匹配的映射"}


# ---------------- 常驻探针 agent：装一次，之后全自动 ----------------
@app.get("/agent.py", response_class=PlainTextResponse)
def api_agent_script(request: Request):
    """下载常驻探针源码（服务端地址已填好，直接 python3 agent.py 就能跑）。"""
    src = (_HERE.parent / "agent_client.py").read_text(encoding="utf-8")
    base = str(request.base_url).rstrip("/")
    return src.replace('os.environ.get("STUN_SERVER") or ""', '"%s"' % base)


INSTALL_SH = r"""#!/bin/sh
# stun 常驻探针一键安装（Linux）
#   curl -fsSL __BASE__/agent/install.sh | sudo sh
# 装完自己常驻、开机自启；以后在页面上加端口，它自己会拉到新清单。
set -e
BASE=__BASE__
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "这台机没有 python3，先装一个：apt-get install -y python3 （或 yum install -y python3）"
  exit 1
fi
DIR=/opt/stun
mkdir -p "$DIR"
echo "下载探针 -> $DIR/agent.py"
curl -fsSL "$BASE/agent.py" -o "$DIR/agent.py"
chmod +x "$DIR/agent.py"
echo "先跑一轮自检："
"$PY" "$DIR/agent.py" --server "$BASE" --once || true

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  cat > /etc/systemd/system/stun-agent.service <<UNIT
[Unit]
Description=stun public port agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $DIR/agent.py --server $BASE --interval 30
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable stun-agent >/dev/null 2>&1 || true
  systemctl restart stun-agent
  echo "OK 已注册 systemd 服务 stun-agent（开机自启，崩了自动拉起）"
  echo "   看状态：systemctl status stun-agent    看日志：journalctl -u stun-agent -f"
else
  # 没有 systemd 的机器（OpenWrt / 老系统）：现在拉起来，并写进 crontab 开机自启
  nohup "$PY" "$DIR/agent.py" --server "$BASE" --interval 30 >>/var/log/stun-agent.log 2>&1 &
  ( crontab -l 2>/dev/null | grep -v 'stun-agent' ; \
    echo "@reboot $PY $DIR/agent.py --server $BASE --interval 30 >>/var/log/stun-agent.log 2>&1 &" ) \
    | crontab - 2>/dev/null || true
  echo "OK 已启动并写入 crontab @reboot（日志 /var/log/stun-agent.log）"
fi
echo "以后这台机的公网端口会自动上报到 $BASE"
"""

INSTALL_PS1 = r"""# stun 常驻探针一键安装（Windows），管理员 PowerShell 里跑：
#   iex (iwr -useb __BASE__/agent/install.ps1)
$Base = "__BASE__"
$Dir  = Join-Path $env:ProgramData "stun"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Write-Host "下载探针 -> $Dir\agent.py"
Invoke-WebRequest -UseBasicParsing "$Base/agent.py" -OutFile "$Dir\agent.py"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "没找到 Python，先装 https://www.python.org/downloads/ （记得勾 Add to PATH）"
  exit 1
}
$PyPath = $py.Source
& $PyPath "$Dir\agent.py" --server $Base --once
schtasks /Create /TN "stun-agent" /SC ONSTART /RU SYSTEM /RL HIGHEST /F /TR "`"$PyPath`" `"$Dir\agent.py`" --server $Base --interval 30" | Out-Null
schtasks /Run /TN "stun-agent" | Out-Null
Write-Host "装好了：开机自启，公网端口会自动上报到 $Base"
"""


@app.get("/agent/install.sh", response_class=PlainTextResponse)
def api_install_sh(request: Request):
    return INSTALL_SH.replace("__BASE__", str(request.base_url).rstrip("/"))


@app.get("/agent/install.ps1", response_class=PlainTextResponse)
def api_install_ps1(request: Request):
    return INSTALL_PS1.replace("__BASE__", str(request.base_url).rstrip("/"))


@app.get("/api/agent/plan")
def api_agent_plan(request: Request, ip: str = ""):
    """目标机来问「我该测哪些端口」。

    清单按来源 IP 现算，所以页面上加/改端口都不用再动那台机 —— 它下一轮自己就拉到了。
    """
    lan_ip = (ip or "").strip() or (request.client.host if request.client else "")
    interval = int(db.get_setting("agent_interval") or 30)
    ports = []
    for m in db.list_mappings():
        if m["lan_ip"] != lan_ip:
            continue
        # 纯 UDP 目前测不了（UDP STUN 走不通），别让它白忙
        if "tcp" not in (m["proto"] or "").lower():
            continue
        p = int(m["lan_port"])
        if p not in ports:
            ports.append(p)
    servers = ["%s:%s" % (s["host"], s["port"])
               for s in db.list_stun_servers() if s.get("status") == "active"][:5]
    return {"lan_ip": lan_ip, "ports": ports, "interval": interval,
            "servers": servers or ["stun.chat.bilibili.com:3478",
                                   "global.stun.twilio.com:3478"]}


class AgentReportIn(BaseModel):
    lan_ip: str
    results: list = []
    hostname: Optional[str] = None
    interval: Optional[int] = None
    version: Optional[str] = None


@app.post("/api/agent/report")
def api_agent_report(req: AgentReportIn):
    """收常驻探针批量回报的公网端口。"""
    matched = _apply_report(req.lan_ip, req.results)
    if req.interval:
        db.set_setting("agent_interval", str(int(req.interval)))
    return {"ok": True, "matched": matched}


@app.get("/api/agent/hosts")
def api_agent_hosts():
    """每台目标机的探针状态：有没有在线、最后一次上报是什么时候。"""
    now_ts = int(time.time())
    hosts = {}
    for m in db.list_mappings():
        h = hosts.setdefault(m["lan_ip"], {"lan_ip": m["lan_ip"], "ports": [],
                                           "seen": 0, "got": 0})
        h["ports"].append(int(m["lan_port"]))
        h["seen"] = max(h["seen"], int(m.get("agent_seen") or 0))
        if m.get("public_port"):
            h["got"] += 1
    out = []
    for h in hosts.values():
        h["ports"].sort()
        h["online"] = bool(h["seen"] and now_ts - h["seen"] < 180)
        h["age"] = (now_ts - h["seen"]) if h["seen"] else None
        out.append(h)
    out.sort(key=lambda x: [int(p) for p in x["lan_ip"].split(".")])
    return out


class DeployIn(BaseModel):
    ip: str
    user: str = "root"
    password: str = ""
    port: int = 22
    kind: str = "auto"          # auto / linux / windows


def _shq(s):
    """把字符串安全地塞进远程 shell 的单引号里。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _ssh(host, user, pwd, port, cmd, timeout=60):
    args = ["sshpass", "-p", pwd, "ssh",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
            "-p", str(port), "%s@%s" % (user, host), cmd]
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "远程执行超时（%ss）" % timeout
    except FileNotFoundError:
        return 127, "容器里没装 sshpass/ssh，镜像需包含 openssh-client 与 sshpass"


@app.post("/api/agent/deploy")
def api_agent_deploy(request: Request, req: DeployIn):
    """替用户把探针装到目标机上：页面填 IP + 账号密码，剩下的容器自己走 SSH 干完。"""
    base = str(request.base_url).rstrip("/")
    kind = req.kind
    if kind == "auto":
        rc, out = _ssh(req.ip, req.user, req.password, req.port, "uname -s", timeout=20)
        if rc != 0:
            log.warning(f"SSH 连不上 {req.ip}: {out.strip()[:200]}")
            return {"ok": False, "stage": "connect", "output": out.strip()[-2000:]}
        u = out.lower()
        kind = ("windows" if ("mingw" in u or "cygwin" in u or "windows" in u)
                else "linux")
    if kind == "windows":
        cmd = ('powershell -NoProfile -Command '
               '"iex (iwr -useb %s/agent/install.ps1)"' % base)
    else:
        inner = (
            'curl -fsSL %s/agent/install.sh -o /tmp/stun-agent-install.sh && '
            '{ if [ "$(id -u)" = 0 ]; then sh /tmp/stun-agent-install.sh; '
            'else printf "%%s\\n" %s | sudo -S -p "" sh /tmp/stun-agent-install.sh; fi }'
            % (base, _shq(req.password)))
        cmd = "sh -c %s" % _shq(inner)
    rc, out = _ssh(req.ip, req.user, req.password, req.port, cmd, timeout=240)
    ok = rc == 0
    txt = out.strip()
    if ok:
        log.info(f"已自动把探针装到 {req.ip}（{kind}）")
    else:
        log.error(f"自动部署到 {req.ip} 失败：{txt[:300]}")
    return {"ok": ok, "stage": "install", "kind": kind,
            "output": txt[-4000:] or ("无输出，退出码 %s" % rc)}


@app.post("/api/ikuai/tidy")
def api_tidy_rules():
    """整理爱快上的规则 ID：把历史空号（删规则留下的）抹掉，重排成 1..N 连续。

    做法是全量读出 → 全部删除 → 按原顺序重建，ID 会重新从 1 开始。
    重建前会在容器里留一份备份，中途失败也能按备份补回来。
    注意：重建期间路由上的映射会短暂失效（十几秒）。
    """
    import json
    import os

    client = ikuai_client.IKuaiClient()
    raws = client.list_dnat()
    if not raws:
        return {"ok": False, "error": "路由上没有规则"}

    items, old_ids = [], []
    for r in sorted(raws, key=lambda x: int(x.get("id") or 0)):
        old_ids.append(int(r.get("id") or 0))
        addr = (r.get("lan_addr") or "").strip()
        if not addr or addr == "0.0.0.0":
            addr = ikuai_client.int_to_ip(r.get("lan_addr_int")) or ""
        if not addr:
            continue
        items.append({
            "tagname": (r.get("tagname") or "").strip(),
            "lan_addr": addr,
            "lan_port": str(r.get("lan_port") or ""),
            "wan_port": str(r.get("wan_port") or ""),
            "protocol": r.get("protocol") or "tcp",
            "interface": r.get("interface") or client.interface,
            "enabled": str(r.get("enabled") or "yes"),
            "comment": r.get("comment") or "",
            "src_addr": r.get("src_addr") or {"custom": [], "object": []},
        })

    backup = os.path.join(config.DATA_DIR, "ikuai_backup_%d.json" % int(time.time()))
    try:
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception as e:
        backup = "（写备份失败：%s）" % e

    for rid in old_ids:
        try:
            client._req("DELETE", "/network/dnat/rules/%d" % rid)
        except Exception as e:
            log.warning(f"整理 ID：删旧规则 {rid} 失败 {e}")

    new_ids, failed = [], 0
    for it in items:
        try:
            d = client._req("POST", "/network/dnat/rules", it)
            new_ids.append(int(d.get("rowid")))
        except Exception as e:
            failed += 1
            log.error(f"整理 ID：重建 {it.get('tagname')} 失败 {e}")

    # 按 (内网地址, 内网端口) 把新 ID 写回本地
    for r in client.read_rules():
        for m in db.list_mappings():
            if (m["lan_ip"] == r["lan_addr"]
                    and int(m["lan_port"]) == int(r["lan_port"])
                    and m["rule_id"] != r["id"]):
                db.update_mapping(m["id"], rule_id=r["id"])

    log.info(f"整理 ID 完成：{old_ids} -> {new_ids}（失败 {failed} 条，备份 {backup}）")
    return {"ok": failed == 0, "before": old_ids, "after": new_ids,
            "failed": failed, "backup": backup}
