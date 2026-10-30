"""STUN 探测、NAT 类型检测与服务器池扫描。

★ 传输方式：默认 **STUN over TCP**（RFC 5389 §7.2.1，裸报文无长度前缀）。
  原因：实测本环境 UDP 3478 完全不通（运营商/路由封锁），而 TCP 3478 通。
  扫描与探测均「先 TCP、失败再回退 UDP」，并把成功的传输方式记入数据库。

原理：在本机用 TCP 绑定 (lan_ip, lan_port) 连到 STUN 服务器，
从响应的 MAPPED-ADDRESS/XOR-MAPPED-ADDRESS 里取出路由器为该源地址
映射出的公网 IP:端口，用于填充爱快 DNAT 规则的 wan_port。

NAT 分类：
    open 直连公网 / nat1 全锥 / nat2 受限锥 / nat3 端口受限锥  -> 可正常映射
    nat4 对称型（含 CGNAT）-> 映射端口不可复用，主轮询暂停并告警
    blocked 全部 STUN 无响应
"""
import logging
import os
import socket
import threading
import time

from . import config, db, rfc5389, settings

log = logging.getLogger("stun.stun")

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stun_servers.txt")

# 兜底：种子文件缺失时至少有一批可探测的 TCP 节点
FALLBACK_SERVERS = [
    ("stun.vomessen.de", 3478, 1),
    ("stun.mixvoip.com", 3478, 1),
    ("stun.nextcloud.com", 3478, 1),
    ("stun.siptrunk.com", 3478, 1),
    ("stun.freeswitch.org", 3478, 1),
]


def _load_seed():
    """解析种子文件，返回 [(host, port, nat_capable), ...]"""
    items = []
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                host = parts[0].strip()
                try:
                    port = int(parts[1].strip())
                except ValueError:
                    continue
                nat = 0
                if len(parts) >= 3:
                    nat = 1 if parts[2].strip() == "1" else 0
                if host and port:
                    items.append((host, port, nat))
    except OSError as e:
        log.warning(f"读取 STUN 种子文件失败: {e}")
    return items or list(FALLBACK_SERVERS)


def seed_servers():
    """首次启动时把内置种子池写入数据库（已存在的不覆盖用户改动）。"""
    for host, port, nat in _load_seed():
        db.upsert_stun_server(host, port, nat_capable=nat)


# ---------------- 服务器池在线更新 ----------------

def _parse_pool(text, nat_keys=None):
    """解析 always-online-stun 清单（每行 host:port）。"""
    nat_keys = nat_keys or set()
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        host, _, rest = line.partition(":")
        port_s = rest.split(":")[0].strip()
        try:
            port = int(port_s)
        except ValueError:
            continue
        if not host or not (0 < port < 65536):
            continue
        items.append((host, port, 1 if f"{host}:{port}" in nat_keys else 0))
    return items


def _url_variants(url):
    """把一个清单地址展开成若干候选地址。

    支持用户直接粘贴 GitHub 网页地址（github.com/.../blob/...），
    自动转成 raw 直链；国内访问不畅时再退回 jsDelivr / 镜像。
    """
    import re

    u = (url or "").strip()
    if not u:
        return []
    out = [u]

    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/(?:blob|raw)/([^/]+)/(.+)$", u)
    if not m:
        m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", u)
    if m:
        owner, repo, branch, path = m.groups()
        out.append(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}")
        out.append(f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")

    if "raw.githubusercontent.com" in u:
        out.append(u.replace("raw.githubusercontent.com", "raw.gitmirror.com"))
        out.append("https://ghfast.top/" + u)
    return out


def refresh_pool(force=False):
    """从 always-online-stun 拉取最新的可用服务器清单并合并入池。

    官方该仓库每小时刷新一次，且明确建议直接硬编码该链接使用。
    拉不到（网络受限）时静默失败，继续沿用内置种子池，不影响探测。
    返回 {"ok":bool, "added":int, "total":int, "error":str|None}
    """
    if not config.STUN_POOL_ENABLED:
        return {"ok": False, "added": 0, "total": 0, "error": "已禁用在线更新", "url": None}
    # 地址优先取 WebUI 设置页填写的，其次环境变量，最后内置默认值
    pool_url = settings.get("stun_pool_url") or config.STUN_POOL_URL
    nat_url = config.STUN_NAT_POOL_URL
    if "/valid_nat_testing_hosts_tcp.txt" not in nat_url:
        nat_url = pool_url.replace("valid_hosts_tcp.txt", "valid_nat_testing_hosts_tcp.txt")
    try:
        import requests
    except ImportError:
        return {"ok": False, "added": 0, "total": 0, "error": "requests 未安装"}

    def get_any(url):
        """依次尝试各候选地址（原始 / raw / 镜像），返回 (text, 实际命中地址)。

        关键：GitHub 网页地址也会返回 200，但内容是 HTML 页面、解析不出任何条目。
        所以这里要校验「真的解析出了条目」才算命中，否则继续试下一个候选。
        """
        last_err = None
        for cand in _url_variants(url):
            try:
                r = requests.get(cand, timeout=config.STUN_POOL_TIMEOUT,
                                 headers={"User-Agent": "stun/1.0"})
                r.raise_for_status()
                text = r.text or ""
                if text.lstrip().startswith("<"):
                    raise ValueError("返回的是网页而非清单文本")
                if not _parse_pool(text):
                    raise ValueError("解析不出任何 host:port 条目")
                return text, cand
            except Exception as e:
                last_err = e
                log.debug(f"清单地址不可用 {cand}: {e}")
        raise last_err or RuntimeError("无候选地址")

    try:
        nat_keys = set()
        try:
            nat_text, _hit = get_any(nat_url)
            for h, p, _n in _parse_pool(nat_text):
                nat_keys.add(f"{h}:{p}")
        except Exception as e:
            log.debug(f"拉取 NAT 检测清单失败（可忽略）: {e}")
        main_text, used_url = get_any(pool_url)
        items = _parse_pool(main_text, nat_keys)
        if not items:
            return {"ok": False, "added": 0, "total": 0,
                    "error": "返回内容为空", "url": used_url}
    except Exception as e:
        log.warning(f"在线更新 STUN 池失败，沿用内置清单: {e}")
        return {"ok": False, "added": 0, "total": 0,
                "error": f"{e}（已尝试 {pool_url} 及其 raw/镜像地址）", "url": pool_url}

    added = 0
    for host, port, nat in items:
        row = db.get_stun_server(host, port)
        if row is None:
            db.upsert_stun_server(host, port, nat_capable=nat, status="active")
            added += 1
        elif row.get("removed"):
            continue      # 用户手动删过的，在线刷新不再把它加回来
        elif nat and not row.get("nat_capable"):
            # 已存在：只纠正 NAT 能力标记，不覆盖状态/延迟等实测数据
            db.update_stun_server(row["id"], nat_capable=1)
    return {"ok": True, "added": added, "total": len(items),
            "error": None, "url": used_url}


# ---------------- 单次探测 ----------------

def _probe_once(host, port, local_addr=None, timeout=None, tid=None,
                change_ip=False, change_port=False):
    """对单个服务器做一次探测，自动 TCP->UDP 回退。

    返回 (resp, elapsed_ms, transport)
    """
    timeout = timeout or config.STUN_TIMEOUT
    req, tid = rfc5389.build_binding(change_ip=change_ip, change_port=change_port, tid=tid)
    try:
        resp, ms = rfc5389.request_tcp(
            host, port, timeout=timeout, local_addr=local_addr, req=req, tid=tid
        )
        return resp, ms, "tcp"
    except Exception as e:
        log.debug(f"TCP STUN {host}:{port} 失败({e})，尝试 UDP")
    try:
        resp, ms, _t = rfc5389.request_udp(
            host, port, timeout=timeout, local_addr=local_addr, req=req, tid=tid
        )
        return resp, ms, "udp"
    except Exception as e:
        log.debug(f"UDP STUN {host}:{port} 失败: {e}")
        return None, None, None


def _ip_is_local(ip):
    """目标 IP 是否在本机网卡上（决定能否精确绑定探测）。"""
    if not ip or ip == "0.0.0.0":
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _probe_port_once(host, port, lan_ip, lan_port, timeout, need="tcp"):
    """对单台服务器探测端口映射。返回 (ext_ip, ext_port, exact)。

    两个关键点：
    1. 从<b>目标机那个端口号</b>发包：爱快保留源端口，容器的包出去时外部口也是
       同一个端口号，于是和目标机自己的流量共用 CGNAT 上同一条映射，测出来的
       公网端口就是外部真正能连进来的那个（实测 .104:24213 测到 4412，
       从公网连 4412 直接通到 .248:24213）。
    2. <b>严格按协议，绝不跨协议回退</b>：CGNAT 上 TCP 与 UDP 是两套独立的公网
       端口，拿 UDP 测出来的端口当 TCP 的公网端口，外面根本连不进来
       （实测 UDP 测出 1767，TCP 连 1767 完全不通）。
    """
    exact = _ip_is_local(lan_ip)
    # 绑不到目标 IP 时，退化为 0.0.0.0:<同一端口> —— 端口号保留，这对
    # 端口映射结果是决定性的，源 IP 退而用容器自身。
    want = (lan_ip, int(lan_port)) if exact else ("0.0.0.0", int(lan_port))
    req, tid = rfc5389.build_binding()

    if need == "udp":
        try:
            resp, _ms, _t = rfc5389.request_udp(
                host, port, timeout=timeout, local_addr=want, req=req, tid=tid
            )
            if resp and resp.get("mapped"):
                return resp["mapped"][0], resp["mapped"][1], exact
        except Exception as e:
            log.debug(f"UDP 探测 {lan_ip}:{lan_port} via {host} 失败: {e}")
        return None, None, exact

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rfc5389.bind_source(s, want)
        s.settimeout(timeout)
        s.connect((socket.gethostbyname(host), port))
        resp = rfc5389.exchange(s, req, tid, timeout)
        if resp.get("mapped"):
            return resp["mapped"][0], resp["mapped"][1], exact
    except Exception as e:
        log.debug(f"TCP 探测 {lan_ip}:{lan_port} via {host} 失败: {e}")
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    return None, None, exact


# 每条映射固定用同一台服务器问。(内网IP, 端口, 协议) -> 上次问通的那台服务器。
# 必须固定：CGNAT 是按「内网IP:端口 + 目标服务器」这条流来分配公网端口的，
# 每轮换一台服务器就等于新开一条流、拿到一个新公网端口，上一条还没老化，
# 于是外部连进来时 CGNAT 不知道该往哪条送 —— 实测端口每轮都变、连不通，
# 而固定服务器那几条 15 分钟纹丝不动，从公网连回来 2/2 通。
_sticky = {}

# 每条映射保持住的那条 TCP 连接：(内网IP, 端口, 协议) -> {sock, server}。
# 必须保持：CGNAT 是「每条新 TCP 连接一个新公网端口」，重连一次端口就换一次
# （实测每轮重连 -> 公网端口从 32xx 整块跳到 40xx）；
# 而同一条连接上反复发问，端口纹丝不动，从公网连回来也通。
_conns = {}
_conn_lock = threading.Lock()

# 每条映射最近一次真正问通的那台服务器：(内网IP, 端口, 协议) -> {host, port, ts}
# 页面靠它显示「这条映射现在到底在问哪台服务器」
_used = {}
_used_lock = threading.Lock()


def _mark_used(key, sv):
    with _used_lock:
        _used[key] = {"host": sv["host"], "port": int(sv["port"]),
                      "transport": sv.get("transport") or "", "ts": time.time()}


def used_server(lan_ip, lan_port, proto=None):
    """这条映射最近一次问通的那台 STUN 服务器；还没问通过则返回 None。"""
    key = (lan_ip, int(lan_port), "udp" if proto == "udp" else "tcp")
    with _used_lock:
        item = _used.get(key)
    return dict(item) if item else None


def _conn_take(key, want, candidates, timeout):
    """取出这条映射保持住的连接；没有或被掐断了就新建一条。"""
    with _conn_lock:
        c = _conns.get(key)
    if c:
        return c["sock"], c["server"]
    for sv in candidates:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rfc5389.bind_source(s, want)
            s.settimeout(timeout)
            s.connect((socket.gethostbyname(sv["host"]), int(sv["port"])))
            # 开 TCP keepalive：STUN 服务器会掐掉空闲连接，一断就得重连，
            # 重连 CGNAT 就给个新公网端口，页面上就会看到端口乱跳
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 20), ("TCP_KEEPINTVL", 10)):
                if hasattr(socket, opt):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                    except OSError:
                        pass
        except Exception as e:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            continue
        with _conn_lock:
            _conns[key] = {"sock": s, "server": sv}
        return s, sv
    return None, None


def _conn_drop(key):
    with _conn_lock:
        c = _conns.pop(key, None)
    if c:
        try:
            c["sock"].close()
        except Exception:
            pass


def prune_conns(alive_keys):
    """删除/挂起的映射，它那条保持住的连接要一并收回。"""
    with _conn_lock:
        keys = [k for k in _conns if k not in alive_keys]
    for k in keys:
        _conn_drop(k)
    with _used_lock:
        for k in [k for k in _used if k not in alive_keys]:
            _used.pop(k, None)


def probe_mapping_port(lan_ip, lan_port, server=None, timeout=None, proto=None):
    """探测某个内网站点端口映射出的公网端口。

    两个必须守住的点：
    1. 只挑<b>支持对应协议</b>的服务器：TCP 的映射就必须用 TCP 服务器问，
       UDP 的映射就必须用 UDP 服务器问，绝不跨协议凑合（UDP 测出的端口 TCP 连不进）。
    2. 每条映射<b>固定同一台服务器</b>：见上面 _sticky 的说明。

    任一台成功即返回。返回 (external_ip, external_port, exact)：
      exact=True   目标 IP 在本机，精确绑定 (lan_ip, lan_port)，结果最可靠
      exact=False  目标 IP 不在本机（macvlan 容器内绑不到别的容器 IP），
                   退化为绑定 0.0.0.0:<同一端口>，端口号一致 —— 端口号一致就够，
                   因为爱快保留源端口，外部那侧的端口号与目标机完全相同
    """
    timeout = timeout or config.STUN_TIMEOUT
    need = "udp" if proto == "udp" else "tcp"
    key = (lan_ip, int(lan_port), need)

    def _match(s):
        return s and s.get("status") == "active" \
            and (s.get("transport") or "") == need

    def _same(a, b):
        return a and b and a.get("host") == b.get("host") \
            and int(a.get("port", 0)) == int(b.get("port", 0))

    # 上次问通的那台优先，其次调用方指定的，都没有就取延迟最低的
    prefer = _sticky.get(key)
    if not _match(prefer):
        prefer = server if _match(server) else db.get_best_stun_server(transport=need)

    candidates = [prefer] if _match(prefer) else []
    for s in db.list_stun_servers():
        if not _match(s) or _same(s, prefer):
            continue
        candidates.append(s)
        if len(candidates) >= 3:
            break
    if not candidates:
        # 池子里没有这个协议的可用服务器，立刻扫一遍自救，而不是干等下一轮
        log.warning(f"没有可用的 {need.upper()} STUN 服务器，立即重新扫描")
        for s in db.list_stun_servers():
            if _match(s):
                candidates.append(s)
            if len(candidates) >= 3:
                break

    if need == "udp":
        # UDP 没有连接概念，绑同一个端口反复发就行，端口本来就不乱跳
        for c in candidates:
            ext_ip, ext_port, exact = _probe_port_once(
                c["host"], int(c["port"]), lan_ip, lan_port, timeout, need
            )
            if ext_port is not None:
                _sticky[key] = c
                _mark_used(key, c)
                return ext_ip, ext_port, exact
        return (None, None, False)

    # TCP：尽量复用保持住的那条连接，只有它坏了才重建（重建会拿到新公网端口）
    exact = _ip_is_local(lan_ip)
    want = (lan_ip, int(lan_port)) if exact else ("0.0.0.0", int(lan_port))
    for attempt in range(3):
        sock, sv = _conn_take(key, want, candidates, timeout)
        if sock is None:
            break
        try:
            req, tid = rfc5389.build_binding()
            sock.settimeout(timeout)
            resp = rfc5389.exchange(sock, req, tid, timeout)
            if resp.get("mapped"):
                _sticky[key] = sv
                _mark_used(key, sv)
                return resp["mapped"][0], resp["mapped"][1], exact
        except Exception as e:
            log.debug(f"复用连接问 {lan_ip}:{lan_port} 失败({e})，重建")
        # 服务器掐了空闲连接，或连接已断：丢掉重建一条
        _conn_drop(key)
    return (None, None, False)


# ---------------- NAT 类型检测 ----------------

def _binding_from_port(server, local_port, timeout, change_ip=False, change_port=False):
    """从指定本地端口发一次 BINDING。

    返回 (resp, actual_local_port)；actual_local_port 用于确认两次探测是否
    真的用了同一个本地端口——否则对称 NAT 判定会失真。
    """
    host = server["host"]
    port = int(server["port"])
    req, tid = rfc5389.build_binding(change_ip=change_ip, change_port=change_port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            s.bind(("0.0.0.0", int(local_port)))
        except OSError:
            s.bind(("0.0.0.0", 0))
        s.settimeout(timeout)
        s.connect((socket.gethostbyname(host), port))
        resp = rfc5389.exchange(s, req, tid, timeout)
        return resp, s.getsockname()[1]
    except Exception as e:
        log.debug(f"BINDING {host}:{port} 失败: {e}")
        return None, None
    finally:
        try:
            s.close()
        except Exception:
            pass


def _test_binding(server, local_port, change_ip=False, change_port=False, timeout=None):
    """做一次 BINDING 测试，返回 (mapped, ok)。"""
    resp, _lp = _binding_from_port(
        server, local_port, timeout or config.STUN_TIMEOUT,
        change_ip=change_ip, change_port=change_port,
    )
    if resp is None or resp.get("error"):
        return None, False
    return resp.get("mapped"), True


def nat_detect():
    """检测 NAT 类型与外部 IP。返回 (label, external_ip, external_port)。"""
    timeout = max(config.STUN_TIMEOUT, 4)

    candidates = [s for s in db.list_stun_servers() if s.get("nat_capable") and s["status"] == "active"]
    if not candidates:
        candidates = db.list_stun_servers()
    if not candidates:
        seed_servers()
        candidates = db.list_stun_servers()
    if not candidates:
        return ("unknown", None, None)

    # Test I：常规 BINDING
    local_port = 54320
    mapped = None
    used = None
    used_local = None
    for srv in candidates[:6]:
        resp, lp = _binding_from_port(srv, local_port, timeout)
        if resp and resp.get("mapped"):
            mapped = resp["mapped"]
            used = srv
            used_local = lp
            break
    if not mapped:
        return ("blocked", None, None)

    ext_ip, ext_port = mapped

    # 本机直连公网判定
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        local_ip = probe.getsockname()[0]
        probe.close()
    except Exception:
        local_ip = None
    if local_ip and ext_ip == local_ip:
        return ("open", ext_ip, ext_port)

    # 对称型判定：同一本地端口访问两个不同服务器，映射端口不同 => 对称 NAT
    others = [s for s in candidates if s is not used][:3]
    if not others:
        others = [s for s in db.list_stun_servers()
                  if s["id"] != used["id"] and s["status"] == "active"][:3]
    symmetric = None
    for srv in others:
        # 必须从「同一个本地端口」发起，否则比较毫无意义
        for _attempt in range(3):
            resp2, lp2 = _binding_from_port(srv, local_port, timeout)
            if resp2 and resp2.get("mapped"):
                if lp2 == used_local:
                    symmetric = (resp2["mapped"][1] != ext_port)
                break
            time.sleep(0.2)   # 端口可能处于 TIME_WAIT，稍等后重试
        if symmetric is not None:
            break
    if symmetric:
        return ("nat4", ext_ip, ext_port)

    # 锥形细分（需服务器支持 RFC5780 CHANGE-REQUEST）
    for srv in candidates[:4]:
        _m, ok = _test_binding(srv, local_port, change_ip=True, change_port=True, timeout=timeout)
        if ok:
            return ("nat1", ext_ip, ext_port)
        break
    for srv in candidates[:4]:
        _m, ok = _test_binding(srv, local_port, change_port=True, timeout=timeout)
        if ok:
            return ("nat2", ext_ip, ext_port)
        break
    return ("nat3", ext_ip, ext_port)


# ---------------- 服务器池扫描 ----------------

def _scan_one(srv):
    """探测单台服务器（供线程池调用，不做数据库写入）。"""
    host = srv["host"]
    port = int(srv["port"])
    try:
        resp, ms, transport = _probe_once(
            host, port, timeout=config.STUN_SCAN_TIMEOUT
        )
        if resp and resp.get("mapped"):
            return {"id": srv["id"], "host": host, "port": port, "ok": True,
                    "latency": round(ms, 1), "external_ip": resp["mapped"][0],
                    "transport": transport}
    except Exception as e:
        log.debug(f"扫描 {host}:{port} 异常: {e}")
    return {"id": srv["id"], "host": host, "port": port, "ok": False,
            "latency": None, "external_ip": None, "transport": ""}


def scan_servers():
    """并行扫描所有 STUN 服务器，更新可用性与延迟。

    池子通常有近百台，串行探测要十几分钟，必须并发。
    """
    from concurrent.futures import ThreadPoolExecutor

    servers = db.list_stun_servers()
    if not servers:
        return []

    with ThreadPoolExecutor(max_workers=config.STUN_SCAN_WORKERS) as ex:
        probed = list(ex.map(_scan_one, servers))

    results = []
    for srv, r in zip(servers, probed):
        if r["ok"]:
            db.update_stun_server(
                r["id"], status="active", latency=r["latency"], fail_count=0,
                last_check=db.now(), transport=r["transport"],
            )
            status = "active"
        else:
            fc = int(srv["fail_count"] or 0) + 1
            status = "down" if fc >= 3 else (srv["status"] or "active")
            db.update_stun_server(
                r["id"], status=status, fail_count=fc,
                last_check=db.now(), transport="",
            )
        results.append({
            "host": r["host"], "port": r["port"], "ok": r["ok"],
            "latency": r["latency"], "external_ip": r["external_ip"],
            "status": status, "transport": r["transport"],
        })

    from . import state
    state.set(last_scan=db.now(), stun_ok=any(r["ok"] for r in results))
    return results
