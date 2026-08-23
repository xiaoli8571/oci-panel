"""定时任务:按每日时间(可选星期几)自动对实例执行 开机/关机/重启。

- 独立守护线程每 30s 检查一次到期任务
- last_run 记录「日期 HH:MM」防止同一时刻重复执行;跨重启不补跑错过的任务
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from . import power
from .database import db

log = logging.getLogger("sched")

_started = threading.Event()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sched_jobs(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    account_id     INTEGER NOT NULL,
    target_id      TEXT NOT NULL,
    target_name    TEXT DEFAULT '',
    provider       TEXT DEFAULT 'oci',
    compartment_id TEXT DEFAULT '',
    region         TEXT DEFAULT '',
    service        TEXT DEFAULT '',
    action         TEXT DEFAULT 'stop',
    time_hhmm      TEXT DEFAULT '08:00',
    weekdays       TEXT DEFAULT '1,2,3,4,5,6,7',
    enabled        INTEGER DEFAULT 1,
    last_run       TEXT DEFAULT '',
    last_result    TEXT DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now','localtime'))
);
"""


def init() -> None:
    with db() as c:
        c.executescript(_SCHEMA)


def get_jobs(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM sched_jobs" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    with db() as c:
        rows = c.execute(sql).fetchall()
    return [dict(r) for r in rows]


def add_job(name: str, row: dict, action: str, hhmm: str, weekdays: str) -> int:
    if not name.strip():
        raise ValueError("任务名称不能为空")
    if action not in power.MAPS:
        raise ValueError(f"不支持的动作:{action}")
    if not _valid_hhmm(hhmm):
        raise ValueError("时间格式应为 HH:MM")
    wd = _norm_weekdays(weekdays)
    with db() as c:
        cur = c.execute(
            "INSERT INTO sched_jobs(name,account_id,target_id,target_name,provider,"
            "compartment_id,region,service,action,time_hhmm,weekdays,enabled)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
            (name.strip(), row.get("account_id") or 0, row["id"],
             (row.get("name") or "").strip(), (row.get("provider") or "oci").lower(),
             row.get("compartment_id") or "", row.get("region") or "",
             row.get("service") or "", action, hhmm, wd))
        return cur.lastrowid


def set_enabled(job_id: int, enabled: bool) -> None:
    with db() as c:
        c.execute("UPDATE sched_jobs SET enabled=? WHERE id=?", (1 if enabled else 0, job_id))


def delete_job(job_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM sched_jobs WHERE id=?", (job_id,))


def _valid_hhmm(s: str) -> bool:
    try:
        h, m = s.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(h) == 2 and len(m) == 2
    except Exception:  # noqa: BLE001
        return False


def _norm_weekdays(s: str) -> str:
    """归一化为逗号分隔的 1-7(周一..周日);空则每天。"""
    out = []
    for tok in (s or "").replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= 7 and tok not in out:
            out.append(tok)
    return ",".join(sorted(out, key=int)) or "1,2,3,4,5,6,7"


def run_due() -> list[dict]:
    """执行所有到期任务;返回本轮触发结果。"""
    now = dt.datetime.now()
    hhmm = now.strftime("%H:%M")
    wd = str(now.isoweekday())
    key = now.strftime("%Y-%m-%d ") + hhmm
    fired = []
    for job in get_jobs(enabled_only=True):
        if job["time_hhmm"] != hhmm or wd not in job["weekdays"].split(","):
            continue
        if job["last_run"] == key:
            continue
        # 先标记再执行,避免执行慢导致重复触发
        with db() as c:
            c.execute("UPDATE sched_jobs SET last_run=?, last_result=? WHERE id=?",
                      (key, "执行中…", job["id"]))
        row = {
            "account_id": job["account_id"], "id": job["target_id"],
            "provider": job["provider"], "compartment_id": job["compartment_id"],
            "region": job["region"], "service": job["service"], "name": job["target_name"],
        }
        try:
            label = power.power_op(row, job["action"])
            result = f"✅ {label}指令已下发"
            fired.append({"job": job["name"], "ok": True})
        except Exception as e:  # noqa: BLE001
            result = f"❌ {e}"
            fired.append({"job": job["name"], "ok": False, "error": str(e)[:120]})
        with db() as c:
            c.execute("UPDATE sched_jobs SET last_result=?, last_run=? WHERE id=?",
                      (result[:200], key, job["id"]))
        log.info("定时任务「%s」:%s", job["name"], result)
    return fired


def status() -> dict:
    jobs = get_jobs()
    return {"total": len(jobs), "enabled": sum(1 for j in jobs if j["enabled"])}


def _loop():
    while True:
        try:
            run_due()
        except Exception:  # noqa: BLE001
            log.exception("定时巡检异常")
        time.sleep(30)


def start():
    if _started.is_set():
        return
    _started.set()
    init()
    threading.Thread(target=_loop, daemon=True, name="sched").start()
    log.info("定时任务线程已启动(30s 粒度检查)")
