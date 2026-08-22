"""安全相关:主密钥、账户私钥加密存储、面板密码、登录会话令牌。"""
import hashlib
import hmac
import json
import os
import secrets
import time

from cryptography.fernet import Fernet

from . import config

COOKIE_NAME = "panel_session"
_PBKDF2_ITER = 200_000

_fernet: Fernet | None = None
_master_key: bytes = b""


# ---------- 初始化 ----------

def init() -> str | None:
    """初始化主密钥与面板密码。若首次运行自动生成随机密码,返回该密码用于打印提示,否则返回 None。"""
    global _fernet, _master_key
    config.ensure_dirs()

    if config.MASTER_KEY_PATH.exists():
        _master_key = config.MASTER_KEY_PATH.read_bytes().strip()
    else:
        _master_key = Fernet.generate_key()
        config.MASTER_KEY_PATH.write_bytes(_master_key)
        try:
            os.chmod(config.MASTER_KEY_PATH, 0o600)
        except OSError:
            pass

    _fernet = Fernet(_master_key)

    cfg = _read_config()
    if cfg.get("password_hash"):
        return None
    env_pw = os.getenv("PANEL_PASSWORD")
    pw = env_pw or secrets.token_urlsafe(9)
    set_password(pw)
    return None if env_pw else pw


def _read_config() -> dict:
    try:
        return json.loads(config.CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_config(cfg: dict) -> None:
    config.ensure_dirs()
    config.CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# ---------- 对称加密(账户 API 私钥落库前加密) ----------

def encrypt(plain: str) -> str:
    if _fernet is None:
        raise RuntimeError("security 未初始化")
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if _fernet is None:
        raise RuntimeError("security 未初始化")
    return _fernet.decrypt(token.encode()).decode()


# ---------- 面板密码 ----------

def _hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITER)
    return dk.hex()


def set_password(password: str) -> None:
    salt = secrets.token_hex(16)
    cfg = _read_config()
    cfg["password_hash"] = f"{salt}${_hash(password, salt)}"
    _write_config(cfg)


def verify_password(password: str) -> bool:
    saved = _read_config().get("password_hash", "")
    if "$" not in saved:
        return False
    salt, digest = saved.split("$", 1)
    return hmac.compare_digest(digest, _hash(password, salt))


# ---------- 会话令牌(无状态 HMAC 签名) ----------

def create_session() -> str:
    exp = int(time.time()) + config.SESSION_TTL
    nonce = secrets.token_hex(8)
    payload = f"{exp}:{nonce}"
    sig = hmac.new(_master_key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        exp_s, nonce, sig = token.split(":", 2)
        payload = f"{exp_s}:{nonce}"
        good = hmac.new(_master_key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return False
        return int(exp_s) > time.time()
    except (ValueError, TypeError):
        return False
