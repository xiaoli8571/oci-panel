"""守护中心接口:规则 / 事件 / 通知设置 / 域名监控 / 手动巡检。"""
from fastapi import APIRouter, HTTPException

from .. import database, domain_monitor, guardian, jobs, tgbot
from ..pcreds import ProviderError
from ..schemas import GuardianRule, TgReq, WebhookReq

router = APIRouter(prefix="/api/guardian", tags=["guardian"])


@router.get("/rules")
def rules():
    return {"items": guardian.get_rules()}


@router.post("/rule")
def save_rule(body: GuardianRule):
    with database.db() as c:
        if not c.execute("SELECT id FROM accounts WHERE id=?", (body.account_id,)).fetchone():
            raise HTTPException(404, "账户不存在")
    if body.traffic_action not in ("notify", "stop"):
        raise HTTPException(400, "traffic_action 仅支持 notify / stop")
    guardian.upsert_rule(body.account_id, body.enabled, body.keepalive,
                         body.traffic_limit_gb, body.traffic_action)
    return {"ok": True}


@router.get("/events")
def events(limit: int = 80):
    return {"items": guardian.recent_events(limit)}


@router.post("/run")
def run_now():
    job = jobs.start_job("guardian_run", lambda progress: guardian.run_once())
    return {"job_id": job["id"]}


@router.get("/webhook")
def webhook_get():
    return {"webhook_url": guardian.get_webhook()}


@router.post("/webhook")
def webhook_set(body: WebhookReq):
    guardian.set_webhook(body.webhook_url)
    return {"ok": True}


# ---------------------------------------------------------------- Telegram 通知

@router.get("/tg")
def tg_get():
    token, chat = guardian.get_tg()
    return {"bot_token": token, "chat_id": chat,
            "enabled": guardian.tg_enabled(), "bot_status": tgbot.status()}


@router.post("/tg")
def tg_set(body: TgReq):
    guardian.set_tg(body.bot_token, body.chat_id)
    guardian.set_tg_enabled(body.enabled)
    return {"ok": True}


@router.get("/tg/bot-status")
def tg_bot_status():
    return tgbot.status()


@router.post("/tg-test")
def tg_test():
    token, chat = guardian.get_tg()
    if not token or not chat:
        raise HTTPException(400, "请先填写并保存 Bot Token 与 Chat ID")
    try:
        guardian.send_tg(token, chat, "✅ OCI Manage Lite 测试消息:Telegram 通知已打通!")
    except ProviderError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- 域名 & SSL 到期监控

@router.get("/domains")
def domains_list():
    return {"items": domain_monitor.list_domains()}


@router.post("/domains")
def domains_add(body: dict):
    name = str(body.get("name") or "").strip()
    if not name or "." not in name:
        raise HTTPException(400, "请填写有效域名,如 example.com")
    items = domain_monitor.add_domain(name, str(body.get("host") or ""),
                                      str(body.get("note") or ""))
    return {"items": items}


@router.delete("/domains")
def domains_remove(name: str):
    return {"items": domain_monitor.remove_domain(name)}


@router.post("/domains/check")
def domains_check():
    """立即探测全部域名,返回完整报告(不产生告警事件)。"""
    if not domain_monitor.list_domains():
        return {"items": [], "checked": 0}
    results = domain_monitor.check_all()
    return {"items": results, "checked": len(results)}
