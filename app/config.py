"""环境变量配置。所有敏感信息（爱快令牌、接口名）均通过环境变量注入，不写死在代码里。"""
import os


def _env(name, default=""):
    v = os.environ.get(name)
    return v if v is not None else default


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _first(*names, default=""):
    """按顺序取第一个非空环境变量。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip():
            return v.strip()
    return default


# ---------------------------------------------------------------------------
# 爱快连接配置
# 命名对齐官方 ikuai-cli：IKUAI_CLI_BASE_URL 只填「路由管理地址」，
# /api/v4.0 由本程序自动拼接，不需要（也不应该）手写在环境变量里。
#   推荐：IKUAI_CLI_BASE_URL=https://192.168.6.254
#   兼容：IKUAI_URL=https://192.168.6.254（同样只填管理地址）
#   若已带 /api/v4.0 后缀（历史写法），则不再重复拼接。
# ---------------------------------------------------------------------------
_IKUAI_BASE = _first("IKUAI_CLI_BASE_URL", "IKUAI_URL", default="https://192.168.6.254")
_IKUAI_BASE = _IKUAI_BASE.rstrip("/")
if not _IKUAI_BASE.rstrip("/").endswith("/api/v4.0"):
    _IKUAI_BASE = _IKUAI_BASE + "/api/v4.0"

IKUAI_URL = _IKUAI_BASE                               # 完整 API 基址（含 /api/v4.0）
IKUAI_CLI_BASE_URL = _IKUAI_BASE.replace("/api/v4.0", "")   # 纯管理地址（仅供展示/排错）
IKUAI_TOKEN = _first("IKUAI_CLI_TOKEN", "IKUAI_TOKEN")      # 个人 API 令牌
IKUAI_INTERFACE = _first("IKUAI_INTERFACE", "IKUAI_CLI_INTERFACE", default="adsl1")
IKUAI_VERIFY_SSL = _bool("IKUAI_CLI_VERIFY_SSL",
                         _bool("IKUAI_VERIFY_SSL", False))  # 自签证书，默认跳过校验
MOCK = _bool("IKUAI_MOCK", False)                     # 离线 Mock 模式（不碰真路由）

DATA_DIR = _env("DATA_DIR", "/data")                  # SQLite 持久化目录（-v data:/data）
WEB_HOST = _env("WEB_HOST", "0.0.0.0")
WEB_PORT = _int("WEB_PORT", 80)

POLL_INTERVAL = _int("POLL_INTERVAL", 60)             # 映射轮询间隔（秒）
STUN_SCAN_INTERVAL = _int("STUN_SCAN_INTERVAL", 3600) # STUN 服务器扫描间隔（秒）
NAT_CHECK_INTERVAL = _int("NAT_CHECK_INTERVAL", 3600) # NAT 复检间隔（秒）
RECONCILE_INTERVAL = _int("RECONCILE_INTERVAL", 300)  # 与爱快对账间隔（秒）
DEFAULT_PROTO = _env("DEFAULT_PROTO", "tcp")          # 新增映射默认协议
STUN_TIMEOUT = _int("STUN_TIMEOUT", 4)

# ---- STUN 服务器池在线更新（pradt2/always-online-stun，官方建议直接硬编码该链接）----
STUN_POOL_URL = _first(
    "STUN_POOL_URL",
    default="https://raw.githubusercontent.com/pradt2/always-online-stun/master/valid_hosts_tcp.txt",
)
STUN_NAT_POOL_URL = _first(
    "STUN_NAT_POOL_URL",
    default="https://raw.githubusercontent.com/pradt2/always-online-stun/master/valid_nat_testing_hosts_tcp.txt",
)
STUN_POOL_REFRESH_INTERVAL = _int("STUN_POOL_REFRESH_INTERVAL", 3600)  # 池子刷新间隔（秒）
STUN_POOL_TIMEOUT = _int("STUN_POOL_TIMEOUT", 15)     # 拉取池子清单超时（秒）
STUN_POOL_ENABLED = _bool("STUN_POOL_ENABLED", True)  # 关掉则只用内置清单

# 扫描必须并行：池子有近百台服务器，串行探测要十几分钟
STUN_SCAN_WORKERS = _int("STUN_SCAN_WORKERS", 16)     # 扫描并发数
STUN_SCAN_TIMEOUT = _int("STUN_SCAN_TIMEOUT", 4)      # 扫描单次超时（秒）
