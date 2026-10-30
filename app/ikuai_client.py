"""爱快 iKuai v4.0 REST API 客户端。

已真机验证的契约（路由 192.168.6.254 / 固件 4.0.313）：
    Base   = https://<host>/api/v4.0
    Auth   = Authorization: Bearer <个人API令牌>
    TLS    = 自签证书，需跳过校验
    DNAT 列表 : GET  /network/dnat/rules          -> {code:0, results:{data:[...]}}
    DNAT 创建 : POST /network/dnat/rules          -> {code:0, rowid:<规则ID>}
    DNAT 更新 : PUT  /network/dnat/rules/{id}      （需传完整字段）
    DNAT 删除 : DELETE /network/dnat/rules/{id}
    DNAT 启停 : PATCH /network/dnat/rules/{id}     body {"enabled":"yes"/"no"}

创建 body 字段：tagname, lan_addr, lan_port, wan_port, protocol,
                interface, enabled, comment, src_addr{custom:[],object:[]}
端口/enable 均为字符串；protocol 支持 tcp / udp / tcp+udp / any。
"""
import logging

import requests

from . import config, settings

log = logging.getLogger("stun.ikuai")

# Mock 模式下的共享规则存储（模拟一台路由器，所有客户端实例共享）
_MOCK_RULES = {}
_MOCK_ID = 0


class IKuaiError(Exception):
    pass


def int_to_ip(n):
    """爱快 GET 规则时会同时返回 lan_addr（字符串）与 lan_addr_int（整数）。
    刚创建完 / 部分老规则 lan_addr 可能为空或还没落定，此时用整数还原成点分十进制。
    直接把整数塞回 PUT 会被爱快判为「参数错误: lan_addr」。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if not 0 <= n <= 0xFFFFFFFF:
        return None
    import socket as _socket
    import struct as _struct
    try:
        return _socket.inet_ntoa(_struct.pack("!I", n))
    except OSError:
        return None


def parse_port(v):
    """爱快的端口字段是字符串，可能是单个端口 '80'，也可能是范围 '1000-2000'。

    返回 (端口号 or None, 是否范围)。范围型规则本工具暂不接管。
    """
    s = str(v or "").strip()
    if not s:
        return None, False
    if "-" in s or "," in s or " " in s:
        return None, True
    try:
        p = int(s)
    except ValueError:
        return None, False
    return (p, False) if 1 <= p <= 65535 else (None, False)


def norm_proto(p):
    """把爱快的协议写法归一化成本工具支持的 tcp / udp / tcp+udp / any。"""
    s = str(p or "").strip().lower()
    if s in ("tcp+udp", "tcpudp", "udp+tcp", "tcp_udp"):
        return "tcp+udp"
    if s in ("tcp", "udp"):
        return s
    if s in ("any", "all", ""):
        return "any"
    return s


class IKuaiClient:
    def __init__(self, base_url=None, token=None, interface=None, verify_ssl=None, mock=None):
        # 值一律「现用现取」，这样在 WebUI 改完配置后，已存在的客户端实例立刻生效，
        # 不需要重启容器（后台轮询线程持有的是同一个客户端对象）。
        self._base_url = base_url.rstrip("/") if base_url else None
        self._token = token
        self._interface = interface
        self._verify_ssl = verify_ssl
        self.mock = config.MOCK if mock is None else mock

    @property
    def base_url(self):
        if self._base_url:
            b = self._base_url
        else:
            b = settings.get("ikuai_base_url").rstrip("/")
        if not b.endswith("/api/v4.0"):
            b = b + "/api/v4.0"
        return b

    @property
    def token(self):
        return self._token if self._token is not None else settings.get("ikuai_token")

    @property
    def interface(self):
        return self._interface or settings.get("ikuai_interface")

    @property
    def verify_ssl(self):
        if self._verify_ssl is not None:
            return self._verify_ssl
        return settings.get_bool("ikuai_verify_ssl")

    # ---------------- 底层请求 ----------------
    def _req(self, method, path, body=None):
        if self.mock:
            return self._mock_req(method, path, body)
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = requests.request(
                method, url, json=body, headers=headers,
                verify=self.verify_ssl, timeout=10,
            )
        except requests.RequestException as e:
            raise IKuaiError(f"请求失败: {e}")
        try:
            data = resp.json()
        except ValueError:
            raise IKuaiError(f"非 JSON 响应 {resp.status_code}: {resp.text[:200]}")
        code = data.get("code")
        if code is not None and code not in (0, 20000) and data.get("message", "").lower() != "success":
            raise IKuaiError(f"爱快错误 code={code} msg={data.get('message')}")
        if resp.status_code >= 400:
            raise IKuaiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return data

    def _mock_req(self, method, path, body):
        if method == "GET" and path == "/network/dnat/rules":
            return {"code": 0, "results": {"data": list(_MOCK_RULES.values())}}
        if method == "GET" and path.startswith("/network/dnat/rules/"):
            rid = int(path.rsplit("/", 1)[1])
            return {"code": 0, "data": _MOCK_RULES.get(rid, {})}
        if method == "POST" and path == "/network/dnat/rules":
            global _MOCK_ID
            _MOCK_ID += 1
            rid = _MOCK_ID
            rule = dict(body)
            rule["id"] = rid
            _MOCK_RULES[rid] = rule
            return {"code": 0, "rowid": rid}
        if method in ("PUT", "PATCH") and path.startswith("/network/dnat/rules/"):
            rid = int(path.rsplit("/", 1)[1])
            if rid in _MOCK_RULES:
                _MOCK_RULES[rid].update(body or {})
            return {"code": 0}
        if method == "DELETE" and path.startswith("/network/dnat/rules/"):
            rid = int(path.rsplit("/", 1)[1])
            _MOCK_RULES.pop(rid, None)
            return {"code": 0}
        return {"code": 0}

    # ---------------- 高层接口 ----------------
    def list_dnat(self):
        data = self._req("GET", "/network/dnat/rules")
        if "results" in data and isinstance(data["results"], dict):
            return data["results"].get("data", []) or []
        return data.get("data", []) or []

    def read_rules(self):
        """读取并解析爱快上现有的 DNAT 规则，供 WebUI 的「发现并导入」使用。

        无法接管的规则（端口范围型、取不到内网地址）会标出 adoptable=False + reason，
        导入时被跳过，避免接过来之后对账逻辑推错端口。
        """
        out = []
        for r in self.list_dnat() or []:
            try:
                rid = int(r.get("id"))
            except (TypeError, ValueError):
                continue
            lan_addr = (r.get("lan_addr") or "").strip()
            if not lan_addr or lan_addr == "0.0.0.0":
                lan_addr = int_to_ip(r.get("lan_addr_int")) or ""
            lan_port, is_range = parse_port(r.get("lan_port"))
            wan_port, wan_range = parse_port(r.get("wan_port"))
            raw_lan = str(r.get("lan_port") or "").strip()
            raw_wan = str(r.get("wan_port") or "").strip()
            item = {
                "id": rid,
                "name": (r.get("tagname") or "").strip() or f"规则{rid}",
                "interface": r.get("interface") or "",
                "lan_addr": lan_addr,
                "lan_port": lan_port,
                "wan_port": wan_port,
                "lan_port_raw": raw_lan,
                "wan_port_raw": raw_wan,
                "proto": norm_proto(r.get("protocol")),
                "enabled": str(r.get("enabled") or "yes").strip().lower() == "yes",
                "comment": r.get("comment") or "",
                "adoptable": True,
                "reason": "",
            }
            if not lan_addr:
                item["adoptable"] = False
                item["reason"] = "取不到内网地址"
            elif is_range or wan_range:
                item["adoptable"] = False
                item["reason"] = f"端口范围（{raw_wan}→{raw_lan}），暂不支持"
            elif lan_port is None:
                item["adoptable"] = False
                item["reason"] = "内网端口无效"
            out.append(item)
        return out

    def get_dnat(self, rule_id):
        """取单条规则。爱快按 ID 查有时返回空壳（字段不全），
        那就退回列表里捞 —— PUT 必须带完整字段，缺 wan_port 会被判「参数错误」。
        """
        raw = {}
        try:
            data = self._req("GET", f"/network/dnat/rules/{rule_id}")
            raw = data.get("data") or data.get("results") or {}
        except IKuaiError:
            raw = {}
        if isinstance(raw, dict) and str(raw.get("wan_port") or "").strip():
            return raw
        for r in self.list_dnat():
            if str(r.get("id")) == str(rule_id):
                return r
        return raw if isinstance(raw, dict) else {}

    def rule_fields(self, m):
        """这条映射在爱快上应该长什么样：一条普普通通的端口映射。

        内网地址、内部端口原样不动，外网端口默认就是内部端口 —— 跟你在爱快界面上
        手工加的端口映射一模一样，不引入任何额外端口。
        """
        return {"tagname": m["name"], "lan_addr": m["lan_ip"],
                "lan_port": str(int(m["lan_port"])),
                "wan_port": str(int(m.get("wan_port") or m["lan_port"])),
                "protocol": m.get("proto") or "tcp",
                "interface": self.interface}

    def create_dnat(self, mapping):
        """创建 DNAT 规则，返回规则 ID（rowid）。mapping 含 name/lan_ip/lan_port/proto/wan_port。"""
        body = {
            "tagname": mapping["name"],
            "lan_addr": mapping["lan_ip"],
            "lan_port": str(int(mapping["lan_port"])),
            "wan_port": str(int(mapping.get("wan_port") or mapping["lan_port"])),
            "protocol": mapping["proto"],
            "interface": self.interface,
            "enabled": "yes",
            # 备注与名称保持一致，路由上按名字一眼就能认出是哪条
            "comment": mapping["name"],
            "src_addr": {"custom": [], "object": []},
        }
        data = self._req("POST", "/network/dnat/rules", body)
        rid = data.get("rowid")
        if rid is None:
            rid = (data.get("data") or {}).get("id")
        return int(rid) if rid is not None else None

    def update_dnat(self, rule_id, fields, fallback=None):
        """更新 DNAT 规则。爱快 PUT 需传完整字段，因此先取回原规则再合并，保证只改传入字段。

        fallback 可传入已知的内网地址/端口等，用于 GET 取不完整时的兜底。
        """
        cur = self.get_dnat(rule_id) or {}

        lan_addr = (cur.get("lan_addr") or "").strip()
        if not lan_addr:
            lan_addr = int_to_ip(cur.get("lan_addr_int")) or ""
        if not lan_addr and fallback:
            lan_addr = fallback.get("lan_addr") or ""

        lan_port = cur.get("lan_port")
        if lan_port in (None, ""):
            lan_port = (fallback or {}).get("lan_port")
        protocol = cur.get("protocol") or (fallback or {}).get("protocol")
        interface = cur.get("interface") or (fallback or {}).get("interface") or self.interface

        if not lan_addr:
            raise IKuaiError(f"规则 {rule_id} 取不到有效的内网地址，无法更新")

        src = cur.get("src_addr")
        if not isinstance(src, dict):
            src = {"custom": [], "object": []}

        wan_port = cur.get("wan_port")
        if wan_port in (None, ""):
            wan_port = (fallback or {}).get("wan_port")
        if wan_port in (None, ""):
            wan_port = lan_port          # 拿不到就按端口保留处理，别把空值塞给爱快

        tag = cur.get("tagname") or (fallback or {}).get("tagname") or ""
        body = {
            "tagname": tag,
            "lan_addr": lan_addr,
            "lan_port": lan_port,
            "wan_port": wan_port,
            "protocol": protocol,
            "interface": interface,
            "enabled": cur.get("enabled") or "yes",
            # 备注跟着名称走：改名时备注一起改，存量规则也在对账时顺手纠正
            "comment": fields.get("tagname") or tag,
            "src_addr": src,
        }
        body.update(fields)
        for k in ("lan_port", "wan_port"):
            if body.get(k) not in (None, ""):
                body[k] = str(int(body[k]))
        return self._req("PUT", f"/network/dnat/rules/{rule_id}", body)

    def delete_dnat(self, rule_id):
        return self._req("DELETE", f"/network/dnat/rules/{rule_id}")

    def toggle_dnat(self, rule_id, enabled):
        return self._req("PATCH", f"/network/dnat/rules/{rule_id}",
                         {"enabled": "yes" if enabled else "no"})
