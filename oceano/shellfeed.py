"""Per-chat shell-activity feed.

Every command Oceano's own run_shell tool runs, and every Bash/shell command Claude or Codex run
natively as the resident mind, gets echoed here as plain terminal text, tagged with the chat
session it belongs to (turnctx.get().session) — so each chat's spectator panel shows only that
chat's own commands, never another conversation running in parallel in a different window. Work
with no session (a scheduled task, a workflow run, a background job — nobody's chat) is tagged
None; it's kept for its own backlog like any other "session" but has no chat panel subscribed to
it, so pushing to it is effectively fire-and-forget.

Tiny pub/sub, mirroring oceano.uibridge: tools/mind loops run on worker threads or asyncio tasks,
the SSE generator runs on the asyncio loop, so delivery hops back with call_soon_threadsafe.

A short per-session backlog is kept (unlike uibridge, which is pure fire-and-forget) so a
spectator panel opened mid-session isn't blank — it gets that chat's recent tail immediately,
then live text after that. Bounded to the most recently active sessions so a long-running daemon
doesn't accumulate one backlog per chat ever created.
"""
import threading
from collections import deque

_lock = threading.Lock()
_listeners = []        # list of (loop, asyncio.Queue, session) — one per connected spectator SSE
_BACKLOG_CAP = 20000    # chars of recent text kept PER SESSION for a freshly-opened spectator
_MAX_SESSIONS = 50      # bound memory: don't keep a backlog for unboundedly many old chats
_backlogs = {}          # session -> deque[text], insertion order == recency (re-inserted on push)
_backlog_lens = {}      # session -> int


def subscribe(loop, session):
    """Register an SSE client for one chat's activity; returns its queue, pre-seeded with that
    chat's recent backlog. Call unsubscribe(q) when it disconnects."""
    import asyncio
    q = asyncio.Queue()
    with _lock:
        for text in _backlogs.get(session, ()):
            q.put_nowait(text)
        _listeners.append((loop, q, session))
    return q


def unsubscribe(q):
    with _lock:
        _listeners[:] = [(lp, x, s) for (lp, x, s) in _listeners if x is not q]


def push(text, session=None):
    """Fan a raw terminal-text chunk out to every spectator watching THIS session, and keep it
    in that session's backlog for the next one to connect. Safe to call from any thread."""
    if not text:
        return
    with _lock:
        buf = _backlogs.pop(session, None) or deque()
        buf.append(text)
        n = _backlog_lens.pop(session, 0) + len(text)
        while n > _BACKLOG_CAP and len(buf) > 1:
            n -= len(buf.popleft())
        _backlogs[session] = buf                  # reinsert last → dict order tracks recency
        _backlog_lens[session] = n
        while len(_backlogs) > _MAX_SESSIONS:
            oldest = next(iter(_backlogs))
            if oldest == session:                 # never evict the one just written to
                break
            del _backlogs[oldest]
            _backlog_lens.pop(oldest, None)
        targets = [(lp, q) for (lp, q, s) in _listeners if s == session]
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(q.put_nowait, text)
        except Exception:
            pass


def listener_count(session=None):
    """Total listeners, or just those watching one session when `session` is given."""
    with _lock:
        if session is None:
            return len(_listeners)
        return sum(1 for (_, _, s) in _listeners if s == session)
