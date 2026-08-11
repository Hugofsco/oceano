"""Optional MCP (Model Context Protocol) client.

Connects to MCP servers listed in data/mcp.json and exposes each server's tools to
the agent as ordinary Oceano tools (named `mcp__<server>__<tool>`), so the model
can call Linear, Notion, a local filesystem server, etc. through the same tool loop.

data/mcp.json:
  {"servers": [
     {"name": "linear", "url": "https://mcp.linear.app/mcp", "transport": "auto",
      "token": "", "headers": {}, "enabled": true},
     {"name": "fs", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/Oceano/workspace"],
      "enabled": true}
  ]}

A server is either remote (`url` — "auto" tries streamable-HTTP then falls back to SSE,
or pin "http"/"sse" explicitly) or local (`command`+`args`, spawned via stdio) — never both.
`token` becomes a `Bearer` Authorization header; `headers` adds/overrides any extra ones.

Graceful by design: no config file, no servers, or a missing `mcp` SDK → no MCP tools and
no errors. MCP is async; we run one event loop in a background thread and bridge the agent's
synchronous tool calls, and config edits (add/remove/disable), onto it via `reload()`.
"""
import asyncio
import json
import threading
import time
import traceback

import config
from oceano import atomicio, safety, secretcrypto, tools

CONFIG = config.WORKSPACE.parent / "data" / "mcp.json"
CALL_TIMEOUT = 120

# Curated starting points for the "common servers" gallery — remote, hosted MCP endpoints.
# Most require an access token from that provider (paste it into the token field after adding);
# `needs_token: False` marks the rare ones that are genuinely open/public as-is.
PRESETS = [
    {"name": "cloudflare-docs", "url": "https://docs.mcp.cloudflare.com/sse",
     "description": "Search Cloudflare's developer docs — public, no token needed.",
     "needs_token": False},
    {"name": "linear", "url": "https://mcp.linear.app/mcp",
     "description": "Linear issues, projects and cycles.", "needs_token": True},
    {"name": "notion", "url": "https://mcp.notion.com/mcp",
     "description": "Notion pages and databases.", "needs_token": True},
    {"name": "sentry", "url": "https://mcp.sentry.dev/mcp",
     "description": "Sentry issues and error tracking.", "needs_token": True},
    {"name": "asana", "url": "https://mcp.asana.com/sse",
     "description": "Asana tasks and projects.", "needs_token": True},
    {"name": "intercom", "url": "https://mcp.intercom.com/mcp",
     "description": "Intercom conversations and tickets.", "needs_token": True},
    {"name": "paypal", "url": "https://mcp.paypal.com/sse",
     "description": "PayPal invoices and transactions.", "needs_token": True},
    {"name": "square", "url": "https://mcp.squareup.com/sse",
     "description": "Square payments and catalog.", "needs_token": True},
    {"name": "github", "url": "https://api.githubcopilot.com/mcp/",
     "description": "GitHub repos, issues and pull requests.", "needs_token": True},
    {"name": "stripe", "url": "https://mcp.stripe.com",
     "description": "Stripe payments, customers and invoices.", "needs_token": True},
    {"name": "atlassian", "url": "https://mcp.atlassian.com/v1/sse",
     "description": "Jira issues and Confluence pages.", "needs_token": True},
    {"name": "neon", "url": "https://mcp.neon.tech/sse",
     "description": "Neon (serverless Postgres) projects and branches.", "needs_token": True},
    {"name": "slack", "url": "https://mcp.slack.com/mcp",
     "description": "Slack search, messaging and canvases. Needs a Slack app + OAuth user "
                     "token, not a plain API key — see docs.slack.dev for the token dance.",
     "needs_token": True},
    {"name": "deepwiki", "url": "https://mcp.deepwiki.com/mcp",
     "description": "Ask questions about any public GitHub repo's docs — public, no token needed.",
     "needs_token": False},
]

_loop = None
_thread = None
_started = False
_sessions = {}          # server name -> ClientSession, only while actively connected
_stop_events = {}        # server name -> asyncio.Event, signals that server's _connect to shut down
_configured_hash = {}     # server name -> hash of the config it's running with (change detection)
_status = {}             # server name -> {server, ok, tools/error, transport} for the UI


def _read_config():
    try:
        return json.loads(CONFIG.read_text()).get("servers", [])
    except (OSError, json.JSONDecodeError):
        return []


def _write_config(servers):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    atomicio.write_text(CONFIG, json.dumps({"servers": servers}, indent=2))


def _cfg_hash(s):
    return json.dumps(s, sort_keys=True)


def _encrypt_dict(d):
    return {k: secretcrypto.encrypt(v) if isinstance(v, str) else v for k, v in (d or {}).items()}


def _decrypt_dict(d):
    return {k: secretcrypto.decrypt(v) if isinstance(v, str) else v for k, v in (d or {}).items()}


def _auth_headers(server):
    headers = _decrypt_dict(server.get("headers"))
    token = secretcrypto.decrypt(server.get("token") or "").strip()
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
    return headers


def _register_tools(name, listed_tools):
    n = 0
    for t in listed_tools:
        full = f"mcp__{name}__{t.name}"
        schema = {"type": "function", "function": {
            "name": full, "description": (t.description or f"{name} tool")[:1024],
            "parameters": t.inputSchema or {"type": "object", "properties": {}}}}
        fn = (lambda s, tn: (lambda **kw: _call_sync(s, tn, kw)))(name, t.name)
        tools.register(full, schema, fn)
        n += 1
    return n


def _call_sync(server, tool_name, kwargs):
    """Synchronous bridge the agent's tool layer calls — hops onto the MCP loop."""
    if safety.taint_active("mcp"):
        return ("Blocked for safety: this turn already read external content (a web page, email, or "
                 "document), so calling connected MCP tools is disabled — injected text must not "
                 "reach them. Ask the user to send a fresh message to use MCP tools.")
    sess = _sessions.get(server)
    if sess is None or _loop is None:
        return f"ERROR: MCP server {server!r} is not connected"
    try:
        fut = asyncio.run_coroutine_threadsafe(sess.call_tool(tool_name, kwargs or {}), _loop)
        res = fut.result(timeout=CALL_TIMEOUT)
    except Exception as e:
        return f"ERROR calling {server}.{tool_name}: {type(e).__name__}: {e}"
    parts = []
    for c in getattr(res, "content", None) or []:
        t = getattr(c, "text", None)
        parts.append(t if t is not None else str(c))
    text = "\n".join(parts) or "(no output)"
    return text[:8000]


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


async def _connect(server, cfg_hash):
    from mcp import ClientSession
    name = server["name"]
    stop = asyncio.Event()
    _stop_events[name] = stop
    if _configured_hash.get(name) != cfg_hash:   # superseded/removed before we even started
        _stop_events.pop(name, None)
        return
    is_stdio = bool(server.get("command"))
    if is_stdio:
        kinds = ["stdio"]
    else:
        transport = server.get("transport") or "auto"
        kinds = ["http", "sse"] if transport == "auto" else [transport]
    last_err = None
    for kind in kinds:
        try:
            if kind == "stdio":
                from mcp.client.stdio import StdioServerParameters, stdio_client
                params = StdioServerParameters(command=server["command"], args=server.get("args", []),
                                               env=_decrypt_dict(server.get("env")) or None)
                cm = stdio_client(params)
            elif kind == "http":
                from mcp.client.streamable_http import streamablehttp_client
                cm = streamablehttp_client(server["url"], headers=_auth_headers(server))
            else:
                from mcp.client.sse import sse_client
                cm = sse_client(server["url"], headers=_auth_headers(server))
            async with cm as rw:
                read, write = rw[0], rw[1]      # streamable-http yields a 3rd item (session-id getter)
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    _sessions[name] = session
                    n = _register_tools(name, listed.tools)
                    _status[name] = {"server": name, "ok": True, "tools": n, "transport": kind}
                    print(f"[mcp] {name}: connected via {kind}, {n} tools")
                    await stop.wait()           # held open until reload()/remove signals shutdown
                    _stop_events.pop(name, None)
                    return                       # clean shutdown, not a failure
        except Exception as e:
            last_err = e
            continue                             # "auto": try the next transport
    _sessions.pop(name, None)
    _status[name] = {"server": name, "ok": False, "error": f"{type(last_err).__name__}: {last_err}"}
    print(f"[mcp] {name}: failed — {last_err}")
    _stop_events.pop(name, None)


def _disconnect_one(name):
    ev = _stop_events.get(name)
    if ev is not None and _loop is not None:
        _loop.call_soon_threadsafe(ev.set)
    tools.unregister_prefix(f"mcp__{name}__")
    _sessions.pop(name, None)
    _status.pop(name, None)
    _configured_hash.pop(name, None)


def _bootstrap_loop():
    global _thread, _started
    if _started:
        return True
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("[mcp] servers configured but the `mcp` SDK isn't installed (pip install mcp)")
        return False
    _started = True
    _thread = threading.Thread(target=_run_loop, daemon=True)
    _thread.start()
    for _ in range(100):                # wait for the loop to come up
        if _loop is not None:
            break
        time.sleep(0.02)
    return True


def start():
    """Connect to all configured, enabled MCP servers (no-op if none / SDK missing)."""
    if not _read_config():
        return
    reload()


def reload():
    """Converge running connections to match data/mcp.json — connect new/changed/enabled
    servers, disconnect removed/disabled/changed ones. Safe to call any time, including
    before the first connect (bootstraps the loop) and after every config CRUD op."""
    if not _bootstrap_loop():
        return
    configured = {s["name"]: s for s in _read_config() if s.get("name")}
    for name in list(_configured_hash):                       # drop removed/disabled/changed
        want = configured.get(name)
        if want is None or not want.get("enabled", True) or _cfg_hash(want) != _configured_hash[name]:
            _disconnect_one(name)
    for name, s in configured.items():                         # connect new/changed/enabled
        if not s.get("enabled", True):
            continue
        h = _cfg_hash(s)
        if _configured_hash.get(name) == h:
            continue                                            # unchanged & already (being) connected
        _configured_hash[name] = h
        _status[name] = {"server": name, "ok": None, "tools": 0}   # "connecting…"
        try:
            asyncio.run_coroutine_threadsafe(_connect(s, h), _loop)
        except Exception:
            traceback.print_exc()


def status():
    return {"started": _started, "servers": list(_status.values())}


# ---------------- config CRUD (used by the /api/mcp/servers routes) ----------------
def list_servers():
    """Configured servers with secrets masked and live status merged in."""
    live = _status
    out = []
    for s in _read_config():
        row = {k: v for k, v in s.items() if k not in ("token", "headers", "env")}
        row["has_token"] = bool((s.get("token") or "").strip())
        row.update(live.get(s["name"], {"ok": None, "tools": 0}))
        row["server"] = s["name"]                  # keep the row's own name authoritative
        out.append(row)
    return out


def add_server(cfg):
    name = (cfg.get("name") or "").strip()
    if not name:
        raise ValueError("server needs a name")
    if not cfg.get("url") and not cfg.get("command"):
        raise ValueError("server needs a url (remote) or a command (local)")
    servers = [s for s in _read_config() if s["name"] != name]
    servers.append({
        "name": name, "url": (cfg.get("url") or "").strip(),
        "transport": cfg.get("transport") if cfg.get("transport") in ("auto", "http", "sse") else "auto",
        "token": secretcrypto.encrypt(cfg.get("token") or ""), "headers": _encrypt_dict(cfg.get("headers")),
        "command": (cfg.get("command") or "").strip(), "args": cfg.get("args") or [],
        "env": _encrypt_dict(cfg.get("env")), "enabled": cfg.get("enabled", True),
    })
    _write_config(servers)
    reload()


def update_server(name, patch):
    servers = _read_config()
    found = False
    for s in servers:
        if s["name"] == name:
            for k in ("url", "transport", "token", "headers", "command", "args", "env", "enabled"):
                if k in patch:
                    if k == "token":
                        s[k] = secretcrypto.encrypt(patch[k] or "")
                    elif k in ("headers", "env"):
                        s[k] = _encrypt_dict(patch[k])
                    else:
                        s[k] = patch[k]
            found = True
    if not found:
        raise ValueError(f"no such server: {name}")
    _write_config(servers)
    reload()


def remove_server(name):
    servers = [s for s in _read_config() if s["name"] != name]
    _write_config(servers)
    _disconnect_one(name)


def wipe():
    """Remove EVERY registered MCP server (Settings → Wipe): clears the stored config
    (urls/commands/tokens) and disconnects each live session so its tools disappear from
    the agent immediately. Returns how many servers were removed."""
    servers = _read_config()
    _write_config([])
    for s in servers:
        _disconnect_one(s.get("name", ""))
    return len(servers)
