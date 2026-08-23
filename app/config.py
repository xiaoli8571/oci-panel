"""全局配置:路径、端口等,支持环境变量覆盖。"""
import os
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "panel.db"
MASTER_KEY_PATH = DATA_DIR / "master.key"     # 用于加密账户私钥的主密钥
CONFIG_PATH = DATA_DIR / "config.json"        # 存放面板密码哈希等

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

VERSION = "0.14.0"

SESSION_TTL = 7 * 24 * 3600   # 登录会话有效期(秒)

# ---- 性能相关(均可通过环境变量调整) ----
# 实例总览缓存秒数:命中缓存直接返回;过期后先返回旧数据、后台刷新(stale-while-revalidate)
INSTANCE_CACHE_TTL = max(int(os.getenv("INSTANCE_CACHE_TTL", "30")), 5)
# 云账户/区域等表单元数据缓存的秒数
META_CACHE_TTL = max(int(os.getenv("META_CACHE_TTL", "120")), 10)
# 并行扫描云账户 / 实例详情的线程池上限
MAX_WORKERS = max(int(os.getenv("MAX_WORKERS", "8")), 2)
# Cookie Secure 属性:auto=HTTPS 下开启 / always=强制 / off=关闭(反代终止 TLS 时用 auto)
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "auto").lower()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def latest_release_sync(timeout: float = 5.0) -> str | None:
    """查询 GitHub 最新 release 版本号(失败返回 None;由后台线程定期调用)。"""
    import requests
    try:
        r = requests.get("https://api.github.com/repos/xiaoli8571/oci-panel/releases/latest",
                         timeout=timeout, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            tag = (r.json() or {}).get("tag_name") or ""
            return tag.lstrip("vV") or None
    except Exception:  # noqa: BLE001
        pass
    return None
