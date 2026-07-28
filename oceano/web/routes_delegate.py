"""Delegation routes: the mind switch (local / Claude / Codex), per-mind model
and reasoning-effort settings, the MCP body-bridge (/api/mcp/*), per-role
delegation config + live probes, and the primary (default) model."""
import asyncio
import hmac

from fastapi import APIRouter, HTTPException, Request

import config
from oceano import mcp_client
from oceano.web.state import endpoint_key

router = APIRouter()


# ---------------- delegation (Claude Code readiness + per-role provider config) ----------------
@router.get("/api/mind")
def mind_get():
    """Which mind drives the primary chat: local, Claude Code, or Codex CLI. Returns the current
    selection plus which external mind binaries are available on this host."""
    from oceano import delegate
    return {"mind": delegate.get_mind(), "claude_available": delegate.available(),
            "codex_available": delegate.codex_available()}


@router.post("/api/mind")
async def mind_set(req: Request):
    from oceano import delegate
    mind = (await req.json()).get("mind", "local")
    return {"mind": delegate.set_mind(mind), "claude_available": delegate.available(),
            "codex_available": delegate.codex_available()}


@router.get("/api/claude-model")
def claude_model_get():
    """Which Claude model the CLI uses (the Claude mind + Claude-Code delegation). '' = CLI default."""
    from oceano import delegate
    return {"model": delegate.get_claude_model(), "options": list(delegate.CLAUDE_MODELS),
            "available": delegate.available()}


@router.post("/api/claude-model")
async def claude_model_set(req: Request):
    from oceano import delegate
    model = (await req.json()).get("model", "")
    return {"ok": True, "model": delegate.set_claude_model(model)}


@router.get("/api/codex-model")
def codex_model_get():
    """Which Codex model the CLI uses for the resident Codex mind. '' = CLI default."""
    from oceano import delegate
    return {"model": delegate.get_codex_model(), "options": list(delegate.CODEX_MODELS),
            "available": delegate.codex_available()}


@router.post("/api/codex-model")
async def codex_model_set(req: Request):
    from oceano import delegate
    model = (await req.json()).get("model", "")
    return {"ok": True, "model": delegate.set_codex_model(model)}


@router.get("/api/claude-effort")
def claude_effort_get():
    """The Claude reasoning-effort level (Claude mind + Claude-Code delegation). '' = CLI default."""
    from oceano import delegate
    return {"effort": delegate.get_claude_effort(), "options": list(delegate.CLAUDE_EFFORTS),
            "available": delegate.available()}


@router.post("/api/claude-effort")
async def claude_effort_set(req: Request):
    from oceano import delegate
    return {"ok": True, "effort": delegate.set_claude_effort((await req.json()).get("effort", ""))}


@router.get("/api/codex-effort")
def codex_effort_get():
    """The Codex reasoning-effort level for the resident Codex mind + delegation. '' = CLI default."""
    from oceano import delegate
    return {"effort": delegate.get_codex_effort(), "options": list(delegate.CODEX_EFFORTS),
            "available": delegate.codex_available()}


@router.post("/api/codex-effort")
async def codex_effort_set(req: Request):
    from oceano import delegate
    return {"ok": True, "effort": delegate.set_codex_effort((await req.json()).get("effort", ""))}


# --- the body-bridge: the Claude-mind's MCP proxy reaches Oceano's tools through here. Token-gated
#     (mindbridge.token()), localhost; exempt from the session middleware above. The token rides in a
#     header (never the URL/body, so it can't leak into access logs) and is compared constant-time. ---
def _mcp_authed(request: Request):
    from oceano import mindbridge
    tok = request.headers.get("x-oceano-mind-token", "")
    return bool(tok) and hmac.compare_digest(tok, mindbridge.token())


@router.get("/api/mcp/tools")
def mcp_tools(request: Request):
    if not _mcp_authed(request):
        raise HTTPException(403, "bad bridge token")
    from oceano import mindbridge
    scope = request.headers.get("x-oceano-scope") or None
    catalog_id = request.headers.get("x-oceano-catalog") or None
    return {"tools": mindbridge.tool_schemas(scope=scope, catalog_id=catalog_id)}


@router.post("/api/mcp/call")
async def mcp_call(req: Request):
    if not _mcp_authed(req):
        raise HTTPException(403, "bad bridge token")
    from oceano import mindbridge
    b = await req.json()
    name, args = b.get("name", ""), b.get("args") or {}
    session = req.headers.get("x-oceano-session") or None    # which chat this mind turn drives (spawn_job routing)
    background = req.headers.get("x-oceano-background") == "1"   # unattended turn → background channel (no live UI)
    client = req.headers.get("x-oceano-client") or "web"      # "desktop" unlocks oceano/tools/desktop.py's tools
    scope = req.headers.get("x-oceano-scope") or None
    catalog_id = req.headers.get("x-oceano-catalog") or None
    print(f"[mind] tool {name}({list(args)})", flush=True)
    return {"result": await asyncio.to_thread(
        mindbridge.run_tool, name, args, session, background, client, scope, catalog_id)}


@router.get("/api/delegate")
def delegate_status():
    """Claude readiness (shared) + per-role config/readiness: 'default' (agent delegate tool)
    and 'improve' (self-improving jobs: skills, evals, memory)."""
    from oceano import delegate
    return {**delegate.status_all(), "enabled": delegate.enabled()}


@router.post("/api/delegate")
async def delegate_set(req: Request):
    from oceano import delegate
    b = await req.json()
    role = b.get("role", "default")
    if role not in delegate.ROLES:
        return {"ok": False, "error": "unknown role"}
    delegate.set_config(b, role=role)
    return {"ok": True, **delegate.status_all()}


@router.post("/api/delegate/test")
async def delegate_test(req: Request):
    """Live probe of a role's provider (proves Claude Code auth, or the API model works).
    Runs in a thread so the ~minute timeout can't block the event loop."""
    from oceano import delegate
    try:
        b = await req.json()
    except Exception:
        b = {}
    role = b.get("role", "default")
    role = role if role in delegate.ROLES else "default"
    return await asyncio.to_thread(delegate.probe, role)


@router.get("/api/default-model")
def get_default_model_api():
    """The primary model + endpoint the agent uses everywhere. The picker lists ALL models
    (/api/models, any endpoint) — local-first is opt-in, so any model can be primary.
      model     the explicit primary the user pinned ('' = none → use the resolved default)
      current   what Oceano actually resolves to right now (primary > env > served)
      fallback  what the 'Default' (un-pinned) choice resolves to: env pin or first Rivers model
      source    where `current` came from: primary | env | served | none"""
    from oceano import delegate
    p = delegate.get_primary()
    r = delegate.resolve_primary()
    implicit = config.MODEL or (delegate.served_models()[:1] or [""])[0]
    return {"model": p["model"], "base_url": p["base_url"],
            "current": r["model"], "source": r["source"], "fallback": implicit,
            "route_by_evals": delegate.get_route_by_evals(),
            "evals_winner": delegate._eval_winner(delegate.served_models())}


@router.post("/api/default-model")
async def set_default_model_api(req: Request):
    """Set the primary model. base_url empty = the default local endpoint; otherwise resolve and
    store that endpoint's api key so the agent can reach it from Telegram/CLI/jobs too."""
    from oceano import delegate
    b = await req.json()
    model = (b.get("model") or "").strip()
    base_url = (b.get("base_url") or "").strip()
    api_key = endpoint_key(base_url) if base_url else ""
    delegate.set_primary(model, base_url, api_key)
    return {"ok": True, "current": delegate.get_default_model()}


@router.post("/api/delegate/route-by-evals")
async def set_route_by_evals_api(req: Request):
    """Toggle eval-leaderboard routing: with no primary pinned, the agent runs the top scorer
    of the latest finished eval run (among served models) instead of llama-swap file order."""
    from oceano import delegate
    b = await req.json()
    on = delegate.set_route_by_evals(bool(b.get("enabled")))
    return {"ok": True, "route_by_evals": on,
            "evals_winner": delegate._eval_winner(delegate.served_models())}


@router.post("/api/delegate/enabled")
async def set_delegation_enabled(req: Request):
    """Master delegation switch. Off → run() refuses (background jobs + the tool) and the
    delegate tool is withheld from the agent. (Also toggleable per-tool under Settings → Tools.)"""
    from oceano import delegate, tools
    b = await req.json()
    on = bool(b.get("enabled", True))
    delegate.set_enabled(on)
    tools.set_enabled("delegate", on)                # keep the agent's delegate tool in sync
    return {"ok": True, "enabled": on, **delegate.status_all()}


@router.get("/api/mcp")
def mcp_status():
    return mcp_client.status()
