"""全局运行状态（NAT 类型、是否被阻断、最近一次外部 IP 等），供 WebUI 读取。"""
import threading

_lock = threading.Lock()
_state = {
    "nat_type": None,       # nat1 / nat2 / nat3 / nat4 / blocked / unknown / error
    "nat_blocked": False,   # True 时暂停主轮询并红字告警
    "external_ip": None,    # 探测到的公网 IP
    "last_nat_check": None,
    "stun_ok": False,
    "last_scan": None,
    # 对账时从爱快读回的真实 wan_port：{rule_id: "5055"}，供 WebUI 标红不一致
    "router_wan_ports": {},
    "last_reconcile": None,
}


def get():
    with _lock:
        return dict(_state)


def set(**kw):
    with _lock:
        _state.update(kw)
