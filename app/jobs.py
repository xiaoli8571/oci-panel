"""轻量后台任务:用于"更换公网 IP"这类需要等待实例关机/开机的长操作。

任务在独立线程执行,前端通过 /api/jobs/{id} 轮询日志与状态。
"""
import secrets
import threading
import time
import traceback

_lock = threading.Lock()
_JOBS: dict = {}
_MAX_KEEP = 60
_MAX_LOG = 2000   # 单任务日志条数上限,防止长任务撑爆内存


def _prune() -> None:
    finished = sorted(
        (j["created"], jid) for jid, j in _JOBS.items() if j["status"] != "running"
    )
    while len(finished) > _MAX_KEEP:
        _, old = finished.pop(0)
        _JOBS.pop(old, None)


def start_job(name: str, fn, *args) -> dict:
    """启动任务。fn 的第一个参数为 progress 回调,用于追加日志。"""
    jid = secrets.token_hex(8)
    job = {
        "id": jid,
        "name": name,
        "status": "running",   # running / done / error
        "log": [],
        "result": None,
        "error": None,
        "created": time.time(),
    }
    with _lock:
        _JOBS[jid] = job
        _prune()

    def cb(msg: str):
        with _lock:
            log_list = job["log"]
            log_list.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(log_list) > _MAX_LOG:
                del log_list[: len(log_list) - _MAX_LOG]

    def worker():
        try:
            job["result"] = fn(cb, *args)
            job["status"] = "done"
            cb("✅ 任务完成")
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(e) or repr(e)
            cb(f"❌ 任务失败:{job['error']}")
            traceback.print_exc()

    threading.Thread(target=worker, daemon=True, name=f"job-{name}-{jid}").start()
    return {"id": jid, "name": name, "status": job["status"], "log": [], "result": None, "error": None}


def get_job(jid: str) -> dict | None:
    """返回任务快照(日志拷贝),避免遍历时列表被并发修改。"""
    with _lock:
        job = _JOBS.get(jid)
        if not job:
            return None
        return {
            "id": job["id"], "name": job["name"], "status": job["status"],
            "log": list(job["log"]), "result": job["result"],
            "error": job["error"], "created": job["created"],
        }
