#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stun 常驻探针（agent）—— 装一次，之后全自动。

它循环做三件事：
  1. 向 stun 服务端拉一份「我该测哪些端口」的清单（服务端按来源 IP 判断）；
  2. 用本机 IP + 那些端口去问 STUN 服务器，拿运营商 CGNAT 给这个端口分配的公网端口；
  3. 批量回报给服务端，由服务端入库、推爱快、发通知。

装好之后页面上新增/修改端口都不用再管它 —— 清单是它自己每轮去拉的，
间隔也是服务端下发的。包必须发得够勤，CGNAT 那条映射才不会被回收。

用法：
    python3 agent.py                       常驻，每 30 秒一轮（默认）
    python3 agent.py --interval 60         自定义间隔
    python3 agent.py --once                只跑一轮就退出（装的时候自检用）
    python3 agent.py --ip 192.168.6.13     多网卡时指定本机 IP

只需 python3 标准库，不用 pip 装任何东西。
"""
import argparse
import json
import os
import platform
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

AGENT_VERSION = "1.0"

MAGIC = 0x2112A442
MAGIC_B = struct.pack("!I", MAGIC)
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020

# 拿不到服务端清单时的兜底
FALLBACK_SERVERS = [
    "stun.chat.bilibili.com:3478",
    "global.stun.twilio.com:3478",
    "stun.sipnet.net:3478",
    "stun.voip.blackberry.com:3478",
    "stun.nextcloud.com:3478",
]


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------- 本机信息 ----------------
def my_lan_ip():
    """本机出网用的 IP（UDP connect 技巧，不会真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def listening_tcp_ports():
    """本机正在监听的 TCP 端口。服务端不可达时用它兜底，免得白装。"""
    ports = set()
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/tcp", encoding="utf-8", errors="ignore") as f:
                for line in f.read().splitlines()[1:]:
                    cols = line.split()
                    if len(cols) > 3 and cols[3] == "0A":      # 0A = LISTEN
                        try:
                            ports.add(int(cols[1].split(":")[1], 16))
                        except ValueError:
                            pass
        else:
            out = subprocess.run(["netstat", "-an"], capture_output=True,
                                 timeout=8).stdout.decode("utf-8", "ignore")
            for line in out.splitlines():
                if "LISTEN" in line and "tcp" in line.lower():
                    parts = line.split()
                    if len(parts) > 1 and ":" in parts[1]:
                        try:
                            ports.add(int(parts[1].rsplit(":", 1)[1]))
                        except ValueError:
                            pass
    except Exception:
        pass
    return sorted(p for p in ports if 1 <= p < 65536)


# ---------------- TCP STUN ----------------
def build_request():
    tid = struct.pack("!I", int(time.time())) + b"\x00" * 8
    return struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC) + tid, tid


def parse_response(data, tid):
    if len(data) < 20:
        return None
    typ, length, magic = struct.unpack("!HHI", data[:8])
    if typ != BINDING_RESPONSE or magic != MAGIC or (tid and data[8:20] != tid):
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
            return socket.inet_ntoa(val[4:8]), port
        i += 4 + alen + ((4 - alen % 4) % 4)
    return None


def stun_once(server, lan_ip, lan_port, timeout=5):
    """从 (lan_ip, lan_port) 发一次 TCP STUN，返回 (公网IP, 公网端口)。"""
    host, _, port = server.partition(":")
    port = int(port or 3478)
    req, tid = build_request()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((lan_ip, lan_port))
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


# ---------------- 与服务端通信 ----------------
def http_json(url, data=None, timeout=8):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_plan(base, lan_ip, timeout=8):
    url = "%s/api/agent/plan?ip=%s" % (base, urllib.parse.quote(lan_ip))
    return http_json(url, timeout=timeout)


def post_report(base, payload, timeout=10):
    return http_json(base + "/api/agent/report", data=payload, timeout=timeout)


# ---------------- 主循环 ----------------
def probe_all(servers, lan_ip, ports, timeout):
    """逐个端口测。端口被本机服务占着时绑不上（OSError），这条就不硬撑了。"""
    results = []
    for p in ports:
        pub, err = None, None
        for sv in servers:
            try:
                pub = stun_once(sv, lan_ip, p, timeout)
                if pub:
                    break
            except OSError as e:
                # 绑不上：端口被本机的服务占着，换服务器也没用，直接记下来
                err = "端口 %s 被本机服务占用，绑不上（%s）" % (p, e)
                break
            except Exception as e:
                err = repr(e)
        results.append({
            "lan_port": p,
            "public_ip": pub[0] if pub else None,
            "public_port": pub[1] if pub else None,
            "ok": 1 if pub else 0,
            "error": err,
        })
    return results


def main():
    ap = argparse.ArgumentParser(description="stun 常驻探针：持续上报本机端口的公网端口")
    ap.add_argument("--server", default=os.environ.get("STUN_SERVER") or "",
                    help="stun 服务端地址，如 http://192.168.6.104")
    ap.add_argument("--ip", default=None, help="本机内网 IP（多网卡时指定）")
    ap.add_argument("--interval", type=int, default=0, help="探测间隔秒数（服务端会下发，0=用服务端的）")
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--scan", action="store_true",
                    help="服务端没给清单时，顺带扫本机监听的 TCP 端口（默认不扫，避免噪音）")
    ap.add_argument("--no-report", action="store_true", help="只打印不回报")
    args = ap.parse_args()

    base = args.server.rstrip("/")
    if not base:
        print("需要 --server http://<stun 地址> 或设置 STUN_SERVER 环境变量", file=sys.stderr)
        return 2

    lan_ip = args.ip or my_lan_ip()
    servers, ports, interval = list(FALLBACK_SERVERS), None, args.interval or 30

    log("stun agent %s 启动：本机 %s -> 服务端 %s" % (AGENT_VERSION, lan_ip, base))

    while True:
        # 1) 拉清单。拉不到就沿用上一轮的，实在没有就扫本机监听端口
        try:
            plan = fetch_plan(base, lan_ip)
            if plan.get("servers"):
                servers = plan["servers"]
            if plan.get("ports"):
                ports = [int(p) for p in plan["ports"]]
            if plan.get("interval"):
                interval = max(10, int(plan["interval"]))
            last_good = time.time()
        except Exception as e:
            log("拉清单失败：%r" % (e,))

        if args.interval:
            interval = args.interval

        if not ports and args.scan:
            ports = listening_tcp_ports()

        # 2) 逐个端口测
        if ports:
            results = probe_all(servers, lan_ip, ports, args.timeout)
            for r in results:
                if r["ok"]:
                    log("%s:%s -> 公网 %s:%s" % (lan_ip, r["lan_port"],
                                                 r["public_ip"], r["public_port"]))
                else:
                    log("%s:%s 探测失败 %s" % (lan_ip, r["lan_port"], r["error"] or ""))

            # 3) 回报
            if not args.no_report:
                try:
                    res = post_report(base, {
                        "lan_ip": lan_ip,
                        "hostname": platform.node(),
                        "interval": interval,
                        "version": AGENT_VERSION,
                        "results": results,
                    })
                    if res.get("matched"):
                        log("已回报，命中：%s" % ", ".join(res["matched"]))
                except Exception as e:
                    log("回报失败：%r" % (e,))
        else:
            log("服务端没给端口清单（这台机还没登记映射，或登记的全是 UDP 端口），"
                "%ss 后再问" % interval)

        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
