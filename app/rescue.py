"""实例救援系统:把故障实例的启动盘挂载到同可用域的健康实例上离线修复。

适用场景:SSH 连不上(改坏了 sshd/fstab/防火墙)、磁盘占满、密码/密钥丢失等。

发起救援(start):
  1 校验救援目标(必须 RUNNING、与故障实例同 AD、非自身)
  2 关停故障实例(软关机超时后自动强制关机)
  3 分离其启动盘
  4 以半虚拟化数据盘方式挂载到救援目标机
  5 记录救援会话(status=rescuing),用户经 Web SSH 在目标机上修复文件

完成还原(finish):
  1 从目标机分离启动盘(要求用户已 umount)
  2 以启动盘方式装回原实例
  3 开机原实例,会话标记 restored

会话期间守护中心的保活不会拉起处于救援中的实例(启动盘已卸下)。
"""
from __future__ import annotations

import logging
import os
import time

from .database import db
from .oci_client import OciError, _ad_short, _client, _sdk, build_config

log = logging.getLogger("rescue")

# 轮询间隔(测试时可调小);各阶段超时(秒)
_POLL = float(os.getenv("RESCUE_POLL", "5"))
_T_STOP_SOFT = 240
_T_STOP_FORCE = 240
_T_DETACH = 300
_T_ATTACH = 300
_T_START = 600


# ---------------------------------------------------------------- 会话存取

def list_sessions(limit: int = 50) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT r.*, a.name AS account_name, a.region AS account_region "
            "FROM rescue_sessions r LEFT JOIN accounts a ON a.id=r.account_id "
            "ORDER BY r.id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: int) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM rescue_sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def rescuing_instance_ids(account_id: int | None = None) -> set[str]:
    """处于救援中(status=rescuing)的故障实例 OCID 集合;守护保活需跳过。"""
    q = "SELECT instance_id FROM rescue_sessions WHERE status='rescuing'"
    args: tuple = ()
    if account_id is not None:
        q += " AND account_id=?"
        args = (account_id,)
    with db() as c:
        return {r["instance_id"] for r in c.execute(q, args).fetchall()}


def forget_session(session_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM rescue_sessions WHERE id=?", (session_id,))


def _session_create(p: dict) -> int:
    with db() as c:
        cur = c.execute(
            "INSERT INTO rescue_sessions(account_id,compartment_id,instance_id,instance_name,"
            "ad,boot_volume_id,rescue_instance_id,rescue_instance_name,status)"
            " VALUES(?,?,?,?,?,?,?,?,'rescuing')",
            (p["account_id"], p["compartment_id"], p["instance_id"], p.get("instance_name") or "",
             p.get("ad") or "", p["boot_volume_id"], p["rescue_instance_id"],
             p.get("rescue_instance_name") or ""))
        return cur.lastrowid


def _session_status(session_id: int, status: str) -> None:
    with db() as c:
        c.execute("UPDATE rescue_sessions SET status=?, updated_at=datetime('now','localtime') "
                  "WHERE id=?", (status, session_id))


# ---------------------------------------------------------------- OCI 基础操作

def _clients(acct: dict):
    """返回 (oci 模块, ComputeClient, BlockstorageClient);测试可整体替换。"""
    oci = _sdk()
    cfg = build_config(acct)
    compute = _client(oci, oci.core.ComputeClient, cfg)
    bs = _client(oci, oci.core.BlockstorageClient, cfg)
    return oci, compute, bs


def _state(compute, instance_id: str) -> str:
    return (compute.get_instance(instance_id).data.lifecycle_state or "").upper()


def _wait_instance(compute, instance_id: str, targets: set[str], cb,
                   timeout: int, what: str) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            cur = _state(compute, instance_id)
        except Exception as e:  # noqa: BLE001
            raise OciError(f"查询实例状态失败:{e}") from e
        if cur != last:
            cb(f"{what}:{cur}")
            last = cur
        if cur in targets:
            return cur
        time.sleep(_POLL)
    raise OciError(f"等待实例{what}超时({timeout}s),请稍后到控制台确认")


def _boot_attachment(compute, compartment_id: str, ad: str | None, instance_id: str):
    kw = {"compartment_id": compartment_id, "instance_id": instance_id}
    if ad:
        kw["availability_domain"] = ad
    for att in compute.list_boot_volume_attachments(**kw).data:
        if (att.lifecycle_state or "").upper() in ("ATTACHED", "ATTACHING"):
            return att
    return None


def _attachment_state(compute, attachment_id: str, is_data: bool = False) -> str:
    try:
        if is_data:
            att = compute.get_volume_attachment(attachment_id).data
        else:
            att = compute.get_boot_volume_attachment(attachment_id).data
        return (att.lifecycle_state or "").upper()
    except Exception:  # noqa: BLE001  # 已删除的附件按 DETACHED 处理
        return "DETACHED"


def _data_attachments(compute, compartment_id: str, volume_id: str) -> list:
    out = []
    for att in compute.list_volume_attachments(compartment_id, volume_id=volume_id).data:
        if (att.lifecycle_state or "").upper() in ("ATTACHED", "ATTACHING"):
            out.append(att)
    return out


def _wait_attach(compute, attachment_id: str, targets: set[str], cb, timeout: int,
                 is_data: bool = False) -> str:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st = _attachment_state(compute, attachment_id, is_data=is_data)
        if st != last:
            if cb:
                cb(f"挂载状态:{st}")
            last = st
        if st in targets:
            return st
        time.sleep(_POLL)
    raise OciError("等待挂载状态变更超时,请稍后在 OCI 控制台确认")


def stop_instance_graceful(progress, compute, acct: dict, compartment_id: str,
                           instance_id: str, name: str = "") -> str:
    """软关机 → 超时强制关机,等待进入 STOPPED/SOFTSTOPPED。"""
    cur = _state(compute, instance_id)
    if cur in ("STOPPED", "SOFTSTOPPED"):
        progress(f"「{name}」已是停止状态({cur}),跳过关机")
        return cur
    label = f"「{name}」" if name else ""
    if cur != "RUNNING":
        progress(f"{label}当前状态 {cur},尝试关机…")
    else:
        progress(f"正在关停{label}(软关机,最长 {_T_STOP_SOFT}s)…")
        compute.instance_action(instance_id, compartment_id, action="SOFTSTOP")
        try:
            return _wait_instance(compute, instance_id, {"STOPPED", "SOFTSTOPPED"},
                                  lambda m: progress(m), _T_STOP_SOFT, "软关机")
        except OciError:
            progress("⚠ 软关机超时,转为强制关机…")
    progress("下发强制关机指令…")
    compute.instance_action(instance_id, compartment_id, action="STOP")
    return _wait_instance(compute, instance_id, {"STOPPED", "SOFTSTOPPED"},
                          lambda m: progress(m), _T_STOP_FORCE, "强制关机")


# ---------------------------------------------------------------- 元数据

def rescue_meta(acct: dict, compartment_id: str, instance_id: str) -> dict:
    """救援弹窗元数据:故障实例信息 + 同 AD 运行中的候选目标机。"""
    from .oci_client import list_instances
    ins_list = list_instances(acct)
    me = next((r for r in ins_list if r["id"] == instance_id), None)
    if not me:
        raise OciError("未在当前账户找到该实例,请刷新列表")
    targets = [
        {k: r[k] for k in ("id", "name", "public_ip", "private_ip", "shape",
                           "ocpus", "mem_gbs", "ad", "compartment_name")}
        for r in ins_list
        if r["id"] != instance_id and r["state"] == "RUNNING" and r["ad"] == me["ad"]
    ]
    return {
        "instance": {k: me.get(k) for k in
                     ("id", "name", "state", "ad", "shape", "ocpus", "mem_gbs",
                      "boot_gbs", "public_ip", "compartment_name")},
        "targets": targets,
    }


# ---------------------------------------------------------------- 发起 / 还原

_RESCUE_HINTS = [
    "① Web SSH 登录目标机,执行 lsblk 找到新磁盘(半虚拟化盘通常为 /dev/sdb)",
    "② 挂载:mount /dev/sdb1 /mnt (无分区则 mount /dev/sdb /mnt)",
    "③ 常见修复:chroot 改密码(/etc/shadow)、修 fstab、清理大文件、补 ssh authorized_keys",
    "④ 修复完成后务必 umount 全部挂载点,再回面板点「完成还原」",
]


def start_rescue(progress, acct: dict, p: dict) -> dict:
    """执行救援挂载流程。p: {compartment_id, instance_id, rescue_instance_id}。"""
    oci, compute, bs = _clients(acct)

    # ---- 校验 ----
    broken = compute.get_instance(p["instance_id"]).data
    target = compute.get_instance(p["rescue_instance_id"]).data
    if broken.id == target.id:
        raise OciError("救援目标不能是故障实例自身")
    b_ad, t_ad = broken.availability_domain or "", target.availability_domain or ""
    if b_ad != t_ad:
        raise OciError(f"启动盘属于 {b_ad.rsplit(':',1)[-1]},只能挂载到同可用域的实例"
                       f"(目标机在 {t_ad.rsplit(':',1)[-1]})")
    t_state = (target.lifecycle_state or "").upper()
    if t_state != "RUNNING":
        raise OciError(f"救援目标机未运行(当前 {t_state}),请先开机")
    comp = p["compartment_id"]

    att = _boot_attachment(compute, comp, b_ad, broken.id)
    if not att:
        raise OciError("未找到故障实例的启动盘附件")
    bv_id = att.boot_volume_id
    progress(f"故障实例:{broken.display_name},启动盘 {bv_id[:28]}…")

    # ---- 关停故障实例 ----
    stop_instance_graceful(progress, compute, acct, comp, broken.id,
                           broken.display_name or "")

    # ---- 分离启动盘 ----
    progress("分离启动盘…")
    try:
        compute.detach_boot_volume(att.id)
    except Exception as e:  # noqa: BLE001
        raise OciError(f"分离启动盘失败:{e}") from e
    deadline = time.time() + _T_DETACH
    while time.time() < deadline:
        st = _attachment_state(compute, att.id)
        if st == "DETACHED":
            break
        time.sleep(_POLL)
    else:
        raise OciError("等待启动盘分离超时")
    # 卷回到 AVAILABLE 再挂载,避免偶发 InvalidVolume
    vol_deadline = time.time() + 120
    while time.time() < vol_deadline:
        v = bs.get_boot_volume(bv_id).data
        if (v.lifecycle_state or "").upper() == "AVAILABLE":
            break
        time.sleep(_POLL)
    progress("✅ 启动盘已从故障实例分离")

    # ---- 挂载到救援目标机(半虚拟化数据盘)----
    progress(f"将启动盘挂载到救援目标机:{target.display_name} …")
    details = oci.core.models.AttachParavirtualizedVolumeDetails(
        availability_domain=t_ad, compartment_id=comp, instance_id=target.id,
        volume_id=bv_id, display_name=f"rescue-{(broken.display_name or 'disk')[:40]}")
    try:
        new_att = compute.attach_volume(details).data
    except Exception as e:  # noqa: BLE001
        raise OciError(f"挂载到目标机失败:{e}"
                       "(若提示不允许将启动盘挂为数据盘,可改用「从启动盘开机」方式救援)") from e
    _wait_attach(compute, new_att.id, {"ATTACHED"}, None, _T_ATTACH, is_data=True)
    progress("✅ 已作为数据盘挂载到目标机")

    sid = _session_create({
        "account_id": acct["id"], "compartment_id": comp,
        "instance_id": broken.id, "instance_name": broken.display_name or "",
        "ad": _ad_short(b_ad), "boot_volume_id": bv_id,
        "rescue_instance_id": target.id,
        "rescue_instance_name": target.display_name or "",
    })
    progress("")
    progress("🛟 救援环境就绪!请在目标机上修复原系统盘:")
    for h in _RESCUE_HINTS:
        progress(h)
    progress(f"(救援会话 #{sid} 已记录,修复完成后点「完成还原」)")
    return {"session_id": sid, "boot_volume_id": bv_id}


def finish_rescue(progress, acct: dict, session_id: int) -> dict:
    """从目标机取回启动盘、装回原实例并开机。"""
    sess = get_session(session_id)
    if not sess:
        raise OciError("救援会话不存在或已被删除")
    if sess["status"] != "rescuing":
        raise OciError(f"会话状态为 {sess['status']},无需重复还原")

    oci, compute, bs = _clients(acct)
    bv_id, orig_id = sess["boot_volume_id"], sess["instance_id"]
    comp, target_id = sess["compartment_id"], sess["rescue_instance_id"]
    orig = compute.get_instance(orig_id).data
    o_ad = orig.availability_domain or ""
    o_state = (orig.lifecycle_state or "").upper()

    # ---- 先校验原实例仍处于关机(任何变更动作之前)----
    if o_state not in ("STOPPED", "SOFTSTOPPED"):
        raise OciError(f"原实例当前为 {o_state},装回启动盘前必须保持关机;"
                       "若它已被其他方式拉起,请先关机再重试")

    # ---- 从目标机分离 ----
    atts = _data_attachments(compute, comp, bv_id)
    on_target = [a for a in atts if a.instance_id == target_id]
    others = [a for a in atts if a.instance_id != target_id]
    if others:
        raise OciError("该启动盘被挂载在未知实例上,请先到控制台检查")
    if on_target:
        progress("提醒:请确认已在目标机内 umount 该盘,否则可能有数据未写回!")
        progress("从救援目标机分离启动盘…")
        try:
            compute.detach_volume(on_target[0].id)
        except Exception as e:  # noqa: BLE001
            raise OciError(f"从目标机分离失败:{e}") from e
        deadline = time.time() + _T_DETACH
        while time.time() < deadline:
            st = _attachment_state(compute, on_target[0].id, is_data=True)
            if st == "DETACHED":
                break
            time.sleep(_POLL)
        else:
            raise OciError("等待分离超时,请稍后重试")
        vol_deadline = time.time() + 120
        while time.time() < vol_deadline:
            v = bs.get_boot_volume(bv_id).data
            if (v.lifecycle_state or "").upper() == "AVAILABLE":
                break
            time.sleep(_POLL)
        progress("✅ 启动盘已从目标机分离")
    else:
        progress("检测到启动盘未挂在目标机上(可能已手动分离),继续装回")

    # ---- 装回原实例 ----
    progress("把启动盘装回原实例…")
    details = oci.core.models.AttachBootVolumeDetails(
        availability_domain=o_ad, compartment_id=comp,
        instance_id=orig_id, boot_volume_id=bv_id)
    boot_att = compute.attach_boot_volume(details).data
    _wait_attach(compute, boot_att.id, {"ATTACHED"}, None, _T_ATTACH)
    progress("✅ 启动盘已装回")

    # ---- 开机 ----
    progress("启动原实例…")
    compute.instance_action(orig_id, comp, action="START")
    _wait_instance(compute, orig_id, {"RUNNING", "STARTING"},
                   lambda m: progress(m), _T_START, "启动")
    _session_status(session_id, "restored")
    progress("🎉 救援完成,实例已恢复运行!")
    ip = None
    try:
        from .oci_client import net_info
        ip = net_info(acct, comp, orig_id).get("public_ip")
    except Exception:  # noqa: BLE001
        pass
    return {"instance_id": orig_id, "public_ip": ip}
