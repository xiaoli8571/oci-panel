"""资源操作接口:元数据 / 创建实例 / 网络(IPv6·保留IP·端口) / 卷 / 配额订阅。"""
import re

from fastapi import APIRouter, HTTPException, Query

from .. import jobs, oci_client
from ..database import db
from ..schemas import (CreateInstanceReq, NetRef, PortsReq, RenameReq,
                       ReservedIpOp, ResizeReq, TerminateReq, VolumeUpdateReq)

router = APIRouter(prefix="/api", tags=["resources"])

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,59}$")


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


# ---------------------------------------------------------------- 表单元数据

@router.get("/meta/compartments")
def meta_compartments(account_id: int):
    return {"items": oci_client.list_compartments(_get_account(account_id))}


@router.get("/meta/ads")
def meta_ads(account_id: int):
    return {"items": oci_client.list_ads(_get_account(account_id))}


@router.get("/meta/images")
def meta_images(account_id: int, compartment_id: str, os: str = "Canonical Ubuntu",
                shape: str = "VM.Standard.E2.1.Micro"):
    return {"items": oci_client.list_platform_images(
        _get_account(account_id), compartment_id, shape, os)}


@router.get("/meta/subnets")
def meta_subnets(account_id: int, compartment_id: str):
    return {"items": oci_client.list_public_subnets(_get_account(account_id), compartment_id)}


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
