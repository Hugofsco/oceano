"""Run the Telegram bot inside the web daemon's event loop.

python-telegram-bot's `run_polling()` wants to own the event loop, but uvicorn
already owns one. So we drive the Application lifecycle by hand:

    initialize → start → updater.start_polling   (and the reverse to stop)

`start()`/`stop()` are idempotent and safe to call from API handlers, so the
Settings → Telegram panel can flip the bot on and off live without restarting
the web service.
"""
import asyncio
import logging

from oceano import telegram_bot

log = logging.getLogger("oceano.telegram")

_app = None        # the running telegram.ext.Application, or None
_username = None   # the bot's @username once connected
_error = None      # last start error message, surfaced in /api/status
_loop = None       # the event loop the bot lives on (so sync threads can push proactive messages)


async def start(token, allowed):
    """(Re)start the bot with this token + allow-list. Returns the bot @username."""
    global _app, _username, _error, _loop
    await stop()
    _loop = asyncio.get_running_loop()             # capture for push() from worker threads
    telegram_bot.ALLOWED = {int(x) for x in (allowed or [])}
    app = telegram_bot.build_application(token)
    try:
        await app.initialize()                     # runs post_init → registers the "/" command menu
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        _error = f"{type(e).__name__}: {e}"
        log.exception("telegram bot failed to start")
        try:
            await app.shutdown()
        except Exception:
            pass
        raise
    _app, _username, _error = app, app.bot.username, None
    log.info("Telegram bot started as @%s", _username)
    return _username


async def stop():
    """Stop the bot if running. No-op otherwise."""
    global _app, _username
    app, _app, _username = _app, None, None
    if app is None:
        return
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("error stopping telegram bot")


def push(text):
    """Proactively message every allow-listed user. Safe to call from a SYNC worker thread (the
    agent, scheduler) — it schedules onto the bot's event loop. Returns the number delivered."""
    app, loop = _app, _loop
    if not app or loop is None or not telegram_bot.ALLOWED:
        return 0
    sent = 0
    for uid in telegram_bot.ALLOWED:
        try:
            fut = asyncio.run_coroutine_threadsafe(app.bot.send_message(uid, (text or "")[:4000]), loop)
            fut.result(timeout=10)
            sent += 1
        except Exception:
            log.exception("telegram push to %s failed", uid)
    return sent


def available():
    """Can we push right now? (bot running AND at least one allow-listed user)."""
    return _app is not None and bool(telegram_bot.ALLOWED)


def status():
    """Lightweight snapshot for the Settings panel / /api/status."""
    return {"running": _app is not None, "username": _username, "error": _error}
