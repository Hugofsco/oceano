"""The body-bridge: lets the Claude-mind use Oceano's OWN tools.

A thin stdio MCP server (oceano.mcp_bridge_server) runs *under* Claude Code and proxies every tool
call back to the daemon over a token-gated localhost endpoint, so Oceano's tools EXECUTE IN THE
DAEMON with full runtime context — ui_open reaches the live browser, memory/calendar hit the real
DBs, search hits the running SearXNG. A detached subprocess couldn't drive the daemon's UI or share
its state, hence the proxy.

Flow:  Claude  →(stdio MCP)→  mcp_bridge_server  →(HTTP + token)→  /api/mcp/call  →  tools.run()
"""
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

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


# The mind's BODY: Oceano's own tools, so the mind acts THROUGH Oceano (and the user can see it).
# Its native Read/Write/Bash cover files+shell, but the WEB is routed here on purpose — Oceano's
# web tools drive the shared live browser, so the user can watch (and hand-solve captchas) instead
# of the mind browsing invisibly with WebFetch. Kept reasonably small so Claude Code loads them all
# up front (exact names in --allowedTools) instead of deferring them behind its flaky ToolSearch.
_ALLOW = {
    # In hybrid resident mode these replace native file/shell tools, allowing the daemon to
    # enforce the same execution policy and per-turn budget before the operation starts.
    "list_files", "read_file", "code_search", "write_file", "edit_file", "make_folder",
    "run_shell", "python_exec", "run_tests", "git",
    "remember", "recall", "forget_memory", "update_memory",   # memory — Oceano's, the one the user sees
    "calendar_events", "manage_calendar", "find_free_slots",  # the calendar
    "schedule_task", "list_tasks", "update_task", "cancel_task",   # the PERSISTENT task scheduler — create/list/edit/cancel; the one the user sees, use instead of the mind's own cron
    "spawn_job", "job_status",                                # background OS jobs Oceano itself owns/outlives — use INSTEAD of your own native background execution, which dies or is orphaned the instant this turn's CLI process exits
    "spawn_agent", "agent_status",                            # background SUB-AGENTS (contained delegate runs) Oceano owns — parallel subtasks that report back into this chat
    "list_skills", "load_skill", "learn_skill",              # skills — reuse + grow Oceano's skill library (parity with the local mind)
    "run_workflow", "list_workflows",                        # workflows — run Oceano's saved multi-step recipes
    "search_docs", "index_docs",                             # RAG — search (and add to) the user's indexed documents
    "list_suggestions", "accept_suggestion", "dismiss_suggestion",   # the self-improvement queue (reflection → approve → act)
    "ui_open", "ui_close", "ui_arrange",                      # the windows (JARVIS bit)
    "notify",                                                 # push a notification to the user
    "web_search", "fetch_url",                               # the web — via the SHARED live browser, so the user watches
    "browser_open", "browser_click", "browser_scroll", "browser_screenshot",   # drive that browser
    "browser_snapshot", "browser_fill", "browser_select", "browser_press",      # …operate forms: map elements, fill, select, submit
    "browser_wait", "browser_extract", "browser_read",                          # …wait for content, extract data, read as markdown
    "browser_eval", "browser_hover", "browser_upload", "browser_dialog", "browser_tab",   # …JS eval (web-only), hover, upload, dialogs, tabs
    "list_hosts", "ssh_run", "sftp",                         # the SSH keychain (still web-channel + per-host policy gated)
    "mail_accounts", "mail_folders", "mail_list", "mail_read",          # email — discover + read
    "mail_move", "mail_delete", "mail_flag", "mail_send", "mail_reply",  # …organize + send (same gates apply)
    "mail_folder", "mail_save_attachment",                              # folders (gated) + save an email attachment to the workspace
    "desktop_notify", "desktop_pick_file", "desktop_save_file",         # OceanoDesktop-only native actions — no-op
    "desktop_reveal_path", "desktop_open_path",                        # unless this turn's client is threaded
    "desktop_clipboard_read", "desktop_clipboard_write", "desktop_screenshot",   # through below (client=...)
}

# A narrower bridge for CONTAINED sub-agents (workflow Delegate / Agent Spawn / Orchestrator-
# plugged nodes): let them reuse Oceano's published skills — so a background sub-agent doesn't
# have to reinvent a procedure Oceano already learned — but NOTHING else from the body. No
# memory (remember/recall/update_memory/forget_memory): a contained sub-agent's task is
# self-contained and must not see or change what the user's own mind remembers. No learn_skill
# either — growing the skill library is a bigger act than reusing it, left to the full bridge
# above. A flow that genuinely needs memory (or the rest of the body) should use an Instructions
# node instead, which runs through the full _ALLOW bridge (scope=None).
_SCOPES = {
    "skills": {"list_skills", "load_skill"},
}


def _allowset(scope):
    if scope is None:
        # the full body bridge: the curated set PLUS whatever MCP servers are connected right
        # now — MCP connections are part of Oceano's body, so the mind sees the same ones the
        # local model does. Computed fresh (not cached) since servers connect/disconnect at
        # runtime (oceano.mcp_client.reload()); a narrow sub-agent `scope` deliberately does NOT
        # get this — see the _SCOPES docstring above.
        return _ALLOW | {s["function"]["name"] for s in tools.schemas()
                         if s["function"]["name"].startswith("mcp__")}
    return _SCOPES.get(scope, _ALLOW)


@dataclass
class _CatalogState:
    route: object
    catalog: list
    allowed: set
    query: str
    model: str
    max_calls: int
    calls: int = 0
    created: float = 0.0


_CATALOGS = {}
_CATALOG_LOCK = threading.RLock()
_CATALOG_TTL = 6 * 3600


def _catalog_state(catalog_id):
    if not catalog_id:
        return None
    now = time.time()
    with _CATALOG_LOCK:
        for key in [key for key, value in _CATALOGS.items()
                    if now - value.created > _CATALOG_TTL]:
            _CATALOGS.pop(key, None)
        return _CATALOGS.get(catalog_id)


def create_catalog(query, model, max_calls, scope=None):
    """Create one opaque, daemon-owned resident catalog and execution budget."""
    from oceano import toolrouter
    catalog = tool_schemas(scope=scope)
    route = toolrouter.route(catalog, query or "", model=model or "resident",
                             surface="resident")
    catalog_id = secrets.token_urlsafe(18)
    state = _CatalogState(route=route, catalog=catalog,
                          allowed={schema["function"]["name"] for schema in catalog},
                          query=query or "", model=model or "resident",
                          max_calls=max(1, int(max_calls or 1)), created=time.time())
    with _CATALOG_LOCK:
        _CATALOGS[catalog_id] = state
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


def tool_schemas(scope=None, catalog_id=None):
    """Tools offered to the mind, optionally narrowed by an opaque per-turn catalog."""
    state = _catalog_state(catalog_id)
    if state is not None:
        return list(state.route.schemas)
    if catalog_id:
        return []
    return [s for s in tools.schemas() if s["function"]["name"] in _allowset(scope)]


def tool_names(scope=None, catalog_id=None):
    return [schema["function"]["name"] for schema in tool_schemas(scope, catalog_id)]


def run_tool(name, args, session=None, background=False, client="web", scope=None,
             catalog_id=None):
    """Execute an Oceano tool IN THE DAEMON. Interactive mind turns run on the 'web' channel (so ui_*
    reach the live browser the user is watching); an UNATTENDED (background) mind turn runs on the
    'background' channel instead — so it can't drive the live browser or UI. Returns the tool's
    string result. Re-checks the denylist so the proxy can't reach a withheld tool.

    `session`, `background`, and `client` are per-CALL turn attributes (from the X-Oceano-Session /
    X-Oceano-Background / X-Oceano-Client headers the mind's bridge forwards, themselves from the
    per-turn MCP config) — NOT process-globals, so concurrent turns for different chats never
    inherit each other's session, channel, or client. `session` routes a spawn_job's result back to
    the right chat; `client` is what oceano/tools/desktop.py's gate checks (only "desktop" — the
    original web request that started this mind turn came from OceanoDesktop — unlocks those tools).

    Carries the injection taint across the bridge: each call runs in its own request thread, so we
    reset the thread-local taint, run, and if the tool read untrusted content (web page / email /
    doc) raise the PROCESS-WIDE bridge taint — so a later ssh_run in the same mind turn is blocked.

    `scope` narrows which tools are reachable (see _SCOPES) — e.g. a contained workflow sub-agent
    gets "skills" (list_skills/load_skill only, no memory), never the full body."""
    state = _catalog_state(catalog_id)
    if catalog_id and state is None:
        return "ERROR: resident catalog expired or is invalid"
    if state is not None:
        ok, reason = consume_catalog_call(catalog_id, name)
        if not ok:
            return f"ERROR: {reason}"
        if name == "discover_tools":
            return discover_catalog(catalog_id, args)
    elif name not in _allowset(scope):
        return f"ERROR: tool {name!r} is not available to the mind"
    from oceano import safety
    with turnctx.push(session=session, channel="background" if background else "web", client=client):
        safety.reset_untrusted()                       # clean slate for this per-call thread
        result = tools.run(name, json.dumps(args or {}))
        if safety.untrusted_seen():                    # this tool ingested untrusted content → taint the turn
            safety.mark_bridge_untrusted()
        return result


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
