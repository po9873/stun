"""运行期配置解析。

优先级：**WebUI 设置页写入数据库的值 > 环境变量 > 内置默认值**。
这样爱快地址/令牌这类敏感项不需要写进 docker-compose，
在浏览器里填一次即可；同时保留环境变量作为无人值守部署的兜底。
"""
from . import db

# key -> (环境变量候选名, 内置默认值)
_SCHEMA = {
    "ikuai_base_url": (("IKUAI_CLI_BASE_URL", "IKUAI_URL"), "https://192.168.6.254"),
    "ikuai_token": (("IKUAI_CLI_TOKEN", "IKUAI_TOKEN"), ""),
    "ikuai_interface": (("IKUAI_INTERFACE", "IKUAI_CLI_INTERFACE"), "adsl1"),
    "ikuai_verify_ssl": (("IKUAI_VERIFY_SSL", "IKUAI_CLI_VERIFY_SSL"), "false"),
    # 爱快 DNAT 的 wan_port 取哪个值：
    #   preserve = 端口保留，等于内网端口（默认，也是最常用/最不容易错的那个）
    #              标准 CGNAT 下，入站流量到达爱快时目的端口就是内网端口。
    #   stun     = STUN 实测的公网端口
    #              仅当改端口的是爱快自己（而不是运营商 CGNAT）时才正确。
    # 判据：STUN 实测端口 == 内网端口 说明整条链路端口保留，两种模式结果一致；
    #       实测端口 != 内网端口 说明是外层 CGNAT 在改端口，此时必须用 preserve。
    "wan_port_mode": (("WAN_PORT_MODE",), "preserve"),
    # STUN 清单地址：可填 raw 直链，也可直接粘 GitHub 网页地址（会自动纠正）
    "stun_pool_url": (("STUN_POOL_URL",), ""),
    # 变更通知（对应 Lucky 的「STUN 穿透 + Webhook 推送」）
    "notify_enabled": (("NOTIFY_ENABLED",), "false"),
    # 推送目标：custom / dingtalk / wecom / feishu / bark
    "notify_kind": (("NOTIFY_KIND",), "custom"),
    "notify_url": (("NOTIFY_URL",), ""),
    # 同一映射的最小推送间隔（秒）。CGNAT 端口会漂移，不加冷却会疯狂刷屏。
    "notify_min_gap": (("NOTIFY_MIN_GAP",), "600"),
    "notify_on": (("NOTIFY_ON",), "port,mapping,reconcile"),
}

#: 对外展示时不应回显明文的配置项
SECRET_KEYS = ("ikuai_token",)


def get(key):
    import os

    v = db.get_setting(key)
    if v is not None and v != "":
        return v
    for name in _SCHEMA.get(key, ((), ""))[0]:
        v = os.environ.get(name)
        if v:
            return v.strip()
    return _SCHEMA.get(key, ((), ""))[1]


def get_bool(key):
    return str(get(key)).strip().lower() in ("1", "true", "yes", "on", "y")


def set_values(data: dict):
    """写入设置。值为空字符串时表示「不修改」（保留原值）。"""
    for k, v in (data or {}).items():
        if k not in _SCHEMA:
            continue
        if v is None:
            continue
        v = str(v).strip()
        if v == "":
            continue  # 空值 = 保持不变（WebUI 里令牌留空即沿用旧令牌）
        db.set_setting(k, v)


def public_view():
    """返回可安全回显给前端的配置（令牌只回传是否已设置）。"""
    base = get("ikuai_base_url") or ""
    if base and not base.endswith("/api/v4.0"):
        api_base = base.rstrip("/") + "/api/v4.0"
    else:
        api_base = base
    return {
        "ikuai_base_url": base.replace("/api/v4.0", "").rstrip("/"),
        "ikuai_api_base": api_base,
        "ikuai_interface": get("ikuai_interface"),
        "ikuai_verify_ssl": get_bool("ikuai_verify_ssl"),
        "wan_port_mode": get("wan_port_mode"),
        "stun_pool_url": get("stun_pool_url"),
        "ikuai_token_set": bool(get("ikuai_token")),
        "ikuai_configured": bool(get("ikuai_token")),
        "notify_enabled": get_bool("notify_enabled"),
        "notify_kind": get("notify_kind"),
        "notify_url": get("notify_url"),
        "notify_min_gap": get("notify_min_gap"),
        "notify_on": get("notify_on"),
        # 来源提示：告诉用户当前值是来自 WebUI 还是环境变量
        "sources": {k: ("webui" if db.get_setting(k) else "env") for k in _SCHEMA},
    }
