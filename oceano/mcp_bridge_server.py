"""stdio MCP server that gives the Claude-mind Oceano's own tools.

Launched by Claude Code (via the --mcp-config the daemon writes). It fetches Oceano's tool schemas
from the daemon and exposes them as MCP tools; each call is proxied straight back to the daemon's
token-gated /api/mcp/call, so the tool runs IN the daemon with full context (live UI, real DBs).

Decoupled on purpose: it imports no Oceano internals — just talks HTTP to the daemon — so it starts
fast and can't accidentally run a tool in this detached process. Config via env:
  OCEANO_MCP_URL    the daemon base URL (e.g. http://127.0.0.1:8800)
  OCEANO_MCP_TOKEN  the shared localhost secret
"""
import asyncio
import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as t

URL = os.environ.get("OCEANO_MCP_URL", "http://127.0.0.1:8800").rstrip("/")
TOKEN = os.environ.get("OCEANO_MCP_TOKEN", "")
SESSION = os.environ.get("OCEANO_MCP_SESSION", "")   # the chat this mind turn drives (per-turn config)
BACKGROUND = os.environ.get("OCEANO_MCP_BACKGROUND", "")   # unattended turn → tools run on the background channel
CLIENT = os.environ.get("OCEANO_MCP_CLIENT", "")     # "desktop" if the web request that started this turn came from OceanoDesktop
SCOPE = os.environ.get("OCEANO_MCP_SCOPE", "")       # narrows the bridge for a contained sub-agent (e.g. "skills")
CATALOG = os.environ.get("OCEANO_MCP_CATALOG", "")   # opaque per-turn dynamic catalog + budget
HEADERS = {"X-Oceano-Mind-Token": TOKEN}             # token in a header, never the URL/body (no log leak)
if SESSION:
    HEADERS["X-Oceano-Session"] = SESSION            # so a spawn_job routes its result back to this chat
if BACKGROUND:
    HEADERS["X-Oceano-Background"] = "1"             # so the daemon gates live-browser/UI tools for this turn
if CLIENT:
    HEADERS["X-Oceano-Client"] = CLIENT              # so oceano/tools/desktop.py's gate unlocks for this turn
if SCOPE:
    HEADERS["X-Oceano-Scope"] = SCOPE                # so the daemon exposes only this scope's curated tools
if CATALOG:
    HEADERS["X-Oceano-Catalog"] = CATALOG            # opaque id; actual allowlist stays in the daemon

server = Server("oceano")
_SCHEMAS = []


@server.list_tools()
async def list_tools():
    return [t.Tool(name=s["function"]["name"],
                   description=s["function"].get("description", ""),
                   inputSchema=s["function"].get("parameters") or {"type": "object", "properties": {}})
            for s in _SCHEMAS]


@server.call_tool()
async def call_tool(name, arguments):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{URL}/api/mcp/call",
                             json={"name": name, "args": arguments or {}}, headers=HEADERS, timeout=600)
            r.raise_for_status()
            out = r.json().get("result", "")
            if name == "discover_tools" and CATALOG and not str(out).startswith("ERROR"):
                listed = await c.get(f"{URL}/api/mcp/tools", headers=HEADERS, timeout=15)
                listed.raise_for_status()
                global _SCHEMAS
                _SCHEMAS = listed.json().get("tools", [])
                await server.request_context.session.send_tool_list_changed()
    except Exception as e:                                 # never crash Claude's tool loop
        out = f"ERROR reaching Oceano: {type(e).__name__}: {e}"
    return [t.TextContent(type="text", text=str(out))]


async def main():
    global _SCHEMAS
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{URL}/api/mcp/tools", headers=HEADERS, timeout=15)
            r.raise_for_status()
            _SCHEMAS = r.json().get("tools", [])
    except Exception:
        _SCHEMAS = []                                      # daemon unreachable → expose nothing, don't crash
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
