"""Optional MCP (Model Context Protocol) client.

Connects to MCP servers listed in data/mcp.json and exposes each server's tools to
the agent as ordinary Oceano tools (named `mcp__<server>__<tool>`), so the model
can call Gmail, Calendar, filesystem servers, etc. through the same tool loop.

data/mcp.json:
  {"servers": [
     {"name": "fs", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/Oceano/workspace"]}
  ]}

Graceful by design: no config file, no servers, or a missing `mcp` SDK → no MCP
tools and no errors. MCP is async; we run one event loop in a background thread and
bridge the agent's synchronous tool calls onto it.
"""
import asyncio
import json
import threading
import time
import traceback

import config
from oceano import tools

CONFIG = config.WORKSPACE.parent / "data" / "mcp.json"
CALL_TIMEOUT = 120

_loop = None
_thread = None
_started = False
_sessions = {}        # server name -> ClientSession
_status = []          # [{server, ok, tools, error}] for the UI / status endpoint


def _read_config():
    try:
        return json.loads(CONFIG.read_text()).get("servers", [])
    except (OSError, json.JSONDecodeError):
        return []


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


def _call_sync(server, tool_name, kwargs):
    """Synchronous bridge the agent's tool layer calls — hops onto the MCP loop."""
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


async def _connect(server):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    name = server["name"]
    params = StdioServerParameters(command=server["command"], args=server.get("args", []),
                                   env=server.get("env") or None)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                _sessions[name] = session
                n = 0
                for t in listed.tools:
                    full = f"mcp__{name}__{t.name}"
                    schema = {"type": "function", "function": {
                        "name": full, "description": (t.description or f"{name} tool")[:1024],
                        "parameters": t.inputSchema or {"type": "object", "properties": {}}}}
                    fn = (lambda s, tn: (lambda **kw: _call_sync(s, tn, kw)))(name, t.name)
                    tools.register(full, schema, fn)
                    n += 1
                _status.append({"server": name, "ok": True, "tools": n})
                print(f"[mcp] {name}: connected, {n} tools")
                await asyncio.Event().wait()        # hold the session open until shutdown
    except Exception as e:
        _sessions.pop(name, None)
        _status.append({"server": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
        print(f"[mcp] {name}: failed — {e}")


def start():
    """Connect to all configured MCP servers (no-op if none / SDK missing)."""
    global _thread, _started
    if _started:
        return
    servers = _read_config()
    if not servers:
        return
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("[mcp] servers configured but the `mcp` SDK isn't installed (pip install mcp)")
        return
    _started = True
    _thread = threading.Thread(target=_run_loop, daemon=True)
    _thread.start()
    for _ in range(100):                # wait for the loop to come up
        if _loop is not None:
            break
        time.sleep(0.02)
    for s in servers:
        try:
            asyncio.run_coroutine_threadsafe(_connect(s), _loop)
        except Exception:
            traceback.print_exc()


def status():
    return {"started": _started, "servers": _status}
