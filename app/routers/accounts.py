"""OCI 账户(API Key)管理:增删改查、测活诊断。私钥加密落库,永不回传明文。"""
import base64
import datetime as dt
import hashlib
import json
import re

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, HTTPException

from .. import oci_client, security
from ..database import db
from ..routers.instances import _invalidate_cache
from ..schemas import AccountIn
from ..ttlcache import client_cache

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

_FP_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){15}$")


def _check_ocid(kind: str, value: str):
    """官方 OCID 形如 ocid1.tenancy.oc1..<id>,realm 兼容 oc1/oc2…/ocid1 等。"""
    if not re.match(rf"^ocid1\.{kind}\.oc[a-z0-9]*\.\.\S{{10,}}$", value):
        raise HTTPException(
            400, f"{kind} OCID 格式不正确,官方格式形如 ocid1.{kind}.oc1..xxxxxxxx")


def _validate(a: AccountIn):
    p = (a.provider or "oci").lower()
    if p == "oci":
        _check_ocid("tenancy", a.tenancy_ocid.strip())
        _check_ocid("user", a.user_ocid.strip())
        if not _FP_RE.match(a.fingerprint.strip().lower()):
            raise HTTPException(400, "API Key 指纹格式不正确")
        if a.region.count("-") < 2:
            raise HTTPException(400, "区域标识不正确,例如 ap-seoul-1")
    elif p == "aws":
        if not re.match(r"^(AKIA|ASIA)[A-Z0-9]{16}$", a.aws_access_key_id.strip()):
            raise HTTPException(400, "Access Key ID 格式不正确(AKIA/ASIA 开头 20 位)")
        sk = a.aws_secret_key.strip()
        if len(sk) < 20:
            raise HTTPException(400, "请填写 Secret Access Key")
        if len(sk) != 40:
            raise HTTPException(
                400, f"Secret Access Key 长度应为 40 位,当前 {len(sk)} 位 —— "
                     "大概率复制不完整(创建时仅显示一次,请重新生成并完整复制)")
    elif p == "cloudflare":
        if len(a.cf_token.strip()) < 30:
            raise HTTPException(400, "请填写 Cloudflare API Token(至少 30 字符)")
    elif p == "dnshe":
        if len(a.dnshe_api_key.strip()) < 5:
            raise HTTPException(400, "请填写 DNSHE API Key(在 my.dnshe.com 客户区 API 管理创建)")
        if len(a.dnshe_api_secret.strip()) < 5:
            raise HTTPException(400, "请填写 DNSHE API Secret")
    else:
        raise HTTPException(400, f"不支持的提供商:{p}")


PROVIDERS = ("oci", "aws", "cloudflare", "dnshe")


def _extra_json(a: AccountIn, keep_old: str = "") -> str:
    """把各平台敏感凭证打包加密;编辑时未填的字段沿用旧值。"""
    old = {}
    if keep_old:
        try:
            old = json.loads(security.decrypt(keep_old))
        except Exception:  # noqa: BLE001
            old = {}
    data = {
        "aws_access_key_id": a.aws_access_key_id.strip() or old.get("aws_access_key_id", ""),
        "aws_secret_key": a.aws_secret_key.strip() or old.get("aws_secret_key", ""),
        "cf_token": a.cf_token.strip() or old.get("cf_token", ""),
        "he_email": a.he_email.strip() or old.get("he_email", ""),
        "he_pass": a.he_pass or old.get("he_pass", ""),
        "he_api_secret": a.he_api_secret.strip() or old.get("he_api_secret", ""),
        "dnshe_api_key": a.dnshe_api_key.strip() or old.get("dnshe_api_key", ""),
        "dnshe_api_secret": a.dnshe_api_secret.strip() or old.get("dnshe_api_secret", ""),
    }
    return security.encrypt(json.dumps(data))


def _row_public(r) -> dict:
    d = dict(r)
    d["provider"] = d.get("provider") or "oci"
    d["has_key"] = bool(d.pop("private_key_enc", None))
    d.pop("extra_enc", None)
    return d


@router.get("")
def list_accounts():
    with db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return {"items": [_row_public(r) for r in rows]}


@router.post("")
async def create_account(body: AccountIn):
    _validate(body)
    p = body.provider.lower()
    key_enc, extra_enc = "", ""
    if p == "oci":
        key = body.private_key.strip()
        if "PRIVATE KEY" not in key:
            raise HTTPException(400, "请上传 .pem 私钥文件或粘贴 PEM 内容")
        key_enc = security.encrypt(key)
    else:
        extra_enc = _extra_json(body)
    with db() as c:
        cur = c.execute(
            "INSERT INTO accounts(provider,name,user_ocid,tenancy_ocid,region,fingerprint,"
            "private_key_enc,extra_enc) VALUES(?,?,?,?,?,?,?,?)",
            (p, body.name.strip(), body.user_ocid.strip(), body.tenancy_ocid.strip(),
             body.region.strip().lower(), body.fingerprint.strip().lower(), key_enc, extra_enc),
        )
        return {"id": cur.lastrowid}


@router.put("/{account_id}")
async def update_account(account_id: int, body: AccountIn):
    _validate(body)
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(404, "账户不存在")
        key_enc = row["private_key_enc"]
        if body.provider.lower() == "oci" and body.private_key.strip():
            if "PRIVATE KEY" not in body.private_key:
                raise HTTPException(400, "私钥需为 PEM 格式")
            key_enc = security.encrypt(body.private_key.strip())
        extra_enc = _extra_json(body, keep_old=row["extra_enc"] or "")
        c.execute(
            "UPDATE accounts SET provider=?,name=?,user_ocid=?,tenancy_ocid=?,region=?,"
            "fingerprint=?,private_key_enc=?,extra_enc=? WHERE id=?",
            (body.provider.lower(), body.name.strip(), body.user_ocid.strip(),
             body.tenancy_ocid.strip(), body.region.strip().lower(),
             body.fingerprint.strip().lower(), key_enc, extra_enc, account_id),
        )
    # 凭据可能已变更:失效该账户的 SDK 客户端与实例缓存
    _invalidate_cache(account_id)
    return {"ok": True}


@router.delete("/{account_id}")
def delete_account(account_id: int):
    with db() as c:
        cur = c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "账户不存在")
    _invalidate_cache(account_id)
    client_cache.drop(f"{account_id}:")
    return {"ok": True}


# ---------------------------------------------------------------- 测活诊断


def _md5hex(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in hashlib.md5(b).digest())


def _local_fp(pem: str) -> dict:
    """解析私钥并按三种候选算法计算公钥 MD5 指纹(兼容不同 OCI 计算口径)。"""
    if not pem:
        return {"parsed": False, "error": "该账户未保存 OCI 私钥(仅 OCI 类型需要)"}
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as e:  # noqa: BLE001
        return {"parsed": False, "error": f"私钥无法解析:{e}"}
    pub = key.public_key()
    out: dict = {"parsed": True, "key_type": type(key).__name__.replace("_", " ")}
    try:
        der = pub.public_bytes(serialization.Encoding.DER,
                               serialization.PublicFormat.SubjectPublicKeyInfo)
        out["md5_der"] = _md5hex(der)
    except Exception:  # noqa: BLE001
        pass
    try:
        parts = pub.public_bytes(serialization.Encoding.OpenSSH,
                                 serialization.PublicFormat.OpenSSH).decode().split()
        blob = base64.b64decode(parts[1])
        out["md5_blob"] = _md5hex(blob)
        out["md5_line"] = _md5hex(" ".join(parts[:2]).encode())
    except Exception:  # noqa: BLE001
        pass
    return out


def _classify(err: str | None) -> str:
    if not err:
        return ""
    e = err.lower()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if "failed to verify the http(s) signature" in e:
        return ("签名校验失败 = 私钥与该指纹不匹配。请逐项核对:\n"
                "① 面板中填写的指纹是否等于 OCI 控制台→用户设置→API密钥 页面显示的那一条;"
                "\n② 上传的 .pem 是否就是【同一次添加】生成的私钥(每添加一次会生成新密钥对,"
                "旧 .pem 配新指纹必失败);\n③ 若不确定,最稳妥做法:控制台删除旧 API Key → "
                "添加 API Key → 下载新 .pem → 把新指纹和新 .pem 一起更新到本面板。")
    if "notauthenticated" in e or "401" in e:
        return f"鉴权失败(401)。若上方时间与你的标准时间相差超过 5 分钟也会导致此错,服务器当前时间:{now}"
    if "notauthorizedorNotFound" in err or "404" in e:
        return "请求到达但被拒绝(404):检查用户是否属于该租户、API 是否启用、区域是否为该账户开通的区域。"
    if "timeout" in e or "timed out" in e:
        return "网络超时:检查面板服务器到 oraclecloud.com 的出网连通性。"
    return ""


@router.get("/{account_id}/check")
def check_account(account_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "账户不存在")
    acct = dict(row)
    p = (acct.get("provider") or "oci").lower()

    if p == "oci":
        return _check_oci(acct)
    if p == "aws":
        return _check_aws(acct)
    if p == "cloudflare":
        return _check_cf(acct)
    if p == "dnshe":
        return _check_he(acct)
    raise HTTPException(400, f"未知提供商:{p}")


def _check_oci(acct: dict) -> dict:
    stored = (acct["fingerprint"] or "").strip().lower()
    pem = ""
    try:
        pem = security.decrypt(acct["private_key_enc"]) if acct.get("private_key_enc") else ""
    except Exception as e:  # noqa: BLE001
        pem = ""
    local = _local_fp(pem)
    matched = [k for k in ("md5_der", "md5_blob", "md5_line")
               if local.get(k) and local[k] == stored]

    remote_ok, err = True, None
    try:
        oci_client.list_ads(acct)
    except Exception as e:  # noqa: BLE001
        remote_ok, err = False, str(e)

    return {
        "provider": "oci",
        "stored_fingerprint": stored,
        "local": local,
        "matched_candidates": matched,
        "match": bool(matched),
        "server_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "remote_ok": remote_ok,
        "error": err,
        "hint": _classify(err),
    }


def _check_aws(acct: dict) -> dict:
    from .. import aws_cloud
    ok, err = True, None
    try:
        aws_cloud._client(acct).describe_regions()
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)
    return {"provider": "aws", "remote_ok": ok, "error": err,
            "hint": "AWS 全区域扫描已在实例列表验证;若失败请检查 Access Key / Secret Key(Secret 应为 40 位)" if err else ""}


def _check_cf(acct: dict) -> dict:
    from .. import cloudflare as cfmod
    try:
        info = cfmod.verify_token(acct)
        ok = bool(info.get("valid"))
        err = None if ok else (info.get("errors") or "Token 无效")
        dns = info.get("dns_read")
        return {"provider": "cloudflare", "remote_ok": ok, "error": err,
                "valid": ok, "zones": info.get("zones"),
                "accounts": info.get("accounts"), "dns_read": dns,
                "hint": "Token 有效;DNS 记录权限:{}".format("✅" if dns else "❌ 缺失(需在 CF 控制台给 Token 加 Zone→DNS→Edit 权限)")}
    except Exception as e:  # noqa: BLE001
        return {"provider": "cloudflare", "remote_ok": False, "error": str(e), "hint": "Token 无效或权限不足,可在账户列表点「验Token」看详细报告"}


def _check_he(acct: dict) -> dict:
    """DNSHE(my.dnshe.com)官方 API 测活:列出子域名 + 查询配额。"""
    from .. import dnshe_api
    ok, err = True, None
    quota_info = None
    try:
        dnshe_api.list_subdomains(acct)
        quota_info = dnshe_api.quota(acct).get("quota")
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)
    hint = ""
    if ok:
        if quota_info:
            hint = ("DNSHE API 正常,可用子域名配额:{}".format(quota_info.get("available"))
                    if quota_info.get("available") is not None else "DNSHE API 正常")
        else:
            hint = "DNSHE API 正常"
    else:
        hint = "请检查 my.dnshe.com 客户区 API 管理里的 API Key / API Secret;若开了 IP 白名单,需把当前服务器 IP 加入白名单"
    return {"provider": "dnshe", "remote_ok": ok, "error": err,
            "hint": hint, "quota": quota_info}


