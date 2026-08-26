"""跨提供商电源操作:供 TG Bot / 定时任务 / 未来自动化共用。"""
from __future__ import annotations

from . import aws_cloud, oci_client
from .database import db
from .pcreds import ProviderError

MAPS = {
    "start":  {"oci": "START",    "aws": "START", "aws_ls": "START",  "ibm": "start",  "gcp": "start",  "label": "开机"},
    "stop":   {"oci": "SOFTSTOP", "aws": "STOP",  "aws_ls": "STOP",   "ibm": "stop",   "gcp": "stop",   "label": "关机"},
    "reboot": {"oci": "RESET",    "aws": "RESET", "aws_ls": "REBOOT", "ibm": "reboot", "gcp": "reset",  "label": "重启"},
}


def get_account(account_id: int) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def power_op(row: dict, action: str) -> str:
    """对实例行(dict,含 account_id/provider/id/compartment_id/service/region)执行电源操作。

    返回动作中文名;失败抛 ProviderError。
    """
    if action not in MAPS:
        raise ProviderError(f"不支持的动作:{action}")
    m = MAPS[action]
    acct = get_account(row.get("account_id") or 0)
    if not acct:
        raise ProviderError("实例所属账户不存在")
    p = (row.get("provider") or "oci").lower()
    if p == "oci":
        oci_client.instance_op(acct, row.get("compartment_id") or "", row["id"], m["oci"])
    elif p == "aws":
        if row.get("service") == "lightsail":
            aws_cloud.lightsail_op(acct, row.get("region") or "", row["id"], m["aws_ls"])
        else:
            aws_cloud.instance_op(acct, row["id"], m["aws"])
    elif p == "ibm":
        from . import ibm_cloud
        ibm_cloud.instance_op(acct, row["id"], m["ibm"])
    elif p == "gcp":
        from . import gcp_cloud
        gcp_cloud.instance_op(acct, row.get("name") or "", row.get("_zone") or "", m["gcp"])
    else:
        raise ProviderError(f"该类型({p})不支持远程电源操作")
    try:
        from .routers.instances import _invalidate_cache
        _invalidate_cache(acct["id"])
    except Exception:  # noqa: BLE001
        pass
    return m["label"]
