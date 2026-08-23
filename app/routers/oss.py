"""OCI 对象存储接口:桶列表/创建/删除、对象浏览/上传/下载/删除。"""
import urllib.parse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import oci_client
from ..database import db
from ..schemas import OssBucketIn, OssObjectIn
from ..ttlcache import TTLCache

router = APIRouter(prefix="/api/oss", tags=["oss"])

# 桶列表短缓存(30s):浏览对象页频繁刷新时减少 API 调用
_bucket_cache: TTLCache = TTLCache(ttl=30.0, max_items=64)


def _get_account(account_id: int) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    return dict(row)


def _oci_account(account_id: int) -> dict:
    acct = _get_account(account_id)
    if (acct.get("provider") or "oci") != "oci":
        raise HTTPException(400, "对象存储仅支持 OCI 账户")
    return acct


@router.get("/buckets")
def buckets(account_id: int):
    key = f"buckets:{account_id}"
    cached = _bucket_cache.get(key)
    if cached is not None:
        return {"items": cached}
    items = oci_client.list_buckets(_oci_account(account_id))
    _bucket_cache.set(key, items)
    return {"items": items}


@router.post("/bucket")
def create_bucket(body: OssBucketIn):
    _oci_account(body.account_id)
    name = body.name.strip().lower()
    if not name.replace("-", "").replace(".", "").replace("_", "").isalnum() or len(name) < 3:
        raise HTTPException(400, "桶名仅限小写字母/数字/._-,长度≥3")
    r = oci_client.create_bucket(_get_account(body.account_id), name,
                                 tier=body.tier or "Standard")
    _bucket_cache.drop(f"buckets:{body.account_id}")
    return r


@router.delete("/bucket")
def delete_bucket(account_id: int, name: str):
    _oci_account(account_id)
    r = oci_client.delete_bucket(_get_account(account_id), name)
    _bucket_cache.drop(f"buckets:{account_id}")
    return r


@router.get("/objects")
def objects(account_id: int, bucket: str, prefix: str = ""):
    _oci_account(account_id)
    return oci_client.list_objects(_oci_account(account_id), bucket, prefix=prefix)


@router.post("/object/upload")
def object_upload(body: OssObjectIn):
    import base64
    _oci_account(body.account_id)
    content = base64.b64decode(body.content_b64 or "")
    if not body.name.strip():
        raise HTTPException(400, "对象名为空")
    return oci_client.put_object_content(_get_account(body.account_id),
                                         body.bucket, body.name.strip(), content)


@router.get("/object/download")
def object_download(account_id: int, bucket: str, name: str):
    """流式下载(session cookie 鉴权,浏览器 <a> 直下)。"""
    acct = _oci_account(account_id)
    data = oci_client.get_object_content(acct, bucket, name)
    fname = name.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}"})


@router.delete("/object")
def object_delete(account_id: int, bucket: str, name: str):
    _oci_account(account_id)
    return oci_client.delete_object(_get_account(account_id), bucket, name)
