"""The body-bridge: lets the Claude-mind use Oceano's OWN tools.

A thin stdio MCP server (oceano.mcp_bridge_server) runs *under* Claude Code and proxies every tool
call back to the daemon over a token-gated localhost endpoint, so Oceano's tools EXECUTE IN THE
DAEMON with full runtime context — ui_open reaches the live browser, memory/calendar hit the real
DBs, search hits the running SearXNG. A detached subprocess couldn't drive the daemon's UI or share
its state, hence the proxy.

Flow:  Claude  →(stdio MCP)→  mcp_bridge_server  →(HTTP + token)→  /api/mcp/call  →  tools.run_result()
"""
import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from oceano import tools, turnctx

# The conversation (chat session id) a tool call belongs to, so spawn_job can route a job's eventual
# result back to the chat that asked for it. It lives on the per-turn TurnContext (oceano.turnctx),
# NOT a process-global, so two mind turns for different sessions never misattribute each other's jobs:
#   • local model — tools run inline on the per-session turn thread, which session() marks directly;
#   • Claude/Codex — the bridged tool call runs on a DIFFERENT daemon thread (/api/mcp/call →
#     to_thread), so the sid rides through the bridge per turn (OCEANO_MCP_SESSION in the mind's own
#     per-turn MCP config → an X-Oceano-Session header on each call) and run_tool() marks the request
#     context for the duration of that one call.


@contextmanager
def session(sid):
    """Mark `sid` as the conversation for tool calls on this turn, for the block's duration
    (save+restore so nested/sequential turns don't clobber an outer one)."""
    with turnctx.push(session=sid):
        yield


def active_session():
    return turnctx.get().session


# The resident mind's full body is the live, globally enabled tool registry. A narrower
# contained-agent scope below deliberately exposes only its explicit capability set.
# A narrower bridge for CONTAINED sub-agents (workflow Delegate / Agent Spawn / Orchestrator-
# plugged nodes): let them reuse Oceano's published skills — so a background sub-agent doesn't
# have to reinvent a procedure Oceano already learned — but NOTHING else from the body. No
# memory (remember/recall/update_memory/forget_memory): a contained sub-agent's task is
# self-contained and must not see or change what the user's own mind remembers. No learn_skill
# either — growing the skill library is a bigger act than reusing it, left to the full bridge
# above. A flow that genuinely needs memory (or the rest of the body) should use an Instructions
# node instead, which runs through the full registry bridge (scope=None).
_SCOPES = {
    "skills": {"list_skills", "load_skill"},
}


def _allowset(scope):
    if scope is None:
        # Full means every globally enabled registered tool, including live MCP tools. Compute
        # this fresh because Settings toggles and MCP reloads change the registry at runtime.
        return {schema["function"]["name"] for schema in tools.schemas()}
    # Named contained-agent scopes are explicit capability boundaries. Unknown scopes fail shut.
    return _SCOPES.get(scope, set())


@dataclass
class _CatalogState:
    route: object
    catalog: list
    allowed: set
    query: str
    model: str
    max_calls: int
    session: str | None = None
    background: bool = False
    client: str = "web"
    scope: str | None = None
    workspace: object = None
    blocked: set[str] = field(default_factory=set)
    calls: int = 0
    created: float = 0.0
    last_used: float = 0.0


_CATALOGS = {}
_CATALOG_LOCK = threading.RLock()
_CATALOG_TTL = 6 * 3600
_CATALOG_MAX = 256


def _cleanup_catalogs(now=None):
    now = now or time.time()
    for key in [key for key, value in _CATALOGS.items()
                if now - value.last_used > _CATALOG_TTL]:
        _CATALOGS.pop(key, None)
    overflow = len(_CATALOGS) - _CATALOG_MAX
    if overflow > 0:
        oldest = sorted(_CATALOGS, key=lambda key: _CATALOGS[key].last_used)
        for key in oldest[:overflow]:
            _CATALOGS.pop(key, None)


def _catalog_state(catalog_id):
    if not catalog_id:
        return None
    now = time.time()
    with _CATALOG_LOCK:
        _cleanup_catalogs(now)
        state = _CATALOGS.get(catalog_id)
        if state is not None:
            state.last_used = now
        return state


def _catalog_owner_error(state, *, session=None, background=False, client="web", scope=None):
    supplied = (session, bool(background), client or "web", scope)
    expected = (state.session, state.background, state.client, state.scope)
    return "" if supplied == expected else "resident catalog does not belong to this turn context"


def close_catalog(catalog_id):
    """Explicitly discard a completed turn catalog; TTL/LRU remain crash fallbacks."""
    from oceano import safety
    safety.reset_bridge_untrusted(catalog_id)   # the turn owning this key is over — don't leak it
    with _CATALOG_LOCK:
        return _CATALOGS.pop(catalog_id, None) is not None


def catalog_inventory():
    with _CATALOG_LOCK:
        _cleanup_catalogs()
        return {"active": len(_CATALOGS), "limit": _CATALOG_MAX}


def block_catalog_tools(catalog_id, names):
    """Deny tools for the remainder of one live catalog, including later discovery."""
    state = _catalog_state(catalog_id)
    if state is None:
        return False
    with _CATALOG_LOCK:
        state.blocked.update(str(name) for name in names)
    return True


def create_catalog(query, model, max_calls, scope=None, *, session=None, background=False,
                   client="web", force=None):
    """Create one opaque, turn-bound daemon catalog and execution budget."""
    from oceano import toolrouter
    catalog = tool_schemas(scope=scope)
    route = toolrouter.route(catalog, query or "", model=model or "resident",
                             surface="resident", force=force)
    catalog_id = secrets.token_urlsafe(18)
    state = _CatalogState(route=route, catalog=catalog,
                          allowed={schema["function"]["name"] for schema in catalog},
                          query=query or "", model=model or "resident",
                          max_calls=max(1, int(max_calls or 1)), session=session,
                          background=bool(background), client=client or "web", scope=scope,
                          workspace=turnctx.get().workspace,
                          created=time.time(), last_used=time.time())
    with _CATALOG_LOCK:
        _CATALOGS[catalog_id] = state
        _cleanup_catalogs()
    toolrouter.telemetry(route, "resident-selected")
    return catalog_id, route


def consume_catalog_call(catalog_id, name, *, require_advertised=True):
    """Atomically reserve one call before execution; shared by bridge and native events."""
    state = _catalog_state(catalog_id)
    if state is None:
        return (False, "resident catalog expired or is invalid") if catalog_id else (True, "")
    with _CATALOG_LOCK:
        advertised = set(state.route.names)
        if require_advertised and name not in advertised:
            return False, f"tool {name!r} is not advertised in this resident catalog"
        if state.calls >= state.max_calls:
            return False, "resident tool-call budget exhausted"
        state.calls += 1
    return True, ""


def discover_catalog(catalog_id, args):
    """Expand one resident MCP catalog through the same bundle policy as API/local agents."""
    from oceano import toolrouter
    state = _catalog_state(catalog_id)
    if state is None:
        return json.dumps({"loaded": [], "message": "resident catalog expired"})
    with _CATALOG_LOCK:
        updated, result = toolrouter.discover(
            state.route, state.catalog, state.allowed, args or {})
        state.route = updated
    toolrouter.telemetry(updated, "resident-discovered")
    return result


def catalog_status(catalog_id):
    state = _catalog_state(catalog_id)
    if state is None:
        return None
    return {"advertised": state.route.selected, "catalog": state.route.total,
            "schema_tokens": state.route.schema_tokens,
            "catalog_schema_tokens": state.route.catalog_schema_tokens,
            "calls": state.calls, "max_calls": state.max_calls,
            "bundles": list(state.route.loaded_bundles), "enabled": state.route.enabled}


@dataclass
class _ReplayEntry:
    fingerprint: str
    created: float
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None


_REPLAYS = {}
_REPLAY_LOCK = threading.RLock()
_REPLAY_TTL = 6 * 3600
_REPLAY_MAX = 2048


def _replay_start(operation_id, name, args, *, catalog_id, session, background, client, scope):
    """Claim an idempotency key, or return the matching in-flight/completed operation."""
    if not operation_id:
        return True, None, None
    operation_id = str(operation_id)
    if len(operation_id) > 256:
        return False, None, tools.ToolResult(
            False, error="idempotency operation ID is too long", code="bad_operation_id")
    canonical = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256((name + "\0" + canonical).encode()).hexdigest()
    owner = catalog_id or "|".join((
        str(session or ""), str(scope or ""), "1" if background else "0", str(client or "web")))
    key = (owner, operation_id)
    now = time.time()
    with _REPLAY_LOCK:
        expired = [item for item, entry in _REPLAYS.items()
                   if entry.event.is_set() and now - entry.created > _REPLAY_TTL]
        for item in expired:
            _REPLAYS.pop(item, None)
        if len(_REPLAYS) >= _REPLAY_MAX:
            completed = sorted(
                ((entry.created, item) for item, entry in _REPLAYS.items() if entry.event.is_set()))
            for _created, item in completed[:max(1, len(_REPLAYS) - _REPLAY_MAX + 1)]:
                _REPLAYS.pop(item, None)
        existing = _REPLAYS.get(key)
        if existing:
            if existing.fingerprint != fingerprint:
                return False, None, tools.ToolResult(
                    False, error="idempotency key was reused for a different operation",
                    code="idempotency_conflict")
            return False, existing, None
        entry = _ReplayEntry(fingerprint=fingerprint, created=now)
        _REPLAYS[key] = entry
        return True, entry, None


def _replay_finish(entry, result):
    if entry is None:
        return
    with _REPLAY_LOCK:
        entry.result = result
        entry.event.set()


_TOKEN = None


def token():
    """The localhost secret shared with the bridge subprocess. Persisted in data/.mind-token so the
    daemon (which validates) and the agent (which writes the MCP config) always agree, and it
    survives a restart mid-conversation."""
    global _TOKEN
    if _TOKEN is None:
        import config
        from oceano import atomicio
        p = config.WORKSPACE.parent / "data" / ".mind-token"
        try:
            _TOKEN = (p.read_text().strip() or None)
        except OSError:
            _TOKEN = None
        if not _TOKEN:
            _TOKEN = secrets.token_urlsafe(24)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                atomicio.write_text(p, _TOKEN)
            except OSError:
                pass
    return _TOKEN


def daemon_url():
    host = os.environ.get("OCEANO_WEB_HOST", "127.0.0.1")
    port = os.environ.get("OCEANO_WEB_PORT", "8800")
    return f"http://{host}:{port}"


def tool_schemas(scope=None, catalog_id=None, *, session=None, background=False, client="web"):
    """Tools offered to the mind, optionally narrowed by a turn-bound catalog."""
    state = _catalog_state(catalog_id)
    if state is not None:
        if _catalog_owner_error(
                state, session=session, background=background, client=client, scope=scope):
            return []
        return [schema for schema in state.route.schemas
                if schema["function"]["name"] not in state.blocked]
    if catalog_id:
        return []
    return [s for s in tools.schemas() if s["function"]["name"] in _allowset(scope)]


def tool_names(scope=None, catalog_id=None, *, session=None, background=False, client="web"):
    return [schema["function"]["name"] for schema in tool_schemas(
        scope, catalog_id, session=session, background=background, client=client)]


def run_tool_result(name, args, session=None, background=False, client="web", scope=None,
                    catalog_id=None, operation_id=None):
    """Execute one body tool and retain its structured result across the MCP boundary."""
    args = args if isinstance(args, dict) else {}
    state = _catalog_state(catalog_id)
    if catalog_id and state is None:
        return tools.ToolResult(False, error="resident catalog expired or is invalid",
                                code="catalog_invalid")
    if state is not None:
        owner_error = _catalog_owner_error(
            state, session=session, background=background, client=client, scope=scope)
        if owner_error:
            return tools.ToolResult(False, error=owner_error, code="catalog_context_mismatch")
        if name in state.blocked:
            return tools.ToolResult(
                False, error=f"tool {name!r} is blocked during parent-turn continuation",
                code="continuation_tool_blocked")
        if name not in set(state.route.names):
            return tools.ToolResult(
                False, error=f"tool {name!r} is not advertised in this resident catalog",
                code="not_advertised")
    elif name not in _allowset(scope):
        return tools.ToolResult(False, error=f"tool {name!r} is not available to the mind",
                                code="not_allowed")

    spec = tools.tool_spec(name)
    replayable = bool(operation_id and spec and spec.side_effecting and not spec.idempotent)
    replay_owner, replay_entry, replay_error = (True, None, None)
    if replayable:
        replay_owner, replay_entry, replay_error = _replay_start(
            operation_id, name, args, catalog_id=catalog_id, session=session,
            background=background, client=client, scope=scope)
        if replay_error is not None:
            return replay_error
        if not replay_owner:
            if not replay_entry.event.wait(timeout=600):
                return tools.ToolResult(
                    False, error="matching operation is still in progress",
                    retryable=True, code="idempotency_in_progress")
            return replay_entry.result or tools.ToolResult(
                False, error="matching operation ended without a result",
                retryable=True, code="idempotency_missing_result")

    result = None
    try:
        if state is not None:
            ok, reason = consume_catalog_call(catalog_id, name)
            if not ok:
                code = "budget_exhausted" if "budget" in reason else "not_advertised"
                result = tools.ToolResult(False, error=reason, code=code)
                return result
            if name == "discover_tools":
                result = tools.ToolResult(True, summary=discover_catalog(catalog_id, args))
                return result
        from oceano import safety
        context = {"session": session,
                   # Bridge taint is keyed on this, NOT on session: session is None for
                   # workflow/scheduler/Telegram-driven resident turns, which would put them all in
                   # one bucket that races itself clean. The catalog id is already unique per
                   # resident turn, and both the marker (here) and the gates resolve the same key.
                   "taint_scope": catalog_id or session,
                   "channel": "background" if background else "web",
                   "client": client}
        if state is not None:
            context["workspace"] = state.workspace
        with turnctx.push(**context):
            safety.reset_untrusted()
            result = tools.run_result(name, json.dumps(args or {}))
            if safety.untrusted_seen():
                safety.mark_bridge_untrusted()
            return result
    finally:
        if replayable and replay_owner:
            _replay_finish(replay_entry, result or tools.ToolResult(
                False, error="operation ended without a result", retryable=True,
                code="execution_interrupted"))


def run_tool(name, args, session=None, background=False, client="web", scope=None,
             catalog_id=None, operation_id=None):
    """Compatibility wrapper returning the historical model-facing string."""
    return run_tool_result(
        name, args, session=session, background=background, client=client, scope=scope,
        catalog_id=catalog_id, operation_id=operation_id).text()


def mcp_config_path(sid=None, background=False, client="web", scope=None,
                    catalog_id=None):
    """Write the --mcp-config Claude Code loads to launch our stdio bridge (daemon URL + token, plus
    this TURN's attributes: the conversation `sid` so a spawn_job routes its result back to this
    chat, `background` so bridged tools run on the background channel — no live browser / UI for
    a turn no one is watching — `client` so oceano/tools/desktop.py's tools unlock when the
    ORIGINAL web request that started this mind turn came from OceanoDesktop, not a browser tab,
    and `scope` to narrow the bridge to a curated subset for a contained sub-agent (see _SCOPES) —
    e.g. "skills" for a workflow Delegate/Agent-spawn node, so it can reuse Oceano's published
    skills but never reach memory or the rest of the body).
    Returns its path. The filename encodes all these attributes, so two concurrent turns (different
    sids, or a session-less scheduler turn overlapping a session-less telegram one) never clobber
    each other's config. data/ is gitignored, so nothing leaves the box."""
    import sys
    from pathlib import Path
    import config
    from oceano import atomicio
    env = {"OCEANO_MCP_URL": daemon_url(), "OCEANO_MCP_TOKEN": token(),
           # PYTHONPATH = the repo root so `-m oceano.mcp_bridge_server` imports even though Claude
           # launches the server with cwd=workspace (where the oceano package isn't on the path).
           "PYTHONPATH": str(config.WORKSPACE.parent)}
    if sid:
        env["OCEANO_MCP_SESSION"] = sid
    if background:
        env["OCEANO_MCP_BACKGROUND"] = "1"
    if client and client != "web":
        env["OCEANO_MCP_CLIENT"] = client
    if scope:
        env["OCEANO_MCP_SCOPE"] = scope
    if catalog_id:
        env["OCEANO_MCP_CATALOG"] = catalog_id
    cfg = {"mcpServers": {"oceano": {
        "command": sys.executable,
        "args": ["-m", "oceano.mcp_bridge_server"],
        "env": env,
    }}}
    fname = ("mind-mcp" + (f"-{sid}" if sid else "")           # sid matches [A-Za-z0-9_-] → safe filename
             + ("-bg" if background else "") + (f"-{client}" if client and client != "web" else "")
             + (f"-{scope}" if scope else "")
             + (f"-cat-{catalog_id[:8]}" if catalog_id else "") + ".json")
    path = config.WORKSPACE.parent / "data" / fname
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomicio.write_text(path, json.dumps(cfg, indent=2))
    except OSError:
        return None
    return str(path)
