#!/usr/bin/env python3
"""stun 探针客户端 —— 在任意一台内网机器上跑，测出它自己某个端口的公网端口并回报。

为什么必须在本机跑：
    CGNAT 的公网端口是「每条流」分配的。容器拿别人的内网 IP 的端口去探测时，
    爱快发现那个外部端口已经被目标机占着，只能另给一个，于是测出来的是容器
    自己那条映射，与目标机无关（实测：目标机 39213，容器测出 2048）。
    只有流量真的从那台机发出去，测出来的才是它自己的公网端口。

用法：
    python3 probe.py 46065                      测一次并回报
    python3 probe.py 46065 --ip 192.168.6.13    指定本机内网 IP（多网卡时用）
    python3 probe.py 46065 --loop 30            每 30 秒测一次，兼作 CGNAT 心跳保活
    python3 probe.py 46065 --server http://192.168.6.104

只需要 python3 标准库，无需 pip 装任何东西。
"""
import argparse
import json
import socket
import struct
import sys
import time
import urllib.request

MAGIC = 0x2112A442
MAGIC_B = struct.pack("!I", MAGIC)
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020

# 内置备用清单（回报地址拿不到清单时用）
FALLBACK_SERVERS = [
    "stun.chat.bilibili.com:3478",
    "global.stun.twilio.com:3478",
    "stun.sipnet.net:3478",
    "stun.voip.blackberry.com:3478",
    "stun.nextcloud.com:3478",
]


def my_lan_ip():
    """用 UDP connect 技巧拿到本机出网用的 IP（不会真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def build_request():
    tid = struct.pack("!I", int(time.time())) + b"\x00" * 8
    return struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC) + tid, tid


def parse_response(data, tid):
    if len(data) < 20:
        return None
    typ, length, magic = struct.unpack("!HHI", data[:8])
    if typ != BINDING_RESPONSE or magic != MAGIC or tid and data[8:20] != tid:
        return None
    body = data[20:20 + length]
    i = 0
    while i + 4 <= len(body):
        atype, alen = struct.unpack("!HH", body[i:i + 4])
        val = body[i + 4:i + 4 + alen]
        if atype == ATTR_XOR_MAPPED_ADDRESS and alen >= 8:
            port = struct.unpack("!H", val[2:4])[0] ^ (MAGIC >> 16)
            ip = socket.inet_ntoa(bytes(b ^ m for b, m in zip(val[4:8], MAGIC_B)))
            return ip, port
        if atype == ATTR_MAPPED_ADDRESS and alen >= 8:
            port = struct.unpack("!H", val[2:4])[0]
            ip = socket.inet_ntoa(val[4:8])
            return ip, port
        i += 4 + alen + ((4 - alen % 4) % 4)
    return None


def stun_once(server, lan_ip, lan_port, timeout=5):
    """从本机 (lan_ip, lan_port) 发一次 TCP STUN，返回 (公网IP, 公网端口)。"""
    host, _, port = server.partition(":")
    port = int(port or 3478)
    req, tid = build_request()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            s.bind((lan_ip, lan_port))
        except OSError as e:
            raise RuntimeError("绑不到 %s:%s —— 端口被别的程序占着，"
                               "或这个 IP 不在本机 (%s)" % (lan_ip, lan_port, e))
        s.settimeout(timeout)
        s.connect((socket.gethostbyname(host), port))
        s.sendall(req)
        buf = b""
        while len(buf) < 20:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if len(buf) >= 20:
            # 长度字段可能还没收全，按需续读
            need = 20 + struct.unpack("!H", buf[2:4])[0]
            while len(buf) < need:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return parse_response(buf, tid)
    finally:
        try:
            s.close()
        except Exception:
            pass


def fetch_servers(stun_url):
    try:
        req = urllib.request.urlopen(stun_url.rstrip("/") + "/api/stun/servers", timeout=4)
        d = json.loads(req.read().decode())
        out = ["%s:%s" % (s["host"], s["port"]) for s in d if s.get("status") == "active"]
        if out:
            return out[:5]
    except Exception:
        pass
    return FALLBACK_SERVERS


def report(stun_url, lan_ip, lan_port, pub_ip, pub_port):
    body = json.dumps({"lan_ip": lan_ip, "lan_port": lan_port,
                       "public_ip": pub_ip, "public_port": pub_port}).encode()
    req = urllib.request.Request(
        stun_url.rstrip("/") + "/api/probe/report", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser(description="测出本机某个端口的公网端口并回报给 stun")
    ap.add_argument("port", type=int, help="要探测的本机端口（即服务的监听端口）")
    ap.add_argument("--ip", default=None, help="本机内网 IP（多网卡时指定）")
    ap.add_argument("--server", default=None, help="stun 地址，如 http://192.168.6.104")
    ap.add_argument("--loop", type=int, default=0, metavar="SEC",
                    help="循环探测间隔秒数，兼作 CGNAT 心跳保活（建议 30）")
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--no-report", action="store_true", help="只打印，不回报")
    args = ap.parse_args()

    lan_ip = args.ip or my_lan_ip()
    # 从环境变量拿 stun 地址，方便直接 curl | python3 用
    stun_url = args.server or __import__("os").environ.get("STUN_SERVER")
    servers = fetch_servers(stun_url) if stun_url else FALLBACK_SERVERS

    if not stun_url:
        print("提示：未指定 --server / STUN_SERVER，只打印不回报", file=sys.stderr)

    while True:
        result = None
        for sv in servers:
            try:
                result = stun_once(sv, lan_ip, args.port, args.timeout)
                if result:
                    break
            except Exception as e:
                print("  %s 失败: %s" % (sv, e), file=sys.stderr)
        if result:
            pub_ip, pub_port = result
            print("%s:%s -> 公网 %s:%s" % (lan_ip, args.port, pub_ip, pub_port))
            if stun_url and not args.no_report:
                try:
                    r = report(stun_url, lan_ip, args.port, pub_ip, pub_port)
                    print("  已回报 stun: %s" % r.get("matched"))
                except Exception as e:
                    print("  回报失败: %s" % e, file=sys.stderr)
        else:
            print("%s:%s -> 探测失败（所有 STUN 服务器都没响应）" % (lan_ip, args.port),
                  file=sys.stderr)
        if not args.loop:
            return 1 if result is None else 0
        time.sleep(args.loop)


if __name__ == "__main__":
    sys.exit(main())
