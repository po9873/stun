"""把运行日志收集进内存环形缓冲，供 WebUI「日志」页查看。

容器日志在 Portainer 里看得到，但这里想直接在工具内看到
「探测/建规则/失败挂起」这条业务主线，省得来回切。
"""
import logging
import threading
from collections import deque

MAX = 500
_buf = deque(maxlen=MAX)
_lock = threading.Lock()


class BufferHandler(logging.Handler):
    """只负责把日志收集进缓冲区。

    容器 stdout 由 logging.basicConfig 安装的 root StreamHandler 负责，
    这里不再重复 print，否则容器日志会出现两遍。
    """

    def emit(self, record):
        with _lock:
            _buf.append({
                "time": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),   # 只存正文，时间戳/级别由前端渲染
            })


def install(level=logging.INFO):
    h = BufferHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("stun")
    root.setLevel(level)
    if not any(isinstance(x, BufferHandler) for x in root.handlers):
        root.addHandler(h)
    return h


def recent(limit=200):
    with _lock:
        items = list(_buf)[-limit:]
    out = []
    import time as _t
    for it in items:
        out.append({
            "time": _t.strftime("%H:%M:%S", _t.localtime(it["time"])),
            "level": it["level"],
            "logger": it["logger"].replace("stun.", ""),
            "msg": it["msg"],
        })
    return out


def clear():
    with _lock:
        _buf.clear()
