"""极简 RFC 5389 / RFC 5780 STUN 客户端（純标准库，支持 TCP 与 UDP）。

为什么要自己写：
    pystun3 只支持 UDP。在 UDP 出向被运营商/路由封锁的环境里（实测本环境
    UDP 3478 完全不通、TCP 3478 通），必须用 STUN over TCP 才能拿到映射端口。

报文格式（RFC 5389 §6）：
    0                   1                   2                   3
    0 1 2 3 4 5 6 7 ...
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |0 0|     STUN Message Type     |         Message Length        |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                         Magic Cookie                          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                     Transaction ID (96 bits)                  |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

TCP 传输（RFC 5389 §7.2.1，非 TLS）：报文直接裸发，无长度前缀。
"""
import os
import socket
import struct
import time

MAGIC = 0x2112A442

BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
BINDING_ERROR_RESPONSE = 0x0111

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_CHANGE_REQUEST = 0x0003
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_RESPONSE_ORIGIN = 0x802B
ATTR_OTHER_ADDRESS = 0x802C
ATTR_ERROR_CODE = 0x0009
ATTR_SOFTWARE = 0x8022


class StunError(Exception):
    pass


def _new_tid():
    return os.urandom(12)


def build_binding(change_ip=False, change_port=False, tid=None):
    """构造 BINDING 请求。change_* 用于 RFC 5780 的 NAT 行为测试。"""
    tid = tid or _new_tid()
    attrs = b""
    if change_ip or change_port:
        flag = (0x04 if change_ip else 0x00) | (0x02 if change_port else 0x00)
        attrs += struct.pack("!HHI", ATTR_CHANGE_REQUEST, 4, flag)
    header = struct.pack("!HHI", BINDING_REQUEST, len(attrs), MAGIC) + tid
    return header + attrs, tid


def _decode_addr(attr_type, value, tid):
    """解析 MAPPED-ADDRESS / XOR-MAPPED-ADDRESS / OTHER-ADDRESS。"""
    if len(value) < 8:
        return None
    _reserved, family, port = struct.unpack("!BBH", value[:4])
    raw = value[4:8]
    if attr_type == ATTR_XOR_MAPPED_ADDRESS:
        xor = struct.pack("!I", MAGIC) + tid
        raw = bytes(b ^ x for b, x in zip(raw, xor))
        port ^= (MAGIC >> 16) & 0xFFFF
    elif attr_type == ATTR_MAPPED_ADDRESS:
        pass
    else:  # OTHER-ADDRESS 与 RESPONSE-ORIGIN 也是 XOR 编码
        xor = struct.pack("!I", MAGIC) + tid
        raw = bytes(b ^ x for b, x in zip(raw, xor))
        port ^= (MAGIC >> 16) & 0xFFFF
    if family != 0x01:  # 只要 IPv4
        return None
    return (socket.inet_ntoa(raw), port)


def parse(data, tid=None):
    """解析 STUN 响应。返回 dict：
        {type, mapped:(ip,port)|None, other:(ip,port)|None, origin:(ip,port)|None,
         software:str|None, error:int|None}
    """
    if len(data) < 20:
        raise StunError("响应过短")
    mtype, length, magic = struct.unpack("!HHI", data[:8])
    if magic != MAGIC:
        raise StunError("Magic Cookie 不匹配")
    resp_tid = data[8:20]
    if tid is not None and resp_tid != tid:
        raise StunError("Transaction ID 不匹配")
    body = data[20:20 + length]
    out = {
        "type": mtype,
        "mapped": None,
        "other": None,
        "origin": None,
        "software": None,
        "error": None,
    }
    i = 0
    while i + 4 <= len(body):
        atype, alen = struct.unpack("!HH", body[i:i + 4])
        val = body[i + 4:i + 4 + alen]
        if atype == ATTR_MAPPED_ADDRESS or atype == ATTR_XOR_MAPPED_ADDRESS:
            out["mapped"] = out["mapped"] or _decode_addr(atype, val, resp_tid)
        elif atype == ATTR_OTHER_ADDRESS:
            out["other"] = _decode_addr(atype, val, resp_tid)
        elif atype == ATTR_RESPONSE_ORIGIN:
            out["origin"] = _decode_addr(atype, val, resp_tid)
        elif atype == ATTR_SOFTWARE:
            out["software"] = val.decode(errors="replace")
        elif atype == ATTR_ERROR_CODE and alen >= 4:
            _r, _c, code = struct.unpack("!HBB", val[:4])
            out["error"] = _r * 100 + code
        i += 4 + alen + ((4 - (alen % 4)) % 4)
    return out


def _read_exact(sock, n, timeout):
    """从流式 socket 精确读取 n 字节。"""
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while len(buf) < n:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def bind_source(sock, local_addr):
    """三级回退绑定源地址，返回 (exact, actual)。

    1) 精确绑定 (ip, port)                 -> exact=True
    2) 退而求其次 0.0.0.0:<同一端口>       -> 端口号保留（对端口映射最关键）
    3) 再不行 0.0.0.0:0（随机端口）
    """
    if local_addr:
        try:
            sock.bind(local_addr)
            return True, local_addr
        except OSError:
            pass
    want_port = int(local_addr[1]) if local_addr else 0
    if want_port:
        try:
            sock.bind(("0.0.0.0", want_port))
            return False, ("0.0.0.0", want_port)
        except OSError:
            pass
    sock.bind(("0.0.0.0", 0))
    return False, ("0.0.0.0", 0)


def connect_binding(host, port, timeout=4.0, local_addr=None):
    """建立到 STUN 服务器的 TCP 连接，并尽量绑定指定源地址。

    退化链：精确绑定 (ip, port) -> 仅保留端口 0.0.0.0:port -> 随机端口。
    返回 (sock, exact, actual_local)；exact=False 表示没能绑到指定 IP。
    """
    ip = socket.gethostbyname(host)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    exact, _actual = bind_source(s, local_addr)
    s.settimeout(timeout)
    s.connect((ip, int(port)))
    return s, exact, s.getsockname()


def exchange(sock, req, tid, timeout):
    """在已连接的 socket 上完成一次 BINDING 请求/响应，返回 resp dict。"""
    sock.settimeout(timeout)
    sock.sendall(req)
    head = _read_exact(sock, 20, timeout)
    if len(head) < 20:
        raise StunError("无响应")
    _t, length, _m = struct.unpack("!HHI", head[:8])
    rest = _read_exact(sock, length, timeout) if length else b""
    return parse(head + rest, tid)


def request_tcp(host, port, timeout=4.0, local_addr=None, req=None, tid=None):
    """通过 TCP 发送 BINDING 请求。返回 (resp_dict, elapsed_ms)"""
    if req is None:
        req, tid = build_binding(tid=tid)
    t0 = time.time()
    s = None
    try:
        s, _exact, _local = connect_binding(host, port, timeout, local_addr)
        resp = exchange(s, req, tid, timeout)
        return resp, (time.time() - t0) * 1000
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def request_udp(host, port, timeout=4.0, local_addr=None, req=None, tid=None, retries=1):
    """通过 UDP 发送 BINDING 请求（UDP 可用时的快速路径）。"""
    if req is None:
        req, tid = build_binding(tid=tid)
    ip = socket.gethostbyname(host)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t0 = time.time()
    try:
        _exact, _actual = bind_source(s, local_addr)
        s.settimeout(timeout)
        for _ in range(retries + 1):
            s.sendto(req, (ip, int(port)))
            try:
                data, _addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            elapsed = (time.time() - t0) * 1000
            return parse(data, tid), elapsed, "udp"
        raise StunError("UDP 超时")
    finally:
        try:
            s.close()
        except Exception:
            pass
