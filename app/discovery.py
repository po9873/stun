"""自动发现目标机上的 BT 客户端管理接口（不用手填地址）。

做法：扫目标机常见的 WebUI / RPC 端口，用协议指纹判断是 qBittorrent 还是
Transmission，再读一次它的监听端口 —— 只有和这条映射的内网端口对得上才认，
避免把别的机器/别的实例配错。

认不出来的（非 QB/TR 的客户端）不猜，留空让用户在界面上填一条自定义命令。
"""
import logging
import socket
from concurrent.futures import ThreadPoolExecutor

import requests

from . import client_ctl, db

log = logging.getLogger("stun.discovery")

# 常见的 WebUI / RPC 端口（QB 常用 8080-8090，TR 常用 9090-9100）
CANDIDATE_PORTS = (
    [8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090]
    + [9090, 9091, 9092, 9093, 9094, 9095, 9096, 9097, 9098, 9099, 9100]
    + [80, 8888, 8181, 8880, 9000, 9001, 51413]
)


def _open(ip, port, timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def open_ports(ip, ports=None):
    with ThreadPoolExecutor(max_workers=40) as ex:
        hits = [(p, ok) for p, ok in
                zip(ports or CANDIDATE_PORTS,
                    ex.map(lambda p: _open(ip, p), ports or CANDIDATE_PORTS))]
    return [p for p, ok in hits if ok]


def fingerprint(base):
    """判断 base 是哪种客户端，返回 'qb' / 'tr' / None。"""
    try:
        r = requests.get(base + "/api/v2/app/version", timeout=2.5)
        if r.status_code == 200 and r.text.strip().startswith("v"):
            return "qb"
    except requests.RequestException:
        pass
    try:
        r = requests.post(base + "/transmission/rpc",
                          json={"method": "session-get",
                                "arguments": ["peer-port"]}, timeout=2.5)
        # 409=需要 Session-Id、401=要账号、200=免密，三种都说明是 TR
        if r.status_code in (200, 401, 409):
            return "tr"
    except requests.RequestException:
        pass
    return None


def discover(ip, lan_port=None, ports=None):
    """在 ip 上找一个「监听端口 == lan_port」的 BT 客户端。

    返回 {"kind": "qb"/"tr", "url": "http://ip:port", "listen_port": n}
    找不到返回 None。
    """
    found = []
    for p in open_ports(ip, ports):
        base = f"http://{ip}:{p}"
        kind = fingerprint(base)
        if not kind:
            continue
        item = {"kind": kind, "url": base, "listen_port": None}
        m = {"client_kind": kind, "client_url": base, "lan_port": lan_port}
        try:
            item["listen_port"] = int(client_ctl.get_port(m))
        except Exception:
            pass
        # 端口对得上就直接用；读不到（要账号）也先收着，等用户补账号
        if lan_port and item["listen_port"] == int(lan_port):
            return item
        found.append(item)
    return found[0] if found else None


def discover_for_mapping(m):
    """给一条映射自动找客户端并入库。返回 (ok, 说明)。"""
    r = discover(m["lan_ip"], m["lan_port"])
    if not r:
        return False, "没找到 BT 客户端管理接口"
    # 端口对不上才是真错；读不到（要账号）也先写入地址，让用户补账号即可
    if r["listen_port"] is not None and r["listen_port"] != int(m["lan_port"]):
        return False, (f"找到 {r['kind']} 在 {r['url']}，但它监听 {r['listen_port']}，"
                       f"与这条映射的 {m['lan_port']} 不一致")
    db.update_mapping(m["id"], client_kind=r["kind"], client_url=r["url"])
    log.info(f"[{m['name']}] 自动发现客户端 {r['kind']} @ {r['url']}")
    if r["listen_port"] is None:
        return True, f"{r['kind']} @ {r['url']}（需补账号才能下发端口）"
    return True, f"{r['kind']} @ {r['url']}"


def discover_all(only_missing=True):
    """扫一遍所有映射（默认只扫还没配客户端的）。"""
    out = []
    for m in db.list_mappings():
        if m["status"] != "active":
            continue
        if only_missing and m.get("client_url"):
            continue
        ok, msg = discover_for_mapping(m)
        out.append({"id": m["id"], "name": m["name"], "ok": ok, "msg": msg})
    return out
