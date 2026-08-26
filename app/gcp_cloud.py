"""Google Cloud Compute Engine 管理(REST API + google-auth 服务账号签名)。

- 凭据:服务账号 JSON 密钥(IAM与管理 → 服务账号 → 密钥),
  需要 Compute Engine 权限(compute.instances.* / machineTypes.list 等,
  建议授予 roles/compute.instanceAdmin.v1 或 roles/compute.viewer + 操作权限)
- 认证:google-auth 签发 OAuth2 Access Token(缓存至过期前 60s),
  调 https://compute.googleapis.com/compute/v1/...
- 能力:全区域实例聚合列表(aggregatedList)/ 电源(start/stop/reset)/
  换外网 IP(delete+addAccessConfig)/ 创建 / 终止 / 元数据(可用区·机型·子网)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

from . import http_pool
from .pcreds import ProviderError, extra_creds

log = logging.getLogger("gcp")

_COMPUTE = "https://compute.googleapis.com/compute/v1"
_SCOPE = "https://www.googleapis.com/auth/compute"
_POLL = float(os.getenv("GCP_POLL", "5"))
_T_WAIT_IP = 180
_T_WAIT_RUN = 420

_cred_cache: dict[str, tuple[float, object]] = {}   # sa hash -> (expire_ts, Credentials)

# 常用公共镜像家族(可手填任意 projects/<proj>/global/images[/family]/<name>)
IMAGE_FAMILIES = [
    ("projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
     "Ubuntu 24.04 LTS (amd64)"),
    ("projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts-jammy",
     "Ubuntu 22.04 LTS (amd64)"),
    ("projects/debian-cloud/global/images/family/debian-12", "Debian 12"),
    ("projects/debian-cloud/global/images/family/debian-11", "Debian 11"),
    ("projects/rocky-linux-cloud/global/images/family/rocky-linux-9",
     "Rocky Linux 9"),
    ("projects/almalinux-cloud/global/images/family/almalinux-9",
     "AlmaLinux 9"),
    ("projects/centos-cloud/global/images/family/centos-stream-9",
     "CentOS Stream 9"),
    ("projects/windows-cloud/global/images/family/windows-2022",
     "Windows Server 2022"),
]

_STATE_MAP = {
    "PROVISIONING": "STARTING", "STAGING": "STARTING", "REPAIRING": "STARTING",
    "RUNNING": "RUNNING", "STOPPING": "STOPPING",
    "TERMINATED": "STOPPED", "SUSPENDED": "STOPPED",
}


def _sa_info(acct: dict) -> dict:
    raw = (extra_creds(acct).get("gcp_sa_json") or "").strip()
    if not raw:
        raise ProviderError("该 Google Cloud 账户未配置服务账号 JSON 密钥")
    try:
        info = json.loads(raw)
    except ValueError as e:
        raise ProviderError(f"服务账号 JSON 解析失败:{e}") from e
    if not isinstance(info, dict) or "private_key" not in info or "client_email" not in info:
        raise ProviderError("服务账号 JSON 不完整(缺少 private_key/client_email),"
                            "请粘贴完整的服务账号密钥文件内容")
    return info


def _project(acct: dict, info: dict | None = None) -> str:
    """项目 ID:优先显式填写的 gcp_project,否则取服务账号 JSON 内的 project_id。"""
    proj = (extra_creds(acct).get("gcp_project") or "").strip()
    if not proj:
        info = info or _sa_info(acct)
        proj = str(info.get("project_id") or "")
    if not proj:
        raise ProviderError("无法确定项目 ID(请填写项目 ID)")
    return proj


def _credentials(info: dict):
    """google-auth 凭据(懒加载,模块缺失给出明确提示)。"""
    try:
        import google.auth.transport.requests
        from google.oauth2 import service_account
    except ImportError as e:
        raise ProviderError("服务器缺少 google-auth 库:"
                            "pip install google-auth 后重启面板") from e
    khash = hashlib.md5(json.dumps(info, sort_keys=True).encode()).hexdigest()[:16]
    hit = _cred_cache.get(khash)
    now = time.time()
    if hit and hit[0] - 60 > now:
        return hit[1]
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[_SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    exp = creds.expiry
    ts = exp.timestamp() if isinstance(exp, datetime) else now + 3600
    _cred_cache[khash] = (ts, creds)
    return creds


def _token(acct: dict) -> str:
    return _credentials(_sa_info(acct)).token


def _req(acct: dict, method: str, path: str, *, params: dict | None = None,
         json_body: dict | None = None, retry_auth: bool = True) -> dict:
    url = path if path.startswith("http") else _COMPUTE + path
    try:
        r = http_pool.request(method, url, params=params, json=json_body,
                              headers={"Authorization": f"Bearer {_token(acct)}"},
                              timeout=30)
        if r.status_code == 401 and retry_auth:
            _cred_cache.clear()
            r = http_pool.request(method, url, params=params, json=json_body,
                                  headers={"Authorization": f"Bearer {_token(acct)}"},
                                  timeout=30)
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"Google Cloud 网络错误:{e}") from e
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code >= 400:
        err = d.get("error") or {}
        msg = err.get("message") or r.text[:180]
        code = err.get("code") or r.status_code
        low = str(msg).lower()
        errs0 = (err.get("errors") or [{}])[0]
        reason = (errs0.get("reason") or "").lower()
        hint = ""
        if "servicedisabled" in reason or "has not been used in project" in low \
                or ("it is disabled" in low):
            # API 未启用:给出可点的启用链接(Google 的报错里通常自带)
            import re as _re
            m = _re.search(r'https://console\.developers\.google\.com/[^\s"\']+', str(msg))
            url = m.group(0) if m else \
                "https://console.cloud.google.com/apis/library/compute.googleapis.com"
            hint = (f"(Compute Engine API 未启用:打开 {url} 点「启用」,等待 1~2 分钟后重试;"
                    "这是项目级开关,与 IAM 角色无关)")
        elif r.status_code in (401, 403):
            hint = "(服务账号权限不足?请授予 roles/compute.instanceAdmin.v1 角色)"
        elif "<html" in low and r.status_code == 404:
            hint = "(操作查询路径不匹配,已自动降级重试;若反复出现请更新面板)"
        elif "not found" in low or "was not found" in low:
            hint = "(检查项目 ID / 资源是否存在)"
        elif "quota" in low:
            hint = "(配额不足:检查该区域的 CPU 配额或机型可用性)"
        elif "billing" in low:
            hint = "(项目未关联结算账号:console.cloud.google.com/billing 绑定后再创建实例)"
        raise ProviderError(f"Google Cloud 错误 [{code}] {msg} {hint}".strip())
    return d


# ---------------------------------------------------------------- 实例列表

def list_instances(acct: dict) -> list[dict]:
    """聚合列出该项目全部区域中的实例(aggregatedList,分页拉全)。"""
    proj = _project(acct)
    rows: list[dict] = []
    page = None
    while True:
        params = {"maxResults": 500}
        if page:
            params["pageToken"] = page
        d = _req(acct, "GET", f"/projects/{proj}/aggregated/instances", params=params)
        items = d.get("items") or {}
        for zone_key, bucket in items.items():
            zone = str(zone_key).split("/")[-1]
            for ins in bucket.get("instances") or []:
                rows.append(_row(acct, zone, ins))
        page = d.get("nextPageToken")
        if not page:
            break
    rows.sort(key=lambda x: x["name"])
    return rows


def _row(acct: dict, zone: str, ins: dict) -> dict:
    state = _STATE_MAP.get(ins.get("status") or "", (ins.get("status") or "?").upper())
    nics = ins.get("networkInterfaces") or []
    pub_ip = pri_ip = ""
    if nics:
        pri_ip = nics[0].get("networkIP") or ""
        acs = nics[0].get("accessConfigs") or []
        if acs:
            pub_ip = acs[0].get("natIP") or ""
    mt = (ins.get("machineType") or "").rsplit("/", 1)[-1]
    ct = (ins.get("creationTimestamp") or "")[:16].replace("T", " ")
    return {
        "account_id": acct["id"],
        "account_name": acct.get("name", ""),
        "provider": "gcp",
        "service": "gce",
        "region": zone.rsplit("-", 1)[0],
        "compartment_id": "-",
        "compartment_name": "Compute Engine",
        "id": str(ins.get("id") or ins.get("name", "")),
        "name": ins.get("name", ""),
        "state": state,
        "shape": mt,
        "ocpus": None,
        "mem_gbs": None,
        "boot_gbs": _boot_size(ins),
        "ad": zone,
        "public_ip": pub_ip or None,
        "public_lifetime": "EPHEMERAL" if pub_ip else None,
        "private_ip": pri_ip or None,
        "vnic_id": (nics[0].get("name") if nics else None),
        "time_created": ct,
        "_zone": zone,
        "_nic0": (nics[0].get("name") if nics else "nic0"),
    }


def _boot_size(ins: dict) -> int | None:
    for disk in ins.get("disks") or []:
        if disk.get("boot"):
            return disk.get("diskSizeGb")
    return None


# ---------------------------------------------------------------- 电源操作

_ACTIONS = {"start", "stop", "reset"}


def instance_op(acct: dict, name: str, zone: str, action: str) -> dict:
    action = action.lower().strip()
    if action == "reboot":
        action = "reset"
    if action not in _ACTIONS:
        raise ProviderError(f"不支持的动作:{action}(支持 start/stop/reboot)")
    if not name or not zone:
        raise ProviderError("缺少实例名或可用区")
    _req(acct, "POST", f"/zones/{zone}/instances/{name}/{action}")
    return {"started": True, "op": action}


def terminate_instance(acct: dict, name: str, zone: str) -> dict:
    if not name or not zone:
        raise ProviderError("缺少实例名或可用区")
    _req(acct, "DELETE", f"/zones/{zone}/instances/{name}")
    return {"ok": True}


# ---------------------------------------------------------------- 换公网 IP

def _get_instance(acct: dict, name: str, zone: str) -> dict:
    return _req(acct, "GET", f"/zones/{zone}/instances/{name}")


def _nat_config(ins: dict) -> tuple[str, dict | None]:
    """返回 (nic0 名字, 外网访问配置)。没有则 nic0 + None。"""
    nics = ins.get("networkInterfaces") or []
    if not nics:
        return "nic0", None
    nic = nics[0]
    acs = nic.get("accessConfigs") or []
    cfg = next((a for a in acs if a.get("type") == "ONE_TO_ONE_NAT"), acs[0] if acs else None)
    return nic.get("name") or "nic0", cfg


def change_public_ip(progress, acct: dict, compartment_id: str, name_zone: str) -> dict:
    """换外网 IP:删除现有访问配置 → 新建空 natIP 的访问配置(获得新的临时公网 IP)。"""
    name, _, zone = (name_zone or "").partition("@")
    if not zone:
        raise ProviderError("内部错误:缺少可用区参数")
    ins = _get_instance(acct, name, zone)
    nic, cfg = _nat_config(ins)
    old_ip = ((cfg or {}).get("natIP")) or ""

    if cfg and cfg.get("name"):
        progress(f"删除现有外网访问配置({old_ip})…")
        _req(acct, "DELETE",
             f"/zones/{zone}/instances/{name}/deleteAccessConfig",
             params={"accessConfigName": cfg["name"], "networkInterface": nic})

    progress("申请新的外网 IP …")
    body = {"name": (cfg or {}).get("name") or "External NAT",
            "type": "ONE_TO_ONE_NAT",
            "natIP": "",
            "networkTier": (cfg or {}).get("networkTier") or "STANDARD"}
    _req(acct, "POST", f"/zones/{zone}/instances/{name}/addAccessConfig",
         params={"networkInterface": nic}, json_body=body)

    deadline = time.time() + _T_WAIT_IP
    new_ip = ""
    while time.time() < deadline:
        ins = _get_instance(acct, name, zone)
        nic2, cfg2 = _nat_config(ins)
        new_ip = (cfg2 or {}).get("natIP") or ""
        if new_ip and new_ip != old_ip:
            break
        time.sleep(_POLL)
    if not new_ip or new_ip == old_ip:
        raise ProviderError("等待新外网 IP 超时,请稍后刷新列表确认")
    progress(f"✅ 新公网 IP:{new_ip}")
    return {"old_ip": old_ip, "new_ip": new_ip}


# ---------------------------------------------------------------- 创建实例

def create_instance(progress, acct: dict, d: dict) -> dict:
    """创建 GCE 实例并等待 RUNNING。

    d: {name, zone, machine_type, image, subnet?, ssh_key?, external_ip?, boot_gbs?}
    image 为完整路径:projects/<proj>/global/images[/<family|image>/<name>]
    """
    proj = _project(acct)
    name = str(d.get("name") or "").strip()
    zone = str(d.get("zone") or "").strip()
    mt = str(d.get("machine_type") or "").strip()
    image = str(d.get("image") or "").strip()
    if not name or not zone or not mt or not image:
        raise ProviderError("缺少必要参数(名称/可用区/机型/镜像)")
    if len(name) > 63:
        raise ProviderError("实例名称最长 63 个字符")

    # 子网传名称则按 zone 所属区域构造相对路径;不传则用默认网络
    subnet = str(d.get("subnet") or "").strip().rsplit("/", 1)[-1]
    region = zone.rsplit("-", 1)[0]
    iface: dict = ({"subnetwork": f"regions/{region}/subnetworks/{subnet}"}
                   if subnet else {"network": "global/networks/default"})

    if d.get("external_ip", True):
        iface["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT",
                                   "name": "External NAT",
                                   "networkTier": "STANDARD"}]

    meta_items = []
    ssh_key = str(d.get("ssh_key") or "").strip()
    if ssh_key:
        meta_items.append({"key": "ssh-keys",
                           "value": f"clouddeck:{ssh_key}"})

    init_params: dict = {"sourceImage": image if "/" in image else
                         f"projects/{proj}/global/images/{image}"}
    boot = d.get("boot_gbs")
    if boot and int(boot) >= 10:
        init_params["diskSizeGb"] = str(int(boot))
    # 磁盘类型:免费层必须用标准盘 pd-standard(平衡盘/SSD 计费)
    disk_type = str(d.get("disk_type") or "").strip()
    if disk_type:
        init_params["diskType"] = f"zones/{zone}/diskTypes/{disk_type}"

    body = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{mt}",
        "disks": [{"boot": True, "autoDelete": True, "initializeParams": init_params}],
        "networkInterfaces": [iface],
        "metadata": {"items": meta_items},
    }
    progress(f"提交创建请求:{name} @ {zone}({mt})…")
    op = _req(acct, "POST", f"/projects/{proj}/zones/{zone}/instances", json_body=body)
    op_name = op.get("name", "")
    op_url = op.get("selfLink") or ""
    zone_ops_base = f"/projects/{proj}/zones/{zone}/operations"
    region_ops_base = "/projects/{}/regions/{}/operations".format(
        proj, zone.rsplit("-", 1)[0])
    global_ops_base = f"/projects/{proj}/global/operations"
    tried = set()
    deadline = time.time() + _T_WAIT_RUN
    while time.time() < deadline:
        st = None
        # 优先用 selfLink;否则按 区域级→全球级 顺序探测(避免 404 HTML)
        for base in ([op_url] if op_url else []) + \
                [zone_ops_base + "/" + op_name,
                 region_ops_base + "/" + op_name,
                 global_ops_base + "/" + op_name]:
            if not base or base in tried:
                continue
            try:
                st = _req(acct, "GET",
                          base if base.startswith("http") else _COMPUTE + base)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        tried.add(op_url)
        if st is None:
            time.sleep(_POLL)
            continue
        s = st.get("status")
        if s == "DONE":
            if st.get("error"):
                errs = (st["error"].get("errors") or [{}])[0].get("message", "")
                raise ProviderError(f"创建失败:{errs or st['error']}")
            break
        progress(f"操作状态:{s}")
        time.sleep(_POLL)
    else:
        raise ProviderError("等待创建完成超时,请到控制台确认")

    ins = _get_instance(acct, name, zone)
    nic, cfg = _nat_config(ins)
    ip = (cfg or {}).get("natIP") or ""
    progress(f"✅ 创建完成,状态 RUNNING,公网 IP:{ip or '(无)'}")
    progress("SSH 登录用户名:clouddeck(密钥已写入实例元数据)")
    return {"instance_id": str(ins.get("id") or name), "public_ip": ip}


# ---------------------------------------------------------------- 元数据

def gcp_meta(acct: dict) -> dict:
    """可用区(全部)+ 公共镜像家族 + 项目 ID。"""
    proj = _project(acct)
    zones = []
    page = None
    while True:
        params = {"maxResults": 500}
        if page:
            params["pageToken"] = page
        d = _req(acct, "GET", f"/projects/{proj}/zones", params=params)
        for z in d.get("items", []):
            zones.append(z.get("name", ""))
        page = d.get("nextPageToken")
        if not page:
            break
    zones.sort()
    return {"project": proj, "zones": zones,
            "images": [{"id": i, "label": lbl} for i, lbl in IMAGE_FAMILIES]}


def mach_types(acct: dict, zone: str) -> list[dict]:
    """指定可用区的机型列表(按 vCPU 升序,e2/small 优先展示)。"""
    if not zone:
        raise ProviderError("请先选择可用区")
    out, page = [], None
    while True:
        params = {"maxResults": 500}
        if page:
            params["pageToken"] = page
        d = _req(acct, "GET", f"/zones/{zone}/machineTypes", params=params)
        for m in d.get("items", []):
            out.append({"name": m.get("name", ""),
                        "cpu": m.get("guestCpus"), "mem_gb": round((m.get("memoryMb") or 0) / 1024)})
        page = d.get("nextPageToken")
        if not page:
            break
    out.sort(key=lambda x: (x.get("cpu") or 0, x["name"]))
    return out


def subnets(acct: dict, region: str) -> list[dict]:
    if not region:
        raise ProviderError("请先选择可用区以确定区域")
    out, page = [], None
    while True:
        params = {"maxResults": 500}
        if page:
            params["pageToken"] = page
        d = _req(acct, "GET", f"/regions/{region}/subnetworks", params=params)
        for s in d.get("items", []):
            out.append({"name": s.get("name", ""),
                        "cidr": s.get("ipCidrRange", ""),
                        "network": (s.get("network") or "").rsplit("/", 1)[-1]})
        page = d.get("nextPageToken")
        if not page:
            break
    return out


# ---------------------------------------------------------------- 测活诊断

def check_account(acct: dict) -> dict:
    ok, err, n = True, None, 0
    try:
        n = len(list_instances(acct))
    except ProviderError as e:
        ok, err = False, str(e)
    return {"provider": "gcp", "remote_ok": ok, "error": err,
            "instance_count": n if ok else None,
            "hint": "" if ok else "请检查服务账号 JSON 与 compute 权限"}
