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

from oceano import tools

# The mind's BODY: Oceano's own tools, so the mind acts THROUGH Oceano (and the user can see it).
# Its native Read/Write/Bash cover files+shell, but the WEB is routed here on purpose — Oceano's
# web tools drive the shared live browser, so the user can watch (and hand-solve captchas) instead
# of the mind browsing invisibly with WebFetch. Kept reasonably small so Claude Code loads them all
# up front (exact names in --allowedTools) instead of deferring them behind its flaky ToolSearch.
_ALLOW = {
    "remember", "recall", "forget_memory", "update_memory",   # memory — Oceano's, the one the user sees
    "calendar_events", "manage_calendar", "find_free_slots",  # the calendar
    "ui_open", "ui_close", "ui_arrange",                      # the windows (JARVIS bit)
    "notify",                                                 # push a notification to the user
    "web_search", "fetch_url",                               # the web — via the SHARED live browser, so the user watches
    "browser_open", "browser_click", "browser_scroll", "browser_screenshot",   # drive that browser
    "list_hosts", "ssh_run", "sftp",                         # the SSH keychain (still web-channel + per-host policy gated)
    "mail_accounts", "mail_folders", "mail_list", "mail_read",          # email — discover + read
    "mail_move", "mail_delete", "mail_flag", "mail_send", "mail_reply",  # …organize + send (same gates apply)
    "mail_folder", "mail_save_attachment",                              # folders (gated) + save an email attachment to the workspace
}

_TOKEN = None
_CONFIG_PATH = None


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


def run_tool(name, args):
    """Execute an Oceano tool IN THE DAEMON (web channel, so ui_* reach the live browser). Returns
    the tool's string result. Re-checks the denylist so the proxy can't reach a withheld tool.

    Carries the injection taint across the bridge: each call runs in its own request thread, so we
    reset the thread-local taint, run, and if the tool read untrusted content (web page / email /
    doc) raise the PROCESS-WIDE bridge taint — so a later ssh_run in the same mind turn is blocked."""
    if name not in _ALLOW:
        return f"ERROR: tool {name!r} is not available to the mind"
    from oceano import safety
    with tools.channel("web"):
        safety.reset_untrusted()                       # clean slate for this per-call thread
        result = tools.run(name, json.dumps(args or {}))
        if safety.untrusted_seen():                    # this tool ingested untrusted content → taint the turn
            safety.mark_bridge_untrusted()
        return result


def mcp_config_path():
    """Write (once) the --mcp-config Claude Code loads to launch our stdio bridge with the daemon URL
    + token, and return its path. data/ is gitignored, so the token never leaves the box."""
    global _CONFIG_PATH
    import sys
    from pathlib import Path
    import config
    from oceano import atomicio
    cfg = {"mcpServers": {"oceano": {
        "command": sys.executable,
        "args": ["-m", "oceano.mcp_bridge_server"],
        # PYTHONPATH = the repo root so `-m oceano.mcp_bridge_server` imports even though Claude
        # launches the server with cwd=workspace (where the oceano package isn't on the path).
        "env": {"OCEANO_MCP_URL": daemon_url(), "OCEANO_MCP_TOKEN": token(),
                "PYTHONPATH": str(config.WORKSPACE.parent)},
    }}}
    path = config.WORKSPACE.parent / "data" / "mind-mcp.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomicio.write_text(path, json.dumps(cfg, indent=2))
    except OSError:
        return None
    _CONFIG_PATH = str(path)
    return _CONFIG_PATH
