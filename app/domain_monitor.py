"""域名与 SSL 证书到期监控(R探长「域名监控」同款能力的轻量实现)。

- SSL 到期:直接对 443 端口做 TLS 握手读取证书 notAfter(无需额外依赖)。
- 域名到期:RDAP(https://rdap.org/domain/{name})解析 expiration 事件;
  部分注册商/后缀不支持 RDAP 时跳过并标注。
- 阈值分档:[30, 14, 7, 3, 1] 天,进入新档位时产生事件并可触发 Telegram/Webhook 通知。
监控清单存于 kv 表(JSON),由 /api/guardian/domains 管理。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config, database, http_pool

log = logging.getLogger("domainmon")

RDAP = "https://rdap.org/domain/{name}"
THRESHOLDS = [30, 14, 7, 3, 1]          # 天数档位(降序)
SSL_PORTS = (443, 8443)                  # 依次尝试的 TLS 端口
WORKERS = 8


# ---------------------------------------------------------------- 监控清单 CRUD(kv)

def _key() -> str:
    return "domain_monitor_list"


def list_domains() -> list[dict]:
    raw = database.get_kv(_key()) or "[]"
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def save_domains(items: list[dict]) -> None:
    clean = []
    for it in items:
        name = str(it.get("name") or "").strip().lower().rstrip(".")
        if not name:
            continue
        clean.append({
            "name": name,
            "host": str(it.get("host") or name).strip(),   # TLS 探测主机,默认同域名
            "note": str(it.get("note") or "")[:100],
        })
    database.set_kv(_key(), json.dumps(clean, ensure_ascii=False))


def add_domain(name: str, host: str = "", note: str = "") -> list[dict]:
    items = [d for d in list_domains() if d["name"] != name.strip().lower()]
    items.append({"name": name, "host": host or name, "note": note})
    save_domains(items)
    return list_domains()


def remove_domain(name: str) -> list[dict]:
    save_domains([d for d in list_domains() if d["name"] != name.strip().lower()])
    return list_domains()


# ---------------------------------------------------------------- 探测实现

def ssl_expiry(host: str, port: int = 443, timeout: float = 8.0) -> dict:
    """TLS 握手取证书到期时间。返回 {ok, expires, days_left, issuer, error}。

    注:verify_mode=CERT_NONE 时 getpeercert() 返回空 dict,
    故用 binary_form 取 DER 后由 cryptography 解析(项目已依赖)。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # 只读到期时间,不校验链(自签也能读)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        exp = cert.not_valid_after_utc
        days = (exp - dt.datetime.now(dt.timezone.utc)).days
        issuer_cn = ""
        for attr in cert.issuer:
            if attr.oid.dotted_string == "2.5.4.10":   # organizationName
                issuer_cn = attr.value
            elif attr.oid.dotted_string == "2.5.4.3" and not issuer_cn:
                issuer_cn = attr.value
        return {"ok": True, "port": port,
                "expires": exp.date().isoformat(),
                "days_left": days,
                "issuer": str(issuer_cn)[:60]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "expires": None, "days_left": None, "error": f"{type(e).__name__}: {e}"[:120]}


def domain_expiry(name: str, timeout: float = 10.0) -> dict:
    """RDAP 查询域名注册到期。返回 {ok, expires, days_left, registrar, error}。"""
    try:
        r = http_pool.get(RDAP.format(name=name), timeout=timeout,
                          headers={"Accept": "application/rdap+json"})
        if r.status_code != 200:
            return {"ok": False, "error": f"RDAP HTTP {r.status_code}"}
        j = r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"网络错误:{e}"[:120]}
    except ValueError:
        return {"ok": False, "error": "RDAP 返回非 JSON"}

    exp, registrar = None, ""
    for ev in j.get("events", []) or []:
        if ev.get("eventAction") == "expiration":
            exp = ev.get("eventDate")
    for ent in j.get("entities", []) or []:
        roles = ent.get("roles") or []
        if "registrar" in roles:
            vcard = ent.get("vcardArray")
            if isinstance(vcard, list) and len(vcard) > 1:
                for item in vcard[1]:
                    if item and item[0] == "fn":
                        registrar = str(item[3])[:60]
                        break
    days = None
    if exp:
        try:
            d = dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
            days = (d - dt.datetime.now(dt.timezone.utc)).days
            exp = d.date().isoformat()
        except ValueError:
            pass
    return {"ok": bool(exp), "expires": exp, "days_left": days,
            "registrar": registrar, "error": None if exp else "RDAP 未返回到期事件"}


def check_one(item: dict) -> dict:
    """探测单个域名:并行做 SSL + RDAP。"""
    host = item.get("host") or item["name"]
    ssl_res = {"ok": False, "error": "未探测"}
    for p in SSL_PORTS:
        ssl_res = ssl_expiry(host, p)
        if ssl_res.get("ok"):
            break
    rd_res = domain_expiry(item["name"])
    out = {
        "name": item["name"], "host": host, "note": item.get("note", ""),
        "ssl_ok": ssl_res.get("ok"), "ssl_expires": ssl_res.get("expires"),
        "ssl_days_left": ssl_res.get("days_left"), "ssl_error": ssl_res.get("error"),
        "whois_ok": rd_res.get("ok"), "domain_expires": rd_res.get("expires"),
        "domain_days_left": rd_res.get("days_left"),
        "registrar": rd_res.get("registrar", ""), "rdap_error": rd_res.get("error"),
    }
    # 综合剩余天数(取更紧急者;仅有一项则用该项)
    candidates = [v for v in (out["ssl_days_left"], out["domain_days_left"])
                  if isinstance(v, int)]
    out["min_days_left"] = min(candidates) if candidates else None
    out["alert_level"] = _level(out["min_days_left"])
    return out


def _level(days: int | None) -> int | None:
    """命中的最紧急警报档位(即匹配的最小天数阈值);未命中返回 None。

    例:days=2 时同时落入 30/14/7/3 档,应报最紧急的 3 天档。
    """
    if days is None:
        return None
    if days < 0:
        days = 0   # 已过期,按最紧急档处理
    matched = [t for t in THRESHOLDS if days <= t]
    return min(matched) if matched else None


def check_all(progress=None) -> list[dict]:
    """并行探测全部监控清单。"""
    items = list_domains()
    if not items:
        return []

    def _one(it):
        try:
            return check_one(it)
        except Exception as e:  # noqa: BLE001
            log.warning("探测 %s 异常:%s", it.get("name"), e)
            return {"name": it.get("name"), "ok": False, "error": str(e)[:120],
                    "alert_level": None}

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as ex:
        results = list(ex.map(_one, items))
    if progress:
        progress(f"已探测 {len(results)} 个域名")
    return results
