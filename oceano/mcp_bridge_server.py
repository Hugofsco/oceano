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
HEADERS = {"X-Oceano-Mind-Token": TOKEN}        # token in a header, never the URL/body (no log leak)

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
