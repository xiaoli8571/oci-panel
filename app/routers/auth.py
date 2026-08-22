"""登录 / 登出 / 状态。带简易防爆破锁定。"""
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import config, security
from ..schemas import LoginReq, PasswordReq

router = APIRouter(prefix="/api", tags=["auth"])

_fail: dict[str, list] = {}
_MAX_FAILS = 5
_LOCK_SECONDS = 60


def _locked(ip: str) -> int:
    """返回剩余锁定秒数;未锁定返回 0。"""
    rec = _fail.get(ip)
    if not rec:
        return 0
    fails, until = rec
    if until and time.time() < until:
        return int(until - time.time())
    if until:
        # 曾被锁定且已过期:重新计数(本次失败将再次从 1 计起)
        _fail[ip] = [0, 0.0]
    return 0


@router.post("/login")
def login(body: LoginReq, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    remain = _locked(ip)
    if remain > 0:
        return JSONResponse({"detail": f"失败次数过多,{remain} 秒后重试"}, status_code=429)

    if not security.verify_password(body.password):
        rec = _fail.get(ip) or [0, 0.0]
        rec[0] += 1
        if rec[0] >= _MAX_FAILS:
            rec[1] = time.time() + _LOCK_SECONDS
        _fail[ip] = rec
        left = _LOCK_SECONDS if rec[0] >= _MAX_FAILS else 0
        detail = "密码错误" + (f",已锁定 {left} 秒" if left else f",剩余 {(_MAX_FAILS - rec[0])} 次机会")
        return JSONResponse({"detail": detail}, status_code=401)

    _fail.pop(ip, None)
    response.set_cookie(
        security.COOKIE_NAME,
        security.create_session(),
        httponly=True,
        samesite="lax",
        max_age=config.SESSION_TTL,
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(security.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/status")
def status():
    return {"ok": True, "version": config.VERSION}


@router.post("/password")
def change_password(body: PasswordReq):
    if not security.verify_password(body.old):
        return JSONResponse({"detail": "当前密码错误"}, status_code=401)
    security.set_password(body.new)
    return {"ok": True}
