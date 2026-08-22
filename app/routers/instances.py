"""实例相关接口:列表 / 电源操作 / 换IP(任务) / 流量统计。"""
import time

from fastapi import APIRouter, HTTPException

from .. import aws_cloud, jobs, oci_client
from ..pcreds import provider_of
from ..database import db
from ..routers.vps import list_vps_rows
from ..schemas import ChangeIpReq, OpReq, TrafficReq

router = APIRouter(prefix="/api", tags=["instances"])

# 实例列表缓存(15 秒),避免高频刷新时反复全量扫云
_INST_CACHE = {"ts": 0.0, "data": None}
_INST_CACHE_TTL = 30.0


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


@router.get("/instances")
def list_instances(account_id: int | None = None):
    if account_id is None:
        now = time.time()
        if _INST_CACHE["data"] is not None and now - _INST_CACHE["ts"] < _INST_CACHE_TTL:
            return _INST_CACHE["data"]
    with db() as c:
        if account_id:
            rows = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    items: list[dict] = []
    errors: list[str] = []
    for r in rows:
        acct = dict(r)
        try:
            p = provider_of(acct)
            if p == "aws":
                items.extend(aws_cloud.list_instances(acct))
                ls_rows, ls_errs = aws_cloud.list_lightsail(acct)
                items.extend(ls_rows)
                errors.extend(ls_errs)
            elif p == "oci":
                items.extend(oci_client.list_instances(acct))
            # dns 类账户(cloudflare/dnshe)不出现在实例视图
        except Exception as e:  # noqa: BLE001
            tag = f"{acct['name']}·{acct.get('region') or ''}"
            errors.append(f"[{tag}] {e}")
    try:
        items.extend(list_vps_rows())   # 手动添加的 VPS 一并展示
    except Exception as e:  # noqa: BLE001
        errors.append(f"[手动VPS] {e}")
    data = {"items": items, "errors": errors}
    if account_id is None:
        _INST_CACHE["ts"] = time.time()
        _INST_CACHE["data"] = data
    return data


@router.post("/instances/op")
def instance_op(body: OpReq):
    acct = _get_account(body.account_id)
    return oci_client.instance_op(acct, body.compartment_id, body.instance_id, body.op.upper())


@router.post("/instances/change-ip")
def change_ip(body: ChangeIpReq):
    acct = _get_account(body.account_id)
    job = jobs.start_job(
        "change_ip", oci_client.change_public_ip, acct, body.compartment_id, body.instance_id
    )
    return {"job_id": job["id"]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return job


@router.post("/traffic")
def traffic(body: TrafficReq, hours: int = 24):
    acct = _get_account(body.account_id)
    return oci_client.traffic_usage(acct, body.compartment_id, hours)
