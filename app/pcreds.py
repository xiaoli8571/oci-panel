"""多提供商凭证解密与统一异常。"""
import json

from . import security


class ProviderError(RuntimeError):
    """对用户友好的第三方平台错误。"""


def provider_of(acct: dict) -> str:
    return (acct.get("provider") or "oci").lower()


def extra_creds(acct: dict) -> dict:
    """解密 extra_enc(JSON)。"""
    enc = acct.get("extra_enc") or ""
    if not enc:
        return {}
    try:
        return json.loads(security.decrypt(enc))
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"凭证解析失败:{e}")
