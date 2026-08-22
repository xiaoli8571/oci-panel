"""SQLite 存储:账户表。"""
import sqlite3
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
"""


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    config.ensure_dirs()
    with db() as c:
        c.executescript(_SCHEMA)
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
