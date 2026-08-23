"""资源操作接口:元数据 / 创建实例 / 网络(IPv6·保留IP·端口) / 卷 / 配额订阅。

v0.10.0:表单元数据(compartments/ads/images/subnets)带 TTL 缓存,表单打开更快。
"""
import re

from fastapi import APIRouter, HTTPException, Query

from .. import config, jobs, oci_client
from ..oci_client import _ad_short
from ..database import db
from ..schemas import (CreateInstanceReq, NetRef, PortsReq, RenameReq,
                       ReservedIpOp, ResizeReq, TerminateReq, VolumeUpdateReq)
from ..ttlcache import TTLCache

router = APIRouter(prefix="/api", tags=["resources"])

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,59}$")

_meta_cache: TTLCache = TTLCache(ttl=float(config.META_CACHE_TTL), max_items=256)


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


def _meta(account_id: int, kind: str, compute):
    """元数据缓存读取:未命中则计算并写入。"""
    key = f"{account_id}:{kind}"
    val = _meta_cache.get(key)
    if val is None:
        val = compute()
        _meta_cache.set(key, val)
    return val


# ---------------------------------------------------------------- 表单元数据

@router.get("/meta/compartments")
def meta_compartments(account_id: int):
    acct = _get_account(account_id)
    return {"items": _meta(account_id, "compartments",
                           lambda: oci_client.list_compartments(acct))}


@router.get("/meta/ads")
def meta_ads(account_id: int):
    acct = _get_account(account_id)
    return {"items": _meta(account_id, "ads", lambda: oci_client.list_ads(acct))}


@router.get("/meta/images")
def meta_images(account_id: int, compartment_id: str, os: str = "Canonical Ubuntu",
                shape: str = "VM.Standard.E2.1.Micro"):
    acct = _get_account(account_id)
    return {"items": _meta(
        account_id, f"images:{compartment_id}:{os}:{shape}",
        lambda: oci_client.list_platform_images(acct, compartment_id, shape, os))}


@router.get("/meta/subnets")
def meta_subnets(account_id: int, compartment_id: str):
    acct = _get_account(account_id)
    return {"items": _meta(
        account_id, f"subnets:{compartment_id}",
        lambda: oci_client.list_public_subnets(acct, compartment_id))}


# ---------------------------------------------------------------- 实例属性操作

@router.post("/instances/create")
def create_instance(body: CreateInstanceReq):
    if not _NAME_RE.match(body.name):
        raise HTTPException(400, "实例名称需以字母开头,仅含字母/数字/._-")
    if not body.ssh_key.strip():
        raise HTTPException(400, "请填写 SSH 公钥")
    if not 46 <= body.boot_gbs <= 2048:
        raise HTTPException(400, "启动盘大小需在 47~2048 GB 之间")
    if body.shape_kind == "arm":
        shape, ocpus, mem = "VM.Standard.A1.Flex", (body.ocpus or 2), (body.mem_gbs or 12)
        if not (1 <= ocpus <= 64 and 1 <= mem <= 1024):
            raise HTTPException(400, "A1.Flex 规格 OCPU 1-64、内存 1-1024 GB(免费额度为 4 核 24G)")
    elif body.shape_kind == "amd":
        shape, ocpus, mem = "VM.Standard.E2.1.Micro", None, None
    else:
        raise HTTPException(400, "shape_kind 仅支持 amd / arm")
    acct = _get_account(body.account_id)
    d = {
        "compartment_id": body.compartment_id, "name": body.name, "ad": body.ad,
        "subnet_id": body.subnet_id, "image_id": body.image_id, "shape": shape,
        "ocpus": ocpus, "mem_gbs": mem, "boot_gbs": body.boot_gbs,
        "ssh_key": body.ssh_key.strip(),
        "retry_attempts": body.retry_attempts, "retry_delay": body.retry_delay,
    }
    job = jobs.start_job("create_instance", oci_client.create_instance, acct, d)
    return {"job_id": job["id"]}


@router.post("/instances/enable-monitoring")
def enable_monitoring(body: NetRef):
    return oci_client.enable_monitoring_plugin(
        _get_account(body.account_id), body.instance_id)


@router.post("/instances/rename")
def rename(body: RenameReq):
    acct = _get_account(body.account_id)
    return oci_client.rename_instance(acct, body.instance_id, body.display_name.strip())


@router.post("/instances/resize")
def resize(body: ResizeReq):
    acct = _get_account(body.account_id)
    return oci_client.resize_instance(acct, body.instance_id, body.ocpus, body.mem_gbs)


@router.post("/instances/terminate")
def terminate(body: TerminateReq):
    acct = _get_account(body.account_id)
    return oci_client.terminate_instance(acct, body.instance_id, body.preserve_boot_volume)


@router.post("/instances/boot-launch")
def boot_launch(body: dict):
    """从现有启动盘创建实例(原实例需已终止且保留了启动盘)。"""
    acct = _get_account(int(body.get("account_id") or 0))
    d = {
        "compartment_id": str(body.get("compartment_id") or ""),
        "name": str(body.get("name") or "").strip(),
        "ad": str(body.get("ad") or ""),
        "subnet_id": str(body.get("subnet_id") or ""),
        "boot_volume_id": str(body.get("boot_volume_id") or ""),
        "ssh_key": str(body.get("ssh_key") or ""),
        "shape": str(body.get("shape") or "VM.Standard.A1.Flex"),
        "ocpus": body.get("ocpus"), "mem_gbs": body.get("mem_gbs"),
    }
    if not _NAME_RE.match(d["name"]):
        raise HTTPException(400, "实例名称需以字母开头,仅含字母/数字/._-")
    if not d["compartment_id"] or not d["ad"] or not d["subnet_id"] or not d["boot_volume_id"]:
        raise HTTPException(400, "缺少必要参数(区间/AD/子网/启动盘)")
    job = jobs.start_job("boot_launch", oci_client.launch_from_boot_volume, acct, d)
    return {"job_id": job["id"]}


@router.get("/net/boot-volumes")
def list_boot_volumes(account_id: int, compartment_id: str):
    """列出区间内全部可用启动盘(供从启动盘开机选择)。"""
    from ..oci_client import list_available_boot_volumes
    return {"items": list_available_boot_volumes(_get_account(account_id), compartment_id)}


# ---------------------------------------------------------------- 网络

@router.get("/net/info")
def net_info(account_id: int, compartment_id: str, instance_id: str):
    return oci_client.net_info(_get_account(account_id), compartment_id, instance_id)


@router.post("/net/add-ipv6")
def add_ipv6(body: NetRef):
    return oci_client.add_ipv6(_get_account(body.account_id), body.compartment_id, body.instance_id)


@router.get("/net/reserved-ips")
def reserved_ips(account_id: int, compartment_id: str):
    return {"items": oci_client.list_reserved_ips(
        _get_account(account_id), compartment_id)}


@router.post("/net/reserved-ip")
def reserved_ip_op(body: ReservedIpOp):
    acct = _get_account(body.account_id)
    if body.op == "create":
        return oci_client.create_reserved_ip(acct, body.compartment_id)
    if body.op == "delete":
        if not body.public_ip_id:
            raise HTTPException(400, "缺少 public_ip_id")
        return oci_client.delete_reserved_ip(acct, body.public_ip_id)
    if body.op in ("bind", "unbind"):
        if not body.public_ip_id or not body.vnic_id:
            raise HTTPException(400, "缺少 public_ip_id 或 vnic_id")
        return oci_client.bind_reserved_ip(acct, body.public_ip_id, body.vnic_id,
                                           bind=(body.op == "bind"))
    raise HTTPException(400, "op 仅支持 create/delete/bind/unbind")


@router.post("/net/open-ports")
def open_ports(body: PortsReq):
    if not all(1 <= p <= 65535 for p in body.ports):
        raise HTTPException(400, "端口范围 1-65535")
    return oci_client.open_ports(
        _get_account(body.account_id), body.compartment_id, body.instance_id, body.ports)


# ---------------------------------------------------------------- 卷

@router.get("/vol/boot")
def boot_volume(account_id: int, compartment_id: str, instance_id: str):
    return oci_client.get_boot_volume(_get_account(account_id), compartment_id, instance_id)


@router.post("/vol/boot-update")
def boot_volume_update(body: VolumeUpdateReq):
    acct = _get_account(body.account_id)
    return oci_client.update_boot_volume(
        acct, body.boot_volume_id, body.size_in_gbs, body.vpus_per_gb)


# ---------------------------------------------------------------- 配额订阅

@router.get("/quota")
def quota(account_id: int):
    return oci_client.account_quota(_get_account(account_id))


# ---------------------------------------------------------------- A1 配额体检

_A1_CACHE: TTLCache = TTLCache(ttl=60.0, max_items=8)


@router.get("/a1-check")
def a1_check():
    """全部 OCI 账户的 A1 体检:各 AD 余量 + 在跑的 A1.Flex 实例(60s 缓存)。

    供前端一键降配(降配需实例先关机,由前端提示)。"""
    import concurrent.futures as cf

    from ..pcreds import provider_of
    from ..database import db as _db

    cached = _A1_CACHE.get("all")
    if cached is not None:
        return cached

    with _db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM accounts").fetchall()]
    ocis = [r for r in rows if provider_of(r) == "oci"]

    def one(acct: dict) -> dict:
        base = {"account_id": acct["id"], "name": acct["name"], "region": acct["region"]}
        try:
            q = oci_client.account_quota(acct)
            core = next((l for l in q["limits"] if l["name"] == "standard-a1-core-count"), None)
            mem = next((l for l in q["limits"] if l["name"] == "standard-a1-memory-count"), None)
            e2m = next((l for l in q["limits"] if l["name"] == "standard-e2-micro-count"), None)
            ads: dict = {}
            for it in (core or {}).get("items", []):
                ads.setdefault(it["ad"], {})["core_avail"] = it["available"]
                ads.setdefault(it["ad"], {})["core_used"] = it["used"]
            for it in (mem or {}).get("items", []):
                ads.setdefault(it["ad"], {})["mem_avail"] = it["available"]
                ads.setdefault(it["ad"], {})["mem_used"] = it["used"]
            e2m_map = {it["ad"]: it for it in (e2m or {}).get("items", [])}
            # 换算每个 AD 还能开几台(向下取整;免费额度按 4核24G 判断)
            ad_out = []
            for k, v in sorted(ads.items()):
                ca, ma = int(v.get("core_avail") or 0), int(v.get("mem_avail") or 0)
                fit_2c12 = min(ca // 2, ma // 12)
                fit_1c6 = min(ca, ma // 6)
                em = e2m_map.get(k) or {}
                ad_out.append({**v, "ad": _ad_short(k),
                               "can_a1_2c12g": max(fit_2c12, 0),
                               "can_a1_1c6g": max(fit_1c6, 0),
                               "can_e2_micro": em.get("available")})
            try:
                ins = oci_client.list_instances(acct)
            except Exception:  # noqa: BLE001
                ins = []
            a1 = [{"id": i["id"], "compartment_id": i["compartment_id"],
                   "name": i["name"], "state": i["state"],
                   "ocpus": i.get("ocpus"), "mem_gbs": i.get("mem_gbs"),
                   "ad": i.get("ad"), "public_ip": i.get("public_ip")}
                  for i in ins if (i.get("shape") or "").startswith("VM.Standard.A1.Flex")]
            return {**base, "ok": True,
                    "payment_model": q.get("payment_model"),
                    "ads": ad_out,
                    "instances": a1}
        except Exception as e:  # noqa: BLE001
            return {**base, "ok": False, "error": str(e)[:200]}

    if len(ocis) == 1:
        results = [one(ocis[0])]
    elif ocis:
        with cf.ThreadPoolExecutor(max_workers=min(config.MAX_WORKERS, len(ocis))) as ex:
            results = list(ex.map(one, ocis))
    else:
        results = []
    data = {"items": results}
    _A1_CACHE.set("all", data)
    return data
