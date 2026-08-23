"""定时任务接口:CRUD + 手动试运行。"""
from fastapi import APIRouter, HTTPException

from .. import sched
from ..database import db

router = APIRouter(prefix="/api/sched", tags=["sched"])


@router.get("/jobs")
def jobs():
    return {"items": sched.get_jobs()}


@router.post("/jobs")
def create(body: dict):
    target = body.get("target") or {}
    try:
        jid = sched.add_job(
            str(body.get("name") or ""), dict(target), str(body.get("action") or "stop"),
            str(body.get("time_hhmm") or "08:00"), str(body.get("weekdays") or "1,2,3,4,5,6,7"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not target.get("account_id"):
        # 回滚无效目标
        sched.delete_job(jid)
        raise HTTPException(400, "缺少实例信息")
    return {"id": jid}


@router.post("/jobs/{job_id}/toggle")
def toggle(job_id: int, body: dict):
    with db() as c:
        row = c.execute("SELECT id FROM sched_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    sched.set_enabled(job_id, bool(body.get("enabled")))
    return {"ok": True}


@router.delete("/jobs/{job_id}")
def remove(job_id: int):
    sched.delete_job(job_id)
    return {"ok": True}


@router.post("/test")
def test_run(body: dict):
    """立即试执行一次(不写 last_run)。"""
    from .. import power
    target = body.get("target") or {}
    try:
        label = power.power_op(dict(target), str(body.get("action") or "stop"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"执行失败:{e}")
    return {"ok": True, "message": label}
