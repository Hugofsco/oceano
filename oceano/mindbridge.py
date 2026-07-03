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
from contextlib import contextmanager

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


def tool_schemas():
    """The Oceano tools offered to the mind: the curated body set, intersected with what's enabled."""
    return [s for s in tools.schemas() if s["function"]["name"] in _ALLOW]


def tool_names():
    return [s["function"]["name"] for s in tool_schemas()]


def run_tool(name, args, session=None, background=False, client="web"):
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
    doc) raise the PROCESS-WIDE bridge taint — so a later ssh_run in the same mind turn is blocked."""
    if name not in _ALLOW:
        return f"ERROR: tool {name!r} is not available to the mind"
    from oceano import safety
    with turnctx.push(session=session, channel="background" if background else "web", client=client):
        safety.reset_untrusted()                       # clean slate for this per-call thread
        result = tools.run(name, json.dumps(args or {}))
        if safety.untrusted_seen():                    # this tool ingested untrusted content → taint the turn
            safety.mark_bridge_untrusted()
        return result


def mcp_config_path(sid=None, background=False, client="web"):
    """Write the --mcp-config Claude Code loads to launch our stdio bridge (daemon URL + token, plus
    this TURN's attributes: the conversation `sid` so a spawn_job routes its result back to this
    chat, `background` so bridged tools run on the background channel — no live browser / UI for
    a turn no one is watching — and `client` so oceano/tools/desktop.py's tools unlock when the
    ORIGINAL web request that started this mind turn came from OceanoDesktop, not a browser tab).
    Returns its path. The filename encodes all three attributes, so two concurrent turns (different
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
    cfg = {"mcpServers": {"oceano": {
        "command": sys.executable,
        "args": ["-m", "oceano.mcp_bridge_server"],
        "env": env,
    }}}
    fname = ("mind-mcp" + (f"-{sid}" if sid else "")           # sid matches [A-Za-z0-9_-] → safe filename
             + ("-bg" if background else "") + (f"-{client}" if client and client != "web" else "") + ".json")
    path = config.WORKSPACE.parent / "data" / fname
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomicio.write_text(path, json.dumps(cfg, indent=2))
    except OSError:
        return None
    return str(path)
