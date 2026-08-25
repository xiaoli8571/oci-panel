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
CREATE TABLE IF NOT EXISTS rescue_sessions(
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id           INTEGER NOT NULL,
    compartment_id       TEXT NOT NULL,
    instance_id          TEXT NOT NULL,
    instance_name        TEXT DEFAULT '',
    ad                   TEXT DEFAULT '',
    boot_volume_id       TEXT NOT NULL,
    rescue_instance_id   TEXT NOT NULL,
    rescue_instance_name TEXT DEFAULT '',
    status               TEXT DEFAULT 'rescuing',
    created_at           TEXT DEFAULT (datetime('now','localtime')),
    updated_at           TEXT DEFAULT (datetime('now','localtime'))
);
"""


_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_g_events_created ON g_events(created_at);
CREATE INDEX IF NOT EXISTS idx_g_events_acct ON g_events(account_id, kind);
CREATE INDEX IF NOT EXISTS idx_vps_host ON vps_hosts(host, port);
"""

_local = threading.local()


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
    """线程本地连接:每线程独立连接 + WAL。

    v0.20.0 修复:原先全局共享单连接,后台任务线程与 Web 线程并发写时,
    一方的 commit 会提交/清掉另一方未完成的事务,触发
    「cannot commit - no transaction is active」。改为 thread-local 后各线程
    事务完全隔离;WAL 允许多读一写,busy_timeout 兜底写竞争。
    连接随线程结束由 GC 关闭(任务线程均为短生命周期守护线程)。
    """
    config.ensure_dirs()
    conn = getattr(_local, "conn", None)
    if conn is None:
        _local.conn = conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
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
