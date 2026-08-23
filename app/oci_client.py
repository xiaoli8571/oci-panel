"""OCI SDK 封装:实例 / 创建 / 网络(IPv4·IPv6·保留IP·安全组) / 卷 / 配额订阅。

所有公开函数第一个参数为数据库中的账户行(dict);
需要长时间等待的函数第一个参数为 progress 日志回调(jobs.start_job 注入)。

性能说明(v0.10.0):
- SDK 客户端按「账户凭据指纹」缓存复用,避免每次请求重复解密私钥/解析 PEM;
- 实例列表的 VNIC / 启动盘 / 保留IP 判定按线程池并行查询;
- 配额、流量等多次独立 API 调用并行发出。
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import logging
import time

from . import config
from .ttlcache import TTLCache

log = logging.getLogger("oci")

ALLOWED_OPS = {"START", "SOFTSTOP", "STOP", "RESET", "SOFTRESET"}

# 常用形状预设(对齐 R探长 的快速配置)
PRESETS = {
    "amd": {"shape": "VM.Standard.E2.1.Micro", "label": "AMD 微型 1C/1G"},
    "arm": {"shape": "VM.Standard.A1.Flex", "label": "ARM A1 2C/12G"},
}

VPU_LABEL = {10: "Balanced(平衡)", 20: "Better(较高)", 120: "Ultra(极高)"}


class OciError(RuntimeError):
    """对用户友好的 OCI 调用异常。"""


def _sdk():
    try:
        import oci  # 延迟导入,加快面板启动
    except ImportError as e:
        raise OciError("服务器未安装 oci-sdk,请执行 pip install oci") from e
    return oci


def build_config(acct: dict) -> dict:
    from . import security
    return {
        "user": acct["user_ocid"],
        "tenancy": acct["tenancy_ocid"],
        "region": acct["region"],
        "fingerprint": acct["fingerprint"],
        "key_content": security.decrypt(acct["private_key_enc"]),
    }


# ---- SDK 客户端缓存:key = (类名, 凭据摘要)。凭据变更后摘要不同即自动失效。 ----
_SDK_CLIENTS: TTLCache = TTLCache(ttl=600.0, max_items=64)


def _cfg_key(cfg: dict) -> str:
    raw = "|".join((cfg.get("user") or "", cfg.get("tenancy") or "",
                    cfg.get("region") or "", cfg.get("fingerprint") or "",
                    hashlib.sha256((cfg.get("key_content") or "").encode()).hexdigest()[:16]))
    return hashlib.md5(raw.encode()).hexdigest()


def _client(oci, cls, cfg):
    """统一创建 SDK 客户端(带缓存):禁用自动重试并设置超时,保证面板响应迅速、失败可见。"""
    key = (cls.__name__, _cfg_key(cfg))
    cli = _SDK_CLIENTS.get(key)
    if cli is None:
        cli = cls(cfg, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(10, 60))
        _SDK_CLIENTS.set(key, cli)
    return cli


def _wrap(e: Exception) -> OciError:
    if isinstance(e, OciError):
        return e
    status = getattr(e, "status", None)
    code = getattr(e, "code", "")
    msg = getattr(e, "message", None) or str(e)
    if status or code:
        hint = ""
        if status == 401:
            hint = "(请检查 API Key / fingerprint / tenancy / user OCID 是否正确)"
        elif status == 404:
            hint = "(资源不存在或无权限访问该区间)"
        if "Out of host capacity" in msg:
            hint += "(当前区域 ARM 容量不足)"
        return OciError(f"OCI 接口错误 [{status} {code}] {msg} {hint}".strip())
    return OciError(f"OCI 调用失败:{msg}")


def _is_capacity_err(e: Exception) -> bool:
    return "Out of host capacity" in str(getattr(e, "message", "") or e)


# ================================================================ 基础元数据

def _iter_compartments(identity, tenancy_id: str) -> list[tuple[str, str]]:
    comps = [(tenancy_id, "root(租户根区间)")]
    for c in identity.list_compartments(
        tenancy_id, compartment_id_in_subtree=True, access_level="ACCESSIBLE"
    ).data:
        if (c.lifecycle_state or "").upper() == "ACTIVE":
            comps.append((c.id, c.name))
    return comps


def list_compartments(acct: dict) -> list[dict]:
    try:
        oci = _sdk()
        cfg = build_config(acct)
        identity = _client(oci, oci.identity.IdentityClient, cfg)
        return [{"id": i, "name": n} for i, n in _iter_compartments(identity, cfg["tenancy"])]
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def list_ads(acct: dict) -> list[str]:
    try:
        oci = _sdk()
        cfg = build_config(acct)
        identity = _client(oci, oci.identity.IdentityClient, cfg)
        return [ad.name for ad in identity.list_availability_domains(cfg["tenancy"]).data]
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def list_platform_images(acct: dict, compartment_id: str, shape: str, os_name: str) -> list[dict]:
    """按形状与操作系统列出官方平台镜像(新→旧)。"""
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        imgs = oci.pagination.list_call_get_all_results(
            compute.list_images, compartment_id,
            operating_system=os_name, shape=shape,
            sort_by="TIMECREATED", sort_order="DESC",
        ).data[:40]
        return [{
            "id": im.id,
            "label": f"{im.operating_system} {im.operating_system_version} · {im.display_name}",
        } for im in imgs]
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def list_public_subnets(acct: dict, compartment_id: str) -> list[dict]:
    try:
        oci = _sdk()
        net = _client(oci, oci.core.VirtualNetworkClient, build_config(acct))
        out = []
        for s in net.list_subnets(compartment_id).data:
            if (s.lifecycle_state or "").upper() != "AVAILABLE":
                continue
            if s.prohibit_public_ip_on_vnic:
                continue
            out.append({"id": s.id, "name": s.display_name or s.id[-12:], "cidr": s.cidr_block})
        return out
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 创建实例(含强开 ARM)

def _launch_once(oci, compute, d) -> object:
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=d["ad"],
        compartment_id=d["compartment_id"],
        display_name=d["name"],
        shape=d["shape"],
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=d.get("ocpus"), memory_in_gbs=d.get("mem_gbs"),
        ) if d.get("ocpus") else None,
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=d["image_id"], boot_volume_size_in_gbs=d.get("boot_gbs"),
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=d["subnet_id"], assign_public_ip=True, display_name=f"{d['name']}-vnic",
        ),
        metadata={"ssh_authorized_keys": d["ssh_key"]},
        is_pv_encryption_in_transit_enabled=True,
    )
    return compute.launch_instance(details).data


def create_instance(progress, acct: dict, d: dict) -> dict:
    """创建实例;A1 形状支持容量不足自动重试(强开)。返回最终公网 IP。"""
    st = None
    try:
        oci = _sdk()
        cfg = build_config(acct)
        compute = _client(oci, oci.core.ComputeClient, cfg)

        attempts = max(int(d.get("retry_attempts") or 1), 1)
        delay = min(int(d.get("retry_delay") or 45), 300)
        ins = None
        for i in range(1, attempts + 1):
            try:
                progress(f"[第{i}/{attempts}次] 提交创建请求({d['shape']}) …")
                ins = _launch_once(oci, compute, d)
                break
            except Exception as e:  # noqa: BLE001
                if _is_capacity_err(e) and i < attempts:
                    progress(f"容量不足(Out of host capacity),{delay}s 后自动重试 …")
                    time.sleep(delay)
                    continue
                raise

        iid = ins.id
        progress(f"实例已提交:{iid}")
        progress("等待实例进入 RUNNING(最长 15 分钟)…")
        deadline = time.time() + 900
        while time.time() < deadline:
            st = (compute.get_instance(iid).data.lifecycle_state or "").upper()
            progress(f"状态:{st}")
            if st in ("RUNNING", "FAILED", "TERMINATED"):
                break
            time.sleep(10)

        net = _client(oci, oci.core.VirtualNetworkClient, cfg)
        ip = None
        for att in compute.list_vnic_attachments(d["compartment_id"], instance_id=iid).data:
            if att.lifecycle_state == "ATTACHED":
                v = net.get_vnic(att.vnic_id).data
                if getattr(v, "is_primary", False):
                    ip = v.public_ip
                    break
        progress(f"✅ 创建完成,公网 IP:{ip or '(暂未分配)'}")
        return {"instance_id": iid, "public_ip": ip, "state": st}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 实例列表 / 电源 / 属性

def _ad_short(ad: str | None) -> str:
    return (ad or "").rsplit(":", 1)[-1]


def list_instances(acct: dict) -> list[dict]:
    """枚举该账户(该区域)下所有活动区间中的实例。"""
    try:
        return _list_instances_impl(acct)
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def _enrich_instance(oci, cfg, comp_id: str, comp_name: str, ins, acct: dict) -> dict:
    """查询单台实例的 VNIC/IP/启动盘详情(供线程池并行调用)。"""
    region = cfg["region"]
    sc = ins.shape_config
    row = {
        "account_id": acct["id"],
        "account_name": acct["name"],
        "region": region,
        "compartment_id": comp_id,
        "compartment_name": comp_name,
        "id": ins.id,
        "name": ins.display_name,
        "state": (ins.lifecycle_state or "").upper(),
        "shape": ins.shape,
        "ocpus": getattr(sc, "ocpus", None) if sc else None,
        "mem_gbs": getattr(sc, "memory_in_gbs", None) if sc else None,
        "boot_gbs": None,
        "boot_volume_id": None,
        "ad": _ad_short(ins.availability_domain),
        "public_ip": None,
        "public_lifetime": None,
        "private_ip": None,
        "vnic_id": None,
        "time_created": (ins.time_created.strftime("%Y-%m-%d %H:%M") if ins.time_created else ""),
    }

    compute = _client(oci, oci.core.ComputeClient, cfg)
    net = _client(oci, oci.core.VirtualNetworkClient, cfg)

    # 主 VNIC 与公网 IP(区分临时/保留)
    try:
        for att in oci.pagination.list_call_get_all_results(
            compute.list_vnic_attachments, comp_id, instance_id=ins.id
        ).data:
            if att.lifecycle_state != "ATTACHED":
                continue
            v = net.get_vnic(att.vnic_id).data
            if getattr(v, "is_primary", False):
                row["public_ip"] = v.public_ip
                row["private_ip"] = v.private_ip
                row["vnic_id"] = v.id
                if v.public_ip:
                    try:
                        pub = net.get_public_ip_by_ip_address(
                            oci.core.models.GetPublicIpByIpAddressDetails(ip_address=v.public_ip)
                        ).data
                        row["public_lifetime"] = pub.lifetime if pub else None
                    except Exception:  # noqa: BLE001
                        pass
                break
    except Exception as e:  # noqa: BLE001
        log.warning("查询实例 %s VNIC 失败:%s", ins.display_name, e)

    # 启动盘
    try:
        boots = compute.list_boot_volume_attachments(
            compartment_id=comp_id,
            availability_domain=ins.availability_domain,
            instance_id=ins.id,
        ).data
        if boots:
            bs = _client(oci, oci.core.BlockstorageClient, cfg)
            bv = bs.get_boot_volume(boots[0].boot_volume_id).data
            row["boot_gbs"] = bv.size_in_gbs
            row["boot_volume_id"] = bv.id
    except Exception as e:  # noqa: BLE001
        log.debug("查询实例 %s 启动盘失败:%s", ins.display_name, e)

    return row


def _list_instances_impl(acct: dict) -> list[dict]:
    oci = _sdk()
    cfg = build_config(acct)
    identity = _client(oci, oci.identity.IdentityClient, cfg)

    # 1) 并行列举各区间中的实例
    comps = _iter_compartments(identity, cfg["tenancy"])

    def _list_comp(comp: tuple[str, str]):
        comp_id, comp_name = comp
        try:
            instances = oci.pagination.list_call_get_all_results(
                _client(oci, oci.core.ComputeClient, cfg).list_instances, comp_id
            ).data
            return [(comp_id, comp_name, i) for i in instances
                    if (i.lifecycle_state or "").upper() != "TERMINATED"]
        except Exception as e:  # noqa: BLE001
            log.warning("列举区间 %s 失败:%s", comp_name, e)
            return []

    with cf.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        comp_results = list(ex.map(_list_comp, comps))
    flat = [t for lst in comp_results for t in lst]

    # 2) 并行补全每台实例的 VNIC/IP/启动盘
    with cf.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
        futs = [ex.submit(_enrich_instance, oci, cfg, c, n, ins, acct)
                for c, n, ins in flat]
        rows = [f.result() for f in futs]

    rows.sort(key=lambda r: (r["account_name"], r["region"], r["name"]))
    return rows


def instance_op(acct: dict, compartment_id: str, instance_id: str, op: str) -> dict:
    if op not in ALLOWED_OPS:
        raise OciError(f"不支持的操作:{op}")
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        compute.instance_action(instance_id, compartment_id, action=op)
        return {"started": True, "op": op}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def rename_instance(acct: dict, instance_id: str, display_name: str) -> dict:
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        compute.update_instance(instance_id, oci.core.models.UpdateInstanceDetails(
            display_name=display_name))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def resize_instance(acct: dict, instance_id: str, ocpus: float, mem_gbs: float) -> dict:
    """升降配(A1.Flex 等弹性形状要求先关机)。"""
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        compute.update_instance(instance_id, oci.core.models.UpdateInstanceDetails(
            shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                ocpus=ocpus, memory_in_gbs=mem_gbs)))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def enable_monitoring_plugin(acct: dict, instance_id: str) -> dict:
    """开启 Oracle Cloud Agent 的 Compute Instance Monitoring(流量统计依赖它)。"""
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        compute.update_instance(instance_id, oci.core.models.UpdateInstanceDetails(
            agent_config=oci.core.models.UpdateInstanceAgentConfigDetails(
                plugins_config=[oci.core.models.InstanceAgentPluginConfigDetails(
                    name="Compute Instance Monitoring",
                    desired_state="ENABLED")])))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def terminate_instance(acct: dict, instance_id: str, preserve_boot_volume: bool) -> dict:
    try:
        oci = _sdk()
        compute = _client(oci, oci.core.ComputeClient, build_config(acct))
        compute.terminate_instance(instance_id, preserve_boot_volume=preserve_boot_volume)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 更换公网 IP

def _wait_state(compute, instance_id: str, targets: set[str], cb, timeout: int = 420):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        cur = (compute.get_instance(instance_id).data.lifecycle_state or "").upper()
        if cur != last:
            cb(f"实例状态:{cur}")
            last = cur
        if cur in targets:
            return cur
        time.sleep(5)
    raise OciError("等待实例状态变更超时,请稍后到控制台确认")


def _primary_vnic(oci, cfg, compartment_id: str, instance_id: str):
    """查主 VNIC:附件列表来自 Compute API,VNIC 详情来自 Network API。"""
    compute = _client(oci, oci.core.ComputeClient, cfg)
    net = _client(oci, oci.core.VirtualNetworkClient, cfg)
    for att in compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data:
        if att.lifecycle_state == "ATTACHED":
            v = net.get_vnic(att.vnic_id).data
            if getattr(v, "is_primary", False):
                return v
    raise OciError("未找到实例的主 VNIC")


def change_public_ip(progress, acct: dict, compartment_id: str, instance_id: str) -> dict:
    """更换临时公网 IP:运行中则先软关机 → 删除旧临时IP → 开机获得新IP。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        compute = _client(oci, oci.core.ComputeClient, cfg)
        net = _client(oci, oci.core.VirtualNetworkClient, cfg)

        orig = (compute.get_instance(instance_id).data.lifecycle_state or "").upper()
        if orig == "RUNNING":
            progress("实例运行中,先软关机…")
            compute.instance_action(instance_id, compartment_id, action="SOFTSTOP")
            _wait_state(compute, instance_id, {"STOPPED"}, progress)
        else:
            progress(f"实例当前状态 {orig},无需关机")

        vnic = _primary_vnic(oci, cfg, compartment_id, instance_id)
        old_ip = vnic.public_ip
        progress(f"当前公网 IP:{old_ip or '无'}")

        if old_ip:
            try:
                pub = net.get_public_ip_by_ip_address(
                    oci.core.models.GetPublicIpByIpAddressDetails(ip_address=old_ip)
                ).data
                if pub and pub.lifetime == "EPHEMERAL":
                    net.delete_public_ip(pub.id)
                    progress(f"已释放临时公网 IP {old_ip}")
                elif pub:
                    progress("检测到保留公网 IP,跳过删除(如需更换请先解绑)")
            except Exception as e:  # noqa: BLE001
                progress(f"查询/释放公网 IP 时出现问题:{e}")

        if orig in ("RUNNING", "STARTING"):
            progress("重新启动实例以获取新 IP …")
            compute.instance_action(instance_id, compartment_id, action="START")
            _wait_state(compute, instance_id, {"RUNNING"}, progress)

        # RUNNING 后公网 IP 分配可能稍有延迟,轮询等待(最多 ~30s)
        new_ip = None
        for _ in range(6):
            new_ip = net.get_vnic(vnic.id).data.public_ip
            if new_ip:
                break
            time.sleep(5)
        progress(f"新公网 IP:{new_ip}")
        return {"old_ip": old_ip, "new_ip": new_ip}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ IPv6

def net_info(acct: dict, compartment_id: str, instance_id: str) -> dict:
    """主 VNIC 网络信息:IPv4 类型、IPv6 地址列表、子网是否支持 IPv6。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        net = _client(oci, oci.core.VirtualNetworkClient, cfg)
        vnic = _primary_vnic(oci, cfg, compartment_id, instance_id)

        subnet = net.get_subnet(vnic.subnet_id).data
        ipv4_type = None
        if vnic.public_ip:
            pub = net.get_public_ip_by_ip_address(
                oci.core.models.GetPublicIpByIpAddressDetails(ip_address=vnic.public_ip)).data
            ipv4_type = pub.lifetime if pub else None
        return {
            "vnic_id": vnic.id,
            "private_ip": vnic.private_ip,
            "public_ip": vnic.public_ip,
            "public_lifetime": ipv4_type,
            "ipv6_addresses": list(getattr(vnic, "ipv6_addresses", None) or []),
            "subnet_ipv6_ready": bool(getattr(subnet, "ipv6_cidr_blocks", None)),
            "subnet_name": subnet.display_name or subnet.id[-12:],
        }
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def add_ipv6(acct: dict, compartment_id: str, instance_id: str) -> dict:
    """为主 VNIC 附加一个 IPv6(若子网启用 IPv6 且 VCN 有互联网关,则同时分配公网 IPv6)。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        net = _client(oci, oci.core.VirtualNetworkClient, cfg)
        vnic = _primary_vnic(oci, cfg, compartment_id, instance_id)
        res = net.create_ipv6(vnic.id, oci.core.models.CreateIpv6Details(
            vnic_subnet_id=vnic.subnet_id, is_public_ip_enabled=True)).data
        return {"ipv6": getattr(res, "ip_address", None),
                "public_ipv6": getattr(res, "public_ip_address", None)}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 保留公网 IP

def list_reserved_ips(acct: dict, compartment_id: str) -> list[dict]:
    try:
        oci = _sdk()
        net = _client(oci, oci.core.VirtualNetworkClient, build_config(acct))
        out = []
        for p in oci.pagination.list_call_get_all_results(
            net.list_public_ips, "REGION", compartment_id=compartment_id,
            lifetime="RESERVED",
        ).data:
            out.append({
                "id": p.id, "ip_address": p.ip_address,
                "assigned": bool(getattr(p, "private_ip_id", None)),
            })
        return out
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def create_reserved_ip(acct: dict, compartment_id: str) -> dict:
    try:
        oci = _sdk()
        net = _client(oci, oci.core.VirtualNetworkClient, build_config(acct))
        p = net.create_public_ip(oci.core.models.CreatePublicIpDetails(
            compartment_id=compartment_id, lifetime="RESERVED")).data
        return {"id": p.id, "ip_address": p.ip_address}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def delete_reserved_ip(acct: dict, public_ip_id: str) -> dict:
    try:
        oci = _sdk()
        net = _client(oci, oci.core.VirtualNetworkClient, build_config(acct))
        net.delete_public_ip(public_ip_id)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def bind_reserved_ip(acct: dict, public_ip_id: str, vnic_id: str, bind: bool) -> dict:
    """绑定/解绑 保留IP 到 VNIC 主私网 IP。"""
    try:
        oci = _sdk()
        net = _client(oci, oci.core.VirtualNetworkClient, build_config(acct))
        priv_ip_id = None
        if bind:
            ips = oci.pagination.list_call_get_all_results(
                net.list_private_ips, vnic_id=vnic_id).data
            primary = next((i for i in ips if getattr(i, "is_primary", False)), None) or (
                ips[0] if ips else None)
            if not primary:
                raise OciError("未找到 VNIC 的私网 IP")
            priv_ip_id = primary.id
        net.update_public_ip(public_ip_id, oci.core.models.UpdatePublicIpDetails(
            private_ip_id=priv_ip_id))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 安全组端口

def open_ports(acct: dict, compartment_id: str, instance_id: str, ports: list[int]) -> dict:
    """向主 VNIC 所在子网的全部安全列表追加 TCP 入站规则(全网段),已存在则跳过。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        net = _client(oci, oci.core.VirtualNetworkClient, cfg)
        vnic = _primary_vnic(oci, cfg, compartment_id, instance_id)
        subnet = net.get_subnet(vnic.subnet_id).data

        added, skipped = [], []

        def _has(rules, port, src):
            for r in rules:
                if r.protocol == "6" and r.source == src:
                    rng = getattr(r.tcp_options, "destination_port_range", None) if r.tcp_options else None
                    if rng is None:
                        return True  # 全端口放行
                    lo, hi = getattr(rng, "min", None), getattr(rng, "max", None)
                    if lo is not None and hi is not None and lo <= port <= hi:
                        return True
            return False

        for sl_id in subnet.security_list_ids or []:
            sl = net.get_security_list(sl_id).data
            rules = list(sl.ingress_security_rules or [])
            new_rules = []
            for port in ports:
                for src in ("0.0.0.0/0", "::/0"):
                    if _has(rules, port, src):
                        skipped.append(f"{port}/{'v6' if ':' in src else 'v4'}")
                        continue
                    new_rules.append(oci.core.models.IngressSecurityRule(
                        protocol="6", source=src, source_type="CIDR_BLOCK",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port))))
                    added.append(f"{port}/{'v6' if ':' in src else 'v4'}")
            if new_rules:
                net.update_security_list(sl_id, oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=rules + new_rules))
        return {"added": added, "skipped": skipped}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 卷(启动盘)

def get_boot_volume(acct: dict, compartment_id: str, instance_id: str) -> dict:
    try:
        oci = _sdk()
        cfg = build_config(acct)
        compute = _client(oci, oci.core.ComputeClient, cfg)
        boots = compute.list_boot_volume_attachments(
            compartment_id=compartment_id, instance_id=instance_id).data
        if not boots:
            raise OciError("未找到启动盘附件")
        bs = _client(oci, oci.core.BlockstorageClient, cfg)
        bv = bs.get_boot_volume(boots[0].boot_volume_id).data
        return {"id": bv.id, "size_in_gbs": bv.size_in_gbs,
                "vpus_per_gb": bv.vpus_per_gb, "state": bv.lifecycle_state}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def update_boot_volume(acct: dict, boot_volume_id: str,
                       size_in_gbs: int | None = None, vpus_per_gb: int | None = None) -> dict:
    """扩容(只能增大)或调整 VPU 性能层级。"""
    try:
        oci = _sdk()
        bs = _client(oci, oci.core.BlockstorageClient, build_config(acct))
        kw = {}
        if size_in_gbs:
            kw["size_in_gbs"] = size_in_gbs
        if vpus_per_gb:
            kw["vpus_per_gb"] = vpus_per_gb
        if not kw:
            raise OciError("未指定任何修改项")
        bv = bs.update_boot_volume(boot_volume_id, oci.core.models.UpdateBootVolumeDetails(**kw)).data
        return {"id": bv.id, "size_in_gbs": bv.size_in_gbs, "vpus_per_gb": bv.vpus_per_gb}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 配额与订阅

_QUOTA_LIMIT_NAMES = [
    "standard-a1-core-count",
    "standard-a1-memory-count",
    "standard-e2-micro-count",
]


def account_quota(acct: dict) -> dict:
    """查询 compute 服务关键配额在各可用域的 用量/余量,以及订阅类型(FREE_TIER/PAYG)。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        limits = _client(oci, oci.limits.LimitsClient, cfg)
        identity = _client(oci, oci.identity.IdentityClient, cfg)
        tenancy = cfg["tenancy"]

        svc = next((s.name for s in limits.list_services(compartment_id=tenancy).data
                    if s.name == "compute"), None)
        if not svc:
            raise OciError("未找到 compute 服务限额")

        wanted = set()
        try:
            defs = oci.pagination.list_call_get_all_results(
                limits.list_limit_definitions, tenancy, service_name=svc).data
        except Exception:  # noqa: BLE001
            defs = oci.pagination.list_call_get_all_results(
                limits.list_limit_definitions, tenancy).data
        for d in defs:
            if d.name in _QUOTA_LIMIT_NAMES:
                wanted.add((d.name, d.description))

        ads = [ad.name for ad in identity.list_availability_domains(tenancy).data]

        # 各限额 × 各 AD 的用量查询相互独立,并行发出(原来串行 3×AD 次调用)
        def _avail(pair: tuple[str, str, str]) -> tuple[str, str, dict]:
            lname, desc, ad = pair
            ra = limits.get_resource_availability(
                svc, lname, tenancy, availability_domain=ad).data
            return (lname, desc, {
                "ad": _ad_short(ad),
                "available": getattr(ra, "available", None),
                "used": getattr(ra, "used", None),
            })

        pairs = [(lname, desc, ad) for lname, desc in sorted(wanted) for ad in ads]
        with cf.ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as ex:
            results = list(ex.map(_avail, pairs))

        grouped: dict[str, dict] = {}
        for lname, desc, item in results:
            g = grouped.setdefault(lname, {"name": lname, "description": desc, "items": []})
            g["items"].append(item)
        limits_out = list(grouped.values())

        # 订阅类型(免费层 / PAYG):需先找 home region
        payment_model = None
        try:
            regions = identity.list_region_subscriptions(tenancy).data
            home = next((r.region_name for r in regions
                         if getattr(r, "is_home_region", False)), None)
            if home:
                sub_cli = _client(oci, oci.osp_gateway.SubscriptionServiceClient, cfg)
                subs = sub_cli.list_subscriptions(home, compartment_id=tenancy).data.items
                if subs:
                    payment_model = getattr(subs[0], "payment_model", None)
        except Exception as e:  # noqa: BLE001
            log.info("查询订阅类型失败(不影响配额展示):%s", e)

        return {"region": cfg["region"], "limits": limits_out, "payment_model": payment_model}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


# ================================================================ 流量统计

def traffic_usage(acct: dict, compartment_id: str, hours: int = 24) -> dict:
    """通过 oci_computeagent 监控指标统计区间内 VNIC 进/出流量(需实例启用监控插件)。"""
    try:
        return _traffic_impl(acct, compartment_id, min(max(hours, 1), 24 * 90))
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def _traffic_impl(acct: dict, compartment_id: str, hours: int) -> dict:
    oci = _sdk()
    mon = _client(oci, oci.monitoring.MonitoringClient, build_config(acct))

    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(hours=hours)
    # 按时间范围自适应聚合粒度,控制点数
    window = "1h" if hours <= 72 else ("6h" if hours <= 240 else "1d")

    def _query(metric: str):
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace="oci_computeagent",
            query=f"{metric}[{window}].sum()",
            start_time=start,
            end_time=end,
        )
        return mon.summarize_metrics_data(compartment_id, details).data

    series: dict[int, dict] = {}
    totals = {"down": 0.0, "up": 0.0}

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_down = ex.submit(_query, "VnicFromNetworkBytes")
        f_up = ex.submit(_query, "VnicToNetworkBytes")
        for key, fut in (("down", f_down), ("up", f_up)):
            for md in fut.result():
                for ts, val in zip(md.timestamps, md.aggregated_samples):
                    bucket = int(ts.timestamp())
                    slot = series.setdefault(bucket, {"t": ts.isoformat(), "down": 0.0, "up": 0.0})
                    slot[key] += float(val or 0)
                    totals[key] += float(val or 0)

    points = [series[k] for k in sorted(series)]
    return {"hours": hours, "window": window, "points": points,
            "total_down_bytes": totals["down"], "total_up_bytes": totals["up"]}


# ================================================================ 对象存储(Object Storage)

MAX_OSS_OBJECT = 50 * 1024 * 1024   # 单对象经面板中转的上限(与 SFTP 一致)


def _os_client(acct: dict):
    oci = _sdk()
    return _client(oci, oci.object_storage.ObjectStorageClient, build_config(acct)), oci


def get_namespace(acct: dict) -> str:
    try:
        cli, _ = _os_client(acct)
        ns = cli.get_namespace().data
        return ns if isinstance(ns, str) else getattr(ns, "value", str(ns))
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def list_buckets(acct: dict, compartment_id: str | None = None) -> list[dict]:
    """列出区间内全部对象存储桶。compartment_id 缺省用租户根。"""
    try:
        cli, _ = _os_client(acct)
        ns = get_namespace(acct)
        comp = compartment_id or acct["tenancy_ocid"]
        out = []
        for b in oci_pagination(cli.list_buckets, ns, compartment_id=comp).data:
            out.append({
                "name": b.name,
                "namespace": ns,
                "compartment_id": b.compartment_id,
                "created": (b.time_created.strftime("%Y-%m-%d") if b.time_created else ""),
                "storage_tier": getattr(b, "storage_tier", "") or "",
                "freeform_tags": getattr(b, "freeform_tags", None) or {},
            })
        return out
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def create_bucket(acct: dict, name: str, compartment_id: str | None = None,
                  tier: str = "Standard") -> dict:
    try:
        oci = _sdk()
        cli, _ = _os_client(acct)
        ns = get_namespace(acct)
        b = cli.create_bucket(
            ns, oci.object_storage.models.CreateBucketDetails(
                name=name, compartment_id=compartment_id or acct["tenancy_ocid"],
                public_access_type="NoPublicAccess", storage_tier=tier)).data
        return {"name": b.name}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def delete_bucket(acct: dict, bucket: str) -> dict:
    try:
        cli, _ = _os_client(acct)
        cli.delete_bucket(get_namespace(acct), bucket)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def list_objects(acct: dict, bucket: str, prefix: str = "", limit: int = 300) -> dict:
    try:
        cli, _ = _os_client(acct)
        ns = get_namespace(acct)
        res = cli.list_objects(ns, bucket, prefix=prefix or None,
                               fields="name,size,timeCreated", limit=min(max(limit, 1), 1000)).data
        objs = [{
            "name": o.name,
            "size": getattr(o, "size", None) or 0,
            "modified": (o.time_created.strftime("%Y-%m-%d %H:%M")
                         if getattr(o, "time_created", None) else ""),
        } for o in (res.objects or [])]
        return {"objects": objs, "prefixes": list(res.prefixes or []),
                "next_start_with": getattr(res, "next_start_with", None)}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def get_object_content(acct: dict, bucket: str, name: str) -> bytes:
    """读取对象内容(上限 MAX_OSS_OBJECT;供下载接口流式回传)。"""
    try:
        cli, _ = _os_client(acct)
        head = cli.head_object(get_namespace(acct), bucket, name)
        size = int(head.headers.get("content-length", 0))
        if size > MAX_OSS_OBJECT:
            raise OciError(f"文件超过 {MAX_OSS_OBJECT // 1024 // 1024}MB 上限,请用 oci 命令行下载")
        resp = cli.get_object(get_namespace(acct), bucket, name)
        return resp.data.content
    except OciError:
        raise
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def put_object_content(acct: dict, bucket: str, name: str, content: bytes) -> dict:
    if len(content) > MAX_OSS_OBJECT:
        raise OciError(f"超过 {MAX_OSS_OBJECT // 1024 // 1024}MB 上限")
    try:
        cli, _ = _os_client(acct)
        cli.put_object(get_namespace(acct), bucket, put_object_body=content,
                       object_name=name, content_length=len(content))
        return {"ok": True, "size": len(content)}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def delete_object(acct: dict, bucket: str, name: str) -> dict:
    try:
        cli, _ = _os_client(acct)
        cli.delete_object(get_namespace(acct), bucket, name)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e


def oci_pagination(fn, *args, **kw):
    """oci.pagination.list_call_get_all_results 的本地包装(避免模块顶部 import oci)。"""
    oci = _sdk()
    return oci.pagination.list_call_get_all_results(fn, *args, **kw)

def launch_from_boot_volume(progress, acct: dict, d: dict) -> dict:
    """用已有启动盘创建新实例(原实例须已终止且保留启动盘,或使用分离的启动盘)。

    d: {compartment_id, name, ad, subnet_id, boot_volume_id, ssh_key}
    """
    try:
        oci = _sdk()
        cfg = build_config(acct)
        compute = _client(oci, oci.core.ComputeClient, cfg)
        progress(f"提交从启动盘开机请求({d['boot_volume_id'][:24]}…) …")
        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=d["ad"],
            compartment_id=d["compartment_id"],
            display_name=d["name"],
            shape=d.get("shape") or "VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=d.get("ocpus"), memory_in_gbs=d.get("mem_gbs")
            ) if d.get("ocpus") else None,
            source_details=oci.core.models.InstanceSourceViaBootVolumeDetails(
                boot_volume_id=d["boot_volume_id"]),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=d["subnet_id"], assign_public_ip=True, display_name=f"{d['name']}-vnic"),
            metadata={"ssh_authorized_keys": d.get("ssh_key", "")},
            is_pv_encryption_in_transit_enabled=True,
        )
        ins = compute.launch_instance(details).data
        progress(f"实例已提交:{ins.id}")
        st = None
        deadline = time.time() + 600
        while time.time() < deadline:
            st = (compute.get_instance(ins.id).data.lifecycle_state or "").upper()
            if st in ("RUNNING", "FAILED", "TERMINATED"):
                break
            time.sleep(10)
        net = _client(oci, oci.core.VirtualNetworkClient, cfg)
        ip = None
        for att in compute.list_vnic_attachments(d["compartment_id"], instance_id=ins.id).data:
            if att.lifecycle_state == "ATTACHED":
                v = net.get_vnic(att.vnic_id).data
                if getattr(v, "is_primary", False):
                    ip = v.public_ip
                    break
        progress(f"✅ 开机完成,状态:{st},公网 IP:{ip or '(暂未分配)'}")
        return {"instance_id": ins.id, "public_ip": ip, "state": st}
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e

def list_available_boot_volumes(acct: dict, compartment_id: str) -> list[dict]:
    """列出区间内 AVAILABLE 状态的启动盘(含所属 AD 与大小)。"""
    try:
        oci = _sdk()
        cfg = build_config(acct)
        bs = _client(oci, oci.core.BlockstorageClient, cfg)
        out = []
        for bv in oci_pagination(bs.list_boot_volumes, compartment_id=compartment_id).data:
            if (bv.lifecycle_state or "").upper() != "AVAILABLE":
                continue
            out.append({
                "id": bv.id,
                "name": getattr(bv, "display_name", "") or bv.id[-12:],
                "size_in_gbs": bv.size_in_gbs,
                "ad": _ad_short(getattr(bv, "availability_domain", "")),
            })
        return out
    except Exception as e:  # noqa: BLE001
        raise _wrap(e) from e
