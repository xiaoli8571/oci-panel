"""多云路由:AWS EC2 / Cloudflare(DNS+Workers) / dns.he.net。"""
from fastapi import APIRouter, HTTPException, Query

from .. import aws_cloud, cloudflare as cfmod, dnshe_api as dnshe
from ..database import db
from ..pcreds import provider_of
from ..schemas import (CfRecordIn, CfRecordUpd, CfRouteIn,
                       DnsheRecordIn, DnsheRecordUpd, DnsheSubdomainIn,
                       WorkerDeploy)

router = APIRouter(prefix="/api", tags=["multi"])


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


def _require(acct: dict, provider: str) -> dict:
    if provider_of(acct) != provider:
        raise HTTPException(400, f"该账户不是 {provider} 类型")
    return acct


# ---------------------------------------------------------------- AWS

@router.get("/aws/instances")
def aws_instances(account_id: int):
    acct = _require(_get_account(account_id), "aws")
    return {"items": aws_cloud.list_instances(acct)}


@router.post("/aws/op")
def aws_op(body: dict):
    acct = _require(_get_account(int(body["account_id"])), "aws")
    service = body.get("service", "ec2")
    op = body["op"].upper()
    if service == "lightsail":
        return aws_cloud.lightsail_op(acct, body.get("region", ""), body["instance_id"], op)
    return aws_cloud.instance_op(acct, body["instance_id"], op)


@router.get("/aws/ec2-meta")
def aws_ec2_meta(account_id: int, region: str):
    acct = _require(_get_account(account_id), "aws")
    return aws_cloud.ec2_meta(acct, region)


@router.post("/aws/ec2-create")
def aws_ec2_create(body: dict):
    acct = _require(_get_account(int(body["account_id"])), "aws")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "请填写实例名称")
    if not body.get("image_id") or not body.get("instance_type"):
        raise HTTPException(400, "请选择 AMI 与实例类型")
    return aws_cloud.ec2_create(
        acct, body["region"], name, body["image_id"], body["instance_type"],
        body.get("subnet_id") or "", body.get("security_group_id") or "",
        body.get("key_name") or "")


@router.post("/aws/change-ip")
def aws_change_ip(body: dict):
    from .. import jobs
    acct = _require(_get_account(int(body["account_id"])), "aws")
    service = body.get("service", "ec2")

    def _run(progress, acct=acct, service=service, body=body):
        if service == "lightsail":
            return aws_cloud.lightsail_change_ip(
                progress, acct, body.get("region", ""), body["instance_id"])
        return aws_cloud.change_public_ip(progress, acct, "", body["instance_id"])

    job = jobs.start_job(f"aws_change_ip_{service}", _run)
    return {"job_id": job["id"]}


@router.get("/aws/lightsail-meta")
def aws_lightsail_meta(account_id: int, region: str = ""):
    acct = _require(_get_account(account_id), "aws")
    return aws_cloud.lightsail_meta(acct, region or None)


@router.post("/aws/lightsail-create")
def aws_lightsail_create(body: dict):
    acct = _require(_get_account(int(body["account_id"])), "aws")
    return aws_cloud.lightsail_create(
        acct, body["region"], body["name"].strip(),
        body["blueprint_id"], body["bundle_id"], body.get("az", ""))


# ---------------------------------------------------------------- IBM Cloud VPC

@router.get("/ibm/instances")
def ibm_instances(account_id: int):
    from .. import ibm_cloud
    acct = _require(_get_account(account_id), "ibm")
    return {"items": ibm_cloud.list_instances(acct)}


@router.post("/ibm/op")
def ibm_op(body: dict):
    from .. import ibm_cloud
    acct = _require(_get_account(int(body["account_id"])), "ibm")
    return ibm_cloud.instance_op(acct, body["instance_id"], body["op"].lower())


@router.post("/ibm/change-ip")
def ibm_change_ip(body: dict):
    from .. import ibm_cloud, jobs
    acct = _require(_get_account(int(body["account_id"])), "ibm")
    job = jobs.start_job(
        "ibm_change_ip", ibm_cloud.change_public_ip, acct, "",
        body["instance_id"])
    return {"job_id": job["id"]}


@router.get("/ibm/meta")
def ibm_meta(account_id: int):
    from .. import ibm_cloud
    acct = _require(_get_account(account_id), "ibm")
    return ibm_cloud.ibm_meta(acct)


@router.post("/ibm/create")
def ibm_create(body: dict):
    from .. import ibm_cloud
    acct = _require(_get_account(int(body["account_id"])), "ibm")
    return ibm_cloud.create_instance(acct, body)


@router.post("/ibm/terminate")
def ibm_terminate(body: dict):
    from .. import ibm_cloud
    acct = _require(_get_account(int(body["account_id"])), "ibm")
    return ibm_cloud.terminate_instance(acct, body["instance_id"])


@router.get("/ibm/floating-ips")
def ibm_floating_ips(account_id: int):
    from .. import ibm_cloud
    acct = _require(_get_account(account_id), "ibm")
    return {"items": ibm_cloud.list_floating_ips(acct)}


@router.get("/ibm/net-debug")
def ibm_net_debug(account_id: int, instance_id: str):
    """公网 IP 排查:返回实例详情主网卡 / 单网卡详情 / 区域浮动 IP 原始数据。"""
    from .. import ibm_cloud
    acct = _require(_get_account(account_id), "ibm")
    out: dict = {"instance_id": instance_id}
    try:
        ins = ibm_cloud._req(acct, "GET", f"/instances/{instance_id}")
        pni = ins.get("primary_network_interface") or {}
        out["instance_status"] = ins.get("status")
        out["pni"] = {k: pni.get(k) for k in ("id", "name") if isinstance(pni, dict)}
    except Exception as e:  # noqa: BLE001
        out["instance_error"] = str(e)
    try:
        out["floating_ips"] = ibm_cloud.list_floating_ips(acct)
    except Exception as e:  # noqa: BLE001
        out["floating_ips_error"] = str(e)
    nic_id = (out.get("pni") or {}).get("id") or ""
    if nic_id:
        try:
            d = ibm_cloud._req(
                acct, "GET", f"/instances/{instance_id}/network_interfaces/{nic_id}")
            out["nic_detail"] = {
                "id": d.get("id"),
                "primary_ip": d.get("primary_ip"),
                "floating_ip": d.get("floating_ip"),
            }
        except Exception as e:  # noqa: BLE001
            out["nic_detail_error"] = str(e)
    return out


# ---------------------------------------------------------------- Google Cloud

@router.get("/gcp/meta")
def gcp_meta(account_id: int):
    from .. import gcp_cloud
    acct = _require(_get_account(account_id), "gcp")
    return gcp_cloud.gcp_meta(acct)


@router.get("/gcp/mach-types")
def gcp_mach_types(account_id: int, zone: str):
    from .. import gcp_cloud
    acct = _require(_get_account(account_id), "gcp")
    return {"items": gcp_cloud.mach_types(acct, zone)}


@router.get("/gcp/subnets")
def gcp_subnets(account_id: int, region: str):
    from .. import gcp_cloud
    acct = _require(_get_account(account_id), "gcp")
    return {"items": gcp_cloud.subnets(acct, region)}


@router.post("/gcp/op")
def gcp_op(body: dict):
    from .. import gcp_cloud
    acct = _require(_get_account(int(body["account_id"])), "gcp")
    return gcp_cloud.instance_op(acct, body["name"], body["zone"], body["op"].lower())


@router.post("/gcp/change-ip")
def gcp_change_ip(body: dict):
    from .. import gcp_cloud, jobs
    acct = _require(_get_account(int(body["account_id"])), "gcp")
    name, zone = body["name"], body["zone"]
    job = jobs.start_job("gcp_change_ip", gcp_cloud.change_public_ip,
                         acct, "", f"{name}@{zone}")
    return {"job_id": job["id"]}


@router.post("/gcp/create")
def gcp_create(body: dict):
    from .. import gcp_cloud, jobs
    acct = _require(_get_account(int(body["account_id"])), "gcp")
    d = {k: body.get(k) for k in ("name", "zone", "machine_type", "image",
                                  "subnet", "ssh_key", "boot_gbs")}
    d["external_ip"] = bool(body.get("external_ip", True))
    if not str(d.get("ssh_key") or "").strip():
        raise HTTPException(400, "请填写 SSH 公钥(否则无法登录实例)")
    job = jobs.start_job("gcp_create", gcp_cloud.create_instance, acct, d)
    return {"job_id": job["id"]}


@router.post("/gcp/terminate")
def gcp_terminate(body: dict):
    from .. import gcp_cloud
    acct = _require(_get_account(int(body["account_id"])), "gcp")
    return gcp_cloud.terminate_instance(acct, body["name"], body["zone"])


# ---------------------------------------------------------------- Cloudflare DNS

@router.get("/cf/zones")
def cf_zones(account_id: int):
    acct = _require(_get_account(account_id), "cloudflare")
    return {"items": cfmod.zones(acct)}


@router.get("/cf/records")
def cf_records(account_id: int, zone_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    return {"items": cfmod.records(acct, zone_id)}


@router.post("/cf/records")
def cf_create_record(body: CfRecordIn):
    acct = _require(_get_account(body.account_id), "cloudflare")
    r = cfmod.create_record(acct, body.zone_id, body.model_dump())
    return {"record_id": r.get("id", "")}


@router.put("/cf/records")
def cf_update_record(body: CfRecordUpd):
    acct = _require(_get_account(body.account_id), "cloudflare")
    cfmod.update_record(acct, body.zone_id, body.record_id, body.model_dump())
    return {"ok": True}


@router.delete("/cf/records")
def cf_delete_record(account_id: int, zone_id: str, record_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    cfmod.delete_record(acct, zone_id, record_id)
    return {"ok": True}


@router.get("/cf/verify")
def cf_verify(account_id: int):
    acct = _require(_get_account(account_id), "cloudflare")
    return cfmod.verify_token(acct)


# ---------------------------------------------------------------- Cloudflare Workers

@router.get("/cf/accounts")
def cf_accounts(account_id: int):
    acct = _require(_get_account(account_id), "cloudflare")
    return {"items": cfmod.accounts_list(acct)}


@router.get("/cf/workers")
def cf_workers(account_id: int):
    acct = _require(_get_account(account_id), "cloudflare")
    cfas = cfmod.accounts_list(acct)
    if not cfas:
        raise HTTPException(400, "Token 下没有可见的 Cloudflare 账户(Account.Read 权限?)")
    items = []
    sub = ""
    # 各账户 Workers 列表并行拉取(原来串行最多 5 次 CF API)
    import concurrent.futures as cfpool

    def _workers(a):
        try:
            return a["name"], cfmod.workers_list(acct, a["cf_account_id"])
        except Exception:  # noqa: BLE001
            return a["name"], []

    with cfpool.ThreadPoolExecutor(max_workers=5) as ex:
        for name, ws in ex.map(_workers, cfas[:5]):
            for w in ws:
                w["cf_account_name"] = name
                items.append(w)
            if not sub:
                try:
                    sub = cfmod.subdomain(acct, cfas[0]["cf_account_id"])
                except Exception:  # noqa: BLE001
                    sub = ""
    return {"items": items, "subdomain": sub}


@router.get("/cf/worker-code")
def cf_worker_code(account_id: int, cf_account_id: str, script_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    return {"code": cfmod.worker_code(acct, cf_account_id, script_id)}


@router.post("/cf/worker-deploy")
def cf_worker_deploy(body: WorkerDeploy):
    acct = _require(_get_account(body.account_id), "cloudflare")
    cfas = [body.cf_account_id] if body.cf_account_id else \
        [a["cf_account_id"] for a in cfmod.accounts_list(acct)]
    if not cfas:
        raise HTTPException(400, "未找到 Cloudflare 账户")
    r = cfmod.worker_deploy(acct, cfas[0], body.name, body.code)
    return r


@router.post("/cf/git-deploy")
def cf_git_deploy(body: dict):
    acct = _require(_get_account(int(body["account_id"])), "cloudflare")
    cfas = [body.get("cf_account_id")] if body.get("cf_account_id") else \
           [a["cf_account_id"] for a in cfmod.accounts_list(acct)]
    if not cfas:
        raise HTTPException(400, "未找到 Cloudflare 账户")
    return cfmod.deploy_from_github(
        acct, cfas[0], body["repo_url"], body.get("branch", "main"),
        body.get("worker_name", ""), body.get("token", ""))


@router.delete("/cf/worker")
def cf_worker_delete(account_id: int, cf_account_id: str, script_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    cfmod.worker_delete(acct, cf_account_id, script_id)
    return {"ok": True}


@router.get("/cf/routes")
def cf_routes(account_id: int, zone_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    return {"items": cfmod.routes(acct, zone_id)}


@router.post("/cf/routes")
def cf_create_route(body: CfRouteIn):
    acct = _require(_get_account(body.account_id), "cloudflare")
    r = cfmod.create_route(acct, body.zone_id, body.pattern, body.script)
    return {"route_id": r.get("id", "")}


@router.delete("/cf/routes")
def cf_delete_route(account_id: int, zone_id: str, route_id: str):
    acct = _require(_get_account(account_id), "cloudflare")
    cfmod.delete_route(acct, zone_id, route_id)
    return {"ok": True}


# ---------------------------------------------------------------- DNSHE(my.dnshe.com)

@router.get("/dnshe/subdomains")
def dnshe_subdomains(account_id: int, search: str = "", status: str = ""):
    acct = _require(_get_account(account_id), "dnshe")
    d = dnshe.list_subdomains(acct, search=search, status=status)
    # 统一返回 items 供前端使用(兼容旧响应保留 subdomains 字段)
    return {"items": d.get("subdomains", []),
            "subdomains": d.get("subdomains", []),
            "pagination": d.get("pagination", {}),
            "count": d.get("count", 0)}


@router.post("/dnshe/subdomains")
def dnshe_register(body: DnsheSubdomainIn):
    acct = _require(_get_account(body.account_id), "dnshe")
    return dnshe.register_subdomain(acct, body.subdomain.strip(),
                                    body.rootdomain.strip())


@router.delete("/dnshe/subdomains")
def dnshe_delete_subdomain(account_id: int, subdomain_id: int):
    acct = _require(_get_account(account_id), "dnshe")
    return dnshe.delete_subdomain(acct, subdomain_id)


@router.get("/dnshe/records")
def dnshe_records(account_id: int, subdomain_id: int):
    acct = _require(_get_account(account_id), "dnshe")
    return {"items": dnshe.list_records(acct, subdomain_id)}


@router.post("/dnshe/records")
def dnshe_create_record(body: DnsheRecordIn):
    acct = _require(_get_account(body.account_id), "dnshe")
    return dnshe.create_record(acct, body.subdomain_id, body.type, body.name,
                               body.content, body.ttl, body.priority)


@router.put("/dnshe/records")
def dnshe_update_record(body: DnsheRecordUpd):
    acct = _require(_get_account(body.account_id), "dnshe")
    return dnshe.update_record(acct, body.subdomain_id, body.record_id,
                               body.type, body.name, body.content, body.ttl,
                               body.priority)


@router.delete("/dnshe/records")
def dnshe_delete_record(account_id: int, subdomain_id: int, record_id: str):
    acct = _require(_get_account(account_id), "dnshe")
    return dnshe.delete_record(acct, subdomain_id, record_id)


@router.get("/dnshe/quota")
def dnshe_quota(account_id: int):
    acct = _require(_get_account(account_id), "dnshe")
    return dnshe.quota(acct)


# ---------------------------------------------------------------- HE Dynamic DNS(nic/update)

@router.post("/he/ddns")
def he_ddns(body: dict):
    from .. import he_dns
    r = he_dns.ddns_update({}, str(body.get("hostname", "")),
                           str(body.get("secret", "")), str(body.get("ip", "") or ""))
    return {"ok": True, "message": f"{r['hostname']} → {r['ip']}({r['result'].split()[0]})"}
