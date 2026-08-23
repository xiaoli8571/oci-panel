"""dns.he.net(Hurricane Electric 免费DNS)逆向接入 —— β 版,仅查看。

HE 无官方 API,此处通过模拟登录解析页面;若账号开启两步验证将无法使用。
"""
from __future__ import annotations

import re

import requests

from . import http_pool
from .pcreds import ProviderError, extra_creds

BASE = "https://dns.he.net"
_UA = "Mozilla/5.0 (X11; Linux x86_64) oci-panel-beta"


def _session(acct: dict):
    c = extra_creds(acct)
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    try:
        r = s.post(BASE + "/", data={"email": c.get("he_email", ""),
                                     "pass": c.get("he_pass", "")}, timeout=25)
    except requests.RequestException as e:
        raise ProviderError(f"HE 网络错误:{e}") from e
    if "logout" not in r.text.lower():
        raise ProviderError("HE 登录失败:请检查邮箱/密码;开启两步验证的账号暂不支持")
    return s


def zones(acct: dict) -> list[dict]:
    s = _session(acct)
    html = s.get(BASE + "/", timeout=25).text
    seen, out = set(), []
    for zid, name in re.findall(r'dom=(\d+)"[^>]*>\s*(?:<[^>]+>\s*)*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})', html):
        if zid not in seen:
            seen.add(zid)
            out.append({"zone_id": zid, "name": name})
    if not out:
        raise ProviderError("HE 已登录但未解析到域名(页面结构可能变化,β 版限制)")
    return out


_RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "SOA", "PTR", "CAA", "SPF")


def records(acct: dict, zone_id: str) -> list[dict]:
    s = _session(acct)
    html = s.get(f"{BASE}/index.cgi", params={"dom": zone_id}, timeout=25).text
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip()
               for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) >= 4 and tds[1].upper() in _RECORD_TYPES:
            rid = (re.search(r"delete_conf\?dom=\d+&id=(\d+)", tr)
                   or re.search(r"id=(\d+)", tr))
            out.append({"record_id": rid.group(1) if rid else "",
                        "name": tds[0], "type": tds[1].upper(),
                        "ttl": tds[2], "content": " ".join(tds[3:])[:300]})
    if not out:
        raise ProviderError("HE 已登录但未解析到记录(β 版限制,页面结构可能变化)")
    return out


def add_record(acct: dict, zone_id: str, name: str, rtype: str,
               content: str, ttl: int) -> dict:
    """新增记录(β):提交后回读校验是否成功。"""
    s = _session(acct)
    try:
        s.post(f"{BASE}/index.cgi",
               data={"dom": zone_id, "name": name, "type": rtype.upper(),
                     "ttl": str(ttl), "content": content}, timeout=25)
    except requests.RequestException as e:
        raise ProviderError(f"HE 网络错误:{e}") from e
    for r in records(acct, zone_id):
        if (r["name"] == name or name == "@" and r["name"].endswith(".")) \
           and r["type"] == rtype.upper() and content in r["content"]:
            return {"ok": True, "record_id": r["record_id"]}
    raise ProviderError("提交后未在校验中找到新记录 —— HE 可能拒绝了该操作"
                        "(β 版限制),请到 dns.he.net 网页确认")


def delete_record(acct: dict, zone_id: str, record_id: str) -> dict:
    """删除记录(β):先试确认链接,再试 POST del,最后回读校验。"""
    s = _session(acct)
    try:
        s.get(f"{BASE}/index.cgi",
              params={"dom": zone_id, "id": record_id, "del": "1"}, timeout=25)
        s.post(f"{BASE}/index.cgi",
               data={"dom": zone_id, "id": record_id, "del": "1"}, timeout=25)
    except requests.RequestException as e:
        raise ProviderError(f"HE 网络错误:{e}") from e
    remaining = [r["record_id"] for r in records(acct, zone_id)]
    if record_id and record_id in remaining:
        raise ProviderError("删除后校验发现记录仍存在 —— HE 可能拒绝了该操作(β 版限制)")
    return {"ok": True}


def ddns_update(acct: dict, hostname: str, secret: str, ip: str = "") -> dict:
    """通过 HE 官方 Dynamic DNS 接口更新指定记录的 IP/TXT(不受 2FA 影响)。

    返回 HE 状态词:good=成功, nochg=IP未变化, badauth=密钥错误等。
    """
    if not ip:
        try:
            ip = http_pool.get("https://ifconfig.me", timeout=10).text.strip()
        except requests.RequestException as e:
            raise ProviderError(f"获取本机公网 IP 失败:{e}") from e
    try:
        r = http_pool.get(f"{BASE}/nic/update",
                          params={"hostname": hostname, "password": secret, "myip": ip},
                          headers={"User-Agent": _UA}, timeout=25)
        text = r.text.strip()
    except requests.RequestException as e:
        raise ProviderError(f"HE DDNS 网络错误:{e}") from e
    if not text.startswith(("good", "nochg")):
        raise ProviderError(f"HE DDNS 更新失败:{text or '(空响应)'}")
    return {"result": text, "ip": ip, "hostname": hostname}
