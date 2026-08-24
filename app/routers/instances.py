"""实例相关接口:列表 / 电源操作 / 换IP(任务) / 流量统计。

v0.10.0 性能:
- 多账户并行扫描(线程池);
- 实例总览缓存升级为 stale-while-revalidate:过期请求立即返回旧数据并触发后台刷新,
  前端不再卡在"加载中";?refresh=1 可强制同步刷新;
- 电源操作后自动失效相关缓存。
"""
import threading
import time

from fastapi import APIRouter, HTTPException

from .. import aws_cloud, config, jobs, oci_client
from ..pcreds import provider_of
from ..database import db
from ..routers.vps import list_vps_rows
from ..schemas import ChangeIpReq, OpReq, TrafficReq

router = APIRouter(prefix="/api", tags=["instances"])

# 实例总览缓存:{key: {"ts": float, "data": dict}};key 为 "all" 或账户 id 字符串
_INST_CACHE: dict = {}
_INST_LOCK = threading.Lock()
_REFRESH_INFLIGHT: set[str] = set()


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


def _scan_accounts(rows) -> tuple[list[dict], list[str]]:
    """并行扫描多个云账户,返回 (items, errors)。"""
    def _one(r):
        acct = dict(r)
        try:
            p = provider_of(acct)
            if p == "aws":
                out = list(aws_cloud.list_instances(acct))
                ls_rows, ls_errs = aws_cloud.list_lightsail(acct)
                out.extend(ls_rows)
                return out, ls_errs
            if p == "oci":
                return oci_client.list_instances(acct), []
            if p == "ibm":
                from .. import ibm_cloud
                return ibm_cloud.list_instances(acct), []
            return [], []   # dns 类账户不出现在实例视图
        except Exception as e:  # noqa: BLE001
            tag = f"{acct['name']}·{acct.get('region') or ''}"
            return [], [f"[{tag}] {e}"]

    rows = list(rows)
    items: list[dict] = []
    errors: list[str] = []
    if len(rows) == 1:
        results = [_one(rows[0])]
    else:
        import concurrent.futures as cf
        workers = min(config.MAX_WORKERS, max(len(rows), 2))
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_one, rows))
    for its, errs in results:
        items.extend(its)
        errors.extend(errs)
    return items, errors


def _build_snapshot(account_id: int | None) -> dict:
    """全量构建一次实例快照(云扫描 + VPS 合并)。"""
    with db() as c:
        if account_id:
            rows = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    items, errors = _scan_accounts(rows)
    try:
        items.extend(list_vps_rows())   # 手动添加的 VPS 一并展示
    except Exception as e:  # noqa: BLE001
        errors.append(f"[手动VPS] {e}")
    return {"items": items, "errors": errors}


def _refresh_async(key: str, account_id: int | None):
    """后台刷新缓存(同 key 去重),完成后原子替换。"""

    def _work():
        try:
            data = _build_snapshot(account_id)
            with _INST_LOCK:
                _INST_CACHE[key] = {"ts": time.time(), "data": data}
        except Exception:  # noqa: BLE001
            pass  # 保留旧缓存,下次再试
        finally:
            with _INST_LOCK:
                _REFRESH_INFLIGHT.discard(key)

    with _INST_LOCK:
        if key in _REFRESH_INFLIGHT:
            return
        _REFRESH_INFLIGHT.add(key)
    threading.Thread(target=_work, daemon=True, name=f"inst-refresh-{key}").start()


def _invalidate_cache(*account_ids: int) -> None:
    with _INST_LOCK:
        _INST_CACHE.pop("all", None)
        for aid in account_ids:
            _INST_CACHE.pop(str(aid), None)


@router.get("/instances")
def list_instances(account_id: int | None = None, refresh: int = 0):
    key = "all" if account_id is None else str(account_id)

    if refresh:
        data = _build_snapshot(account_id)
        with _INST_LOCK:
            _INST_CACHE[key] = {"ts": time.time(), "data": data}
        return data

    with _INST_LOCK:
        cached = dict(_INST_CACHE.get(key) or {})

    if cached and time.time() - cached["ts"] < config.INSTANCE_CACHE_TTL:
        return cached["data"]

    # 过期/缺失:stale-while-revalidate —— 有旧数据先返回并后台刷新;无旧数据同步拉取
    _refresh_async(key, account_id)
    if cached:
        return cached["data"]

    data = _build_snapshot(account_id)
    with _INST_LOCK:
        _INST_CACHE[key] = {"ts": time.time(), "data": data}
    return data


@router.post("/instances/op")
def instance_op(body: OpReq):
    acct = _get_account(body.account_id)
    result = oci_client.instance_op(acct, body.compartment_id, body.instance_id, body.op.upper())
    _invalidate_cache(body.account_id)
    return result


@router.post("/instances/change-ip")
def change_ip(body: ChangeIpReq):
    acct = _get_account(body.account_id)
    _invalidate_cache(body.account_id)

    dns = body.dns_update
    if dns:
        # 换 IP 后自动更新 Cloudflare A 记录(R探长同款联动)
        from .. import cloudflare as cfmod

        def _with_dns(progress, acct=acct, cid=body.compartment_id, iid=body.instance_id,
                      dns=dns):
            res = oci_client.change_public_ip(progress, acct, cid, iid)
            new_ip = res.get("new_ip")
            if not new_ip:
                progress("⚠ 未获取到新 IP,跳过 DNS 更新")
                return res
            try:
                cf_acct = _get_account(dns.cf_account_id)
                r = cfmod.upsert_a_record(cf_acct, dns.zone, dns.record_name, new_ip,
                                          proxied=dns.proxied)
                progress(f"✅ DNS 已更新:{r['name']} → {new_ip}({r['action']})")
            except Exception as e:  # noqa: BLE001
                progress(f"⚠ DNS 更新失败:{e}")
            return res

        job = jobs.start_job("change_ip_dns", _with_dns)
    else:
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
