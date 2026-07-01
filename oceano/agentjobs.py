"""Background AGENT registry — spawnable sub-agents owned by the daemon.

The delegate tool (and the workflow delegate node) runs a contained agent — Claude CLI, Codex
CLI, a cloud API model, or the local model — but BLOCKS until it finishes. spawn_agent hands the
same contained run to this registry instead: a daemon thread runs the agent while the caller (a
chat mind mid-turn, or a workflow that keeps walking its nodes) continues immediately; the result
is tracked here, announced, delivered into the spawning conversation, and joinable from a
workflow's `await` node.

Sibling of bgjobs.py, same shapes on purpose (record + one lock bracketing state AND persist +
terminal-transition announce/hook + pending/mark_delivered + status/tail) — the difference is the
job is a THREAD in this process, not a detached OS process. That makes restart reconciliation
simpler and stricter: threads die with the daemon, so anything still 'running' in the persisted
state after a restart is honestly `lost` (no /proc pid checks, no watcher — there is nothing left
to watch).

Guardrails live HERE (not in the tool wrapper): a concurrency cap (OCEANO_AGENTS_MAX), a single
slot for the `local` provider (one resident model on the box — parallel local agents would just
queue behind llama-swap, and the weak local model unsupervised is a known failure mode, hence
LOCAL_WARNING), timeouts clamped to the delegation cap, and NO recursion — a spawned api/local
agent is built without spawn/delegate/workflow tools, and CLI providers never get the Oceano
bridge in delegation mode anyway.
"""
import itertools
import json
import os
import threading
import time

import config
from oceano import atomicio

STATE_PATH = config.WORKSPACE.parent / "data" / "agentjobs.json"
LOG_DIR = config.WORKSPACE.parent / "data" / "agent-logs"
TAIL_CHARS = 4000        # bounded progress tail, matches bgjobs
MAX_KEPT = 200           # prune finished agents beyond this (oldest-ended first)
OUTPUT_CAP = 8000        # final output kept in the record itself (delivery needs no file read)
MAX_AGENTS = int(os.environ.get("OCEANO_AGENTS_MAX", "3"))
DEFAULT_TIMEOUT = 600    # per-agent wall clock; clamped to the delegation cap below

PROVIDERS = ("claude", "codex", "api", "local")
# What a spawned api/local agent may NOT do: spawn more agents, delegate, or fire workflows —
# fan-out stays a decision made at THIS level, never compounding out of sight.
EXCLUDE = {"spawn_agent", "delegate", "delegate_to_claude", "run_workflow"}
LOCAL_WARNING = ("note: the LOCAL provider shares the one resident model — this agent queues "
                 "behind other local work (serialized), and the local model is weak for large "
                 "unsupervised tasks. Prefer provider 'api', 'claude', or 'codex' for anything heavy.")

_mx = threading.Lock()   # guards _jobs AND brackets every persist (snapshot + write together)
_jobs = {}               # id(int) -> record
_on_complete = None      # optional hook(rec), fired once at the terminal transition (web layer)


def set_on_complete(cb):
    """Register a callback fired once with the record when an agent reaches a terminal state
    (done/failed/lost). The web layer delivers the result into the spawning conversation.
    Best-effort — a raising callback never disturbs bookkeeping."""
    global _on_complete
    _on_complete = cb


def _load():
    try:
        d = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    out = {}
    for k, v in (d.get("jobs") or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return out


_jobs = _load()
_counter = itertools.count(max(_jobs, default=0) + 1)   # ids keep climbing across restarts


def _persist():
    """Snapshot + disk write under ONE lock acquisition (see bgjobs._persist for why)."""
    with _mx:
        finished = sorted((j for j in _jobs.values() if j["state"] not in ("running", "starting")),
                          key=lambda j: j.get("ended") or 0)
        for j in finished[:max(0, len(finished) - MAX_KEPT)]:
            _jobs.pop(j["id"], None)
        snapshot = json.dumps({"jobs": {str(k): v for k, v in _jobs.items()}})
    try:
        atomicio.write_text(STATE_PATH, snapshot)
    except OSError:
        pass


def _tail_file(path, max_chars=TAIL_CHARS):
    """Last `max_chars` of the progress log without loading the whole file (bgjobs pattern)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars * 4))
            data = f.read()
        return data.decode("utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _resolve_provider(provider):
    """'' → whatever the delegate 'default' role is configured to use; else one of PROVIDERS."""
    p = (provider or "").strip().lower()
    if not p:
        from oceano import delegate
        eff = delegate.resolve("default")["provider"]
        p = {"claude_cli": "claude", "codex_cli": "codex", "api": "api"}.get(eff, "claude")
    if p not in PROVIDERS:
        raise RuntimeError(f"unknown provider {provider!r} — use one of: {', '.join(PROVIDERS)} "
                           "(or omit it for the configured delegation default)")
    return p


def spawn(task, provider="", label="", tools=None, timeout=0, cwd=None, sid=None):
    """Start a contained agent on a background daemon thread and return its record immediately.
    Refuses (RuntimeError with a user-relayable message) when the concurrency cap is hit, when a
    `local` agent is already running, or on an unknown provider. `sid` is the conversation that
    spawned it (result delivered back there); None outside a chat (workflows/scheduler)."""
    from oceano import delegate
    p = _resolve_provider(provider)
    timeout = min(int(timeout) or DEFAULT_TIMEOUT, delegate._DELEGATE_MAX)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _mx:
        running = [j for j in _jobs.values() if j["state"] in ("starting", "running")]
        if len(running) >= MAX_AGENTS:
            raise RuntimeError(f"agent limit reached ({MAX_AGENTS} running) — wait for one to "
                               f"finish (agent_status lists them), or raise OCEANO_AGENTS_MAX")
        if p == "local" and any(j.get("provider") == "local" for j in running):
            raise RuntimeError("a LOCAL agent is already running and the box serves one resident "
                               "model — wait for it, or spawn on 'api'/'claude'/'codex' instead")
        jid = next(_counter)
        rec = {"id": jid, "label": (label or task)[:140], "task": str(task)[:4000], "provider": p,
               "state": "running", "started": time.time(), "ended": None,
               "ok": None, "output": "", "error": "",
               "log_path": str(LOG_DIR / f"{jid}.log"), "sid": sid, "delivered": False,
               "warning": LOCAL_WARNING if p == "local" else ""}
        _jobs[jid] = rec
    _persist()
    threading.Thread(target=_work, args=(jid, task, p, tools, timeout, cwd), daemon=True).start()
    return dict(rec)


def _work(jid, task, provider, tools, timeout, cwd):
    """Owns the agent run for this job's lifetime (the thread analog of bgjobs._reap)."""
    log_path = None
    with _mx:
        rec = _jobs.get(jid)
        if rec:
            log_path = rec["log_path"]
    logf = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None

    def on_progress(ev):
        if logf is None:
            return
        try:
            k = ev.get("kind")
            if k == "text" and ev.get("text"):
                logf.write(ev["text"].rstrip() + "\n")
            elif k == "tool":
                logf.write(f"[tool] {ev.get('tool', 'tool')} {ev.get('detail', '')}".rstrip() + "\n")
            logf.flush()
        except Exception:
            pass

    try:
        r = _dispatch(provider, task, tools, timeout, cwd, on_progress)
    except Exception as e:                       # a provider crash is a failed agent, not a dead thread
        r = {"ok": False, "output": "", "error": f"{type(e).__name__}: {e}"}
    finally:
        if logf is not None:
            try:
                logf.close()
            except OSError:
                pass
    with _mx:
        rec = _jobs.get(jid)
        if rec is None:
            return
        rec["ok"] = bool(r.get("ok"))
        rec["output"] = (r.get("output") or "")[:OUTPUT_CAP]
        rec["error"] = (r.get("error") or "")[:2000]
        rec["ended"] = time.time()
        rec["state"] = "done" if rec["ok"] else "failed"
        snap = dict(rec)
    _persist()
    _announce(snap)


def _dispatch(provider, task, tools, timeout, cwd, on_progress):
    """Run the task on the chosen provider, reusing delegation's contained primitives directly
    (delegate.run()'s role-based resolution is deliberately untouched — these four are the seams).
    Returns the provider's {ok, output, error} dict."""
    from oceano import delegate
    cwd = cwd or config.WORKSPACE
    spec = tools or delegate.DEFAULT_TOOLS
    if provider == "claude":
        return delegate.to_claude_stream(task, cwd=cwd, tools=spec, max_total=timeout,
                                         on_progress=on_progress)
    if provider == "codex":
        return delegate.to_codex(task, cwd=cwd, tools=spec, timeout=timeout)
    if provider == "api":
        return delegate.to_api(task, cwd=cwd, tools=spec, timeout=timeout,
                               on_progress=on_progress, exclude=EXCLUDE)
    return _run_local(task, spec, timeout, cwd, on_progress)


def _run_local(task, tools_spec, timeout, cwd, on_progress):
    """The task on the LOCAL primary model through OUR agent loop — delegate.to_api's shape, but
    on resolve_primary() instead of a role's cloud config, and inside the global serialization
    gate (gate=True) so it queues visibly behind other work on the one resident model."""
    from oceano import delegate, jobs
    from oceano import tools as _tools
    prim = delegate.resolve_primary()
    if not prim["model"]:
        return {"ok": False, "output": "",
                "error": "no local model is set up — serve one in Brain → Rivers first"}

    def _on_ev(kind, data):
        if kind == "tool_call" and on_progress:
            on_progress({"kind": "tool", "tool": (data or {}).get("name", "tool"), "detail": ""})

    try:
        from oceano.agent import Agent
        ag = Agent(model=prim["model"], base_url=prim["base_url"] or None,
                   api_key=prim["api_key"] or None, learn=False, inject_context=False,
                   exclude_tools=EXCLUDE, only_tools=delegate._api_only_tools(tools_spec),
                   on_event=_on_ev)
        deadline = (time.monotonic() + timeout) if timeout else None
        with jobs.job("agent", label=str(task)[:140], gate=True):
            ctx = _tools.background_workspace(cwd) if cwd else _tools.background()
            with ctx:
                out = ag.run(str(task), deadline=deadline)
        return {"ok": True, "output": (out or "").strip(), "error": ""}
    except TimeoutError:
        return {"ok": False, "output": "", "error": f"local agent timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "output": "", "error": f"local agent error: {type(e).__name__}: {e}"}


def _announce(rec):
    """Tell the user the agent's outcome (the daemon-side fulfilment of 'it'll report back'),
    then fire the delivery hook. Best-effort on every leg (bgjobs pattern)."""
    try:
        from oceano import scheduler, logs
        dur = round((rec["ended"] or time.time()) - rec["started"], 1)
        if rec["state"] == "done":
            msg = f'Agent "{rec["label"]}" ({rec["provider"]}) finished after {dur}s.'
        elif rec["state"] == "failed":
            msg = (f'Agent "{rec["label"]}" ({rec["provider"]}) FAILED after {dur}s: '
                   + (rec.get("error") or "no output"))
        else:                                    # "lost" — reconciled after a daemon restart
            msg = (f'Agent "{rec["label"]}" did not survive an Oceano restart (agents run in the '
                   f'daemon, so a restart ends them); its result could not be recovered.')
        scheduler.notify(msg, title="Oceano — agent")
        logs.log_run(kind="agent", title=rec["label"], status="ok" if rec["state"] == "done" else "error",
                     summary=(rec.get("output") or rec.get("error") or "")[:2000],
                     duration=dur, ref=f"agent:{rec['id']}")
    except Exception:
        pass
    if _on_complete is not None:
        try:
            _on_complete(rec)
        except Exception:
            pass


def pending_for(sid):
    """Terminal agents for conversation `sid` not yet delivered into the chat (web-poll path)."""
    if not sid:
        return []
    with _mx:
        return [dict(j) for j in _jobs.values()
                if j.get("sid") == sid and not j.get("delivered")
                and j["state"] in ("done", "failed", "lost")]


def mark_delivered(agent_id):
    """Flag an agent's result as delivered so it's never printed twice (persisted)."""
    try:
        aid = int(agent_id)
    except (TypeError, ValueError):
        return False
    with _mx:
        rec = _jobs.get(aid)
        if rec is None or rec.get("delivered"):
            return False
        rec["delivered"] = True
    _persist()
    return True


def status(agent_id=None):
    """No id → every tracked agent (most recent first). An id → that record plus a bounded
    tail of its progress log. None if the id doesn't exist."""
    if not agent_id:
        with _mx:
            return [dict(j) for j in sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)]
    try:
        aid = int(agent_id)
    except (TypeError, ValueError):
        return None
    with _mx:
        rec = _jobs.get(aid)
        rec = dict(rec) if rec else None
    if rec is not None:
        rec["tail"] = _tail_file(rec["log_path"]) if rec.get("log_path") else ""
    return rec


def _reconcile():
    """Run once at import (daemon startup). Agents run as threads IN the daemon, so anything
    still 'running' in the persisted state belonged to a previous daemon process and is simply
    gone — mark it `lost` and announce, exactly once."""
    lost = []
    with _mx:
        for r in _jobs.values():
            if r["state"] in ("running", "starting"):
                r["state"] = "lost"
                r["ended"] = r.get("ended") or time.time()
                lost.append(dict(r))
    if lost:
        _persist()
        for snap in lost:
            _announce(snap)


_reconcile()      # must be last: everything it calls must already be defined
