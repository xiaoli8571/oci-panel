"""Telegram Bot 指令控制:在聊天里直接管理云实例(对齐 R探长 的核心能力)。

- 长轮询 getUpdates,独立守护线程;未启用/未配置时静默待机
- 安全:仅响应「通知设置」里配置的 Chat ID,其他会话一律忽略
- 实例定位:按 名称模糊 / 精确IP 匹配面板全部 OCI+AWS 账户的实例(60s 缓存)
- 命令:/help /ping /status /list /ip /on /off /reboot /quota /dom /guard
"""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

from . import aws_cloud, database, domain_monitor, http_pool, oci_client
from .pcreds import ProviderError

log = logging.getLogger("tgbot")

_started = threading.Event()
_last_poll = 0.0

# 实例快照缓存(避免每条消息全量扫云)
_snap_lock = threading.Lock()
_snap = {"ts": 0.0, "rows": []}
_SNAP_TTL = 60.0

# 多匹配候选记忆:{chat_id: (ts, [row])},供 /on 2 这类序号选择
_cands: dict[str, tuple[float, list]] = {}
_CAND_TTL = 300.0


# ---------------------------------------------------------------- 开关/配置

def enabled() -> bool:
    return database.get_kv("tg_bot_enabled") == "1"


def set_enabled(v: bool) -> None:
    database.set_kv("tg_bot_enabled", "1" if v else "0")


def _creds() -> tuple[str, str] | None:
    token, chat = database.get_kv("tg_bot_token") or "", database.get_kv("tg_chat_id") or ""
    return (token.strip(), chat.strip()) if token and chat else None


def status() -> dict:
    return {"enabled": enabled(), "running": _started.is_set(), "last_poll": _last_poll or None}


# ---------------------------------------------------------------- 消息发送

def _reply(chat_id: str, text: str) -> None:
    token, _ = (_creds() or ("", ""))
    if not token:
        return
    try:
        # Telegram 单条消息上限 4096
        for i in range(0, len(text), 4000):
            http_pool.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text[i:i + 4000],
                                 "disable_web_page_preview": True}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("TG 回复失败:%s", e)


# ---------------------------------------------------------------- 实例快照与匹配

def _snapshot() -> list[dict]:
    with _snap_lock:
        fresh = time.time() - _snap["ts"] < _SNAP_TTL and _snap["rows"]
    if fresh:
        return _snap["rows"]
    from .routers.instances import _build_snapshot
    data = _build_snapshot(None)
    rows = [r for r in data.get("items", []) if r.get("provider") in ("oci", "aws")]
    with _snap_lock:
        _snap.update(ts=time.time(), rows=rows)
    return rows


def _match(rows: list[dict], q: str) -> list[dict]:
    """按名称模糊或 IP 精确匹配。"""
    q = q.strip().lower()
    if not q:
        return []
    exact_ip = [r for r in rows if q in ((r.get("public_ip") or ""), (r.get("private_ip") or ""))]
    if exact_ip:
        return exact_ip
    return [r for r in rows if q in (r.get("name") or "").lower()]


def _fmt_row(r: dict) -> str:
    spec = ""
    if r.get("ocpus"):
        spec = f" {int(r['ocpus'])}C/{r.get('mem_gbs')}G"
    ip = f" {r['public_ip']}" if r.get("public_ip") else ""
    state = {"RUNNING": "✅", "STOPPED": "⏸", "STARTING": "🔄", "STOPPING": "🔄",
             "PENDING": "🔄", "TERMINATED": "🗑"}.get(r.get("state"), "❔")
    return f"{state} {r['name']}{ip} [{r.get('state','?')}{spec}] @{r.get('region','-')}"


def _power_op(row: dict, action: str) -> str:
    """执行电源操作(委托共享模块 power.power_op)。action: start/stop/reboot"""
    from . import power
    label = power.power_op(row, action)
    with _snap_lock:
        _snap["ts"] = 0   # 操作后强制刷新缓存
    return f"✅ 已向「{row['name']}」下发{label}指令"


def _cmd_stats(chat_id: str, arg: str) -> str:
    """VPS 资源监控:/stats <名称>(仅支持手动添加且保存了凭据的 VPS 行)。"""
    if not arg:
        return "用法:/stats <VPS名称>"
    rows = [r for r in _snapshot() if r.get("provider") == "vps"]
    m, err = _resolve(rows, chat_id, arg)
    if err or not m:
        return err or "未找到该 VPS(资源监控仅支持手动添加的 VPS)"
    r = m[0]
    if not r.get("vps_id"):
        return "⚠ 该行缺少 vps_id"
    try:
        from .routers.vps import collect_stats
        s = collect_stats(int(r["vps_id"]))
    except Exception as e:  # noqa: BLE001
        return f"⚠ 采集失败:{e}"
    return (f"📊 {r['name']}\n"
            f"CPU:{s.get('cpu_pct','-')}%\n"
            f"内存:{s.get('mem_used','-')}/{s.get('mem_total','-')}MB({s.get('mem_pct','-')}%)\n"
            f"磁盘:{s.get('disk_used','-')}/{s.get('disk_total','-')}GB({s.get('disk_pct','-%')})\n"
            f"运行:{s.get('uptime','-')}")


def _resolve(rows: list[dict], chat_id: str, arg: str) -> tuple[list[dict] | None, str]:
    """把参数解析为唯一实例;多匹配时缓存候选并返回提示。"""
    if arg.isdigit() and arg != "":
        ent = _cands.get(chat_id)
        if ent and time.time() - ent[0] < _CAND_TTL:
            lst = ent[1]
            idx = int(arg) - 1
            if 0 <= idx < len(lst):
                return [lst[idx]], ""
        return None, "没有可用的候选列表,请先用 /list 关键词 缩小范围"
    m = _match(rows, arg)
    if not m:
        return None, f"❌ 未找到匹配「{arg}」的实例(试试 /list {arg})"
    if len(m) > 1:
        _cands[chat_id] = (time.time(), m)
        lines = [_fmt_row(r) for r in m[:10]]
        return None, ("⚠ 匹配到多台,请用序号选择:\n" +
                      "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines)) +
                      "\n例如:/on 2")
    return m, ""


# ---------------------------------------------------------------- 各命令实现

def _cmd_help() -> str:
    return (
        "🤖 OCI Panel Bot 指令:\n"
        "/status — 全部实例状态汇总\n"
        "/list [关键词] — 实例列表\n"
        "/ip <名称|IP> — 查看实例 IP\n"
        "/on <名称|IP|序号> — 开机\n"
        "/off <名称|IP|序号> — 关机\n"
        "/reboot <名称|IP|序号> — 重启\n"
        "/reip <名称|IP|序号> — 更换公网 IP(1~3 分钟)\n"
        "/open <名称|序号> <端口> — 开放 TCP 端口\n"
        "/quota — A1 配额余量\n"
        "/dom — 域名/SSL 到期监控\n"
        "/stats <VPS名> — 资源监控(CPU/内存/磁盘)\n"
        "/guard — 守护规则状态\n"
        "/ping — 面板存活\n"
        "💡 名称支持模糊匹配;多台命中时会给出序号候选"
    )


def _cmd_status() -> str:
    rows = _snapshot()
    if not rows:
        return "当前没有可管理的云实例"
    run = sum(1 for r in rows if r.get("state") == "RUNNING")
    stop = sum(1 for r in rows if r.get("state") in ("STOPPED",))
    accts = {}
    for r in rows:
        k = f"{r.get('account_name','?')}·{r.get('region','?')}"
        accts.setdefault(k, []).append(r)
    out = [f"🖥 共 {len(rows)} 台 | 运行 {run} · 停止 {stop}", ""]
    for k, lst in list(accts.items())[:12]:
        out.append(f"📂 {k}")
        out += ["  " + _fmt_row(r) for r in lst[:12]]
    return "\n".join(out)


def _cmd_list(arg: str) -> str:
    rows = _snapshot()
    if arg:
        rows = _match(rows, arg) or []
    if not rows:
        return "没有匹配的实例"
    return "\n".join(_fmt_row(r) for r in rows[:40])


def _cmd_ip(arg: str) -> str:
    rows = _snapshot()
    m, err = _resolve(rows, "", arg)
    if err or not m:
        return err or "未找到实例"
    r = m[0]
    pub = r.get("public_ip") or "无"
    pri = r.get("private_ip") or "无"
    life = {"RESERVED": "(保留)", "EPHEMERAL": "(临时)"}.get(r.get("public_lifetime"), "")
    return f"📡 {r['name']}\n公网:{pub} {life}\n私网:{pri}"


def _make_power_cmd(op_key: str, label: str):
    action = {"on": "start", "off": "stop", "reboot": "reboot"}[op_key]

    def _run(chat_id: str, arg: str) -> str:
        if not arg:
            return f"用法:/{op_key} <名称|IP|序号>"
        rows = _snapshot()
        m, err = _resolve(rows, chat_id, arg)
        if err or not m:
            return err or "未找到实例"
        try:
            msg = _power_op(m[0], action)
        except ProviderError as e:
            return f"⚠ {e}"
        except Exception as e:  # noqa: BLE001
            return f"⚠ 失败:{e}"
        return msg + "(可用 /ip " + m[0]["name"] + " 查看结果)"
    return _run


def _run_reip(chat_id: str, arg: str) -> str:
    """换公网 IP:同步执行,期间发进度消息(约 1~3 分钟)。"""
    if not arg:
        return "用法:/reip <名称|IP|序号>"
    rows = _snapshot()
    m, err = _resolve(rows, chat_id, arg)
    if err or not m:
        return err or "未找到实例"
    r = m[0]
    if r.get("provider") not in ("oci", "aws", "ibm"):
        return "⚠ 换 IP 仅支持 OCI / AWS / IBM 实例"
    from . import jobs
    from .database import db
    with db() as c:
        acct_row = c.execute("SELECT * FROM accounts WHERE id=?", (r["account_id"],)).fetchone()
    if not acct_row:
        return "⚠ 账户不存在"
    _reply(chat_id, f"⏳ 开始更换「{r['name']}」的公网 IP(后台任务,预计 1~5 分钟)…")
    acct = dict(acct_row)
    if r["provider"] == "oci":
        def _job(progress):
            res = oci_client.change_public_ip(progress, acct, r["compartment_id"], r["id"])
            with _snap_lock:
                _snap["ts"] = 0
            return res
        job = jobs.start_job(f"tg_reip_{r['name']}", _job)
        return (f"📤 任务已提交(job {job['id'][:8]}…)\n完成后新 IP 会写进任务日志;"
                f"\n也可稍后用 /ip {r['name']} 查看")
    if r["provider"] == "ibm":
        from . import ibm_cloud

        def _job_ibm(progress):
            res = ibm_cloud.change_public_ip(progress, acct, "", r["id"])
            with _snap_lock:
                _snap["ts"] = 0
            return res
        job = jobs.start_job(f"tg_reip_{r['name']}", _job_ibm)
        return f"📤 任务已提交(job {job['id'][:8]}…),完成后可用 /ip {r['name']} 查看"

    # AWS EC2 / Lightsail 同步执行(带进度消息)
    from . import aws_cloud

    def progress(msg):
        pass
    try:
        if r.get("service") == "lightsail":
            res = aws_cloud.lightsail_change_ip(progress, acct, r.get("region") or "", r["id"])
        else:
            res = aws_cloud.change_public_ip(progress, acct, "", r["id"])
    except Exception as e:  # noqa: BLE001
        return f"⚠ 换 IP 失败:{e}"
    with _snap_lock:
        _snap["ts"] = 0
    return f"✅ {r['name']} 换 IP 完成:\n旧:{res.get('old_ip') or '无'} → 新:{res.get('new_ip') or '(未获取到)'}"


def _run_open(chat_id: str, arg: str) -> str:
    """开放 TCP 端口:/open <名称|序号> <端口>。"""
    parts = arg.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return "用法:/open <名称|序号> <端口>(如 /open web-prod 8080)"
    name_q, port = parts[0], int(parts[1])
    if not (1 <= port <= 65535):
        return "⚠ 端口范围 1-65535"
    rows = _snapshot()
    m, err = _resolve(rows, chat_id, name_q)
    if err or not m:
        return err or "未找到实例"
    r = m[0]
    if r.get("provider") != "oci":
        return "⚠ 开端口目前仅支持 OCI 实例"
    try:
        from .database import db
        with db() as c:
            acct_row = c.execute("SELECT * FROM accounts WHERE id=?", (r["account_id"],)).fetchone()
        res = oci_client.open_ports(dict(acct_row), r["compartment_id"], r["id"], [port])
        added, skipped = res.get("added") or [], res.get("skipped") or []
        if added:
            return f"✅ 「{r['name']}」已放行 TCP/{port}(v4+v6 全网段)"
        if skipped:
            return f"ℹ TCP/{port} 此前已放行,无需重复操作"
        return f"完成:{added} 放行,{skipped} 已存在"
    except Exception as e:  # noqa: BLE001
        return f"⚠ 开端口失败:{e}"


def _cmd_quota() -> str:
    from .database import db
    with db() as c:
        accs = [dict(r) for r in c.execute("SELECT * FROM accounts").fetchall()]
    ocis = [a for a in accs if (a.get("provider") or "oci") == "oci"]
    if not ocis:
        return "没有 OCI 账户"
    outs = []
    for a in ocis[:8]:
        try:
            d = oci_client.account_quota(a)
        except Exception as e:  # noqa: BLE001
            outs.append(f"📦 {a['name']}:查询失败 {e}")
            continue
        head = f"📦 {a['name']}({d.get('region')},{d.get('payment_model') or '订阅未知'})"
        outs.append(head)
        for lim in d.get("limits", []):
            if lim["name"] not in ("standard-a1-core-count", "standard-a1-memory-count"):
                continue
            tag = "A1核" if "core" in lim["name"] else "A1内存G"
            for it in lim["items"]:
                outs.append(f"  {it['ad']}: {tag} 已用 {it['used']} / 余 {it['available']}")
    return "\n".join(outs[:40])


def _cmd_dom() -> str:
    items = domain_monitor.list_domains()
    if not items:
        return "尚未配置域名监控(面板→守护中心)"
    res = sorted(domain_monitor.check_all(),
                 key=lambda x: (x.get("min_days_left") is None, x.get("min_days_left") or 9999))
    out = ["🌐 域名监控:"]
    for r in res[:20]:
        seg = [f"· {r['name']}"]
        if isinstance(r.get("ssl_days_left"), int):
            seg.append(f"SSL {r['ssl_days_left']}天")
        if isinstance(r.get("domain_days_left"), int):
            seg.append(f"域名 {r['domain_days_left']}天")
        out.append(" ".join(seg))
    return "\n".join(out)


def _cmd_guard() -> str:
    from . import guardian
    rules = guardian.get_rules()
    if not rules:
        return "尚无守护规则(面板→守护中心)"
    return "\n".join(
        f"· {r.get('name') or r['account_id']}: {'🟢启用' if r.get('enabled') else '⚪停用'}"
        f"{' · 保活' if r.get('keepalive') else ''}"
        f"{' · 流量阈值' + str(r.get('traffic_limit_gb')) + 'GB' if r.get('traffic_limit_gb') else ''}"
        for r in rules[:20])


# ---------------------------------------------------------------- 主循环

_CMDS = {
    "/help": lambda cid, arg: _cmd_help(),
    "/start": lambda cid, arg: _cmd_help(),
    "/ping": lambda cid, arg: f"🏓 面板在线 v{_version()}",
    "/status": lambda cid, arg: _cmd_status(),
    "/list": lambda cid, arg: _cmd_list(arg),
    "/ls": lambda cid, arg: _cmd_list(arg),
    "/ip": lambda cid, arg: _cmd_ip(arg),
    "/on": _make_power_cmd("on", "开机"),
    "/up": _make_power_cmd("on", "开机"),
    "/off": _make_power_cmd("off", "软关机"),
    "/down": _make_power_cmd("off", "软关机"),
    "/reboot": _make_power_cmd("reboot", "重启"),
    "/reip": lambda cid, arg: _run_reip(cid, arg),
    "/open": lambda cid, arg: _run_open(cid, arg),
    "/stats": _cmd_stats,
    "/quota": lambda cid, arg: _cmd_quota(),
    "/dom": lambda cid, arg: _cmd_dom(),
    "/guard": lambda cid, arg: _cmd_guard(),
}


def _version() -> str:
    from . import config
    return config.VERSION


def _handle_update(u: dict, chat_id: str) -> None:
    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    cid = str((msg.get("chat") or {}).get("id") or "")
    if not text or cid != chat_id:
        return   # 仅响应配置的 Chat ID
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    fn = _CMDS.get(cmd)
    if not fn:
        _reply(cid, f"未知指令 {cmd},发送 /help 查看支持列表")
        return
    try:
        _reply(cid, fn(cid, arg))
    except Exception as e:  # noqa: BLE001
        log.exception("处理 %s 异常", cmd)
        _reply(cid, f"⚠ 处理失败:{e}")


def _loop():
    global _last_poll
    offset = int(database.get_kv("tg_offset") or 0)
    while True:
        if not enabled() or not _creds():
            time.sleep(15)
            continue
        params = {"timeout": 25, "offset": offset,
                  "allowed_updates": json.dumps(["message"])}
        if offset == 0:
            params["drop_pending_updates"] = "true"   # 首次启动清积压
        token, _ = _creds()
        try:
            r = http_pool.get(f"https://api.telegram.org/bot{token}/getUpdates",
                              params=params, timeout=35)
            d = r.json()
        except requests.RequestException as e:
            log.warning("getUpdates 网络异常:%s", e)
            time.sleep(10)
            continue
        except ValueError:
            time.sleep(10)
            continue
        _last_poll = time.time()
        if not d.get("ok"):
            log.warning("TG API 错误:%s", str(d)[:200])
            time.sleep(15)
            continue
        _, chat_id = _creds()
        for u in d.get("result", []):
            try:
                offset = max(offset, u["update_id"] + 1)
                database.set_kv("tg_offset", str(offset))
                _handle_update(u, chat_id)
            except Exception:  # noqa: BLE001
                log.exception("处理 update 异常")


def start():
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_loop, daemon=True, name="tgbot").start()
    log.info("Telegram Bot 线程已启动(是否响应取决于开关与 Token 配置)")
