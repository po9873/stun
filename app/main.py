"""FastAPI 后端 + WebUI。

启动钩子：初始化数据库、种入预置 STUN 服务器、启动后台轮询线程。
"""
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import config, db, ikuai_client, logbuf, poller, settings, state, stun_probe

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


class SettingsIn(BaseModel):
    ikuai_base_url: Optional[str] = None
    ikuai_token: Optional[str] = None      # 留空 = 沿用已保存的令牌
    ikuai_interface: Optional[str] = None
    ikuai_verify_ssl: Optional[bool] = None
    wan_port_mode: Optional[str] = None    # stun | preserve
    stun_pool_url: Optional[str] = None


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
    settings.set_values(data)
    log.info(f"设置已保存（外网端口模式={settings.get('wan_port_mode')}）")
    return settings.public_view()


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
    settings.set_values(data)
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
    m = db.create_mapping(
        name=req.name, lan_ip=req.lan_ip, lan_port=req.lan_port,
        proto=req.proto, wan_port=wan_port, wan_port_manual=manual,
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
    m = db.get_mapping(m["id"])
    log.info(f"新增映射 [{m['name']}] {m['lan_ip']}:{m['lan_port']} "
             f"爱快侧端口={m['wan_port']} 规则ID={m['rule_id']}")
    # 后台即时探测一次，刷新「公网侧端口」（仅用于展示访问地址，不写回爱快）
    threading.Thread(target=poller._provision_probe, args=(m["id"],), daemon=True).start()
    return m


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
    db.delete_mapping(mid)
    return {"ok": True}


@app.post("/api/mappings/{mid}/resume")
def api_resume_mapping(mid: int):
    m = poller.resume_mapping(mid)
    if not m:
        raise HTTPException(404, "映射不存在")
    log.info(f"手动恢复映射 [{m['name']}]")
    return m


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
