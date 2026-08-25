"""面板入口:装配路由、登录中间件、异常处理、静态页面。"""
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audit, config, database, guardian, sched, security, tgbot
from .oci_client import OciError
from .pcreds import ProviderError
from .routers import wssh as wssh_router
from .routers import multi as multi_router
from .routers import accounts, auth, instances, resources, vps
from .routers import guardian as guardian_router
from .routers import oss as oss_router
from .routers import sched as sched_router
from .routers import rescue as rescue_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("panel")

# ---- 启动初始化(首次运行会生成随机密码并打印)----
generated_pw = security.init()
database.init()
audit.init()
guardian.start()
sched.start()   # 定时任务(每日定时开关机等)
tgbot.start()   # Telegram Bot 指令控制(未启用时静默待机)

if generated_pw:
    bar = "=" * 52
    log.warning("\n%s\n  首次运行已生成随机面板密码:%s\n  (也可用环境变量 PANEL_PASSWORD 指定)\n%s", bar, generated_pw, bar)

app = FastAPI(title=config.APP_NAME, version=config.VERSION, docs_url=None, redoc_url=None)

# JSON/HTML 响应 GZip 压缩(单页 HTML ~100KB、实例列表等大 JSON 收益明显)
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(instances.router)
app.include_router(resources.router)
app.include_router(vps.router)
app.include_router(guardian_router.router)
app.include_router(wssh_router.router)
app.include_router(multi_router.router)
app.include_router(oss_router.router)
app.include_router(sched_router.router)
app.include_router(rescue_router.router)


# ---- 登录守卫:保护所有 /api/*(除 login/status)与 /healthz ----
EXEMPT = {"/api/login", "/api/status", "/healthz"}


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in EXEMPT:
        if not security.verify_session(request.cookies.get(security.COOKIE_NAME)):
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    t0 = time.perf_counter()
    response = await call_next(request)
    # 操作审计:记录全部 API 写操作与登录尝试
    try:
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api"):
            audit.record(request.method, path,
                         status=getattr(response, "status_code", None),
                         ms=int((time.perf_counter() - t0) * 1000),
                         ip=request.client.host if request.client else "")
    except Exception:  # noqa: BLE001
        pass
    return response


@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(OciError)
async def oci_error_handler(request: Request, exc: OciError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):  # noqa: BLE001
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse({"detail": f"服务器内部错误:{exc}"}, status_code=500)


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


_latest_ver = {"v": None, "ts": 0.0}


@app.on_event("startup")
async def _version_checker():
    import asyncio

    async def _sleep(sec: float):
        await asyncio.sleep(sec)

    async def _loop():
        while True:
            try:
                v = await asyncio.to_thread(config.latest_release_sync)
                if v:
                    _latest_ver["v"], _latest_ver["ts"] = v, time.time()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(6 * 3600)

    async def _runner():
        await asyncio.sleep(20.0)
        await _loop()

    asyncio.create_task(_runner())


@app.get("/healthz")
def healthz():
    """容器健康检查:验证应用与数据库可用。"""
    with database.db() as c:
        c.execute("SELECT 1").fetchone()
    return {"ok": True, "version": config.VERSION}


class CachedStatic(StaticFiles):
    """带浏览器缓存的静态文件(vendor 库内容稳定;index.html 已单独 no-cache)。"""

    def file_response(self, *args, **kwargs):
        resp: Response = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp


app.mount("/static", CachedStatic(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
