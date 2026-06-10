"""Oceano engine — the single daemon. One process runs everything:

  • the FastAPI web UI                     (the main server, owns the event loop)
  • the Telegram bot                       (started by the web app's lifespan;
                                            toggle it in Settings → Telegram)
  • the scheduled-task runner              (a background task; autonomous agent)
  • the llama.cpp embedding server (:8082) (a supervised child process — it's a
                                            C++ binary, so it can't live *inside*
                                            Python, but the engine starts it,
                                            restarts it on crash, reaps it on
                                            shutdown, and pipes its logs into this
                                            one journal)

    ./venv/bin/python -m oceano.engine

Run it as the single systemd unit (systemd/oceano.service). If you'd rather run
the embedding server yourself (its own unit, a remote box, …), set
OCEANO_EMBED_MANAGED=0 and the engine won't touch it.
"""
import asyncio
import os
import signal
from pathlib import Path

import uvicorn

from oceano import scheduler
from oceano.web.server import app

ROOT = Path(__file__).resolve().parent.parent
EMBED_SCRIPT = ROOT / "scripts" / "serve-embeddings.sh"
SCHED_INTERVAL = 30      # seconds between due-task checks


def log(msg):
    print(msg, flush=True)   # flush: stdout is block-buffered into the journal


async def _sleep_or_stop(stop, secs):
    """Sleep up to `secs`, waking early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except asyncio.TimeoutError:
        pass


async def embed_supervisor(stop):
    """Run the embedding server as a child process; restart it if it dies, and
    terminate it cleanly when `stop` is set."""
    if os.environ.get("OCEANO_EMBED_MANAGED", "1") != "1":
        log("[embed] unmanaged (OCEANO_EMBED_MANAGED=0) — assuming it runs elsewhere")
        return
    if not EMBED_SCRIPT.exists():
        log(f"[embed] launcher missing: {EMBED_SCRIPT} — not starting embed server")
        return

    backoff = 2
    while not stop.is_set():
        proc = await asyncio.create_subprocess_exec("bash", str(EMBED_SCRIPT))
        log(f"[embed] embedding server up (pid {proc.pid})")

        # Wait for the child to exit OR a shutdown request, whichever comes first.
        waiter = asyncio.ensure_future(proc.wait())
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)

        if stop.is_set():
            if proc.returncode is None:        # shutting down → take the child with us
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            for f in (waiter, stopper):
                f.cancel()
            log("[embed] embedding server stopped")
            return

        stopper.cancel()                       # child died on its own → restart it
        log(f"[embed] exited (rc={proc.returncode}); restarting in {backoff}s")
        await _sleep_or_stop(stop, backoff)
        backoff = min(backoff * 2, 30)         # back off on a crash-loop, cap at 30s


async def scheduler_loop(stop):
    """Check for due scheduled tasks every SCHED_INTERVAL seconds."""
    log(f"[scheduler] watching for due tasks (every {SCHED_INTERVAL}s)")
    while not stop.is_set():
        try:
            ran = await asyncio.to_thread(scheduler.run_due_once)  # blocking → worker thread
            if ran:
                log(f"[scheduler] ran {ran} task(s)")
        except Exception as e:
            log(f"[scheduler] tick error: {e}")
        await _sleep_or_stop(stop, SCHED_INTERVAL)
    log("[scheduler] stopped")


async def run():
    host = os.environ.get("OCEANO_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("OCEANO_WEB_PORT", "8800"))
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=15))
    server.install_signal_handlers = lambda: None   # the engine owns the signals, not uvicorn

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        stop.set()
        server.should_exit = True               # make uvicorn's serve() return

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    bg = [asyncio.create_task(embed_supervisor(stop), name="embed"),
          asyncio.create_task(scheduler_loop(stop), name="scheduler")]

    log(f"⚓ Oceano engine — web http://{host}:{port} · telegram + scheduler + embeddings in-process")
    try:
        # Runs the app lifespan (which starts the Telegram bot), then serves until
        # request_stop() fires; on the way out the lifespan stops the bot.
        await server.serve()
    finally:
        request_stop()
        try:
            await asyncio.wait_for(asyncio.gather(*bg, return_exceptions=True), timeout=14)
        except asyncio.TimeoutError:
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
        log("⚓ Oceano engine stopped")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
