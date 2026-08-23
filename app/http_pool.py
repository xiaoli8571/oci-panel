"""进程级共享 requests.Session:复用 TCP/TLS 连接,显著降低对 CF/DNSHE/通知渠道 的请求延迟。

各模块不要自行 requests.get/post,统一走这里的 request() / request_json()。
"""
from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter

_pool_lock = threading.Lock()
_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        with _pool_lock:
            if _session is None:
                s = requests.Session()
                adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                s.headers["User-Agent"] = "oci-panel"
                _session = s
    return _session


def request(method: str, url: str, **kw):
    """带默认超时的共享会话请求。"""
    kw.setdefault("timeout", 25)
    return session().request(method, url, **kw)


def get(url: str, **kw):
    return request("GET", url, **kw)


def post(url: str, **kw):
    return request("POST", url, **kw)
