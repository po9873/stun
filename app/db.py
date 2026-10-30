"""SQLite 数据层。两张表：mappings（映射）、stun_servers（STUN 服务器池）。

每次操作独立建连（低并发场景下最简单且线程安全）。
"""
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()


def db_path():
    d = config.DATA_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "stun.db")


def _conn():
    c = sqlite3.connect(db_path(), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init():
    with _lock:
        c = _conn()
        c.execute(
            """CREATE TABLE IF NOT EXISTS mappings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                lan_ip TEXT NOT NULL,
                lan_port INTEGER NOT NULL,
                proto TEXT NOT NULL,
                wan_port INTEGER,
                rule_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                fail_count INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                note TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS stun_servers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 3478,
                status TEXT NOT NULL DEFAULT 'active',
                latency REAL,
                last_check TEXT,
                fail_count INTEGER DEFAULT 0,
                manual INTEGER DEFAULT 0,
                transport TEXT DEFAULT '',
                nat_capable INTEGER DEFAULT 0,
                UNIQUE(host, port)
            )"""
        )
        # 在线迁移：为旧库补齐新列
        have = {r["name"] for r in c.execute("PRAGMA table_info(stun_servers)").fetchall()}
        for col, ddl in (("transport", "TEXT DEFAULT ''"),
                         ("nat_capable", "INTEGER DEFAULT 0"),
                         ("removed", "INTEGER DEFAULT 0")):
            if col not in have:
                c.execute(f"ALTER TABLE stun_servers ADD COLUMN {col} {ddl}")

        # 在线迁移：为已有库增加 public_port（STUN 实测的公网侧端口）
        have_m = {r["name"] for r in c.execute("PRAGMA table_info(mappings)").fetchall()}
        for col, ddl in (("public_port", "INTEGER"),
                         ("public_port_manual", "INTEGER DEFAULT 0"),
                         # wan_port_manual=1 表示这条映射的外网端口是用户手填的，
                         # 模式切换时不能被自动覆盖
                         ("wan_port_manual", "INTEGER DEFAULT 0")):
            if col not in have_m:
                c.execute(f"ALTER TABLE mappings ADD COLUMN {col} {ddl}")

        # 运行期配置（爱快地址/令牌等），供 WebUI 设置页持久化
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        c.commit()
        c.close()


# ---------------- mappings ----------------

def list_mappings():
    with _lock:
        c = _conn()
        rows = c.execute("SELECT * FROM mappings ORDER BY id").fetchall()
        c.close()
        return [dict(r) for r in rows]


def get_mapping(id):
    with _lock:
        c = _conn()
        r = c.execute("SELECT * FROM mappings WHERE id=?", (id,)).fetchone()
        c.close()
        return dict(r) if r else None


def create_mapping(name, lan_ip, lan_port, proto, wan_port=None, rule_id=None,
                   wan_port_manual=0):
    t = now()
    with _lock:
        c = _conn()
        cur = c.execute(
            """INSERT INTO mappings(name, lan_ip, lan_port, proto, wan_port, rule_id,
                                    wan_port_manual, status, fail_count, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?, 'active', 0, ?, ?)""",
            (name, lan_ip, int(lan_port), proto, wan_port, rule_id,
             int(wan_port_manual), t, t),
        )
        mid = cur.lastrowid
        c.commit()
        c.close()
    return get_mapping(mid)


def update_mapping(id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [now(), id]
    with _lock:
        c = _conn()
        c.execute(f"UPDATE mappings SET {sets}, updated_at=? WHERE id=?", vals)
        c.commit()
        c.close()


def delete_mapping(id):
    with _lock:
        c = _conn()
        c.execute("DELETE FROM mappings WHERE id=?", (id,))
        c.commit()
        c.close()


# ---------------- stun_servers ----------------

def list_stun_servers():
    """只列出未被用户删除的服务器（按延迟升序，未测过的排最后）。"""
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM stun_servers WHERE IFNULL(removed,0)=0 "
            "ORDER BY latency IS NULL, latency, id"
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]


def get_stun_server(host, port):
    """按 host:port 取记录（含已被标记为删除的）。"""
    with _lock:
        c = _conn()
        r = c.execute(
            "SELECT * FROM stun_servers WHERE host=? AND port=?", (host, int(port))
        ).fetchone()
        c.close()
    return dict(r) if r else None


def upsert_stun_server(host, port, **fields):
    with _lock:
        c = _conn()
        c.execute(
            "INSERT OR IGNORE INTO stun_servers(host, port) VALUES(?,?)", (host, int(port))
        )
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(
                f"UPDATE stun_servers SET {sets} WHERE host=? AND port=?",
                list(fields.values()) + [host, int(port)],
            )
        c.commit()
        c.close()


def update_stun_server(id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        c = _conn()
        c.execute(f"UPDATE stun_servers SET {sets} WHERE id=?", list(fields.values()) + [id])
        c.commit()
        c.close()


def delete_stun_server(id):
    """软删除：标记为 removed，避免在线刷新池子时又把它加回来。"""
    with _lock:
        c = _conn()
        c.execute("UPDATE stun_servers SET removed=1 WHERE id=?", (id,))
        c.commit()
        c.close()


def get_best_stun_server(nat_only=False):
    """返回延迟最低、状态可用的服务器。

    nat_only=True 时只返回具备 RFC5780 NAT 检测能力的节点。
    list_stun_servers() 已按「延迟升序（NULL 最后）」排序。
    """
    for s in list_stun_servers():
        if s["status"] != "active":
            continue
        if nat_only and not s.get("nat_capable"):
            continue
        return s
    return None


# ---------------- settings ----------------

def get_setting(key, default=None):
    with _lock:
        c = _conn()
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        c.close()
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        c.commit()
        c.close()


def all_settings():
    with _lock:
        c = _conn()
        rows = c.execute("SELECT key, value FROM settings").fetchall()
        c.close()
    return {r["key"]: r["value"] for r in rows}
