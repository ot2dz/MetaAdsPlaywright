# -*- coding: utf-8 -*-
"""Notifications: Telegram bot and/or a generic webhook."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger("notify")


def _telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


def _webhook(url: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        logger.warning("webhook send failed: %s", exc)
        return False


def notify_new_ads(settings: dict, target: str, new_ads: list[dict], total: int) -> dict:
    """Send a 'new ads found' notification. Returns a result summary."""
    if settings.get("notify_enabled", "false").lower() not in ("1", "true", "yes", "on"):
        return {"sent": False, "reason": "disabled"}
    if not new_ads:
        return {"sent": False, "reason": "no new ads"}

    lines = [f"🆕 <b>{len(new_ads)} إعلان جديد</b> لهدف: <b>{target}</b>"]
    for ad in new_ads[:10]:
        page = ad.get("page_name") or ""
        store = ad.get("store_domain") or ""
        link = ad.get("link_url") or ""
        lines.append(f"• {page} {('· '+store) if store else ''}")
        if link:
            lines.append(f"  {link}")
    if len(new_ads) > 10:
        lines.append(f"… و{len(new_ads) - 10} أخرى")
    lines.append(f"(الإجمالي المخزّن: {total})")
    text = "\n".join(lines)

    result = {"sent": False}
    token = settings.get("telegram_token", "").strip()
    chat = settings.get("telegram_chat_id", "").strip()
    if token and chat:
        result["telegram"] = _telegram(token, chat, text)
        result["sent"] = result["sent"] or result["telegram"]
    hook = settings.get("notify_webhook", "").strip()
    if hook:
        result["webhook"] = _webhook(hook, {
            "event": "new_ads", "target": target, "count": len(new_ads),
            "total": total, "ads": new_ads,
        })
        result["sent"] = result["sent"] or result["webhook"]
    return result
