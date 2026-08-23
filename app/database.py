"""SQLite 存储:账户表。WAL 模式 + 连接复用,支持 Web 与守护线程并发读写。"""
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    user_ocid        TEXT NOT NULL,
    tenancy_ocid     TEXT NOT NULL,
    region           TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    private_key_enc  TEXT NOT NULL,
    created_at       TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS guardian(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER UNIQUE NOT NULL,
    enabled          INTEGER DEFAULT 0,
    keepalive        INTEGER DEFAULT 0,
    traffic_limit_gb REAL DEFAULT 0,
    traffic_action   TEXT DEFAULT 'notify',
    updated_at       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS g_events(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER,
    kind             TEXT,
    message          TEXT,
    created_at       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS kv(
    k                TEXT PRIMARY KEY,
    v                TEXT
);
CREATE TABLE IF NOT EXISTS vps_hosts(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    host             TEXT NOT NULL,
    port             INTEGER DEFAULT 22,
    username         TEXT NOT NULL,
    auth_type        TEXT DEFAULT 'password',
    secret_enc       TEXT DEFAULT '',
    region           TEXT DEFAULT '',
    note             TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS ssh_creds(
    cred_key         TEXT PRIMARY KEY,
    username         TEXT NOT NULL,
    host             TEXT NOT NULL,
    port             INTEGER DEFAULT 22,
    auth_type        TEXT DEFAULT 'password',
    secret_enc       TEXT NOT NULL
);
"""


_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_g_events_created ON g_events(created_at);
CREATE INDEX IF NOT EXISTS idx_g_events_acct ON g_events(account_id, kind);
CREATE INDEX IF NOT EXISTS idx_vps_host ON vps_hosts(host, port);
"""

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL 模式:读写不互斥,面板请求(读)与守护线程(写)并发不再互相阻塞;
    # busy_timeout 兜底偶发锁竞争。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def db():
    """复用单个长连接(加锁串行化写事务),避免每请求重建连接的开销。"""
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                config.ensure_dirs()
                _conn = _connect()
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


def init() -> None:
    config.ensure_dirs()
    with db() as c:
        c.executescript(_SCHEMA)
        c.executescript(_INDEXES)
        # 多提供商迁移:provider + 加密的额外凭证(extra_enc)
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)").fetchall()}
        if "provider" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN provider TEXT DEFAULT 'oci'")
        if "extra_enc" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN extra_enc TEXT DEFAULT ''")


def get_kv(key: str) -> str | None:
    with db() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row["v"] if row else None


def set_kv(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
