"""AWS 管理(boto3):EC2 全区域扫描 + Lightsail 全区域扫描。

v0.10.0:boto3 客户端按「凭据摘要+区域」缓存复用,避免每次扫描重复构建客户端。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from . import ttlcache
from .pcreds import ProviderError, extra_creds

log = logging.getLogger("aws")

_BOTO_CLIENTS: ttlcache.TTLCache = ttlcache.TTLCache(ttl=600.0, max_items=128)


def _cred_hash(c: dict) -> str:
    raw = f"{c.get('aws_access_key_id','')}:{c.get('aws_secret_key','')}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _client(acct: dict, region: str | None = None):
    c = extra_creds(acct)
    region = region or acct.get("region") or "us-east-1"
    key = ("ec2", _cred_hash(c), region)
    cli = _BOTO_CLIENTS.get(key)
    if cli is None:
        try:
            cli = boto3.client(
                "ec2",
                region_name=region,
                aws_access_key_id=c["aws_access_key_id"],
                aws_secret_access_key=c["aws_secret_key"],
            )
        except (BotoCoreError, KeyError) as e:
            raise ProviderError(f"AWS 客户端创建失败:{e}") from e
        _BOTO_CLIENTS.set(key, cli)
    return cli


def _wrap(e: Exception) -> ProviderError:
    if isinstance(e, ProviderError):
        return e
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        msg = e.response.get("Error", {}).get("Message", str(e))
        hint = ""
        if code in ("AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch"):
            hint = "(请检查 Access Key / Secret Key 是否正确、账户是否已激活)"
        elif code == "UnauthorizedOperation":
            hint = "(IAM 权限不足,需要 ec2:Describe* / StartInstances 等权限)"
        return ProviderError(f"AWS 错误 [{code}] {msg} {hint}".strip())
    return ProviderError(f"AWS 调用失败:{e}")


_AUTH_FAIL_CODES = {"AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch",
                    "UnauthorizedOperation", "OptInRequired", "UnrecognizedClientException"}


def _scan_ec2_region(acct: dict, region: str) -> tuple[list[dict], str | None]:
    """扫描单个区域的 EC2 实例。返回 (rows, err_code)。"""
    try:
        ec2 = _client(acct, region)
        rows = []
        token = None
        while True:
            kw = {"MaxResults": 1000}
            if token:
                kw["NextToken"] = token
            resp = ec2.describe_instances(**kw)
            for res in resp.get("Reservations", []):
                for i in res.get("Instances", []):
                    name = next((t["Value"] for t in i.get("Tags", [])
                                 if t["Key"] == "Name"), i["InstanceId"])
                    state = (i.get("State", {}).get("Name") or "").upper()
                    if state == "TERMINATED":
                        continue
                    boot = None
                    for d in i.get("BlockDeviceMappings", []):
                        if d.get("Ebs"):
                            boot = d["Ebs"].get("VolumeSize")
                            break
                    ct = i.get("LaunchTime")
                    rows.append({
                        "account_id": acct["id"],
                        "account_name": acct["name"],
                        "provider": "aws",
                        "service": "ec2",
                        "region": region,
                        "compartment_id": "-", "compartment_name": "EC2",
                        "id": i["InstanceId"],
                        "name": name,
                        "state": state,
                        "shape": i.get("InstanceType", ""),
                        "ocpus": None, "mem_gbs": None,
                        "boot_gbs": boot,
                        "ad": i.get("Placement", {}).get("AvailabilityZone", ""),
                        "public_ip": i.get("PublicIpAddress"),
                        "public_lifetime": None,
                        "private_ip": i.get("PrivateIpAddress"),
                        "vnic_id": None,
                        "time_created": (ct.strftime("%Y-%m-%d %H:%M") if ct else ""),
                    })
            token = resp.get("NextToken")
            if not token:
                break
        return rows, None
    except ClientError as e:
        return [], e.response.get("Error", {}).get("Code", "Unknown")
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:80]


def list_instances(acct: dict) -> list[dict]:
    """扫描账户全部已启用区域的 EC2 实例(并行),不受表单区域限制。"""
    try:
        base = _client(acct)
        try:
            regions = [r["RegionName"] for r in base.describe_regions()["Regions"]]
        except (BotoCoreError, ClientError) as e:
            raise _wrap(e) from e

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda r: _scan_ec2_region(acct, r), regions))

        rows, auth_fail, other = [], 0, []
        for reg, (r_rows, err) in zip(regions, results):
            rows.extend(r_rows)
            if err in _AUTH_FAIL_CODES:
                auth_fail += 1
            elif err:
                other.append(f"{reg}: {err}")
        if auth_fail >= len(regions):
            raise ProviderError(
                "AWS 凭证无效(所有区域均鉴权失败)。Secret Access Key 应为 40 位"
                "(创建时仅显示一次);请到 IAM 控制台重新生成并完整粘贴。")
        # 个别区域失败(如未启用)静默忽略,其余错误附加在行外由调用方展示
        if other:
            log.warning("EC2 部分区域扫描失败:%s", other[:3])
        rows.sort(key=lambda r: (r["region"], r["name"]))
        return rows
    except ProviderError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def instance_op(acct: dict, instance_id: str, op: str) -> dict:
    op_map = {"START": "start_instances", "STOP": "stop_instances",
              "SOFTSTOP": "stop_instances", "RESET": "reboot_instances",
              "TERMINATE": "terminate_instances"}
    if op not in op_map:
        raise ProviderError(f"暂不支持的操作:{op}(AWS 支持 启动/停止/重启)")
    try:
        ec2 = _client(acct)
        getattr(ec2, op_map[op])(InstanceIds=[instance_id])
        return {"started": True, "op": op}
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e


def _state(ec2, iid: str) -> tuple[str, str | None]:
    r = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return (r.get("State", {}).get("Name") or "").upper(), r.get("PublicIpAddress")


def change_public_ip(progress, acct: dict, compartment_id: str, instance_id: str) -> dict:
    """默认 VPC 下 停止→启动 会更换公网 IP。"""
    try:
        ec2 = _client(acct)
        old = _state(ec2, instance_id)[1]
        progress(f"当前公网 IP:{old}")
        progress("执行 stop_instances …")
        ec2.stop_instances(InstanceIds=[instance_id])
        t0 = time.time()
        while time.time() - t0 < 300:
            st, _ = _state(ec2, instance_id)
            progress(f"状态:{st}")
            if st == "STOPPED":
                break
            time.sleep(8)
        else:
            raise ProviderError("等待停止超时(5 分钟),请稍后手动重试")
        progress("重新启动以获取新 IP …")
        ec2.start_instances(InstanceIds=[instance_id])
        t0 = time.time()
        while time.time() - t0 < 300:
            st, ip = _state(ec2, instance_id)
            progress(f"状态:{st} IP:{ip}")
            if st == "RUNNING" and ip:
                progress(f"新公网 IP:{ip}")
                return {"old_ip": old, "new_ip": ip}
            time.sleep(8)
        raise ProviderError("等待启动超时")
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e


# ================================================================ Lightsail(轻量云)

_LS_FALLBACK = [
    "us-east-1", "us-east-2", "us-west-2",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-southeast-1", "ap-southeast-2",
    "ca-central-1", "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
]


def _ls(acct: dict, region: str):
    c = extra_creds(acct)
    key = ("lightsail", _cred_hash(c), region)
    cli = _BOTO_CLIENTS.get(key)
    if cli is None:
        cli = boto3.client("lightsail", region_name=region,
                           aws_access_key_id=c["aws_access_key_id"],
                           aws_secret_access_key=c["aws_secret_key"])
        _BOTO_CLIENTS.set(key, cli)
    return cli


def lightsail_regions(acct: dict) -> list[str]:
    """枚举 Lightsail 支持的全部区域(失败时退回内置清单)。"""
    try:
        r = _ls(acct, "us-east-1").get_regions(includeAvailabilityZones=False)
        names = [x["name"] for x in r.get("regions", [])]
        return names or _LS_FALLBACK
    except Exception:  # noqa: BLE001
        return _LS_FALLBACK


def list_lightsail(acct: dict) -> tuple[list[dict], list[str]]:
    """扫描所有 Lightsail 区域的实例(轻量服务器,区域并行)。"""
    import datetime as dt
    from concurrent.futures import ThreadPoolExecutor

    def scan_region(reg):
        rows = []
        err = None
        try:
            ls = _ls(acct, reg)
            for i in ls.get_instances().get("instances", []):
                st = ((i.get("state") or {}).get("name") or "").upper()
                if st in ("DELETED",):
                    continue
                hw = i.get("hardware") or {}
                ct = i.get("createdAt")
                rows.append({
                    "account_id": acct["id"],
                    "account_name": acct["name"],
                    "provider": "aws",
                    "service": "lightsail",
                    "region": reg,
                    "compartment_id": "-", "compartment_name": "Lightsail",
                    "id": i["name"],
                    "name": i["name"],
                    "state": st,
                    "shape": i.get("bundleId", ""),
                    "ocpus": (hw.get("cpuCount") or None),
                    "mem_gbs": (hw.get("ramSizeInGb") or None),
                    "boot_gbs": hw.get("diskSizeInGb"),
                    "ad": reg,
                    "public_ip": i.get("publicIpAddress"),
                    "public_lifetime": None,
                    "private_ip": i.get("privateIpAddress"),
                    "vnic_id": None,
                    "time_created": (ct.strftime("%Y-%m-%d %H:%M") if ct else ""),
                })
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            err = "AUTH_FAIL" if code in _AUTH_FAIL_CODES else \
                  f"{reg}: {e.response.get('Error',{}).get('Message','')}"
        except Exception as e:  # noqa: BLE001
            err = f"{reg}: {e}"
        return reg, rows, err

    regions = lightsail_regions(acct)
    rows = []
    errors = []
    auth_fail = 0
    with ThreadPoolExecutor(max_workers=min(10, len(regions) or 1)) as ex:
        for reg, r_rows, err in ex.map(scan_region, regions):
            rows.extend(r_rows)
            if err == "AUTH_FAIL":
                auth_fail += 1
            elif err:
                errors.append(f"Lightsail {err}")
    if auth_fail and auth_fail >= len(regions) and not rows:
        errors.append(f"Lightsail:全部 {len(regions)} 个区域鉴权失败,请检查 AWS 密钥")
    rows.sort(key=lambda r: r["name"])
    return rows, errors


def lightsail_op(acct: dict, region: str, name: str, op: str) -> dict:
    m = {"START": "start_instance", "STOP": "stop_instance",
         "SOFTSTOP": "stop_instance", "RESET": "reboot_instance",
         "TERMINATE": "delete_instance"}
    if op not in m:
        raise ProviderError(f"Lightsail 暂不支持:{op}")
    try:
        getattr(_ls(acct, region), m[op])(instanceName=name)
        return {"started": True, "op": op}
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e


def _ls_state(ls, name: str) -> tuple[str, str | None]:
    i = ls.get_instance(instanceName=name)["instance"]
    return ((i.get("state") or {}).get("name") or "").upper(), i.get("publicIpAddress")


def lightsail_change_ip(progress, acct: dict, region: str, name: str) -> dict:
    """停止→启动以更换公网 IP(若绑定了静态 IP 则不会变化)。"""
    try:
        ls = _ls(acct, region)
        old = _ls_state(ls, name)[1]
        progress(f"当前公网 IP:{old}(注意:绑定了静态 IP 则不会变化)")
        progress("执行 stop …")
        ls.stop_instance(instanceName=name)
        t0 = time.time()
        while time.time() - t0 < 300:
            st, _ = _ls_state(ls, name)
            progress(f"状态:{st}")
            if st == "STOPPED":
                break
            time.sleep(8)
        progress("重新启动 …")
        ls.start_instance(instanceName=name)
        t0 = time.time()
        while time.time() - t0 < 300:
            st, ip = _ls_state(ls, name)
            progress(f"状态:{st} IP:{ip}")
            if st == "RUNNING" and ip:
                progress(f"新公网 IP:{ip}")
                return {"old_ip": old, "new_ip": ip}
            time.sleep(8)
        raise ProviderError("等待启动超时")
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e


# ================================================================ Lightsail 创建

def lightsail_meta(acct: dict, region: str | None = None) -> dict:
    """返回指定区域的可用镜像(blueprint)、套餐(bundle)和区域列表。"""
    region = region or (acct.get("region") or "ap-northeast-1")
    ls = _ls(acct, region)
    blueprints = [{
        "blueprint_id": b["blueprintId"],
        "name": b.get("name", ""),
        "platform": b.get("platform", ""),
        "os": b.get("osVersion", ""),
        "type": b.get("type", ""),
    } for b in ls.get_blueprints().get("blueprints", [])]
    bundles = [{
        "bundle_id": b["bundleId"],
        "name": b.get("name", ""),
        "power": b.get("power", ""),
        "ram": b.get("ramSizeInGb", 0),
        "cpu": b.get("cpuCount", 0),
        "disk": b.get("diskSizeInGb", 0),
        "price": b.get("price", 0),
        "monthly_transfer": b.get("monthlyTransferGb", 0),
    } for b in ls.get_bundles().get("bundles", [])]
    return {"region": region, "regions": lightsail_regions(acct),
            "blueprints": blueprints, "bundles": bundles}


def lightsail_create(acct: dict, region: str, name: str, blueprint_id: str,
                     bundle_id: str, az: str = "") -> dict:
    """创建 Lightsail 实例(异步,数秒后进入 pending,随后 running)。"""
    try:
        ls = _ls(acct, region)
        kw = {"instanceNames": [name], "blueprintId": blueprint_id, "bundleId": bundle_id}
        if az:
            kw["availabilityZone"] = az
        resp = ls.create_instances(**kw)
        ops = resp.get("operations", [])
        return {"name": name, "region": region, "operations": len(ops),
                "status": (ops[0].get("status") if ops else "started")}
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e

# ================================================================ EC2 创建

_EC2_TYPES = [
    "t2.micro", "t2.small", "t2.medium", "t3.micro", "t3.small", "t3.medium",
    "t3a.micro", "t3a.small", "t4g.micro", "t4g.small", "t4g.medium",
    "m5.large", "m5.xlarge", "m6i.large", "m6i.xlarge",
    "c5.large", "c5.xlarge", "c6i.large", "c6i.xlarge",
    "r5.large", "r5.xlarge", "r6i.large",
]


def ec2_meta(acct: dict, region: str) -> dict:
    """返回 EC2 创建所需元数据:AMI、实例类型、子网、安全组。"""
    ec2 = _client(acct, region)
    try:
        imgs = ec2.describe_images(Owners=["amazon"], Filters=[
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ], MaxResults=50).get("Images", [])
        # 按创建时间倒序取前 20
        imgs.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
        amis = [{"id": i["ImageId"], "name": i.get("Name", i["ImageId"])} for i in imgs[:20]]
        subnets = [{"id": s["SubnetId"], "name": s.get("Tags", [{"Key": "Name", "Value": ""}])[0].get("Value", ""),
                    "az": s.get("AvailabilityZone", ""), "public": s.get("MapPublicIpOnLaunch", False)}
                   for s in ec2.describe_subnets().get("Subnets", [])]
        sgs = [{"id": g["GroupId"], "name": g.get("GroupName", ""), "vpc": g.get("VpcId", "")}
               for g in ec2.describe_security_groups().get("SecurityGroups", [])]
        return {"region": region, "amis": amis, "instance_types": _EC2_TYPES,
                "subnets": subnets, "security_groups": sgs}
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e


def ec2_create(acct: dict, region: str, name: str, image_id: str, instance_type: str,
               subnet_id: str = "", security_group_id: str = "", key_name: str = "") -> dict:
    """创建 EC2 实例。子网/安全组留空则使用默认 VPC 默认子网/组。"""
    try:
        ec2 = _client(acct, region)
        kw = {"ImageId": image_id, "InstanceType": instance_type,
              "MinCount": 1, "MaxCount": 1, "TagSpecifications": [{
                  "ResourceType": "instance",
                  "Tags": [{"Key": "Name", "Value": name}]}]}
        if subnet_id:
            kw["SubnetId"] = subnet_id
        if security_group_id:
            kw["SecurityGroupIds"] = [security_group_id]
        if key_name:
            kw["KeyName"] = key_name
        resp = ec2.run_instances(**kw)
        iid = resp["Instances"][0]["InstanceId"]
        return {"instance_id": iid, "region": region}
    except (BotoCoreError, ClientError) as e:
        raise _wrap(e) from e
