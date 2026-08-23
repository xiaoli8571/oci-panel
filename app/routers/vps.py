"""手动添加的 VPS 主机:任意云厂商的服务器统一纳管。

存储:vps_hosts 表(密码/私钥 Fernet 加密落盘)。
能力:增删改查、SSH 测活(连接耗时)、远程重启、并入实例列表。
"""
from __future__ import annotations

import asyncio
import re
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


def _lookup_saved_cred(username: str, host: str, port: int):
    """从凭据库取明文凭据;返回 (auth_type, secret) 或 None。"""
    k = _cred_key(username, host, port)
    with db() as c:
        row = c.execute("SELECT auth_type,secret_enc FROM ssh_creds WHERE cred_key=?", (k,)).fetchone()
    if not row or not row["secret_enc"]:
        return None
    return (row["auth_type"] or "password"), security.decrypt(row["secret_enc"])


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


# ---------- SSH 批量命令(多主机并行执行,R探长同款能力) ----------

def _resolve_target(t: dict) -> dict:
    """把目标解析为 asyncssh 连接参数。支持 vps_id / cred_key / host+username(凭据库)。"""
    if t.get("vps_id"):
        row = _get_row(int(t["vps_id"]))
        _, kw = _creds(row)
        return {"label": row["name"], "host": row["host"], "kw": kw}
    if t.get("cred_key") or (t.get("username") and t.get("host")):
        import asyncssh as _a
        if t.get("cred_key"):
            k = str(t["cred_key"])
            try:
                username, rest = k.split("@", 1)
                host, port_s = rest.rsplit(":", 1)
                port = int(port_s)
            except ValueError:
                raise ProviderError(f"凭据键格式错误:{k}")
        else:
            host, port, username = str(t["host"]), int(t.get("port", 22)), str(t["username"])
        saved = _lookup_saved_cred(username, host, port)
        if not saved:
            raise ProviderError(f"{username}@{host}:{port} 没有保存的凭据")
        auth_t, sec = saved
        kw: dict = dict(port=port, username=username, known_hosts=None)
        if auth_t == "key":
            kw["client_keys"] = [_a.import_private_key(sec)]
        else:
            kw["password"] = sec
        return {"label": f"{username}@{host}", "host": host, "kw": kw}
    raise ProviderError("目标缺少可用的连接方式(vps_id / cred_key / host+username)")


@router.post("/batch-cmd")
async def batch_cmd(body: dict):
    """多主机并行执行命令。

    body: {targets:[{vps_id}|{cred_key}|{host,port,username}], cmd:str, timeout:int=30}
    返回 {items:[{target,label,ok,exit_code,output,error,ms}]}(单机输出截断 8KB)。
    """
    cmd = str(body.get("cmd") or "").strip()
    if not cmd:
        raise HTTPException(400, "请填写要执行的命令")
    targets = body.get("targets") or []
    if not targets or len(targets) > 50:
        raise HTTPException(400, "目标数量需在 1~50 之间")
    timeout = min(int(body.get("timeout") or 30), 120)

    resolved: list[dict] = []
    errors: list[dict] = []
    for t in targets:
        label0 = t.get("cred_key") or t.get("host") or f"vps-{t.get('vps_id')}"
        try:
            resolved.append(_resolve_target(dict(t)))
        except Exception as e:  # noqa: BLE001
            errors.append({"target": label0, "label": label0, "ok": False,
                           "error": f"解析失败:{e}", "output": ""})

    async def _run_one(tg: dict) -> dict:
        t0 = time.time()
        conn = None
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(tg["host"], **tg["kw"]), timeout=15)
            res = await asyncio.wait_for(conn.run(cmd), timeout=timeout)
            out = (res.stdout or "") + (("\n[stderr] " + res.stderr) if res.stderr else "")
            return {
                "target": tg["host"], "label": tg["label"], "ok": True,
                "exit_code": res.exit_status,
                "output": out[:8192],
                "ms": int((time.time() - t0) * 1000),
            }
        except Exception as e:  # noqa: BLE001
            return {"target": tg["host"], "label": tg["label"], "ok": False,
                    "output": "", "error": f"{type(e).__name__}: {e}"[:160],
                    "ms": int((time.time() - t0) * 1000)}
        finally:
            if conn:
                conn.close()

    results = list(await asyncio.gather(*[_run_one(t) for t in resolved]))
    return {"items": results + errors}


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


# ---------- VPS 资源监控(单机采集) ----------

_STATS_CMD = ("LANG=C top -bn1 | head -5; echo '---MEM---'; free -m | head -2; "
              "echo '---DISK---'; df -P / | tail -1; echo '---UP---'; "
              "uptime -p 2>/dev/null || uptime")


def _parse_stats(out: str) -> dict:
    cpu_pct = mem_pct = None
    mem_total = mem_used = disk_total = disk_used = None
    disk_pct = "-"
    uptime = ""
    section = "top"
    for ln in out.splitlines():
        if ln.startswith("---"):
            section = {"---MEM---": "mem", "---DISK---": "disk", "---UP---": "up"}.get(ln.strip(), section)
            continue
        if "%Cpu(s):" in ln and cpu_pct is None:
            try:
                idle = None
                for tok in ln.replace("%Cpu(s):", "").split(","):
                    t = tok.strip().split()
                    if len(t) == 2 and t[1] in ("id", "id,"):
                        idle = float(t[0])
                        break
                if idle is None:
                    # 某些 top 输出没有标签,取倒数第4个数值(id 通常在 wa 前)
                    nums = [t for t in ln.replace("%", "").replace(":", ",").split(",")]
                    vals = []
                    for t in nums:
                        p = t.strip().split()
                        if p:
                            try:
                                vals.append(float(p[0]))
                            except ValueError:
                                pass
                    if len(vals) >= 4:
                        idle = vals[3]
                if idle is not None:
                    cpu_pct = round(max(0.0, min(100.0, 100.0 - idle)), 1)
            except Exception:  # noqa: BLE001
                pass
        elif ln.startswith("Mem:") and mem_total is None:
            try:
                parts = ln.split()
                mem_total, mem_used = int(parts[1]), int(parts[2])
                avail = int(parts[-1]) if len(parts) >= 7 else None   # available 列
                used = (mem_total - avail) if avail is not None and avail > 0 else mem_used
                mem_pct = round(min(100.0, used * 100.0 / max(mem_total, 1)), 1)
                mem_used = used
            except Exception:  # noqa: BLE001
                pass
        elif section == "disk" and "%" in ln and disk_total is None:
            try:
                parts = ln.split()
                # df -P 标准: FS 1K-blocks Used Available Capacity% Mounted
                if len(parts) >= 5 and parts[1].isdigit():
                    disk_total = round(int(parts[1]) / 1048576, 1)      # KB→GB
                    disk_used = round(int(parts[2]) / 1048576, 1)
                    disk_pct = parts[4]
                else:
                    # 兜底:human-readable(40G 8.2G 32G 21%)
                    nums = [p for p in parts if p.endswith(("G", "M", "T")) or p.endswith("%")]
                    if len(nums) >= 4:
                        def to_gb(v):
                            v = v[:-1]
                            unit = {"G": 1, "M": 1/1024, "T": 1024}.get(v[-1] if False else "", 1)
                            return v
                        # 简化:直接取 G/T 数值,M 忽略精度
                        def f(v):
                            n = float(re.findall(r"([\d.]+)", v)[0])
                            if v.endswith("T"): return round(n * 1024, 1)
                            if v.endswith("M"): return round(n / 1024, 2)
                            return round(n, 1)
                        disk_total, disk_used = f(nums[0]), f(nums[1])
                        disk_pct = nums[3]
            except Exception:  # noqa: BLE001
                pass
        elif section == "up" and ln.strip() and not uptime:
            uptime = ln.strip()
    return {"cpu_pct": cpu_pct,
            "mem_total": mem_total, "mem_used": mem_used, "mem_pct": mem_pct,
            "disk_total": disk_total, "disk_used": disk_used, "disk_pct": disk_pct,
            "uptime": uptime or "-"}


async def collect_stats(vps_id: int) -> dict:
    """连接 VPS 执行一条组合命令并解析资源占用。失败抛异常。"""
    row = _get_row(vps_id)
    _, kw = _creds(row)
    conn = None
    try:
        conn = await asyncio.wait_for(asyncssh.connect(row["host"], **kw), timeout=15)
        res = await asyncio.wait_for(conn.run(_STATS_CMD), timeout=20)
        return _parse_stats(res.stdout or "")
    finally:
        if conn:
            conn.close()


@router.post("/stats")
async def stats(body: dict):
    """VPS 资源监控:{vps_id} 或 {cred_key}。"""
    import asyncssh

    if body.get("vps_id"):
        vid = int(body["vps_id"])
    elif body.get("cred_key"):
        k = str(body["cred_key"])
        try:
            username, rest = k.split("@", 1)
            host, port_s = rest.rsplit(":", 1)
            with db() as c:
                r = c.execute("SELECT id FROM vps_hosts WHERE host=? AND username=?",
                              (host, username)).fetchone()
            if not r:
                raise HTTPException(404, f"凭据 {k} 未登记为手动 VPS")
            vid = r["id"]
        except ValueError:
            raise HTTPException(400, "凭据键格式错误")
    else:
        raise HTTPException(400, "缺少 vps_id 或 cred_key")
    try:
        data = await collect_stats(vid)
        data["ok"] = True
        return data
    except asyncio.TimeoutError:
        raise HTTPException(502, "采集超时(主机无响应)")
    except asyncssh.Error as e:
        raise HTTPException(502, f"SSH 错误:{getattr(e, 'reason', e)}")
    except OSError as e:
        raise HTTPException(502, f"无法连接:{e}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"采集失败:{e}")
