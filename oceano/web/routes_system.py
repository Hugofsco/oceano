"""System routes: providers/endpoints/prefs, Telegram + notification settings,
the interactive terminal WebSocket, service status/health, background jobs and
the activity logs, the agent-tools list, aggregated models, and wipe."""
import asyncio
import shutil
import time
import traceback

import requests
from fastapi import APIRouter, HTTPException, Request, WebSocket

import config
from oceano import chats, embeddings, memory, rag, rerank, rivers, scheduler, skills
from oceano.web import telegram_runtime
from oceano.web.state import (
    PROVIDERS,
    SESSION_COOKIE,
    _BOOT_TS,
    _TOOL_CATEGORY,
    _apply_telegram,
    _effective_model,
    _embed_reachable,
    _is_default_pw,
    _notify_seed,
    _telegram_seed,
    _token_user,
    list_models,
    load,
    save,
)

router = APIRouter()


@router.get("/api/providers")
def providers():
    return PROVIDERS


@router.get("/api/config")
def get_config():
    data = load()
    eps = [{"name": e["name"], "base_url": e["base_url"], "has_key": bool(e.get("api_key"))}
           for e in data["endpoints"]]
    tg = data.get("telegram", {})
    return {"endpoints": eps, "prefs": data.get("prefs", {}),
            "telegram": {"enabled": bool(tg.get("enabled")),
                         "has_token": bool(tg.get("token")),   # token itself is never sent down
                         "allowed": tg.get("allowed", []),
                         "status": telegram_runtime.status()}}


@router.post("/api/endpoints")
async def add_endpoint(req: Request):
    body = await req.json()
    data = load()
    data["endpoints"] = [e for e in data["endpoints"] if e["name"] != body["name"]]
    data["endpoints"].append({"name": body["name"], "base_url": body["base_url"].rstrip("/"),
                              "api_key": body.get("api_key", "")})
    save(data)
    return {"ok": True}


@router.delete("/api/endpoints/{name}")
def del_endpoint(name: str):
    data = load()
    data["endpoints"] = [e for e in data["endpoints"] if e["name"] != name]
    save(data)
    return {"ok": True}


@router.post("/api/prefs")
async def set_prefs(req: Request):
    data = load()
    data["prefs"] = {**data.get("prefs", {}), **(await req.json())}
    save(data)
    return {"ok": True}


# ---------------- telegram (folded into this daemon) ----------------
def _parse_ids(value):
    """Accept a list or a comma/space-separated string of Telegram user IDs -> [int]."""
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    out = []
    for x in value or []:
        try:
            out.append(int(str(x).strip()))
        except (TypeError, ValueError):
            pass
    return sorted(set(out))


@router.post("/api/telegram")
async def set_telegram(req: Request):
    body = await req.json()
    data = load()
    tg = data.get("telegram", _telegram_seed())
    if "enabled" in body:
        tg["enabled"] = bool(body["enabled"])
    if body.get("clear_token"):
        tg["token"] = ""
    elif body.get("token"):                     # blank token = "leave it unchanged"
        tg["token"] = body["token"].strip()
    if "allowed" in body:
        tg["allowed"] = _parse_ids(body["allowed"])
    data["telegram"] = tg
    save(data)
    result = await _apply_telegram(data)
    return {"ok": "error" not in result, **result, "status": telegram_runtime.status()}


# ---------------- notifications (how the agent pings you: ntfy + Telegram) ----------------
@router.get("/api/notify")
def get_notify():
    from oceano import notifications
    n = load().get("notify", _notify_seed())
    ready = notifications.channels_ready()
    return {"ntfy_url": n.get("ntfy_url", "https://ntfy.sh"), "ntfy_topic": n.get("ntfy_topic", ""),
            "telegram": n.get("telegram", True) is not False,
            "ready": ready, "telegram_running": telegram_runtime.status().get("running", False)}


@router.post("/api/notify")
async def set_notify(req: Request):
    b = await req.json()
    data = load()
    n = data.get("notify", _notify_seed())
    if "ntfy_url" in b:
        n["ntfy_url"] = (b.get("ntfy_url") or "https://ntfy.sh").strip().rstrip("/")
    if "ntfy_topic" in b:
        n["ntfy_topic"] = (b.get("ntfy_topic") or "").strip()
    if "telegram" in b:
        n["telegram"] = bool(b["telegram"])
    data["notify"] = n
    save(data)
    return {"ok": True, **{k: get_notify()[k] for k in ("ntfy_topic", "ntfy_url", "telegram", "ready")}}


@router.post("/api/notify/test")
async def test_notify():
    from oceano import notifications
    msg = await asyncio.to_thread(notifications.send, "This is a test notification from Oceano. 🌊", "Oceano test")
    return {"ok": msg.startswith("notified"), "result": msg}


# ---------------- interactive terminal (PTY ↔ xterm.js over a WebSocket) ----------------
@router.websocket("/api/terminal/ws")
async def terminal_ws(ws: WebSocket):
    """A real shell in the workspace. Auth-gated here (the HTTP _require_auth middleware doesn't
    cover the WS handshake): same-origin + a valid session cookie + a non-default password."""
    # Cross-Site WebSocket Hijacking guard: a WS handshake isn't bound by CORS and the browser
    # attaches cookies, so a malicious page could otherwise open a shell using the user's session.
    # Require the handshake Origin to be THIS server's own origin. The browser sets Origin and
    # forbids page JS from changing it, so a cross-site page can't pass this; non-browser clients
    # can spoof Origin but hold no session cookie. (Empty/foreign Origin → rejected.)
    host = ws.headers.get("host", "")
    origin = ws.headers.get("origin", "")
    if not host or origin not in (f"http://{host}", f"https://{host}"):
        await ws.close(code=1008)
        return
    auth = load().get("auth", {})
    if not _token_user(ws.cookies.get(SESSION_COOKIE, ""), auth) or _is_default_pw(auth):
        await ws.close(code=1008)                  # policy violation
        return
    await ws.accept()
    from oceano.web import terminal
    host_param = (ws.query_params.get("host") or "").strip()
    try:
        if host_param:                             # a LIVE SSH session into a registered host
            from oceano import hosts
            h = hosts._resolve(host_param)
            err = None
            if not h:
                err = f"no host named {host_param!r}"
            elif not h.get("host_key"):
                err = f"host {h['name']!r} has no pinned key — open Hosts and Test & pin it first"
            elif not (hosts.is_armed(h["id"]) or h.get("policy") == "trusted"):
                err = (f"host {h['name']!r} is locked — an interactive shell can't be command-filtered, "
                       f"so Arm it in the Hosts panel (or set its policy to trusted) first")
            if err:
                await ws.send_bytes(f"\r\n\x1b[31m{err}\x1b[0m\r\n".encode())
            else:
                await terminal.serve_host(ws, h, hosts._armed_secret(h["id"]))
        else:
            await terminal.serve(ws)               # the local workspace shell
    except Exception:
        traceback.print_exc()
    try:
        await ws.close()
    except Exception:
        pass


def _searxng_reachable():
    try:                                        # SearXNG (:8080) reachable?
        return requests.get(config.SEARXNG_URL, timeout=2).ok
    except requests.RequestException:
        return False


def _rerank_status():
    """Reranker (:8084) — OPTIONAL. {enabled: model present, ok: server reachable}. When no model is
    installed, reranking is off and RAG stays dense (enabled=False)."""
    if not config.RERANK_MODEL.exists():
        return {"enabled": False, "ok": False}
    try:
        requests.get(rerank.RERANK_URL.rstrip("/") + "/health", timeout=2)
        return {"enabled": True, "ok": True}
    except requests.RequestException:
        return {"enabled": True, "ok": False}


@router.get("/api/status")
def system_status():
    """Live state of the consolidated daemons, for the Settings → Services panel."""
    from oceano import voice
    beat = scheduler.last_beat()
    return {"embed": _embed_reachable(),
            "rerank": _rerank_status(),
            "scheduler_beat_ago": (time.time() - beat) if beat else None,
            "telegram": telegram_runtime.status(),
            "llamaswap": _llamaswap_status(),
            "searxng": _searxng_reachable(),
            "voice": voice.status()}


@router.post("/api/services/restart")
async def services_restart(request: Request):
    """Restart an individual in-process service. External units (llama-swap :8081, SearXNG :8080) are
    NOT restartable from here — the daemon runs with NoNewPrivileges — so they return a manual hint."""
    name = ((await request.json()).get("service") or "").lower()
    if name == "embeddings":
        from oceano import engine
        ok = engine.restart_embed()
        return {"ok": ok, "msg": "embedding server restarting…" if ok
                else "embedding server isn't managed by the daemon here"}
    if name in ("rerank", "reranker"):
        from oceano import engine
        ok = engine.restart_rerank()
        return {"ok": ok, "msg": "reranker restarting…" if ok
                else "reranker isn't running here (no model installed, or unmanaged)"}
    if name == "telegram":
        await telegram_runtime.stop()
        st = await _apply_telegram()                      # re-reads saved settings, starts if enabled
        return {"ok": "error" not in st,
                "msg": "Telegram restarted" if st.get("running") else "Telegram stopped (not enabled)",
                "error": st.get("error")}
    if name in ("tts", "stt", "voice"):
        from oceano import voice
        voice.reload()
        return {"ok": True, "msg": "voice models reloaded — the next utterance loads them fresh"}
    if name in ("llamaswap", "llama-swap", "chat-models"):
        # plain systemctl (NOT sudo — sudo would trip NoNewPrivileges); the polkit rule from
        # scripts/install.sh authorizes the daemon's user to manage this one unit.
        import subprocess, sys

        def _restart_swap():
            try:
                r = subprocess.run(["systemctl", "restart", "--no-block", "oceano-llama-swap.service"],
                                   capture_output=True, text=True, timeout=20)
                print(f"[services] systemctl restart oceano-llama-swap -> rc={r.returncode} "
                      f"err={(r.stderr or '').strip()!r}", file=sys.stderr, flush=True)
                if r.returncode == 0:
                    return {"ok": True, "msg": "chat model server restarting…"}
                err = (r.stderr or r.stdout or "").strip().splitlines()
                err = err[-1] if err else "systemctl restart failed"
                if "authentication" in err.lower() or "authorized" in err.lower():
                    err = "not authorized — install the polkit rule (re-run scripts/install.sh) to enable this"
                return {"ok": False, "error": err}
            except FileNotFoundError:
                return {"ok": False, "error": "systemctl not available on this host"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "restart timed out"}
        return await asyncio.to_thread(_restart_swap)
    return {"ok": False, "error": "managed by systemd — can't restart from here. "
            "Run on the host:  sudo systemctl restart oceano-llama-swap"}


def _llamaswap_status():
    """llama-swap reachability + which model it currently has loaded. The model list
    comes from /v1/models; the live-loaded model from llama-swap's /running admin route
    (best-effort — tolerant of shape/version differences, never raises)."""
    base = config.LLM_BASE_URL.rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base   # admin routes live off /v1
    out = {"ok": False, "loaded": None, "models": []}
    try:
        r = requests.get(base + "/models", timeout=2)
        out["ok"] = r.ok
        out["models"] = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except (requests.RequestException, ValueError):
        return out
    try:                                            # llama-swap: GET /running -> currently-up upstream(s)
        rr = requests.get(root + "/running", timeout=2)
        if rr.ok:
            data = rr.json()
            running = data.get("running") if isinstance(data, dict) else data
            if isinstance(running, list) and running and isinstance(running[0], dict):
                out["loaded"] = running[0].get("model") or running[0].get("id")
    except (requests.RequestException, ValueError):
        pass
    return out


@router.get("/api/health")
def health_dashboard():
    """Aggregated live health of the whole self-hosted stack, for the Health window:
    uptime, the inference + embedding servers, scheduler heartbeat, Telegram, the
    knowledge stores, and GPU/VRAM. Each piece degrades independently."""
    beat = scheduler.last_beat()
    try:
        tasks = len(scheduler.all_tasks())
    except Exception:
        tasks = None
    try:
        docs = rag.stats()
    except Exception:
        docs = {}
    try:
        hw = rivers.hw()
    except Exception:
        hw = {}
    return {
        "uptime_s": time.time() - _BOOT_TS,
        "model": _effective_model(),
        "llamaswap": _llamaswap_status(),
        "embed": {"ok": _embed_reachable(), "model": embeddings.EMBED_MODEL, "url": embeddings.EMBED_URL},
        "scheduler": {"beat_ago_s": (time.time() - beat) if beat else None, "tasks": tasks},
        "telegram": telegram_runtime.status(),
        "memory": {"count": memory.count()},
        "rag": docs,
        "hw": hw,
    }


# ---------------- background jobs: live registry + serialization (queue) toggle ----------
@router.get("/api/jobs")
def jobs_snapshot():
    """What background work is in flight right now + the serialize setting (for the
    running indicators and the Settings toggle). Running spawn_job OS-processes (bgjobs)
    are folded in so they show in the same indicator as Oceano's in-process work."""
    from oceano import jobs, bgjobs, agentjobs
    s = jobs.snapshot()
    now = time.time()
    extra = [{"id": f"bg{j['id']}", "kind": "job", "label": j["label"], "ref": f"bgjob:{j['id']}",
              "state": "running", "elapsed": round(now - j["started"], 1)}
             for j in bgjobs.status() if j["state"] in ("running", "starting")]
    # running sub-agents too — except LOCAL ones, which already sit in the jobs registry via
    # the serialization gate (counting them here would double them in the indicator)
    extra += [{"id": f"ag{j['id']}", "kind": "agent", "label": j["label"], "ref": f"agent:{j['id']}",
               "state": "running", "elapsed": round(now - j["started"], 1)}
              for j in agentjobs.status()
              if j["state"] in ("running", "starting") and j.get("provider") != "local"]
    if extra:
        s["jobs"] = list(s.get("jobs", [])) + extra
        s["running"] = s.get("running", 0) + len(extra)
    return s


@router.post("/api/jobs/{jid}/cancel")
def jobs_cancel(jid: int):
    """Stop a running/queued job from the jobs popup — workflow, scheduled task, research, or a
    LOCAL spawn_agent (the kinds that live in jobs.py's own registry with a real numeric id; a
    spawn_job OS-process or a non-local spawn_agent, merged into /api/jobs under a synthetic
    "bg"/"ag" id, aren't covered yet). False if `jid` already finished or never existed."""
    from oceano import jobs
    return {"ok": jobs.cancel(jid)}


@router.get("/api/bgjobs")
def bgjobs_list(session: str = ""):
    """Background OS-jobs (spawn_job) AND sub-agents (spawn_agent). `pending` = terminal items
    for this conversation whose result hasn't been printed into the chat yet, each tagged with
    `kind` ("job" | "agent") so the client acks against the right registry; `jobs`/`agents` =
    everything tracked, for a detail view."""
    from oceano import bgjobs, agentjobs
    pending = []
    if session:
        pending = ([{**r, "kind": "job"} for r in bgjobs.pending_for(session)]
                   + [{**r, "kind": "agent"} for r in agentjobs.pending_for(session)])
    return {"jobs": bgjobs.status(), "agents": agentjobs.status(), "pending": pending}


@router.post("/api/bgjobs/{jid}/ack")
def bgjobs_ack(jid: int, kind: str = "job"):
    """Mark a job's/agent's result as delivered into its conversation (never printed twice)."""
    from oceano import bgjobs, agentjobs
    reg = agentjobs if kind == "agent" else bgjobs
    return {"ok": reg.mark_delivered(jid)}


@router.get("/api/logs")
def activity_logs(kind: str = "", limit: int = 200):
    """The durable activity log — finished unattended runs (scheduled tasks, workflows, research,
    evals, memory upkeep…) with status, duration, and the result the agent produced."""
    from oceano import logs
    return {"runs": logs.recent(min(max(int(limit), 1), 500), kind or None), "kinds": logs.kinds()}


@router.get("/api/logs/system")
async def system_logs(unit: str = "oceano", lines: int = 400):
    """Tail of a daemon's systemd journal (oceano / llama-swap) — runs journalctl off the event loop."""
    from oceano import logs
    return await asyncio.to_thread(logs.system_log, unit, lines)


@router.post("/api/jobs/serialize")
async def jobs_set_serialize(req: Request):
    """Turn the queue on/off. `enabled` → background jobs; `chat` → chat turns. Both run
    one-at-a-time through one shared gate instead of hitting the local model in parallel."""
    from oceano import jobs
    b = await req.json()
    if "enabled" in b:
        jobs.set_serialize(bool(b["enabled"]))
    if "chat" in b:
        jobs.set_serialize_chat(bool(b["chat"]))
    s = jobs.snapshot()
    return {"ok": True, "serialize": s["serialize"], "serialize_chat": s["serialize_chat"]}


# ---------------- agent tools (read-only list for Settings → Tools) ----------


@router.get("/api/tools")
def list_tools():
    """Each agent tool with its verifiable capability surface — the parameters it
    actually accepts (read straight from the registered JSON schema)."""
    from oceano import tools
    out = []
    for s in tools.all_schemas():                 # ALL tools (incl. disabled) so the toggles show
        fn = s["function"]
        params = fn.get("parameters", {}) or {}
        props = params.get("properties", {}) or {}
        required = set(params.get("required", []))
        name = fn["name"]
        cat = "mcp" if name.startswith("mcp__") else _TOOL_CATEGORY.get(name, "other")
        out.append({
            "name": name,
            "description": fn.get("description", ""),
            "category": cat,
            "enabled": tools.is_enabled(name),
            "params": [{"name": k, "type": v.get("type", "any"),
                        "required": k in required, "description": v.get("description", "")}
                       for k, v in props.items()],
        })
    return out


@router.post("/api/tools/toggle")
async def toggle_tool(req: Request):
    """Enable/disable a tool (or all of them) for the model. Disabled tools are dropped
    from the prompt, lowering context. body: {name, enabled} or {all: true|false}."""
    from oceano import tools
    b = await req.json()
    if "all" in b:
        tools.set_all(bool(b["all"]))
    elif b.get("name"):
        tools.set_enabled(b["name"], bool(b.get("enabled", True)))
    return {"ok": True, "enabled": len(tools.schemas()), "total": len(tools.all_schemas())}


@router.get("/api/tools/chat")
def chat_tools_state():
    """Which memory tools are offered in plain chat mode (Agent mode off)."""
    from oceano import tools
    return {"tools": tools.chat_tool_state()}


@router.post("/api/tools/chat")
async def chat_tools_set(req: Request):
    """Toggle a memory tool's availability in chat-only mode. body: {name, enabled}."""
    from oceano import tools
    b = await req.json()
    if b.get("name"):
        tools.set_chat_tool(b["name"], bool(b.get("enabled", True)))
    return {"ok": True, "tools": tools.chat_tool_state()}


@router.get("/api/tools/limits")
def tool_limits():
    """Current tool-call budgets (Settings → Tools): the interactive/background agent loop's
    turn cap, and Claude/Codex CLI delegation's own --max-turns. Each *_override is null when
    unset (the *_default applies); set one to explicitly raise/lower it."""
    from oceano import delegate, tools
    return {"max_steps_override": tools.get_max_steps_override() or None, "max_steps_default": config.MAX_STEPS,
            "max_delegate_turns_override": tools.get_max_delegate_turns() or None,
            "max_delegate_turns_default": delegate._DELEGATE_TURNS}


@router.post("/api/tools/limits")
async def set_tool_limits(req: Request):
    """body: {max_steps?, max_delegate_turns?} — either 0/null clears back to the built-in
    default. Values are clamped (1-500) inside tools.set_max_steps/set_max_delegate_turns."""
    from oceano import tools
    b = await req.json()
    if "max_steps" in b:
        tools.set_max_steps(b["max_steps"] or 0)
    if "max_delegate_turns" in b:
        tools.set_max_delegate_turns(b["max_delegate_turns"] or 0)
    return {"ok": True, "max_steps": tools.get_max_steps(),
            "max_delegate_turns": tools.get_max_delegate_turns() or None}


@router.get("/api/models")
def models():
    return list_models()


# ---------------- wipe (Settings → destructive, per-target) ----------------
@router.post("/api/wipe/{target}")
def wipe(target: str):
    if target == "chats":
        return {"ok": True, "removed": chats.wipe(), "what": "chats"}
    if target == "documents":
        n = 0
        for c in config.WORKSPACE.iterdir():
            if c.name == ".gitkeep":
                continue
            try:
                shutil.rmtree(c) if c.is_dir() else c.unlink()
                n += 1
            except OSError:
                pass
        return {"ok": True, "removed": n, "what": "workspace items"}
    if target == "skills":                          # the agent's self-learned (non-published) skills
        learnt = [s for s in skills.all_skills() if s.get("status") != "published"]
        for s in learnt:
            skills.delete_skill(s["dir"])
        return {"ok": True, "removed": len(learnt), "what": "learnt skills"}
    if target == "memory":
        return {"ok": True, "removed": memory.wipe(), "what": "memories"}
    if target == "knowledge":
        return {"ok": True, "removed": rag.wipe(), "what": "indexed chunks"}
    raise HTTPException(400, f"unknown wipe target: {target}")
