"""面板入口:装配路由、登录中间件、异常处理、静态页面。"""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, database, guardian, security
from .oci_client import OciError
from .pcreds import ProviderError
from .routers import wssh as wssh_router
from .routers import multi as multi_router
from .routers import accounts, auth, instances, resources
from .routers import guardian as guardian_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("panel")

# ---- 启动初始化(首次运行会生成随机密码并打印)----
generated_pw = security.init()
database.init()
guardian.start()

if generated_pw:
    bar = "=" * 52
    log.warning("\n%s\n  首次运行已生成随机面板密码:%s\n  (也可用环境变量 PANEL_PASSWORD 指定)\n%s", bar, generated_pw, bar)

app = FastAPI(title="OCI Manage Lite", version=config.VERSION, docs_url=None, redoc_url=None)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(instances.router)
app.include_router(resources.router)
app.include_router(guardian_router.router)
app.include_router(wssh_router.router)
app.include_router(multi_router.router)


# ---- 登录守卫:保护所有 /api/*(除 login/status)----
EXEMPT = {"/api/login", "/api/status"}


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in EXEMPT:
        if not security.verify_session(request.cookies.get(security.COOKIE_NAME)):
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return await call_next(request)


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
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
