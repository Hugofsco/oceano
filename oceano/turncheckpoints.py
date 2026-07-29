"""Durable, compact checkpoints for interrupted resident-agent turns.

The store deliberately excludes prompts, arguments, results, and answers. It retains only
operation fingerprints, tool names, typed outcomes, side effects, and verification evidence.
"""
import hashlib
import json
import threading
import time

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "resident_turn_checkpoints.json"
_LOCK = threading.RLock()


def _key(session, provider):
    if not session:
        return None
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:24]
    return f"{provider}:{digest}"


def _load():
    try:
        value = json.loads(STORE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(value):
    atomicio.write_text(STORE, json.dumps(value, ensure_ascii=True, sort_keys=True))


def begin(session, provider, task):
    key = _key(session, provider)
    if key is None:
        return None
    record = {
        "provider": provider,
        "started": time.time(),
        "updated": time.time(),
        "task": {
            "requires_action": bool(getattr(task, "requires_action", False)),
            "verify_code": bool(getattr(task, "verify_code", False)),
        },
        "state": {"events": [], "side_effects": [], "verification": []},
        "reason": "",
    }
    with _LOCK:
        data = _load()
        data[key] = record
        _save(data)
    return key


def update(key, state, reason=""):
    if not key:
        return
    payload = state.checkpoint_data() if hasattr(state, "checkpoint_data") else dict(state or {})
    with _LOCK:
        data = _load()
        record = data.get(key)
        if not record:
            return
        record["updated"] = time.time()
        record["state"] = payload
        if reason:
            record["reason"] = str(reason)[:240]
        data[key] = record
        _save(data)


def clear(key):
    if not key:
        return False
    with _LOCK:
        data = _load()
        removed = data.pop(key, None) is not None
        if removed:
            _save(data)
        return removed


def recovery_note(session, provider):
    key = _key(session, provider)
    if key is None:
        return ""
    with _LOCK:
        record = _load().get(key)
    if not record:
        return ""
    state = record.get("state") or {}
    events = state.get("events") or []
    completed = [event.get("name") for event in events if event.get("ok")]
    failed = [f"{event.get('name')}:{event.get('code') or 'error'}"
              for event in events if not event.get("ok") and not event.get("resolved")]
    effects = state.get("side_effects") or []
    verified = state.get("verification") or []
    lines = [
        "INTERRUPTED TURN RECOVERY - a previous resident turn ended before clean completion.",
        "Inspect current state before continuing and do not repeat recorded side effects.",
    ]
    if completed:
        lines.append("Completed tools: " + ", ".join(dict.fromkeys(completed)))
    if effects:
        lines.append("Recorded side effects: " + ", ".join(effects))
    if verified:
        lines.append("Verification evidence: " + ", ".join(verified))
    if failed:
        lines.append("Still unresolved: " + ", ".join(failed))
    if record.get("reason"):
        lines.append("Prior stop reason: " + str(record["reason"]))
    return "\n".join(lines)


def status():
    with _LOCK:
        data = _load()
    return {"recoverable": len(data), "providers": sorted({
        str(record.get("provider") or "resident") for record in data.values()})}
