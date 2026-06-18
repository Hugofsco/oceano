"""Server→browser UI command channel.

Lets the agent drive the web UI — open/close/arrange windows — the same way the live browser
streams to it. A tool calls push({...}); every connected browser (subscribed via /api/ui/stream)
receives it and executes it against the windows it already has. Tiny pub/sub: tools run on worker
threads, the SSE generators run on the asyncio loop, so delivery hops back to each listener's loop
with call_soon_threadsafe (the same bridge /api/chat uses).

Fire-and-forget: commands go only to currently-connected browsers (no buffering). UI control is
gated to the WEB channel at the tool layer — Telegram / background jobs never push here.
"""
import threading

_lock = threading.Lock()
_listeners = []        # list of (loop, asyncio.Queue) — one per connected browser SSE


def subscribe(loop):
    """Register an SSE client; returns its queue. Call unsubscribe(q) when it disconnects."""
    import asyncio
    q = asyncio.Queue()
    with _lock:
        _listeners.append((loop, q))
    return q


def unsubscribe(q):
    with _lock:
        _listeners[:] = [(lp, x) for (lp, x) in _listeners if x is not q]


def push(cmd):
    """Fan a UI command (a dict) out to every connected browser. Safe to call from any thread."""
    with _lock:
        targets = list(_listeners)
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(q.put_nowait, cmd)
        except Exception:
            pass


def listener_count():
    with _lock:
        return len(_listeners)
