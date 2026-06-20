"""Notifications — how Oceano reaches you when it isn't a live chat.

Used by the `notify` tool and the scheduler (task results). Fans a message out to every channel
you've turned on:
  • ntfy  — a push to your phone (https://ntfy.sh or a self-hosted server) via a private topic
  • Telegram — a proactive message to the allow-listed users of your bot

Config lives in data/web.json under "notify" (set in Settings → Telegram → Notifications), with the
old OCEANO_NTFY_* env vars honoured as a fallback so existing setups keep working.
"""
import os

import requests

_DEFAULT_NTFY_URL = "https://ntfy.sh"


def config():
    """Effective notify config: web.json values, falling back to the env vars."""
    n = {}
    try:
        from oceano.web import server
        n = server.load().get("notify", {}) or {}
    except Exception:
        n = {}
    return {
        "ntfy_url": (n.get("ntfy_url") or os.environ.get("OCEANO_NTFY_URL") or _DEFAULT_NTFY_URL).rstrip("/"),
        "ntfy_topic": (n.get("ntfy_topic") or os.environ.get("OCEANO_NTFY_TOPIC") or "").strip(),
        # default Telegram ON: if the bot is configured, route notifications there too (no UI needed
        # to get notified). The send path no-ops gracefully when the bot isn't running.
        "telegram": n.get("telegram", True) is not False,
    }


def channels_ready():
    """Which channels can actually deliver right now — for the UI/status."""
    cfg = config()
    tg = False
    try:
        from oceano.web import telegram_runtime
        tg = telegram_runtime.available()
    except Exception:
        tg = False
    return {"ntfy": bool(cfg["ntfy_topic"]), "telegram": bool(cfg["telegram"] and tg)}


def _send_ntfy(cfg, message, title):
    try:
        requests.post(f"{cfg['ntfy_url']}/{cfg['ntfy_topic']}",
                      data=(message or "").encode("utf-8"),
                      headers={"Title": title or "Oceano"}, timeout=10)
        return True
    except requests.RequestException:
        return False


def _send_telegram(message, title):
    try:
        from oceano.web import telegram_runtime
        head = f"🔔 {title}\n" if (title and title != "Oceano") else "🔔 "
        return telegram_runtime.push(head + (message or "")) > 0
    except Exception:
        return False


def send(message, title="Oceano"):
    """Deliver `message` to every enabled channel. Returns a short human status string
    (also handy as a tool result)."""
    cfg = config()
    sent = []
    if cfg["ntfy_topic"] and _send_ntfy(cfg, message, title):
        sent.append("ntfy")
    if cfg["telegram"] and _send_telegram(message, title):
        sent.append("Telegram")
    if sent:
        return "notified via " + " + ".join(sent)
    # nothing delivered — explain how to fix it
    if not cfg["ntfy_topic"] and not channels_ready()["telegram"]:
        return ("(no notification channel is set up — add an ntfy topic in Settings → Telegram → "
                "Notifications, or connect the Telegram bot and keep Telegram notifications on)")
    return "(notification could not be delivered — check Settings → Telegram → Notifications)"
