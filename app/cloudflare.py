"""Cloudflare API 封装(REST,无需 SDK)。使用进程级连接池复用 TLS 连接。"""
from __future__ import annotations

import re

import requests

from . import http_pool
from .pcreds import ProviderError, extra_creds

API = "https://api.cloudflare.com/client/v4"


def token_of(acct: dict) -> str:
    """读取 Token 并自净化(去首尾空白与引号)。"""
    t = (extra_creds(acct).get("cf_token") or "").strip()
    return t.strip("\"").strip("'")


def verify_token(acct: dict) -> dict:
    """调用 CF 官方 /user/tokens/verify,并探测 DNS/账户可见性,返回结构化诊断。"""
    token = token_of(acct)
    if not token:
        raise ProviderError("未配置 API Token")
    info: dict = {"len": len(token),
                  "looks_like_global_key": bool(re.match(r"^[0-9a-f]{37,40}$", token, re.I)),
                  "prefix": token[:5]}
    try:
        r = http_pool.get(f"{API}/user/tokens/verify",
                          headers={"Authorization": f"Bearer {token}"}, timeout=20)
        d = r.json()
    except requests.RequestException as e:
        raise ProviderError(f"Cloudflare 网络错误:{e}") from e
    except ValueError as e:
        raise ProviderError(f"Cloudflare 返回异常:{r.text[:150]}") from e

    if d.get("success") and (d.get("result") or {}).get("status") == "active":
        info["valid"] = True
        info["status"] = "active"
        zone_items: list[dict] = []
        try:
            zone_items = zones(acct)
            info["zones"] = len(zone_items)
        except ProviderError as e:
            info["zones_error"] = str(e)
        try:
            info["accounts"] = len(accounts_list(acct))
        except ProviderError as e:
            info["accounts_error"] = str(e)
        if zone_items:
            try:
                records(acct, zone_items[0]["id"])
                info["dns_read"] = True
            except ProviderError as e:
                info["dns_read"] = False
                info["dns_error"] = str(e)
        else:
            if "zones" not in info:
                info["zones"] = 0
            info.setdefault("dns_read", None)
    else:
        info["valid"] = False
        info["errors"] = d.get("errors") or [{"code": r.status_code, "message": r.text[:120]}]
    return info


def cf(acct_or_token, method: str, path: str, *, raw: bool = False, **kw):
    token = acct_or_token if isinstance(acct_or_token, str) else token_of(acct_or_token)
    try:
        r = http_pool.request(method, API + path, timeout=25,
                              headers={"Authorization": f"Bearer {token}", **kw.pop("headers", {})},
                              **kw)
    except requests.RequestException as e:
        raise ProviderError(f"Cloudflare 网络错误:{e}") from e
    if raw:
        if r.status_code >= 400:
            try:
                msg = ";".join(e_.get("message", "") for e_ in r.json().get("errors", []))
            except Exception:  # noqa: BLE001
                msg = r.text[:200]
            raise ProviderError(f"Cloudflare [{r.status_code}] {msg}")
        return r.text
    try:
        data = r.json()
    except ValueError as e:
        raise ProviderError(f"Cloudflare 返回异常:{r.text[:200]}") from e
    if not data.get("success"):
        msgs = ";".join(e_.get("message", "") for e_ in data.get("errors", []))
        raise ProviderError(f"Cloudflare [{r.status_code}] {msgs or '未知错误'}")
    return data["result"]


# ---------------------------------------------------------------- DNS

def zones(acct: dict) -> list[dict]:
    out, page = [], 1
    while True:
        res = cf(acct, "GET", f"/zones?page={page}&per_page=50")
        out += [{"id": z["id"], "name": z["name"], "status": z.get("status", ""),
                 "plan": (z.get("plan") or {}).get("name", "")} for z in res]
        if len(res) < 50 or page > 10:
            break
        page += 1
    return out


def records(acct: dict, zone_id: str) -> list[dict]:
    out, page = [], 1
    while True:
        res = cf(acct, "GET", f"/zones/{zone_id}/dns_records?page={page}&per_page=100")
        out += [{"record_id": r["id"], "type": r["type"], "name": r["name"],
                 "content": r["content"], "ttl": r["ttl"], "proxied": bool(r.get("proxied")),
                 "priority": r.get("priority")} for r in res]
        if len(res) < 100 or page > 20:
            break
        page += 1
    return out


def create_record(acct: dict, zone_id: str, body: dict) -> dict:
    payload = {"type": body["type"], "name": body["name"], "content": body["content"],
               "ttl": body.get("ttl") or 300, "proxied": bool(body.get("proxied"))}
    if payload["type"] == "MX" and body.get("priority") is not None:
        payload["priority"] = body["priority"]
    return cf(acct, "POST", f"/zones/{zone_id}/dns_records", json=payload)


def update_record(acct: dict, zone_id: str, record_id: str, body: dict) -> dict:
    payload = {"type": body["type"], "name": body["name"], "content": body["content"],
               "ttl": body.get("ttl") or 300, "proxied": bool(body.get("proxied"))}
    return cf(acct, "PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)


def delete_record(acct: dict, zone_id: str, record_id: str) -> dict:
    return cf(acct, "DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


# ---------------------------------------------------------------- Workers

def accounts_list(acct: dict) -> list[dict]:
    res = cf(acct, "GET", "/accounts?per_page=20")
    return [{"cf_account_id": a["id"], "name": a["name"]} for a in res]


def workers_list(acct: dict, cf_account_id: str) -> list[dict]:
    res = cf(acct, "GET", f"/accounts/{cf_account_id}/workers/scripts")
    return [{"script_id": s["id"], "modified_on": s.get("modified_on", ""),
             "created_on": s.get("created_on", "")} for s in res]


def worker_code(acct: dict, cf_account_id: str, script_id: str) -> str:
    return cf(acct, "GET", f"/accounts/{cf_account_id}/workers/scripts/{script_id}", raw=True)


def worker_deploy(acct: dict, cf_account_id: str, name: str, code: str) -> dict:
    r = cf(acct, "PUT", f"/accounts/{cf_account_id}/workers/scripts/{name}",
           data=code.encode(), headers={"Content-Type": "application/javascript"})
    return {"script_id": (r or {}).get("id", name)}


def worker_delete(acct: dict, cf_account_id: str, script_id: str) -> dict:
    return cf(acct, "DELETE", f"/accounts/{cf_account_id}/workers/scripts/{script_id}")


def subdomain(acct: dict, cf_account_id: str) -> str:
    try:
        r = cf(acct, "GET", f"/accounts/{cf_account_id}/workers/subdomain")
        return r.get("subdomain", "")
    except ProviderError:
        return ""


def routes(acct: dict, zone_id: str) -> list[dict]:
    res = cf(acct, "GET", f"/zones/{zone_id}/workers/routes")
    return [{"route_id": r.get("id", ""), "pattern": r.get("pattern", ""),
             "script": r.get("script", "")} for r in res]


def create_route(acct: dict, zone_id: str, pattern: str, script: str) -> dict:
    return cf(acct, "POST", f"/zones/{zone_id}/workers/routes",
              json={"pattern": pattern, "script": script})


def delete_route(acct: dict, zone_id: str, route_id: str) -> dict:
    return cf(acct, "DELETE", f"/zones/{zone_id}/workers/routes/{route_id}")


# ================================================================ 从 GitHub 仓库部署 Worker

def deploy_from_github(acct: dict, cf_account_id: str, repo_url: str,
                       branch: str = "main", worker_name: str = "",
                       token: str = "") -> dict:
    """下载 GitHub 仓库(公开或私有 Token),找到 Worker 入口 JS,部署到 Cloudflare。"""
    import io
    import re
    import tarfile

    if not worker_name:
        raise ProviderError("请指定 Worker 名称")
    url = repo_url.rstrip("/")
    if "github.com/" in url:
        parts = url.split("github.com/")[-1].split("/")[:2]
    elif "/" in url and not url.startswith("http"):
        parts = url.split("/")[:2]
    else:
        raise ProviderError("仓库格式应为 owner/repo 或完整 GitHub URL")
    if len(parts) < 2:
        raise ProviderError("无法解析仓库地址")
    owner, repo = parts[0], parts[1]
    branch = branch or "main"
    headers = {"User-Agent": "oci-panel"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = http_pool.get(
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}",
            headers=headers, timeout=60)
        if r.status_code != 200 and token:
            r = http_pool.get(
                f"https://api.github.com/repos/{owner}/{repo}/tarball/{branch}",
                headers=headers, timeout=60)
        if r.status_code != 200:
            raise ProviderError(f"GitHub 仓库下载失败:HTTP {r.status_code}"
                                "(私有仓库请填写有读取权限的 GitHub Token)")
    except requests.RequestException as e:
        raise ProviderError(f"GitHub 网络错误:{e}") from e

    try:
        tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    except Exception as e:  # noqa: BLE001
        raise ProviderError(f"仓库压缩包解析失败:{e}") from e

    names = [m.name for m in tf.getmembers() if m.isfile()]
    main_rel = ""
    for n in names:
        if n.endswith(("wrangler.toml", "wrangler.jsonc")):
            f = tf.extractfile(n)
            if f:
                text = f.read().decode("utf-8", "replace")
                m = re.search(r'(?:^|\n)\s*main\s*=\s*["\']([^"\']+)["\']', text)
                if m:
                    main_rel = m.group(1)
                    break

    entry = ""
    if main_rel:
        hit = [n for n in names if n.endswith(main_rel)]
        if hit:
            entry = hit[0]
        else:
            raise ProviderError(f"wrangler 配置的入口文件不存在:{main_rel}")
    else:
        for pat in ("src/index.js", "src/worker.js", "worker.js", "index.js",
                    "src/index.mjs", "src/worker.mjs", "index.mjs"):
            hit = [n for n in names if n.endswith(pat)]
            if hit:
                entry = hit[0]
                break
    if not entry:
        raise ProviderError("未找到 Worker 入口文件(支持 index.js / src/index.js / worker.js / wrangler.toml main)")

    f = tf.extractfile(entry)
    code = f.read().decode("utf-8", "replace")
    return worker_deploy(acct, cf_account_id, worker_name, code)
