"""Structured runtime tracing for workflows, agents, model calls, and tools.

Append-only JSONL keeps this cheap and robust: every trace event is a single line that
can be written from any worker thread without needing schema migrations or a long-lived
DB connection. Callers opt in with `scope(...)`; outside a scope `record(...)` is a no-op.
"""
import contextvars
import json
import time
import uuid

import config
from oceano import atomicio

TRACE_PATH = config.WORKSPACE.parent / "data" / "traces.jsonl"
_ctx = contextvars.ContextVar("oceano_trace", default=None)


def _now():
    return time.time()


def new_run_id(prefix="run"):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class scope:
    """Bind a trace context for the current thread/task."""

    def __init__(self, **fields):
        cur = _ctx.get() or {}
        merged = dict(cur)
        merged.update({k: v for k, v in fields.items() if v is not None})
        if "run_id" not in merged:
            merged["run_id"] = new_run_id()
        self._value = merged
        self._token = None

    def __enter__(self):
        self._token = _ctx.set(self._value)
        return self._value

    def __exit__(self, exc_type, exc, tb):
        _ctx.reset(self._token)


def current():
    return _ctx.get() or {}


def record(event, **fields):
    """Append one trace event. Returns the written payload, or None when tracing is off."""
    cur = current()
    if not cur:
        return None
    payload = {"ts": _now(), "event": event, **cur, **fields}
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            atomicio.secure(TRACE_PATH)
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        return None
    return payload


def record_global(event, **fields):
    """Append a content-free operational metric even outside a run scope.

    Used for routing telemetry, which deliberately stores model/tool names and counts only—never
    prompts, tool arguments, results, or answers. Keeping this separate preserves record()'s
    opt-in scope behavior for normal traces.
    """
    payload = {"ts": _now(), "event": event, **fields}
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            atomicio.secure(TRACE_PATH)
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        return None
    return payload


def query(run_id=None, workflow_id=None, limit=500):
    """Recent trace events, optionally filtered by run or workflow."""
    try:
        lines = TRACE_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id is not None and ev.get("run_id") != run_id:
            continue
        if workflow_id is not None and ev.get("workflow_id") != workflow_id:
            continue
        out.append(ev)
        if len(out) >= limit:
            break
    return list(reversed(out))



def turn_health(limit=20):
    """Content-free health summary for recent agent/resident turns."""
    events = query(limit=max(200, int(limit) * 20))
    rows = []
    for event in reversed(events):
        if event.get("event") == "resident_turn":
            rows.append({
                "ts": event.get("ts"), "mind": event.get("mind") or "resident",
                "healthy": not bool(event.get("incomplete")) and not int(event.get("errors") or 0),
                "incomplete": bool(event.get("incomplete")),
                "errors": int(event.get("errors") or 0),
                "historical_errors": int(event.get("historical_errors") or 0),
                "tool_calls": int(event.get("tool_calls") or 0),
                "elapsed_ms": int(event.get("elapsed_ms") or 0),
                "used_tools": list(event.get("used_tools") or []),
                "advertised_tools": int(event.get("catalog_advertised") or 0),
                "catalog_tools": int(event.get("catalog_catalog") or 0),
                "schema_tokens": int(event.get("catalog_schema_tokens") or 0),
                "catalog_schema_tokens": int(event.get("catalog_catalog_schema_tokens") or 0),
            })
        elif (event.get("event") == "tool_routing"
              and event.get("phase") in {"completed", "step-limit"}):
            errors = int(event.get("tool_errors") or event.get("errors") or 0)
            rows.append({
                "ts": event.get("ts"), "mind": event.get("model") or "api/local",
                "healthy": event.get("phase") == "completed" and errors == 0,
                "incomplete": event.get("phase") != "completed", "errors": errors,
                "historical_errors": int(event.get("historical_errors") or errors),
                "tool_calls": int(event.get("tool_calls") or 0),
                "elapsed_ms": int(event.get("elapsed_ms") or 0),
                "used_tools": list(event.get("used_tools") or []),
                "advertised_tools": int(event.get("advertised_tools") or 0),
                "catalog_tools": int(event.get("catalog_tools") or 0),
                "schema_tokens": int(event.get("schema_tokens") or 0),
                "catalog_schema_tokens": int(event.get("catalog_schema_tokens") or 0),
            })
        if len(rows) >= limit:
            break
    healthy = sum(row["healthy"] for row in rows)
    return {
        "summary": {"turns": len(rows), "healthy": healthy,
                    "incomplete": sum(row["incomplete"] for row in rows),
                    "unresolved_errors": sum(row["errors"] for row in rows),
                    "avg_tool_calls": (round(sum(row["tool_calls"] for row in rows) / len(rows), 1)
                                       if rows else 0)},
        "recent": rows,
    }

def clear():
    try:
        TRACE_PATH.unlink()
        return True
    except OSError:
        return False
