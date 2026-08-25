"""救援系统接口:元数据 / 发起 / 会话列表 / 完成还原 / 删除记录。

发起与还原都是长操作(等待关机、分离、挂载、开机),走 jobs 后台任务,
前端通过 /api/jobs/{id} 轮询进度日志。
"""
from fastapi import APIRouter, HTTPException, Query

from .. import jobs, rescue
from ..database import db
from ..schemas import RescueFinishReq, RescueStartReq

router = APIRouter(prefix="/api/rescue", tags=["rescue"])


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


@router.get("/meta")
def meta(account_id: int, compartment_id: str, instance_id: str):
    """故障实例信息 + 同可用域运行中的候选救援目标机。"""
    acct = _get_account(account_id)
    return rescue.rescue_meta(acct, compartment_id, instance_id)


@router.post("/start")
def start(body: RescueStartReq):
    if body.instance_id == body.rescue_instance_id:
        raise HTTPException(400, "救援目标不能是故障实例自身")
    acct = _get_account(body.account_id)
    p = {"compartment_id": body.compartment_id, "instance_id": body.instance_id,
         "rescue_instance_id": body.rescue_instance_id}
    job = jobs.start_job("rescue_start", rescue.start_rescue, acct, p)
    return {"job_id": job["id"]}


@router.get("/sessions")
def sessions(limit: int = Query(default=50, ge=1, le=200)):
    return {"items": rescue.list_sessions(limit)}


@router.post("/finish")
def finish(body: RescueFinishReq):
    sess = rescue.get_session(body.session_id)
    if not sess:
        raise HTTPException(404, "救援会话不存在或已被删除")
    acct = _get_account(sess["account_id"])
    job = jobs.start_job("rescue_finish", rescue.finish_rescue, acct, body.session_id)
    return {"job_id": job["id"]}


@router.post("/forget")
def forget(body: RescueFinishReq):
    """仅删除本地会话记录,不影响云端资源。"""
    rescue.forget_session(body.session_id)
    return {"ok": True}
