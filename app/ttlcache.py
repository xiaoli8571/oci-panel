"""极简进程内 TTL 缓存:线程安全,用于元数据/客户端等可短期复用的结果。

用法:
    cache = TTLCache(ttl=120)
    val = cache.get(key)
    if val is _MISS: ... 计算后 cache.set(key, val)
或装饰器式:
    @cached(ttl=120)
    def expensive(arg): ...
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable

_MISS = object()


class TTLCache:
    """带 TTL 与条目上限的 KV 缓存。超限时按写入时间淘汰最旧的一半。"""

    def __init__(self, ttl: float = 120.0, max_items: int = 256):
        self.ttl = max(float(ttl), 1.0)
        self.max_items = max(int(max_items), 8)
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return default
            ts, val = item
            if time.time() - ts > self.ttl:
                # 过期即删(惰性),避免额外清理线程
                self._data.pop(key, None)
                return default
            return val

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self.max_items:
                for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[: self.max_items // 2]:
                    self._data.pop(k, None)
            self._data[key] = (time.time(), value)

    def drop(self, *prefixes: Hashable) -> int:
        """删除以任一前缀开头的键(如账户 id 变更后失效相关缓存)。返回删除数。"""
        n = 0
        with self._lock:
            for k in list(self._data):
                if any(str(k).startswith(str(p)) for p in prefixes):
                    self._data.pop(k, None)
                    n += 1
        return n


def cached(ttl: float = 120.0, maxsize: int = 128,
           keyfn: Callable[..., Hashable] | None = None) -> Callable:
    """装饰器:把函数结果缓存 ttl 秒(keyfn 可自定义缓存键)。"""
    cache = TTLCache(ttl=ttl, max_items=maxsize)

    def deco(fn: Callable):
        def wrapper(*args, **kw):
            if keyfn:
                key = keyfn(*args, **kw)
            else:
                key = (fn.__module__, fn.__name__, args,
                       tuple(sorted(kw.items())) if kw else ())
            val = cache.get(key, _MISS)
            if val is _MISS:
                val = fn(*args, **kw)
                cache.set(key, val)
            return val
        return wrapper
    return deco


# 全局共享实例(不同场景各自命名,便于统一失效)
meta_cache = TTLCache(ttl=120.0)      # 表单元数据(compartments / ads / images / subnets)
client_cache = TTLCache(ttl=600.0)    # 云 SDK 客户端对象
