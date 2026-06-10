"""Telegram frontend for Oceano — a thin face over Agent.run().

Two ways to run it:
  • Folded into the web daemon (default) — enable it in Settings → Telegram.
    The web server drives the bot on its own event loop (see web/telegram_runtime).
  • Standalone:  python -m oceano.telegram_bot   (reads OCEANO_TELEGRAM_* from env)

SECURITY: the agent can run shell commands, so the bot ONLY responds to user IDs
in the allow-list (`ALLOWED`). Everyone else is refused (and told their own ID so
the owner can allowlist them).
"""
import asyncio

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import config
from oceano.agent import Agent

# Live allow-list of Telegram user IDs. Seeded from env/config; the web runtime
# overrides this from Settings so allow-list edits apply without a code change.
ALLOWED = set(config.TELEGRAM_ALLOWED)

_agents = {}  # chat_id -> Agent (one conversation/history per chat)


def _agent_for(chat_id):
    if chat_id not in _agents:
        _agents[chat_id] = Agent()
    return _agents[chat_id]


def _denied(update: Update):
    """Return a refusal string if the sender isn't allowlisted, else None."""
    uid = update.effective_user.id if update.effective_user else None
    if uid in ALLOWED:
        return None
    return (f"⛔ Not authorized. Your Telegram ID is {uid}.\n"
            f"Ask the owner to add it to the allow-list (Settings → Telegram).")


async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    denied = _denied(update)
    await update.message.reply_text(
        denied or "🌊 Oceano online. Send me a task.\n/reset clears our conversation.")


async def reset(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    if _denied(update):
        return
    _agents.pop(update.effective_chat.id, None)
    await update.message.reply_text("🧹 Conversation cleared.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    denied = _denied(update)
    if denied:
        await update.message.reply_text(denied)
        return

    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

    agent = _agent_for(chat_id)
    used = []
    agent.on_event = lambda kind, data: used.append(data["name"]) if kind == "tool_call" else None

    try:
        # Agent.run blocks (LLM + tools) — run it off the event loop.
        answer = await asyncio.to_thread(agent.run, update.message.text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ error: {e}")
        return

    if used:
        answer += "\n\n🔧 " + ", ".join(dict.fromkeys(used))
    await update.message.reply_text((answer or "(no response)")[:4000])


def build_application(token):
    """Wire the handlers onto a fresh Application. Shared by standalone + web."""
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("Set OCEANO_TELEGRAM_TOKEN (put it in oceano.env).")
    if not ALLOWED:
        print("WARNING: allow-list is empty — the bot will refuse everyone. Send "
              "it /start to learn your ID, then add it in Settings → Telegram.")
    app = build_application(config.TELEGRAM_TOKEN)
    print("Oceano Telegram bot running (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
