"""MCP (Model Context Protocol) client routes: configure remote/local MCP servers
(data/mcp.json via oceano.mcp_client) and browse the common-servers preset gallery.

Separate from the /api/mcp/tools and /api/mcp/call routes in routes_delegate.py, which
are the OTHER direction — Oceano's own tools exposed outward to the Claude-mind bridge."""
from fastapi import APIRouter, HTTPException, Request

from oceano import mcp_client

router = APIRouter()


@router.get("/api/mcp/servers")
def mcp_servers():
    return {"servers": mcp_client.list_servers(), "started": mcp_client.status()["started"]}


@router.get("/api/mcp/presets")
def mcp_presets():
    return {"presets": mcp_client.PRESETS}


@router.post("/api/mcp/servers")
async def mcp_add_server(req: Request):
    body = await req.json()
    try:
        mcp_client.add_server(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.patch("/api/mcp/servers/{name}")
async def mcp_update_server(name: str, req: Request):
    try:
        mcp_client.update_server(name, await req.json())
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.delete("/api/mcp/servers/{name}")
def mcp_remove_server(name: str):
    mcp_client.remove_server(name)
    return {"ok": True}
