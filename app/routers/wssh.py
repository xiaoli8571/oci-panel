"""Web SSH:WebSocket 桥接到 asyncssh。

前端每个终端标签页建立一条 /ws/ssh 连接,协议(JSON 文本帧):
  C→S open   {host,port,username,auth:"password"|"key",secret,cols,rows}
  S→C hostkey {fingerprint}            # 首次连接要求确认
  C→S trust  {} | reject {}
  S→C opened {}
  C→S input  {data}
  S→C data   {data}
  C→S resize {cols,rows}
  S→C exit   {code?} / error {message}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import pathlib

import asyncssh
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config, security
from ..database import db
from ..pcreds import ProviderError
from ..routers.vps import _creds, _get_row, _cred_key, _lookup_saved_cred


log = logging.getLogger("wssh")

router = APIRouter()

KNOWN_HOSTS_PATH = config.DATA_DIR / "known_hosts.json"


def _load_known() -> dict:
    try:
        return json.loads(KNOWN_HOSTS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_known(d: dict) -> None:
    config.ensure_dirs()
    KNOWN_HOSTS_PATH.write_text(json.dumps(d, indent=1))


def _fp_of(key) -> str:
    try:
        fp = key.get_fingerprint(hash_name="sha256")
        if isinstance(fp, str):
            return fp if fp.startswith("SHA256:") else "SHA256:" + fp
    except Exception:  # noqa: BLE001
        pass
    try:  # 兜底:对公钥导出字节做哈希
        raw = key.export_public_key()
        b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        return "SHA256:" + b64
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


async def _send(ws: WebSocket, **obj):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


@router.websocket("/ws/ssh")
async def ws_ssh(websocket: WebSocket):
    # 手动鉴权(中间件不覆盖 ws)
    await websocket.accept()
    if not security.verify_session(websocket.cookies.get(security.COOKIE_NAME)):
        await _send(websocket, type="error", message="未登录或会话已过期")
        await websocket.close(code=4401)
        return

    conn = None
    writer = None
    pump_task = None
    pending_trust = False
    # TOFU 校验上下文:open 时记住主机与指纹,trust 确认后据此写入 known_hosts
    cur_host, cur_port, cur_fp = "", 22, ""

    async def pump(reader):
        nonlocal writer
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                await _send(websocket, type="data", data=data.decode("utf-8", "replace"))
            await _send(websocket, type="exit")
        except Exception:  # noqa: BLE001
            pass

    def _hostkey_fp():
        try:
            key = conn.get_server_host_key()
            return _fp_of(key) if key else None
        except Exception:  # noqa: BLE001
            return None

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            if t == "open":
                if msg.get("use_saved"):
                    # 用面板加密存储的 VPS 凭据直连
                    try:
                        row = _get_row(int(msg.get("vps_id", 0)))
                    except Exception:  # noqa: BLE001
                        await _send(websocket, type="error", message="VPS 记录不存在")
                        continue
                    secret_plain, vkw = _creds(row)
                    host, port, user = row["host"], int(vkw.pop("port")), str(vkw.pop("username"))
                    kw = dict(vkw)
                elif msg.get("use_saved_cred") and msg.get("host"):
                    # 按 user@host:port 从 SSH 凭据库取
                    host = str(msg["host"]); port = int(msg.get("port", 22)); user = str(msg["username"])
                    saved = _lookup_saved_cred(user, host, port)
                    if not saved:
                        await _send(websocket, type="error", message="凭据库里没有这台主机的保存凭据")
                        continue
                    auth_t, sec = saved
                    kw = dict(port=port, username=user, known_hosts=None)
                    if auth_t == "key":
                        kw["client_keys"] = [asyncssh.import_private_key(sec)]
                    else:
                        kw["password"] = sec
                else:
                    host = str(msg["host"]); port = int(msg.get("port", 22))
                    user = str(msg["username"])
                    kw = dict(port=port, username=user, known_hosts=None)
                    if msg.get("auth") == "key":
                        kw["client_keys"] = [asyncssh.import_private_key(msg["secret"])]
                    else:
                        kw["password"] = str(msg.get("secret", ""))
                try:
                    conn = await asyncio.wait_for(asyncssh.connect(host, **kw), timeout=20)
                except asyncio.TimeoutError:
                    await _send(websocket, type="error", message="连接超时")
                    continue
                except asyncssh.ConnectionLost as e:
                    await _send(websocket, type="error", message=f"连接被拒绝:{e}")
                    continue
                except OSError as e:
                    await _send(websocket, type="error", message=f"无法连接:{e}")
                    continue
                except asyncssh.Error as e:
                    await _send(websocket, type="error",
                                message=f"SSH 错误:{getattr(e, 'reason', e)}")
                    continue
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=str(e))
                    continue

                # 主机指纹 TOFU 校验
                cur_host, cur_port = host, port
                fp = _hostkey_fp() or ""
                cur_fp = fp
                known = _load_known()
                k = f"{host}:{port}"
                if known.get(k) and known[k] != fp:
                    await _send(websocket, type="error",
                                message=f"主机密钥不匹配!记录:{known[k][:30]}… 当前:{fp[:30]}… 可能存在中间人风险")
                    continue
                if not known.get(k) and not msg.get("trust"):
                    pending_trust = True
                    await _send(websocket, type="hostkey", fingerprint=fp)
                    # 等待 trust/reject(下一条消息在主循环处理)
                    continue

                writer_obj, reader, _ = await conn.open_session(
                    term_type="xterm-256color",
                    term_size=(int(msg.get("cols", 120)), int(msg.get("rows", 32))),
                    encoding=None)
                writer = writer_obj
                pump_task = asyncio.create_task(pump(reader))
                await _send(websocket, type="opened")

            elif t == "trust" and pending_trust and conn is not None:
                # 前端只发 {type:"trust"},主机信息用 open 时记住的 cur_host/cur_port/cur_fp
                known = _load_known()
                if cur_host:
                    known[f"{cur_host}:{cur_port}"] = cur_fp
                    _save_known(known)
                pending_trust = False
                writer_obj, reader, _ = await conn.open_session(
                    term_type="xterm-256color",
                    term_size=(int(msg.get("cols", 120)), int(msg.get("rows", 32))),
                    encoding=None)
                writer = writer_obj
                pump_task = asyncio.create_task(pump(reader))
                await _send(websocket, type="opened")

            elif t == "reject":
                await _send(websocket, type="error", message="已取消连接")
                break

            elif t == "input" and writer is not None:
                try:
                    writer.write(str(msg.get("data", "")).encode())
                except Exception:  # noqa: BLE001
                    await _send(websocket, type="exit")

            elif t == "resize" and writer is not None:
                try:
                    writer.change_terminal_size(int(msg.get("cols", 120)),
                                                int(msg.get("rows", 32)))
                except Exception:  # noqa: BLE001
                    pass

            elif t == "ping":
                await _send(websocket, type="pong")

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ws 异常:%s", e)
    finally:
        if pump_task:
            pump_task.cancel()
        if conn:
            conn.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# ================================================================ SFTP 文件管理

# SFTP 文件类型判断常量(POSIX)
_S_IFMT = 0o170000
_S_IFDIR = 0o040000


class MaxTransferError(Exception):
    pass


MAX_TRANSFER = 50 * 1024 * 1024  # 单文件传输上限 50MB


def _is_dir(attrs) -> bool:
    t = getattr(attrs, "type", None)
    if t is None:
        perms = getattr(attrs, "permissions", 0) or 0
        return (perms & _S_IFMT) == _S_IFDIR
    return (t & _S_IFMT) == _S_IFDIR


@router.websocket("/ws/sftp")
async def ws_sftp(websocket: WebSocket):
    """浏览器 ↔ 面板 SFTP 桥接(文本帧 JSON,内容 base64)。"""
    await websocket.accept()
    if not security.verify_session(websocket.cookies.get(security.COOKIE_NAME)):
        await _send(websocket, type="error", message="未登录或会话已过期")
        await websocket.close(code=4401)
        return

    conn = None
    sftp = None
    up_buf: bytearray | None = None
    up_path = ""

    async def listing(path: str):
        names = await sftp.readdir(path)
        entries = []
        for n in names:
            if n.filename in (".", ".."):
                continue
            a = n.attrs
            entries.append({
                "name": n.filename,
                "is_dir": _is_dir(a),
                "size": getattr(a, "size", None) or 0,
                "mtime": int(getattr(a, "mtime", 0) or 0),
            })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        await _send(websocket, type="listing", path=path, entries=entries)

    async def _connect(msg):
        if msg.get("use_saved"):
            try:
                row = _get_row(int(msg.get("vps_id", 0)))
            except Exception:  # noqa: BLE001
                raise ProviderError("VPS 记录不存在")
            _, vkw = _creds(row)
            port = int(vkw.pop("port")); username = str(vkw.pop("username"))
            host = row["host"]
            return await asyncio.wait_for(
                asyncssh.connect(host, port=port, username=username,
                                 known_hosts=None, **vkw), timeout=20)
        if msg.get("use_saved_cred") and msg.get("host"):
            saved = _lookup_saved_cred(str(msg["username"]), str(msg["host"]), int(msg.get("port", 22)))
            if not saved:
                raise ProviderError("凭据库里没有这台主机的保存凭据")
            auth_t, sec = saved
            kw = dict(port=int(msg.get("port", 22)), username=str(msg["username"]), known_hosts=None)
            if auth_t == "key":
                kw["client_keys"] = [asyncssh.import_private_key(sec)]
            else:
                kw["password"] = sec
            return await asyncio.wait_for(asyncssh.connect(str(msg["host"]), **kw), timeout=20)
        kw = dict(port=int(msg.get("port", 22)), username=str(msg["username"]), known_hosts=None)
        if msg.get("auth") == "key":
            kw["client_keys"] = [asyncssh.import_private_key(msg["secret"])]
        else:
            kw["password"] = str(msg.get("secret", ""))
        return await asyncio.wait_for(asyncssh.connect(str(msg["host"]), **kw), timeout=20)

    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            t = msg.get("type")

            if t == "open":
                try:
                    conn = await _connect(msg)
                    sftp = await conn.start_sftp_client()
                    home = await sftp.realpath(".")
                    await _send(websocket, type="opened", home=str(home))
                    await listing(str(home))
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"连接失败:{e}")

            elif t == "list":
                try:
                    await listing(str(msg["path"]))
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"读取目录失败:{e}")

            elif t == "mkdir":
                try:
                    await sftp.mkdir(str(msg["path"]))
                    await _send(websocket, type="ok")
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"创建失败:{e}")

            elif t == "delete":
                try:
                    p = str(msg["path"])
                    if msg.get("is_dir"):
                        await sftp.rmdir(p)
                    else:
                        await sftp.remove(p)
                    await _send(websocket, type="ok")
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"删除失败:{e}")

            elif t == "rename":
                try:
                    await sftp.rename(str(msg["from"]), str(msg["to"]))
                    await _send(websocket, type="ok")
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"重命名失败:{e}")

            elif t == "download":
                try:
                    p = str(msg["path"])
                    size = (await sftp.stat(p)).size or 0
                    if size > MAX_TRANSFER:
                        raise MaxTransferError(f"文件超过 {MAX_TRANSFER // 1024 // 1024}MB 上限")
                    await _send(websocket, type="dmeta", path=p, size=size)
                    async with sftp.open(p, "rb") as f:
                        while True:
                            chunk = await f.read(256 * 1024)
                            if not chunk:
                                break
                            await _send(websocket, type="dchunk",
                                        data=base64.b64encode(chunk).decode())
                    await _send(websocket, type="deof")
                except MaxTransferError as e:
                    await _send(websocket, type="error", message=str(e))
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"下载失败:{e}")

            elif t == "upload":
                up_path = str(msg["path"])
                size = int(msg.get("size", 0))
                if size > MAX_TRANSFER:
                    await _send(websocket, type="error",
                                message=f"文件超过 {MAX_TRANSFER // 1024 // 1024}MB 上限")
                    up_buf = None
                else:
                    up_buf = bytearray()
                    await _send(websocket, type="uready")

            elif t == "uchunk":
                if up_buf is not None:
                    up_buf += base64.b64decode(msg.get("data", ""))

            elif t == "udone":
                try:
                    async with sftp.open(up_path, "wb") as f:
                        await f.write(bytes(up_buf or b""))
                    await _send(websocket, type="ok")
                except Exception as e:  # noqa: BLE001
                    await _send(websocket, type="error", message=f"上传失败:{e}")
                finally:
                    up_buf = None

    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("sftp ws 异常:%s", e)
    finally:
        if conn:
            conn.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
