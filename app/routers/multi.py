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
    for a in cfas[:5]:
        try:
            ws = cfmod.workers_list(acct, a["cf_account_id"])
        except Exception:  # noqa: BLE001
            ws = []
        for w in ws:
            w["cf_account_name"] = a["name"]
            items.append(w)
        if not sub:
            sub = cfmod.subdomain(acct, a["cf_account_id"])
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
    return dnshe.list_subdomains(acct, search=search, status=status)


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
