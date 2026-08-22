"""手动添加的 VPS 主机:任意云厂商的服务器统一纳管。

存储:vps_hosts 表(密码/私钥 Fernet 加密落盘)。
能力:增删改查、SSH 测活(连接耗时)、远程重启、并入实例列表。
"""
from __future__ import annotations

import asyncio
import time

import asyncssh
from fastapi import APIRouter, HTTPException

from .. import security
from ..database import db
from ..pcreds import ProviderError
from ..schemas import VpsHostIn

router = APIRouter(prefix="/api/vps", tags=["vps"])


def _get_row(host_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM vps_hosts WHERE id=?", (host_id,)).fetchone()
    if not row:
        raise HTTPException(404, "VPS 主机不存在")
    return dict(row)


def _creds(row: dict) -> tuple[str, dict]:
    """返回 (明文secret, asyncssh 连接参数)。"""
    kw: dict = {"port": int(row["port"] or 22), "username": row["username"], "known_hosts": None}
    secret = ""
    if row["secret_enc"]:
        try:
            secret = security.decrypt(row["secret_enc"])
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"凭证解密失败:{e}")
    if (row["auth_type"] or "password") == "key":
        if secret:
            kw["client_keys"] = [asyncssh.import_private_key(secret)]
    else:
        kw["password"] = secret
    return secret, kw


def _public(row: dict, mask_secret: bool = True) -> dict:
    d = {
        "id": row["id"], "name": row["name"], "host": row["host"],
        "port": row["port"], "username": row["username"],
        "auth_type": row["auth_type"] or "password",
        "region": row["region"] or "", "note": row["note"] or "",
        "created_at": row["created_at"],
    }
    if mask_secret:
        d["has_secret"] = bool(row["secret_enc"])
        d["secret_hint"] = ("已保存" if row["secret_enc"] else "未设置")
    return d


@router.get("")
def list_vps():
    with db() as c:
        rows = c.execute("SELECT * FROM vps_hosts ORDER BY id").fetchall()
    return {"items": [_public(dict(r)) for r in rows]}


@router.post("")
def add_vps(body: VpsHostIn):
    if body.auth_type not in ("password", "key"):
        raise HTTPException(400, "auth_type 必须是 password 或 key")
    enc = security.encrypt(body.secret) if body.secret else ""
    with db() as c:
        cur = c.execute(
            "INSERT INTO vps_hosts(name,host,port,username,auth_type,secret_enc,region,note)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (body.name.strip(), body.host.strip(), body.port, body.username.strip(),
             body.auth_type, enc, body.region.strip(), body.note.strip()))
        new_id = cur.lastrowid
    return {"id": new_id}


@router.put("/{host_id}")
def update_vps(host_id: int, body: VpsHostIn):
    row = _get_row(host_id)
    enc = security.encrypt(body.secret) if body.secret else row["secret_enc"]
    with db() as c:
        c.execute(
            "UPDATE vps_hosts SET name=?,host=?,port=?,username=?,auth_type=?,secret_enc=?,region=?,note=?"
            " WHERE id=?",
            (body.name.strip(), body.host.strip(), body.port, body.username.strip(),
             body.auth_type, enc, body.region.strip(), body.note.strip(), host_id))
    return {"ok": True}


@router.delete("/{host_id}")
def delete_vps(host_id: int):
    _get_row(host_id)
    with db() as c:
        c.execute("DELETE FROM vps_hosts WHERE id=?", (host_id,))
    return {"ok": True}


# ---------- SSH 测活 ----------

@router.post("/{host_id}/test")
async def test_vps(host_id: int):
    row = _get_row(host_id)
    _, kw = _creds(row)
    t0 = time.time()
    try:
        conn = await asyncio.wait_for(asyncssh.connect(row["host"], **kw), timeout=15)
    except asyncio.TimeoutError:
        raise HTTPException(502, "连接超时(15 秒无响应)")
    except asyncssh.Error as e:
        raise HTTPException(502, f"SSH 错误:{getattr(e, 'reason', e)}")
    except OSError as e:
        raise HTTPException(502, f"无法连接:{e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"连接失败:{e}")
    try:
        res = await asyncio.wait_for(conn.run("echo ok"), timeout=10)
        ms = int((time.time() - t0) * 1000)
        out = (res.stdout or "").strip()
        return {"ok": out == "ok", "latency_ms": ms,
                "message": f"连接正常,命令回显成功({ms}ms)" if out == "ok" else f"已连上但回显异常:{out!r}"}
    finally:
        conn.close()


# ---------- 远程重启 ----------

@router.post("/{host_id}/reboot")
async def reboot_vps(host_id: int):
    row = _get_row(host_id)
    _, kw = _creds(row)
    try:
        conn = await asyncio.wait_for(asyncssh.connect(row["host"], **kw), timeout=15)
    except asyncio.TimeoutError:
        raise HTTPException(502, "连接超时")
    except (asyncssh.Error, OSError) as e:
        raise HTTPException(502, f"连接失败:{getattr(e, 'reason', e)}")
    try:
        cmd = "sudo -n systemctl reboot 2>/dev/null || sudo -n reboot 2>/dev/null || systemctl reboot || reboot"
        try:
            res = await asyncio.wait_for(conn.run(cmd), timeout=20)
            err = (res.stderr or "").strip()
            if res.exit_status not in (0, None) and err:
                raise HTTPException(502, f"重启指令被拒绝:{err[:200]}")
        except asyncssh.ProcessError as e:
            # 部分系统重启瞬间会话被切断,视为已下发
            pass
        except asyncssh.ConnectionLost:
            pass  # 连接因重启中断 = 指令已生效
        return {"ok": True, "message": "重启指令已下发"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------- SSH 凭据库(任意主机可保存凭据,与 VPS 登记解耦) ----------

def _cred_key(username: str, host: str, port: int) -> str:
    return f"{username}@{host}:{int(port or 22)}"


@router.get("/saved-cred")
def get_saved_cred(u: str, h: str, p: int = 22):
    """返回某主机已保存的凭据(明文,仅登录会话可取;前端填表用)。"""
    k = _cred_key(u, h, p)
    with db() as c:
        row = c.execute("SELECT username,auth_type,secret_enc FROM ssh_creds WHERE cred_key=?", (k,)).fetchone()
    if not row or not row["secret_enc"]:
        raise HTTPException(404, "该主机没有保存过凭据")
    try:
        secret = security.decrypt(row["secret_enc"])
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"凭据解密失败:{e}")
    return {"username": row["username"], "auth_type": row["auth_type"] or "password", "secret": secret}


@router.post("/saved-cred")
def save_cred(body: VpsHostIn):
    if body.auth_type not in ("password", "key"):
        raise HTTPException(400, "auth_type 必须是 password 或 key")
    if not body.secret.strip():
        raise HTTPException(400, "凭据内容不能为空")
    enc = security.encrypt(body.secret)
    k = _cred_key(body.username.strip(), body.host.strip(), body.port)
    with db() as c:
        c.execute(
            "INSERT INTO ssh_creds(cred_key,username,host,port,auth_type,secret_enc)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(cred_key) DO UPDATE SET auth_type=excluded.auth_type,"
            " secret_enc=excluded.secret_enc",
            (k, body.username.strip(), body.host.strip(), body.port, body.auth_type, enc))
    return {"ok": True}


@router.delete("/saved-cred")
def delete_cred(u: str, h: str, p: int = 22):
    with db() as c:
        c.execute("DELETE FROM ssh_creds WHERE cred_key=?", (_cred_key(u, h, p),))
    return {"ok": True}


@router.get("/saved-cred/list")
def list_saved_creds():
    """凭据清单(不含明文):供弹窗提示「该主机已有保存凭据」。"""
    with db() as c:
        rows = c.execute("SELECT cred_key,username,host,port,auth_type FROM ssh_creds ORDER BY host").fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------- 实例列表合并用的行构造 ----------

def list_vps_rows() -> list[dict]:
    """把手动 VPS 转成实例条目结构,供 /api/instances 合并。"""
    rows_out: list[dict] = []
    with db() as c:
        rows = c.execute("SELECT * FROM vps_hosts ORDER BY id").fetchall()
    for r in rows:
        h = dict(r)
        rows_out.append({
            "account_id": None,
            "vps_id": h["id"],
            "account_name": h["name"],
            "provider": "vps",
            "service": "vps",
            "region": h["region"] or "-",
            "compartment_id": "-",
            "compartment_name": "手动添加",
            "id": f"vps-{h['id']}",
            "name": h["name"],
            "state": "VPS",
            "shape": f"{h['username']}@{h['host']}:{h['port']}",
            "ocpus": None, "mem_gbs": None, "boot_gbs": None,
            "ad": h["note"] or "",
            "public_ip": h["host"],
            "public_lifetime": None,
            "private_ip": None,
            "vnic_id": None,
            "time_created": (h["created_at"] or "")[:16],
        })
    return rows_out
