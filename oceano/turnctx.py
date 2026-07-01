"""The ONE per-turn execution context: channel, workspace override, chat session, taint.

These four used to live in separate thread-locals scattered across tools/core.py (channel,
workspace), safety.py (taint), and mindbridge.py (session) — four bracketing disciplines that
every new entry point (web turn, Telegram, scheduler, workflows, bridge calls, worker threads)
had to remember independently, and worker threads silently LOST all of them (a fresh thread
starts with defaults). Consolidating them into one immutable TurnContext in a single ContextVar
gives one place to look, one bracketing API, and — the real win over threading.local — a way to
CARRY the whole context into a worker thread or asyncio task (`carry()`), so "runs on another
thread" no longer means "silently runs as an interactive web turn in the global workspace".

Semantics preserved from the thread-local era:
  • a fresh thread still starts at the defaults (contextvars don't inherit across raw
    threading.Thread, same as threading.local) — brackets like tools.channel(...) or an
    explicit carry() establish the context, exactly as before;
  • asyncio.to_thread / FastAPI's sync-route threadpool COPY the caller's context in, which
    only ever adds correctness (the bridge endpoints bracket per call regardless).

The process-wide bridge taint (safety._bridge_seen) is deliberately NOT part of this: it spans
threads by design (each bridged tool call is its own request thread) and may only over-block.
"""
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TurnContext:
    channel: str = "web"      # web (interactive, may drive the live browser) | telegram | background
    workspace: object = None  # Path override for file/shell tools (None → config.WORKSPACE)
    session: str = None       # the chat sid this turn drives (spawn_job result routing)
    tainted: bool = False     # this turn ingested untrusted content (web page / email / doc)


_var = contextvars.ContextVar("oceano_turnctx", default=TurnContext())


def get():
    """The current turn's context (defaults if nothing bracketed — an interactive web turn)."""
    return _var.get()


def mutate(**fields):
    """Replace fields on the CURRENT context in place — for state that changes mid-turn
    (wrap_untrusted flipping `tainted`), not for scoped overrides (use push())."""
    _var.set(replace(_var.get(), **fields))


@contextmanager
def push(**fields):
    """Override fields for the block's duration, restoring the previous context on exit
    (exception-safe, nests correctly)."""
    token = _var.set(replace(_var.get(), **fields))
    try:
        yield _var.get()
    finally:
        _var.reset(token)


def carry(fn):
    """Bind `fn` to a COPY of the calling thread's context, for handing to a worker thread:
        threading.Thread(target=turnctx.carry(work)).start()
    The worker then sees the caller's channel/workspace/session/taint instead of silently
    reverting to defaults. Mutations inside the worker stay in its copy (no leak back)."""
    ctx = contextvars.copy_context()
    return lambda *a, **kw: ctx.run(fn, *a, **kw)
