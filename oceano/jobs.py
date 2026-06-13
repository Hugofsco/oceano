"""Live registry of background jobs + an optional global serialization gate.

Every unattended job (scheduled task, workflow, eval suite, research, skills/memory
maintenance) runs inside `jobs.job(...)`, so the UI can show what's in flight and for how
long. When the user turns on serialization (Settings), `job()` acquires a single global lock
before its work — a second job then QUEUES behind the first instead of hammering the one
local model in parallel. Interactive chat is intentionally never gated (it stays responsive).

Each job carries a `ref` that mirrors the scheduler's `source` tag (e.g. "workflow:3",
"research:2", "task:7") so the various panels can highlight exactly what's running.
"""
import itertools
import json
import threading
import time
from contextlib import contextmanager

import config
from oceano import atomicio

STATE_PATH = config.WORKSPACE.parent / "data" / "jobs.json"

# RLock, not Lock: a gated unit of work may legitimately spawn another gated unit IN THE SAME
# THREAD (e.g. a chat turn or a scheduled task whose agent calls run_workflow). A plain Lock
# would self-deadlock there; an RLock lets the same thread re-enter while still blocking others.
_gate = threading.RLock()
_mx = threading.Lock()           # guards _jobs
_jobs = {}                       # id -> {id, kind, label, ref, state, since}
_counter = itertools.count(1)


def _load():
    try:
        d = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return bool(d.get("serialize", False)), bool(d.get("serialize_chat", False))


_serialize, _serialize_chat = _load()


def _save():
    try:
        atomicio.write_text(STATE_PATH, json.dumps({"serialize": _serialize, "serialize_chat": _serialize_chat}))
    except OSError:
        pass


def serialize_enabled():
    return _serialize


def serialize_chat_enabled():
    return _serialize_chat


def set_serialize(on):
    """Background-job queue on/off (persisted across restarts)."""
    global _serialize
    _serialize = bool(on)
    _save()
    return _serialize


def set_serialize_chat(on):
    """Route chat turns through the same gate too, when on (persisted)."""
    global _serialize_chat
    _serialize_chat = bool(on)
    _save()
    return _serialize_chat


def snapshot():
    """Everything in flight right now, for the UI's running indicators."""
    now = time.time()
    with _mx:
        js = sorted(_jobs.values(), key=lambda j: j["since"])
    out = [{"id": j["id"], "kind": j["kind"], "label": j["label"], "ref": j["ref"],
            "state": j["state"], "elapsed": round(now - j["since"], 1)} for j in js]
    return {"serialize": _serialize, "serialize_chat": _serialize_chat, "jobs": out,
            "running": sum(1 for j in out if j["state"] == "running"),
            "queued": sum(1 for j in out if j["state"] == "queued")}


@contextmanager
def job(kind, label="", ref=None, gate=None):
    """Register a job for the lifetime of the with-block. `gate` decides whether it acquires
    the global serialization lock: None → follow the background `serialize` setting (the
    default for background jobs); True/False → force it (chat passes its own toggle; the eval
    suite passes False to opt out). While waiting for the gate the job shows as 'queued', then
    'running'. The registry entry is removed when the block exits (success or error)."""
    jid = next(_counter)
    gated = _serialize if gate is None else bool(gate)
    info = {"id": jid, "kind": kind, "label": (label or kind)[:140], "ref": ref,
            "state": "queued" if gated else "running", "since": time.time()}
    with _mx:
        _jobs[jid] = info
    held = False
    try:
        if gated:
            _gate.acquire()
            held = True
            with _mx:
                info["state"] = "running"
                info["since"] = time.time()    # reset so "running for Xs" is accurate
        yield jid
    finally:
        if held:
            _gate.release()
        with _mx:
            _jobs.pop(jid, None)
