"""IBM Cloud VPC 管理(REST API,无需 SDK)。

- 凭据:IAM API Key(用户 IBM Cloud 控制台 → Manage → Access (IAM) → API keys 创建)
- 认证:用 API Key 换 IAM Token(缓存至过期前 60s),再调 VPC 区域端点
  https://{region}.iaas.cloud.ibm.com/v1/...
- 能力:实例列表 / 电源操作(start/stop/reboot/hard-reboot)/ 换浮动IP / 资源行结构对齐面板
- 区域:如 us-south、eu-de、jp-tok、au-syd
"""
from __future__ import annotations

import logging
import time

from . import http_pool
from .pcreds import ProviderError, extra_creds

log = logging.getLogger("ibm")

_IAM = "https://iam.cloud.ibm.com/identity/token"
_token_cache: dict[str, tuple[float, str]] = {}   # key: api_key hash → (expire_ts, token)

# IBM VPC API 强制要求 version 与 generation 查询参数
_IBM_VERSION = "2023-01-01"
_IBM_GENERATION = "2"


def _api_key(acct: dict) -> str:
    key = (extra_creds(acct).get("ibm_api_key") or "").strip()
    if not key:
        raise ProviderError("该 IBM Cloud 账户未配置 IAM API Key")
    return key


def _token(api_key: str, force: bool = False) -> str:
    import hashlib
    khash = hashlib.md5(api_key.encode()).hexdigest()
    hit = _token_cache.get(khash)
    if not force and hit and hit[0] - 60 > time.time():
        return hit[1]
    try:
        r = http_pool.post(_IAM,
                           data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                                 "apikey": api_key},
                           headers={"Content-Type": "application/x-www-form-urlencoded"},
                           timeout=20)
        d = r.json()
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"IBM IAM 认证失败:{e}") from e
    if r.status_code != 200 or "access_token" not in d:
        msg = (d.get("errorMessage") or r.text[:150]) if isinstance(d, dict) else r.text[:150]
        hint = "(请检查 IAM API Key 是否正确)" if "apikey" in str(msg).lower() else ""
        raise ProviderError(f"IBM IAM 认证失败 [{r.status_code}] {msg} {hint}".strip())
    tok = d["access_token"]
    exp = time.time() + int(d.get("expires_in", 3600))
    _token_cache[khash] = (exp, tok)
    return tok


def _base(acct: dict) -> str:
    region = (acct.get("region") or "us-south").strip().lower()
    return f"https://{region}.iaas.cloud.ibm.com/v1"


def _req(acct: dict, method: str, path: str, *, params: dict | None = None,
         json_body: dict | None = None, retry_auth: bool = True) -> dict:
    api_key = _api_key(acct)
    url = _base(acct) + path
    # 合并 VPC API 必填参数(version/generation)
    params = dict(params or {})
    params.setdefault("version", _IBM_VERSION)
    params.setdefault("generation", _IBM_GENERATION)
    try:
        r = http_pool.request(method, url,
                              params=params,
                              json=json_body,
                              headers={"Authorization": f"Bearer {_token(api_key)}",
                                       "Content-Type": "application/json"},
                              timeout=30)
        # token 过期自动重试一次
        if r.status_code == 401 and retry_auth:
            r = http_pool.request(method, url, params=params, json=json_body,
                                  headers={"Authorization": f"Bearer {_token(api_key, force=True)}",
                                           "Content-Type": "application/json"},
                                  timeout=30)
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"IBM Cloud 网络错误:{e}") from e
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code >= 400:
        errs = d.get("errors") or []
        msg = ";".join(e.get("message", "") for e in errs) or r.text[:180]
        code = (errs[0].get("code") if errs else "") or r.status_code
        hint = ""
        if r.status_code in (401, 403):
            hint = "(IAM Key 权限不足?需要 VPC 实例 Viewer/Operator 角色)"
        elif r.status_code == 404 and "region" not in path:
            hint = "(检查账户区域是否正确)"
        raise ProviderError(f"IBM Cloud 错误 [{code}] {msg} {hint}".strip())
    return d


# ---------------------------------------------------------------- 实例列表

def list_instances(acct: dict) -> list[dict]:
    """列出该区域全部 VPC 实例(分页拉全 + 并行补全网卡/IP 详情)。

    注意:/instances 列表视图的 network_interfaces 只有摘要(无 IP),
    浮动 IP 与私网 IP 需逐实例调 /instances/{id}/network_interfaces 获取。
    """
    items: list[dict] = []
    start = None
    while True:
        params = {"limit": 100}
        if start:
            params["start"] = start
        d = _req(acct, "GET", "/instances", params=params)
        for ins in d.get("instances", []):
            items.append(_row(acct, ins))
        start = d.get("next", {}).get("href", "")
        if not start:
            break
        start = start.split("start=")[-1].split("&")[0]
    _enrich_network(acct, items)
    return items


def _fetch_nics(acct: dict, instance_id: str) -> list[dict]:
    """拉取实例全部网卡(分页)。注:集合成员可能是摘要,floating_ip 以单卡详情为准。"""
    out, start = [], None
    while True:
        params = {"limit": 50}
        if start:
            params["start"] = start
        d = _req(acct, "GET", f"/instances/{instance_id}/network_interfaces",
                 params=params)
        out += d.get("network_interfaces", [])
        start = (d.get("next") or {}).get("href", "")
        if not start:
            break
        start = start.split("start=")[-1].split("&")[0]
    return out


def _floating_ip_map(acct: dict) -> dict[str, dict]:
    """区域内全部浮动 IP 按 目标网卡ID 建索引:{nic_id: {id, address}}。"""
    out: dict[str, dict] = {}
    start = None
    while True:
        params = {"limit": 100}
        if start:
            params["start"] = start
        d = _req(acct, "GET", "/floating_ips", params=params)
        for f in d.get("floating_ips", []):
            t = f.get("target") or {}
            tid = t.get("id")
            if tid and f.get("address"):
                out[tid] = {"id": f.get("id") or "", "address": f["address"]}
        start = (d.get("next") or {}).get("href", "")
        if not start:
            break
        start = start.split("start=")[-1].split("&")[0]
    return out


def _primary_nic_id(acct: dict, instance_id: str) -> str:
    """实例详情兜底获取主网卡 ID(列表视图可能缺 pni)。"""
    try:
        d = _req(acct, "GET", f"/instances/{instance_id}")
        pni = d.get("primary_network_interface")
        if isinstance(pni, dict) and pni.get("id"):
            return pni["id"]
        nis = d.get("network_interfaces") or []
        if nis and isinstance(nis[0], dict):
            return nis[0].get("id") or ""
    except Exception as e:  # noqa: BLE001
        log.debug("IBM 实例详情 %s 获取失败:%s", instance_id, e)
    return ""


def _enrich_network(acct: dict, rows: list[dict]) -> None:
    """并行补全每台实例的 私网 IP / 浮动公网 IP。

    三个数据源交叉(任一命中即用,互相兜底):
      A. 区域浮动 IP 表(/floating_ips → target.id 索引),一次调用覆盖全区域;
      B. 单网卡详情(GET /instances/{id}/network_interfaces/{nic},含权威 floating_ip);
      C. 列表/实例详情给出的主网卡 ID(vnic_id)。
    """
    import concurrent.futures as cf

    from . import config

    try:
        fip_map = _floating_ip_map(acct)
    except Exception as e:  # noqa: BLE001
        log.warning("IBM 浮动 IP 列表获取失败(跳过该数据源):%s", e)
        fip_map = {}

    def one(row: dict) -> None:
        try:
            nic_id = row.get("vnic_id") or ""
            if not nic_id:
                nic_id = _primary_nic_id(acct, row["id"])
                if nic_id:
                    row["vnic_id"] = nic_id
            fip = fip_map.get(nic_id) if nic_id else None
            pri = ""
            if nic_id:
                try:
                    d = _req(acct, "GET",
                             f"/instances/{row['id']}/network_interfaces/{nic_id}")
                    pri = ((d.get("primary_ip") or {}).get("address")) or ""
                    if not pri:
                        ips = d.get("allowed_ips") or []
                        if ips and isinstance(ips[0], dict):
                            pri = ips[0].get("address") or ""
                    f = d.get("floating_ip") or {}
                    if f.get("address"):
                        fip = {"id": f.get("id") or "", "address": f["address"]}
                except Exception as e:  # noqa: BLE001
                    log.debug("IBM 网卡详情 %s/%s 获取失败:%s",
                              row["id"], nic_id, e)
            if pri:
                row["private_ip"] = pri
            if fip and fip.get("address"):
                row["public_ip"] = fip["address"]
                row["_fip_id"] = fip.get("id") or ""
                row["public_lifetime"] = "RESERVED"
            elif not row.get("public_ip"):
                row["public_lifetime"] = None
        except Exception as e:  # noqa: BLE001
            log.debug("IBM 实例 %s 网络补全失败:%s", row.get("name"), e)

    if not rows:
        return
    workers = min(config.MAX_WORKERS, max(len(rows), 2))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, rows))
    n_fip = sum(1 for r in rows if r.get("public_ip"))
    log.info("IBM 实例网络补全:%d 台,其中 %d 台有公网 IP(区域浮动 IP 映射 %d 条)",
             len(rows), n_fip, len(fip_map))


def _row(acct: dict, ins: dict) -> dict:
    state = (ins.get("status") or "").lower()
    smap = {"running": "RUNNING", "stopped": "STOPPED", "starting": "STARTING",
            "stopping": "STOPPING", "restarting": "STARTING", "failed": "FAILED",
            "pending": "STARTING"}
    pub_ip = pri_ip = ""
    fip_id = ""
    for ni in ins.get("network_interfaces", []) or []:
        for a in ni.get("allowed_ips", []):
            pri_ip = a.get("address") or pri_ip
        fip = ni.get("floating_ip") or {}
        if fip.get("address"):
            pub_ip = fip["address"]
            fip_id = fip.get("id", "")
    if not pri_ip:
        ni = ins.get("primary_network_interface")
        if isinstance(ni, dict):
            pri_ip = (ni.get("primary_ip") or {}).get("address") or pri_ip
            fip = ni.get("floating_ip") or {}
            if fip.get("address"):
                pub_ip = fip["address"]
                fip_id = fip.get("id", "")
    vnic_id = ""
    pni = ins.get("primary_network_interface")
    if isinstance(pni, dict):
        vnic_id = pni.get("id", "")
    if not vnic_id:
        nis = ins.get("network_interfaces") or []
        if nis:
            vnic_id = (nis[0] or {}).get("id", "")
    vcpu = ((ins.get("vcpu") or {}).get("count"))
    mem = ins.get("memory")
    profile = ins.get("profile", {}).get("name", "") if isinstance(ins.get("profile"), dict) else ""
    zone = ins.get("zone", {}).get("name", "") if isinstance(ins.get("zone"), dict) else ""
    return {
        "account_id": acct["id"],
        "account_name": acct.get("name", ""),
        "provider": "ibm",
        "service": "vpc",
        "region": (acct.get("region") or "").lower(),
        "compartment_id": "-",
        "compartment_name": "IBM VPC",
        "id": ins.get("id", ""),
        "name": ins.get("name", ""),
        "state": smap.get(state, (state or "?").upper()),
        "shape": profile or "vpc",
        "ocpus": vcpu,
        "mem_gbs": mem,
        "boot_gbs": None,
        "ad": zone,
        "public_ip": pub_ip or None,
        "public_lifetime": "RESERVED" if fip_id else ("EPHEMERAL" if pub_ip else None),
        "private_ip": pri_ip or None,
        "vnic_id": vnic_id,
        "time_created": (ins.get("created_at") or "")[:16].replace("T", " "),
        "_fip_id": fip_id or None,
        "_resource_group": (ins.get("resource_group") or {}).get("id", "") if isinstance(ins.get("resource_group"), dict) else "",
    }


# ---------------------------------------------------------------- 电源操作

_ACTION_MAP = {"start": "start", "stop": "stop", "reboot": "reboot", "hard_reboot": "hard_reboot"}


def instance_op(acct: dict, instance_id: str, action: str) -> dict:
    """电源操作。action: start / stop / reboot / hard_reboot。"""
    action = _ACTION_MAP.get(action.lower())
    if not action:
        raise ProviderError(f"不支持的动作:{action}(支持 start/stop/reboot/hard_reboot)")
    _req(acct, "POST", f"/instances/{instance_id}/actions",
         json_body={"type": action})
    return {"started": True, "op": action}


# ---------------------------------------------------------------- 浮动IP(换IP)

def list_floating_ips(acct: dict) -> list[dict]:
    out, start = [], None
    while True:
        params = {"limit": 100}
        if start:
            params["start"] = start
        d = _req(acct, "GET", "/floating_ips", params=params)
        out += [{"id": f["id"], "address": f.get("address", ""),
                 "attached": bool((f.get("target") or {}))}
                for f in d.get("floating_ips", [])]
        start = (d.get("next") or {}).get("href", "")
        if not start:
            break
        start = start.split("start=")[-1].split("&")[0]
    return out


def change_public_ip(progress, acct: dict, compartment_id: str, instance_id: str) -> dict:
    """换公网 IP:释放当前浮动IP并新建一个绑定到主网卡(临时IP场景)。"""
    rows = [x for x in list_instances(acct) if x["id"] == instance_id]
    if not rows:
        raise ProviderError("实例不存在")
    row = rows[0]
    old_ip = row.get("public_ip")
    fip_id = row.get("_fip_id") or ""
    progress(f"当前公网 IP:{old_ip or '无'}")

    nic_id = (row.get("vnic_id") or "")
    if not nic_id:
        raise ProviderError("未找到主网卡")

    if fip_id:
        try:
            progress(f"解绑并释放浮动 IP {old_ip} …")
            _req(acct, "DELETE", f"/floating_ips/{fip_id}")
        except ProviderError as e:
            progress(f"释放旧浮动 IP 失败(继续创建新 IP):{e}")

    progress("申请新浮动 IP 并绑定 …")
    d = _req(acct, "POST", "/floating_ips",
             json_body={"name": f"fip-{row['name']}-{int(time.time()) % 100000}",
                        "target": {"id": nic_id}})
    new_ip = d.get("address", "")
    progress(f"✅ 新公网 IP:{new_ip}")
    return {"old_ip": old_ip, "new_ip": new_ip}


# ---------------------------------------------------------------- 测活诊断

def check_account(acct: dict) -> dict:
    ok, err, n = True, None, 0
    try:
        n = len(list_instances(acct))
    except ProviderError as e:
        ok, err = False, str(e)
    return {"provider": "ibm", "remote_ok": ok, "error": err,
            "region": (acct.get("region") or "").lower(),
            "instance_count": n if ok else None,
            "hint": "" if ok else "请检查 API Key、区域(如 us-south/eu-de/jp-tok)与 IAM 权限"}


# ---------------------------------------------------------------- 创建/终止实例

def ibm_meta(acct: dict) -> dict:
    """返回创建实例所需的 VPC/子网/镜像/Profile/密钥/可用区。"""
    d = {}
    for path, key in (("/vpcs", "vpcs"), ("/subnets", "subnets"),
                      ("/instance/profiles", "profiles"), ("/keys", "keys")):
        try:
            out, start = [], None
            while True:
                params = {"limit": 100}
                if start:
                    params["start"] = start
                r = _req(acct, "GET", path, params=params)
                out += r.get(key, [])
                start = (r.get("next") or {}).get("href", "")
                if not start:
                    break
                start = start.split("start=")[-1].split("&")[0]
            d[key] = out
        except ProviderError as e:
            d[key] = []
            log.warning("IBM 元数据 %s 获取失败:%s", path, e)

    # 镜像(公开+私有;公开可能非常多,取前 100 并尽量保留常用 Linux)
    try:
        imgs = []
        for vis in ("public", "private"):
            r = _req(acct, "GET", "/images", params={"limit": 50, "visibility": vis})
            imgs += r.get("images", [])
        # 按名称粗略排序:Ubuntu/Debian/CentOS/Windows 优先
        def sort_key(x):
            n = (x.get("name") or "").lower()
            for i, kw in enumerate(["ubuntu", "debian", "rocky", "centos", "almalinux", "windows"]):
                if kw in n:
                    return i
            return 99
        imgs.sort(key=lambda x: (sort_key(x), x.get("created_at", "")))
        d["images"] = imgs[:80]
    except ProviderError as e:
        d["images"] = []
        log.warning("IBM 镜像获取失败:%s", e)

    # 可用区
    try:
        zs = []
        for z in _req(acct, "GET", "/regions", params={"limit": 100}).get("regions", []):
            if z.get("name") == (acct.get("region") or "").lower():
                zs = [x.get("name", "") for x in z.get("endpoints", [])] or [z.get("name", "")]
        d["zones"] = zs or [(acct.get("region") or "").lower() + "-1"]
    except ProviderError:
        d["zones"] = [(acct.get("region") or "").lower() + "-1"]

    # 简化输出字段
    return {
        "vpcs": [{"id": x["id"], "name": x.get("name", "")} for x in d.get("vpcs", [])],
        "subnets": [{"id": x["id"], "name": x.get("name", ""),
                     "zone": (x.get("zone") or {}).get("name", ""),
                     "vpc": (x.get("vpc") or {}).get("id", "")} for x in d.get("subnets", [])],
        "images": [{"id": x["id"], "name": x.get("name", "")} for x in d.get("images", [])],
        "profiles": [{"name": x.get("name", ""),
                      "cpu": ((x.get("vcpu") or {}).get("count")),
                      "mem": x.get("memory", {}).get("value") if isinstance(x.get("memory"), dict) else x.get("memory"),
                      "label": x.get("name", "")} for x in d.get("profiles", [])],
        "keys": [{"id": x["id"], "name": x.get("name", "")} for x in d.get("keys", [])],
        "zones": d.get("zones", []),
    }


def create_instance(acct: dict, d: dict) -> dict:
    """创建 VPC 实例。

    d: {name, vpc_id, subnet_id, image_id, profile, key_id, zone}
    """
    name = str(d.get("name") or "").strip()
    if not name:
        raise ProviderError("实例名称不能为空")
    if not (d.get("vpc_id") and d.get("subnet_id") and d.get("image_id") and d.get("profile") and d.get("zone")):
        raise ProviderError("缺少必要参数(VPC/子网/镜像/Profile/可用区)")
    body = {
        "name": name,
        "vpc": {"id": d["vpc_id"]},
        "zone": {"name": d["zone"]},
        "profile": {"name": d["profile"]},
        "image": {"id": d["image_id"]},
        "primary_network_interface": {"subnet": {"id": d["subnet_id"]}},
    }
    if d.get("key_id"):
        body["keys"] = [{"id": d["key_id"]}]
    r = _req(acct, "POST", "/instances", json_body=body)
    return {"instance_id": r.get("id", ""), "name": r.get("name", name)}


def terminate_instance(acct: dict, instance_id: str) -> dict:
    """终止(删除)VPC 实例。"""
    _req(acct, "DELETE", f"/instances/{instance_id}")
    return {"ok": True}
