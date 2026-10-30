"""端口借用：容器独占一个端口，替这条映射把 CGNAT 上的公网端口问出来并保活。

背景：
    CGNAT 的公网端口是每条流单独分的。容器拿目标机的端口去测，爱快发现那个外部
    端口已经被目标机占着，只能另给容器分一个，测出来的是容器那条（实测目标机 39213、
    容器测出 2048）。把探针装到目标机上，又会撞上软件自己 LISTEN 的端口（QB 的 46065，
    bind 直接报 Address already in use）。两头都测不了，那就换个做法：

        容器独占一个别人都没用的端口 P，一直从 P 发包 —— 这个包会一路穿到公网，
        CGNAT 因此会为它留一条映射：公网 g  <->  (爱快 WAN 口, P)
        容器问 STUN 服务器拿到 g，爱快上把 WAN:P 指到目标机，
        于是「公网IP:g」就成了这条映射可用的公网地址。

    这样做的关键好处：容器只负责占端口和问端口，<b>流量不经过容器</b> ——
    外网连进来时，爱快直接把 WAN:P 转到目标机，来源 IP 也原样保留，
    内部端口还是目标机那个端口，一点没动。它每轮发一次包同时也是心跳，
    映射才不会被运营商收回去（Lucky 那种做法是让流量也走它自己，我们不需要）。
"""
import logging
import socket
import threading

from . import db, rfc5389

log = logging.getLogger("stun.relay")

_lock = threading.Lock()
_ip_cache = None
_ports = {}        # 容器端口 -> 目标机 (ip, port)，只作记录，页面用来看状态


def container_ip():
    """容器在业务网段上的 IP —— 必须绑它，绑到 docker 网桥 IP 就白搭了。"""
    global _ip_cache
    if _ip_cache:
        return _ip_cache
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
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
    # 优先挑与映射同 /24 的那个：macvlan 的 IP 一般就在业务网段上
    pfx = {}
    for m in db.list_mappings():
        p = ".".join(str(m["lan_ip"]).split(".")[:3])
        pfx[p] = pfx.get(p, 0) + 1
    for p, _n in sorted(pfx.items(), key=lambda kv: -kv[1]):
        for ip in ips:
            if ip.startswith(p + "."):
                _ip_cache = ip
                return ip
    _ip_cache = sorted(ips)[0] if ips else "0.0.0.0"
    return _ip_cache


def _port_free(port):
    """这个端口在容器上能不能绑（不要求能监听，只要求没被别的进程占死）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((container_ip(), port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port():
    """挑一个没人用的容器端口。

    要避开两样：任何映射的内网端口（那会在爱快 NAT 上跟目标机抢同一个外部端口，
    容器就分不到 P 了，测出来的自然也不是 P 的映射），以及容器本地已占住的端口。
    """
    used = set()
    for m in db.list_mappings():
        try:
            used.add(int(m["lan_port"]))
        except (TypeError, ValueError):
            pass
        if m.get("relay_port"):
            used.add(int(m["relay_port"]))
    with _lock:
        used.update(_ports)
    port = 40000
    for _ in range(1000):
        if port not in used and _port_free(port):
            return port
        port += 1
    return port


def attach(m):
    """登记这条映射借用的端口（只记账，不监听 —— 流量不经过容器）。"""
    port = int(m.get("relay_port") or 0)
    if port:
        with _lock:
            _ports[port] = (m["lan_ip"], int(m["lan_port"]))


def sync_all():
    """按库里的开关维持记账：关掉中转的端口要还回去。"""
    want = set()
    for m in db.list_mappings():
        if m.get("relay") and m.get("relay_port") and m["status"] == "active":
            want.add(int(m["relay_port"]))
            attach(m)
    with _lock:
        for port in list(_ports):
            if port not in want:
                _ports.pop(port, None)
                log.info("端口 %s 已归还" % port)


def probe(port, server=None, timeout=8):
    """从容器那个端口发一次 TCP STUN，问出它的公网端口。

    每轮发一次既是探测也是心跳：这个端口一直在往外发，CGNAT 上那条映射就不会被回收。
    """
    ip = container_ip()
    cands = []
    if server:
        cands.append(server)
    for s in db.list_stun_servers():
        if s.get("status") != "active":
            continue
        if server and s["host"] == server.get("host") \
                and int(s["port"]) == int(server.get("port", 0)):
            continue
        cands.append(s)
    for sv in cands:
        host, sport = sv["host"], int(sv["port"])
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, port))
            sock.settimeout(timeout)
            sock.connect((socket.gethostbyname(host), sport))
            req, tid = rfc5389.build_binding()
            resp = rfc5389.exchange(sock, req, tid, timeout)
            if resp.get("mapped"):
                return resp["mapped"][0], resp["mapped"][1]
        except Exception as e:
            log.debug("借端口探测 %s:%s 经 %s:%s 失败：%s" % (ip, port, host, sport, e))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return None, None


def status():
    """给页面看：容器 IP，以及每个借出去的端口对应哪台机。"""
    with _lock:
        return {
            "ip": container_ip(),
            "ports": sorted(_ports),
            "dests": {"%s" % k: "%s:%s" % v for k, v in _ports.items()},
        }
