"""守护中心:停机保活 / 月流量守护 / 事件记录与 Webhook 通知。

独立后台线程按 GUARDIAN_INTERVAL(默认 300s)巡检所有启用规则。
被流量守护关停的账号,当月不会被保活重新拉起。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
import traceback
import urllib.parse
import urllib.request

import requests

from . import aws_cloud, database, http_pool, oci_client, rescue
from .pcreds import ProviderError

log = logging.getLogger("guardian")

INTERVAL = max(int(os.getenv("GUARDIAN_INTERVAL", "300")), 60)

_started = threading.Event()


# ---------------------------------------------------------------- 规则 CRUD

def get_rules() -> list[dict]:
    with database.db() as c:
        rows = c.execute(
            "SELECT g.*, a.name, a.region FROM guardian g "
            "LEFT JOIN accounts a ON a.id = g.account_id ORDER BY g.account_id").fetchall()
    return [dict(r) for r in rows]


def upsert_rule(account_id: int, enabled: bool, keepalive: bool,
                traffic_limit_gb: float, traffic_action: str) -> None:
    with database.db() as c:
        c.execute(
            "INSERT INTO guardian(account_id,enabled,keepalive,traffic_limit_gb,traffic_action)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,"
            " keepalive=excluded.keepalive, traffic_limit_gb=excluded.traffic_limit_gb,"
            " traffic_action=excluded.traffic_action, updated_at=datetime('now','localtime')",
            (account_id, int(enabled), int(keepalive), traffic_limit_gb, traffic_action))


def recent_events(limit: int = 80) -> list[dict]:
    with database.db() as c:
        rows = c.execute(
            "SELECT e.*, a.name AS account_name FROM g_events e "
            "LEFT JOIN accounts a ON a.id=e.account_id "
            "ORDER BY e.id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
    return [dict(r) for r in rows]


def _record_event(account_id: int | None, kind: str, message: str,
                  notify: bool = False, dedupe_minutes: int = 30) -> None:
    """写入事件表;同类消息在 dedupe_minutes 内去重;可选 Webhook 通知。"""
    with database.db() as c:
        dup = c.execute(
            "SELECT id FROM g_events WHERE account_id IS ? AND kind=? AND message=? "
            "AND created_at > datetime('now','localtime', ?)",
            (account_id, kind, message, f"-{dedupe_minutes} minutes")).fetchone()
        if dup:
            return
        c.execute("INSERT INTO g_events(account_id,kind,message) VALUES(?,?,?)",
                  (account_id, kind, message))
    if notify:
        _notify(f"[OCI面板] {message}")


# ---------------------------------------------------------------- 通知

def get_webhook() -> str:
    return database.get_kv("notify_webhook") or ""


def set_webhook(url: str) -> None:
    database.set_kv("notify_webhook", url.strip())


def get_tg() -> tuple[str, str]:
    return (database.get_kv("tg_bot_token") or "", database.get_kv("tg_chat_id") or "")


def set_tg(bot_token: str, chat_id: str) -> None:
    database.set_kv("tg_bot_token", bot_token.strip())
    database.set_kv("tg_chat_id", chat_id.strip())


def tg_enabled() -> bool:
    from . import tgbot
    return tgbot.enabled()


def set_tg_enabled(v: bool) -> None:
    from . import tgbot
    tgbot.set_enabled(v)


def send_tg(token: str, chat_id: str, text: str) -> None:
    """通过 Telegram Bot API 发送消息;失败抛 ProviderError(带官方描述)。"""
    try:
        r = http_pool.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat_id, "text": text,
                                 "disable_web_page_preview": True}, timeout=10)
        d = r.json()
    except requests.RequestException as e:
        raise ProviderError(f"Telegram 网络错误:{e}") from e
    except ValueError as e:
        raise ProviderError(f"Telegram 返回异常:{r.text[:150]}") from e
    if not d.get("ok"):
        raise ProviderError(f"Telegram 拒绝:{d.get('description', '未知错误')}")


def _notify(text: str) -> None:
    # 优先 Telegram,未配置则回退通用 Webhook
    token, chat = get_tg()
    if token and chat:
        try:
            send_tg(token, chat, f"[OCI面板] {text}")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("TG 推送失败,尝试 Webhook 回退:%s", e)
    url = get_webhook()
    if not url:
        return
    try:
        final = url.replace("{msg}", urllib.parse.quote_plus(text))
        req = urllib.request.Request(final, headers={"User-Agent": "oci-panel"})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:  # noqa: BLE001
        log.warning("Webhook 通知失败:%s", e)


# ---------------------------------------------------------------- 巡检逻辑

def _month_traffic_gb(acct: dict) -> float | None:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hours = max(int((now - start).total_seconds() // 3600) + 1, 1)
    d = oci_client.traffic_usage(acct, acct["tenancy_ocid"], hours)
    return (d["total_down_bytes"] + d["total_up_bytes"]) / 1024 ** 3


def run_once() -> dict:
    """执行一轮巡检(也可由面板手动触发)。"""
    stats = {"checked": 0, "started": [], "stopped": [], "errors": []}
    for rule in get_rules():
        if not rule.get("enabled"):
            continue
        aid = rule["account_id"]
        with database.db() as c:
            row = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if not row:
            continue
        acct = dict(row)
        p = (acct.get("provider") or "oci").lower()
        if p not in ("oci", "aws", "ibm"):
            continue   # DNS 类账户不参与守护
        stats["checked"] += 1
        try:
            if p == "aws":
                rows = aws_cloud.list_instances(acct)
                ls_rows, ls_errs = aws_cloud.list_lightsail(acct)
                rows += ls_rows
            else:
                rows = oci_client.list_instances(acct)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"{acct['name']}: {e}")
            _record_event(aid, "error", f"巡检失败:{e}")
            continue

        # 救援中的实例:启动盘已卸下,保活拉起/自动关停都可能破坏救援流程,一律跳过
        rescuing = set()
        if p == "oci":
            try:
                rescuing = rescue.rescuing_instance_ids(aid)
            except Exception:  # noqa: BLE001
                pass

        running = [r for r in rows if r["state"] == "RUNNING"]
        stopped = [r for r in rows if r["state"] in ("STOPPED", "SOFTSTOPPED")]

        from . import power as power_mod

        def _start(r):
            """按提供商拉起实例(共享路由)。"""
            return power_mod.power_op(r, "start")

        def _stop(r):
            return power_mod.power_op(r, "stop")

        # --- 流量守护(暂仅 OCI,依赖 computeagent 指标) ---
        limit = float(rule.get("traffic_limit_gb") or 0) if p == "oci" else 0
        over = False
        if limit > 0 and (running or stopped):
            try:
                gb = _month_traffic_gb(acct)
                if gb is not None and gb >= limit:
                    over = True
                    action = rule.get("traffic_action", "notify")
                    msg = f"「{acct['name']}」当月流量 {gb:.2f}GB 已超阈值 {limit}GB"
                    if action == "stop":
                        database.set_kv(f"tgstop:{aid}", dt.date.today().strftime("%Y-%m"))
                        for r in running:
                            if r["id"] in rescuing:
                                continue   # 救援中的目标机不动
                            _stop(r)
                            stats["stopped"].append(r["name"])
                            _record_event(aid, "traffic", f"{msg},已自动关停「{r['name']}」", notify=True)
                    else:
                        _record_event(aid, "traffic", msg + "(仅通知)", notify=True)
            except Exception as e:  # noqa: BLE001
                _record_event(aid, "error", f"流量统计失败:{e}")

        # --- 停机保活(被流量关停的账号当月跳过)---
        if rule.get("keepalive") and stopped and not over:
            cur_month = dt.date.today().strftime("%Y-%m")
            if database.get_kv(f"tgstop:{aid}") == cur_month:
                _record_event(aid, "keepalive",
                              "该账号已被流量守护关停,本月保活暂停")
                continue
            for r in stopped:
                if r["id"] in rescuing:
                    _record_event(aid, "keepalive",
                                  f"「{r['name']}」处于救援会话中,跳过保活拉起")
                    continue
                try:
                    _start(r)
                    stats["started"].append(r["name"])
                    _record_event(aid, "keepalive",
                                  f"检测到「{r['name']}」处于停止状态,已自动拉起", notify=True)
                except Exception as e:  # noqa: BLE001
                    _record_event(aid, "error", f"拉起「{r['name']}」失败:{e}")
    return stats


def check_domains() -> dict:
    """执行一轮域名/SSL 到期检查,命中阈值档位时写事件+通知(每域名每档位每天最多提醒一次)。"""
    from . import domain_monitor
    items = domain_monitor.list_domains()
    if not items:
        return {"checked": 0, "alerts": []}
    results = domain_monitor.check_all()
    today = dt.date.today().isoformat()
    alerts = []
    for r in results:
        lvl = r.get("alert_level")
        if lvl is None:
            continue
        name = r["name"]
        dedupe_key = f"domalert:{name}:{lvl}"
        if database.get_kv(dedupe_key) == today:
            continue
        database.set_kv(dedupe_key, today)
        parts = [f"「{name}」"]
        if isinstance(r.get("ssl_days_left"), int):
            parts.append(f"SSL 证书剩余 {r['ssl_days_left']} 天(到期 {r.get('ssl_expires')})")
        if isinstance(r.get("domain_days_left"), int):
            parts.append(f"域名注册剩余 {r['domain_days_left']} 天(到期 {r.get('domain_expires')})")
        msg = ",".join(parts) + ",请及时处理"
        _record_event(None, "domain", msg, notify=True, dedupe_minutes=20 * 60)
        alerts.append({"name": name, "level": lvl})
    return {"checked": len(results), "alerts": alerts}


def _loop():
    last_domain_check = 0.0
    while True:
        try:
            s = run_once()
            if s["checked"]:
                log.info("巡检完成:%s", s)
        except Exception:  # noqa: BLE001
            log.error("巡检异常:%s", traceback.format_exc())
        # 域名/SSL 到期检查:每 6 小时一轮(告警档位内每日去重)
        if time.time() - last_domain_check >= 6 * 3600:
            last_domain_check = time.time()
            try:
                r = check_domains()
                if r["alerts"]:
                    log.info("域名告警:%s", r["alerts"])
            except Exception:  # noqa: BLE001
                log.error("域名检查异常:%s", traceback.format_exc())
        time.sleep(INTERVAL)


def start():
    if _started.is_set():
        return
    _started.set()
    threading.Thread(target=_loop, daemon=True, name="guardian").start()
    log.info("守护线程已启动,巡检间隔 %ss", INTERVAL)
