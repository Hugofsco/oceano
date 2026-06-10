"""Run the Telegram bot inside the web daemon's event loop.

python-telegram-bot's `run_polling()` wants to own the event loop, but uvicorn
already owns one. So we drive the Application lifecycle by hand:

    initialize → start → updater.start_polling   (and the reverse to stop)

`start()`/`stop()` are idempotent and safe to call from API handlers, so the
Settings → Telegram panel can flip the bot on and off live without restarting
the web service.
"""
import logging

from oceano import telegram_bot

log = logging.getLogger("oceano.telegram")

_app = None        # the running telegram.ext.Application, or None
_username = None   # the bot's @username once connected
_error = None      # last start error message, surfaced in /api/status


async def start(token, allowed):
    """(Re)start the bot with this token + allow-list. Returns the bot @username."""
    global _app, _username, _error
    await stop()
    telegram_bot.ALLOWED = {int(x) for x in (allowed or [])}
    app = telegram_bot.build_application(token)
    try:
        await app.initialize()
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


def status():
    """Lightweight snapshot for the Settings panel / /api/status."""
    return {"running": _app is not None, "username": _username, "error": _error}
