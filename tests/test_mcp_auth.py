"""Regression test for the MCP-routes auth gap: the app's single auth middleware
(oceano.web.server._require_auth) used to exempt the ENTIRE /api/mcp/ prefix, meaning to
cover only the two token-gated mind-bridge routes (/api/mcp/tools, /api/mcp/call) — but that
let routes_mcp.py's server-registration endpoints (which can spawn an arbitrary local process
via add_server()) run with NO auth at all. This pins the fix: routes_mcp.py's routes now
require the normal session cookie, while the bridge routes keep using their own token check.

Builds a minimal FastAPI app (the real middleware + the real routers, no lifespan) so this
never touches the real data/web.json, starts MCP servers, or otherwise has side effects.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from oceano.web import routes_delegate, routes_mcp, state  # noqa: E402
from oceano.web.server import _require_auth  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STORE", tmp_path / "web.json")


@pytest.fixture
def client():
    app = FastAPI()
    app.middleware("http")(_require_auth)
    app.include_router(routes_mcp.router)
    app.include_router(routes_delegate.router)
    return TestClient(app)


def test_mcp_servers_routes_require_a_session_cookie(client):
    assert client.get("/api/mcp/servers").status_code == 401
    assert client.post("/api/mcp/servers", json={"name": "x", "command": "bash"}).status_code == 401
    assert client.patch("/api/mcp/servers/x", json={"enabled": False}).status_code == 401
    assert client.delete("/api/mcp/servers/x").status_code == 401
    assert client.get("/api/mcp/presets").status_code == 401


def test_bridge_routes_still_gated_by_mind_token_not_cookie(client):
    # No cookie AND no bridge token: the bridge's own _mcp_authed() check must fire (403),
    # not the cookie gate (401) — proves the narrowed exemption didn't touch these two routes.
    assert client.get("/api/mcp/tools").status_code == 403
    assert client.post("/api/mcp/call", json={"tool": "x", "args": {}}).status_code == 403


def test_dynamic_catalog_routes_schema_and_call_through_token_gated_bridge(
        client, monkeypatch, tmp_path):
    from oceano import mindbridge, toolrouter, tools
    config_path = tmp_path / "resident-tools.toml"
    config_path.write_text(
        '[surfaces.resident]\nmode = "hybrid"\nschema_budget = 500\n'
        'max_schema_budget = 2000\ndiscovery = true\n')
    monkeypatch.setenv("OCEANO_TOOL_CONFIG", str(config_path))
    toolrouter._CACHE.update({"path": None, "mtime": None, "data": {}})
    schema = {"type": "function", "function": {
        "name": "bridge_probe", "description": "Controlled bridge probe",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                       "required": ["value"]}}}
    tools.register("bridge_probe", schema, lambda value: "probe:" + value)
    mindbridge._ALLOW.add("bridge_probe")
    try:
        catalog_id, _route = mindbridge.create_catalog(
            "use the bridge probe", "claude:test", max_calls=1)
        headers = {"X-Oceano-Mind-Token": mindbridge.token(),
                   "X-Oceano-Catalog": catalog_id}
        advertised = client.get("/api/mcp/tools", headers=headers)
        assert advertised.status_code == 200
        assert "bridge_probe" in {tool["function"]["name"] for tool in advertised.json()["tools"]}
        called = client.post("/api/mcp/call", headers=headers,
                             json={"name": "bridge_probe", "args": {"value": "ok"}})
        assert called.json()["result"] == "probe:ok"
        exhausted = client.post("/api/mcp/call", headers=headers,
                                json={"name": "bridge_probe", "args": {"value": "again"}})
        assert "budget exhausted" in exhausted.json()["result"]
    finally:
        mindbridge._ALLOW.discard("bridge_probe")
        tools.unregister_prefix("bridge_probe")
