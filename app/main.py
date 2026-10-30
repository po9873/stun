"""FastAPI 后端 + WebUI。

启动钩子：初始化数据库、种入预置 STUN 服务器、启动后台轮询线程。
"""
import logging
import re
import socket
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import (config, db, ikuai_client, logbuf, notifier, poller, settings,
               state, stun_probe)

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
    return {"ok": True}
