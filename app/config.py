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

VERSION = "0.9.1"

SESSION_TTL = 7 * 24 * 3600   # 登录会话有效期(秒)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
