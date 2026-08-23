"""DNSHE(my.dnshe.com)官方 API v2.0 封装:免费子域名 + DNS 记录管理。

API 文档:https://my.dnshe.com/knowledgebase/13/DNSHE免费域名API使用文档V2.0.html
认证:X-API-Key + X-API-Secret 请求头(API Key 与 API Secret 成对创建)。
"""
from __future__ import annotations

import requests

from . import http_pool
from .pcreds import ProviderError, extra_creds

API = "https://api005.dnshe.com/index.php"


def _creds(acct: dict) -> tuple[str, str]:
    c = extra_creds(acct)
    key = (c.get("dnshe_api_key") or "").strip()
    secret = (c.get("dnshe_api_secret") or "").strip()
    if not key or not secret:
        raise ProviderError("该 DNSHE 账户未配置 API Key / API Secret")
    return key, secret


def _req(acct: dict, endpoint: str, action: str = "",
         method: str = "GET", data: dict | None = None) -> dict:
    key, secret = _creds(acct)
    url = f"{API}?m=domain_hub&endpoint={endpoint}"
    if action:
        url += f"&action={action}"
    headers = {"X-API-Key": key, "X-API-Secret": secret,
               "Content-Type": "application/json"}
    try:
        if method == "GET":
            r = http_pool.get(url, headers=headers, params=data, timeout=25)
        else:
            r = http_pool.request(method, url, headers=headers, json=data or {}, timeout=25)
    except requests.RequestException as e:
        raise ProviderError(f"DNSHE 网络错误:{e}") from e
    try:
        j = r.json()
    except ValueError as e:
        raise ProviderError(f"DNSHE 返回异常:{r.text[:150]}") from e
    if not j.get("success"):
        code = j.get("error_code", "")
        msg = j.get("message") or j.get("error") or f"HTTP {r.status_code}"
        hint = ""
        if code in ("auth_invalid_credentials",):
            hint = "(请检查 API Key / API Secret 是否正确,可在 my.dnshe.com API 管理里重新生成)"
        elif code == "auth_ip_not_allowed":
            hint = "(你的 API Key 开了 IP 白名单,当前服务器 IP 不在白名单内)"
        raise ProviderError(f"DNSHE 错误 [{code}] {msg} {hint}".strip())
    return j


# ---------------------------------------------------------------- 子域名

def list_subdomains(acct: dict, search: str = "", status: str = "",
                    page: int = 1, per_page: int = 200) -> dict:
    d = {"page": page, "per_page": per_page}
    if search:
        d["search"] = search
    if status:
        d["status"] = status
    j = _req(acct, "subdomains", "list", "GET", d)
    return {"subdomains": j.get("subdomains", []),
            "pagination": j.get("pagination", {}),
            "count": j.get("count", 0)}


def register_subdomain(acct: dict, subdomain: str, rootdomain: str) -> dict:
    return _req(acct, "subdomains", "register", "POST",
                {"subdomain": subdomain, "rootdomain": rootdomain})


def delete_subdomain(acct: dict, subdomain_id: int) -> dict:
    return _req(acct, "subdomains", "delete", "POST", {"subdomain_id": subdomain_id})


def renew_subdomain(acct: dict, subdomain_id: int) -> dict:
    return _req(acct, "subdomains", "renew", "POST", {"subdomain_id": subdomain_id})


# ---------------------------------------------------------------- DNS 记录

def list_records(acct: dict, subdomain_id: int) -> list[dict]:
    j = _req(acct, "dns_records", "list", "GET", {"subdomain_id": subdomain_id})
    return j.get("records", [])


def create_record(acct: dict, subdomain_id: int, rtype: str, name: str,
                  content: str, ttl: int = 600, priority: int | None = None) -> dict:
    d = {"subdomain_id": subdomain_id, "type": rtype.upper(),
         "name": name, "content": content, "ttl": ttl}
    if priority is not None:
        d["priority"] = priority
    return _req(acct, "dns_records", "create", "POST", d)


def update_record(acct: dict, subdomain_id: int, record_id: str, rtype: str,
                  name: str, content: str, ttl: int = 600,
                  priority: int | None = None) -> dict:
    d = {"subdomain_id": subdomain_id, "id": record_id, "type": rtype.upper(),
         "name": name, "content": content, "ttl": ttl}
    if priority is not None:
        d["priority"] = priority
    return _req(acct, "dns_records", "update", "POST", d)


def delete_record(acct: dict, subdomain_id: int, record_id: str) -> dict:
    return _req(acct, "dns_records", "delete", "POST",
                {"subdomain_id": subdomain_id, "id": record_id})


# ---------------------------------------------------------------- 配额 / 信息

def quota(acct: dict) -> dict:
    return _req(acct, "quota", "", "GET")
