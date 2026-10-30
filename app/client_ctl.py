"""BT 客户端端口下发（qBittorrent / Transmission）。

穿透模式下，实测到的公网端口要同时写到两个地方：
  1. 爱快 DNAT 的「内部端口」（外网端口保持 = 打洞端口，不动）
  2. BT 客户端的监听端口（这样它对外通告的就是公网端口，外部才连得进来）

只改监听端口这一项，其它设置一律不碰。
"""
import json
import logging

import requests

log = logging.getLogger("stun.client")

TIMEOUT = 8


class ClientError(Exception):
    pass


def _base(m):
    b = (m.get("client_url") or "").strip().rstrip("/")
    if not b:
        raise ClientError("没填客户端地址")
    if not b.startswith("http"):
        b = "http://" + b
    return b


def _auth(m):
    u = (m.get("client_user") or "").strip()
    p = m.get("client_pass") or ""
    return (u, p) if u else None


# ---------------- qBittorrent ----------------

def qb_session(m):
    s = requests.Session()
    base = _base(m)
    if _auth(m):
        r = s.post(base + "/api/v2/auth/login",
                   data={"username": _auth(m)[0], "password": _auth(m)[1]},
                   headers={"Referer": base}, timeout=TIMEOUT)
        if r.status_code != 200 or r.text.strip() not in ("Ok.", "ok"):
            raise ClientError(f"QB 登录失败 HTTP{r.status_code}")
    return s


def qb_get_port(m):
    s = qb_session(m)
    r = s.get(_base(m) + "/api/v2/app/preferences", timeout=TIMEOUT)
    if r.status_code != 200:
        raise ClientError(f"QB 读取设置失败 HTTP{r.status_code}")
    return int(r.json().get("listen_port"))


def qb_set_port(m, port):
    s = qb_session(m)
    # QB 的 WebUI 只认 application/x-www-form-urlencoded 的 json=<json>
    r = s.post(_base(m) + "/api/v2/app/setPreferences",
               data={"json": json.dumps({"listen_port": int(port)})},
               headers={"Referer": _base(m)}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ClientError(f"QB 下发端口失败 HTTP{r.status_code}")
    return True


# ---------------- Transmission ----------------

def _tr_rpc(m, payload):
    url = _base(m) + "/transmission/rpc"
    auth = _auth(m)
    for _ in range(2):
        r = requests.post(url, json=payload, auth=auth, timeout=TIMEOUT)
        if r.status_code == 409:
            sid = r.headers.get("X-Transmission-Session-Id")
            if not sid:
                raise ClientError("TR 未返回 Session-Id")
            headers = {"X-Transmission-Session-Id": sid}
            r = requests.post(url, json=payload, auth=auth,
                              headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            raise ClientError(f"TR RPC 失败 HTTP{r.status_code}")
        data = r.json()
        if data.get("result") != "success":
            raise ClientError(f"TR RPC 返回 {data.get('result')}")
        return data.get("arguments", {})
    raise ClientError("TR RPC 重试仍失败")


def tr_get_port(m):
    return int(_tr_rpc(m, {"method": "session-get",
                           "arguments": ["peer-port"]}).get("peer-port"))


def tr_set_port(m, port):
    _tr_rpc(m, {"method": "session-set",
                "arguments": {"peer-port": int(port)}})
    return True


# ---------------- 统一入口 ----------------

def get_port(m):
    kind = (m.get("client_kind") or "qb").lower()
    return tr_get_port(m) if kind == "tr" else qb_get_port(m)


def set_port(m, port):
    kind = (m.get("client_kind") or "qb").lower()
    return tr_set_port(m, port) if kind == "tr" else qb_set_port(m, port)


def test(m):
    """设置页/界面「测试连接」用：读一次端口，能读通就说明地址、账号都对。"""
    return get_port(m)
