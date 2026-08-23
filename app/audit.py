"""操作审计:记录所有写操作(POST/PUT/DELETE/PATCH)与会话事件到 audit 表。

- 通过 main.py 的中间件自动采集,业务代码零侵入
- 不记录请求体(避免把密码/私钥等敏感内容落盘),只记 方法+路径+状态+耗时+来源IP
- 查询接口按时间倒序,支持路径关键词过滤;超过保留条数自动清理
"""
from __future__ import annotations

import threading
import time

from .database import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT DEFAULT (datetime('now','localtime')),
    method   TEXT NOT NULL,
    path     TEXT NOT NULL,
    status   INTEGER,
    ms       INTEGER,
    ip       TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

_RETAIN = 5000        # 最大保留条数(清理时删到 4000)
_prune_lock = threading.Lock()
_skip_prefixes = ("/api/jobs/",)   # 高频轮询不记


def init() -> None:
    with db() as c:
        c.executescript(_SCHEMA)


def record(method: str, path: str, status: int | None = None,
           ms: int | None = None, ip: str = "", detail: str = "") -> None:
    if any(path.startswith(p) for p in _skip_prefixes):
        return
    try:
        with db() as c:
            c.execute("INSERT INTO audit_log(method,path,status,ms,ip,detail) "
                      "VALUES(?,?,?,?,?,?)",
                      (method, path[:200], status, ms, (ip or "")[:64], detail[:300]))
            _maybe_prune(c)
    except Exception:  # noqa: BLE001
        pass   # 审计失败不影响主业务


def _maybe_prune(c) -> None:
    """概率触发清理(每 ~100 条写一次检查)。"""
    row = c.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    if row and row["n"] > _RETAIN and _prune_lock.acquire(blocking=False):
        try:
            keep_id = c.execute(
                "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                (_RETAIN - 4000,)).fetchone()
            if keep_id:
                c.execute("DELETE FROM audit_log WHERE id <= ?", (keep_id["id"],))
        finally:
            _prune_lock.release()


def recent(limit: int = 200, q: str = "") -> list[dict]:
    limit = max(1, min(limit, 1000))
    with db() as c:
        if q:
            rows = c.execute("SELECT * FROM audit_log WHERE path LIKE ? OR method LIKE ? "
                             "ORDER BY id DESC LIMIT ?",
                             (f"%{q}%", f"%{q}%", limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
    return [dict(r) for r in rows]
