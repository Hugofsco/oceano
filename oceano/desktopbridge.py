"""Server↔OceanoDesktop RPC channel — the request/response sibling of uibridge.py.

uibridge.push() is fire-and-forget: the agent tells the web UI to open/close/arrange a window and
doesn't need anything back. Native OS actions (a screenshot, a file picker, a notification) can only
run in OceanoDesktop's Electron MAIN process — not a browser tab, and not even the web UI's renderer,
which has no OS access by design (contextIsolation, no nodeIntegration) — and the agent needs the
RESULT back (the path the user picked) to keep reasoning. So this is a tiny synchronous RPC: call()
pushes a command to the connected desktop app and blocks the calling tool's worker thread (see
routes_chat.py's worker()) on a threading.Event until resolve() answers it or the timeout passes.

Fire-and-forget delivery, request/response completion: like uibridge, nothing is buffered for a
client that isn't connected right now — call() fails fast in that case instead of queuing.
"""
import itertools
import threading

_lock = threading.Lock()
_listeners = []     # list of (loop, asyncio.Queue) — in practice at most one: the desktop app's main process
_pending = {}        # request id -> {"event": threading.Event, "ok": bool, "result": object}
_ids = itertools.count(1)


def subscribe(loop):
    """Register the desktop app's SSE connection; returns its queue. Call unsubscribe(q) on disconnect."""
    import asyncio
    q = asyncio.Queue()
    with _lock:
        _listeners.append((loop, q))
    return q


def unsubscribe(q):
    with _lock:
        _listeners[:] = [(lp, x) for (lp, x) in _listeners if x is not q]


def listener_count():
    with _lock:
        return len(_listeners)


def call(action, timeout=15, **payload):
    """Ask the connected desktop app to perform `action`; blocks the calling thread until it answers
    or `timeout` seconds pass. Returns (ok, result_or_message) — ok=False covers "not connected",
    "timed out", and the desktop app reporting its own failure, so callers just need one branch."""
    with _lock:
        targets = list(_listeners)
    if not targets:
        return False, "the desktop app isn't connected right now"
    rid = next(_ids)
    ev = threading.Event()
    slot = {"event": ev, "ok": False, "result": None}
    with _lock:
        _pending[rid] = slot
    cmd = {"type": "rpc", "id": rid, "action": action, **payload}
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(q.put_nowait, cmd)
        except Exception:
            pass
    try:
        got = ev.wait(timeout)
    finally:
        with _lock:
            _pending.pop(rid, None)
    if not got:
        return False, "the desktop app didn't respond in time"
    return slot["ok"], slot["result"]


def resolve(rid, ok, result):
    """The desktop app's answer to a pending call(), matched by id. Returns False (no-op) if that
    call already timed out or `rid` is unknown — never raises, so a stray/duplicate POST is harmless."""
    with _lock:
        slot = _pending.get(rid)
    if not slot:
        return False
    slot["ok"], slot["result"] = ok, result
    slot["event"].set()
    return True
