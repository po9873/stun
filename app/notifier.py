"""变更通知：把端口变化、映射增删、对账修正推送到外部 Webhook。

对应 Lucky 里的「STUN 穿透 + Webhook 推送」：穿透拿到公网端口后推给你，
端口一变再推一次。支持自定义 URL 与几个常见平台。

CGNAT 的公网端口会漂移（实测同一端口间隔 75 秒就从 5010 变成 3628），
所以必须带冷却，否则一条映射就能把手机刷爆。
"""
import json
import logging
import threading
import time
import urllib.parse

import requests

from . import db, settings

log = logging.getLogger("stun.notify")

_lock = threading.Lock()
_last = {}          # dedup_key -> 上次推送时间


def _min_gap():
    try:
        return max(0, int(settings.get("notify_min_gap") or 600))
    except ValueError:
        return 600


def _wants(kind):
    """按设置里的开关过滤事件类型：port / mapping / reconcile / error。"""
    on = (settings.get("notify_on") or "port,mapping,reconcile").lower()
    return kind in [x.strip() for x in on.split(",") if x.strip()]


def enabled(kind=None):
    if not settings.get_bool("notify_enabled"):
        return False
    if not (settings.get("notify_url") or "").strip():
        return False
    if kind and not _wants(kind):
        return False
    return True


def send(kind, title, body, dedup_key=None):
    """推送一条通知。

    kind:      事件类型，用于开关过滤（port / mapping / reconcile / error）
    dedup_key: 冷却键。同一键在 notify_min_gap 秒内只推一次。
    返回是否真的发出去了。
    """
    if not enabled(kind):
        return False

    if dedup_key:
        with _lock:
            now = time.time()
            if now - _last.get(dedup_key, 0) < _min_gap():
                return False
            _last[dedup_key] = now

    url = (settings.get("notify_url") or "").strip()
    plat = (settings.get("notify_kind") or "custom").strip().lower()
    text = f"{title}\n{body}" if body else title
    try:
        _post(plat, url, title, body, text)
        log.info(f"通知已推送 [{plat}] {title}")
        return True
    except Exception as e:
        log.error(f"通知推送失败 [{plat}]: {e}")
        return False


def _post(plat, url, title, body, text):
    timeout = 8
    if plat == "bark":
        # https://api.day.app/<key>/<title>/<body>
        full = "%s/%s/%s" % (url.rstrip("/"),
                             urllib.parse.quote(title, safe=""),
                             urllib.parse.quote(body or title, safe=""))
        _check(requests.get(full, timeout=timeout))
        return
    if plat in ("dingtalk", "wecom"):
        payload = {"msgtype": "text", "text": {"content": text}}
    elif plat == "feishu":
        payload = {"msg_type": "text", "content": {"text": text}}
    else:
        # 通用：直接 POST 结构化 JSON，方便接到任意自建服务 / Home Assistant 等
        payload = {
            "source": "stun",
            "title": title,
            "text": body,
            "time": db.now(),
        }
    _check(requests.post(url, json=payload, timeout=timeout))


def _check(resp):
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:150]}")
    # 钉钉/企业微信/飞书即使 HTTP 200，业务失败也返回在 body 里
    try:
        d = resp.json()
    except ValueError:
        return
    if isinstance(d, dict):
        code = d.get("errcode", d.get("code"))
        if code not in (0, None, "0", 20000):
            raise RuntimeError(f"平台返回 errcode={code}: {d.get('errmsg') or d.get('msg')}")


# ---------------- 具体事件 ----------------

def public_port_changed(m, old_port, new_port, ext_ip):
    addr = f"{ext_ip}:{new_port}" if ext_ip else str(new_port)
    old_addr = f"{ext_ip}:{old_port}" if (ext_ip and old_port) else str(old_port or "未知")
    return send("port",
                f"[stun] 公网端口变化 · {m['name']}",
                f"内网 {m['lan_ip']}:{m['lan_port']}\n"
                f"公网 {old_addr} → {addr}",
                dedup_key=f"port:{m['id']}")


def mapping_created(m):
    return send("mapping",
                f"[stun] 新增映射 · {m['name']}",
                f"内网 {m['lan_ip']}:{m['lan_port']} ({m['proto']})\n"
                f"爱快规则 ID {m.get('rule_id') or '未创建'}")


def mapping_deleted(m):
    return send("mapping",
                f"[stun] 删除映射 · {m['name']}",
                f"内网 {m['lan_ip']}:{m['lan_port']}\n"
                f"爱快规则 ID {m.get('rule_id') or '无'} 已一并移除")


def reconciled(name, old_wan, new_wan):
    return send("reconcile",
                f"[stun] 对账修正 · {name}",
                f"爱快外网端口 {old_wan} → {new_wan}",
                dedup_key=f"reconcile:{name}")


def rule_failed(name, reason):
    return send("error",
                f"[stun] 爱快规则操作失败 · {name}",
                f"{reason}",
                dedup_key=f"error:{name}")


def test():
    """设置页「发送测试」用，忽略冷却与事件开关。"""
    url = (settings.get("notify_url") or "").strip()
    if not url:
        return False, "还没填推送地址"
    plat = (settings.get("notify_kind") or "custom").strip().lower()
    title = "[stun] 测试通知"
    body = "收到这条说明通知配置正确。"
    try:
        _post(plat, url, title, body, f"{title}\n{body}")
        log.info(f"测试通知已发送 [{plat}]")
        return True, f"已发送到 {plat}"
    except Exception as e:
        log.error(f"测试通知发送失败 [{plat}]: {e}")
        return False, str(e)
