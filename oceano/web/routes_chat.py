"""Chat routes: the SSE streaming turn (with reconnect buffering), stop/live,
the composer slash-commands (/context /compact /status), server-side chat
persistence, and the file/image drop uploads that feed a turn."""
import asyncio
import threading
import time
import traceback

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

import config
from oceano import chats, rag, skills
from oceano.web.state import (
    _CHAT_LIVE_KEEP,
    _agent,
    _cancels,
    _chat_live,
    _compactions,
    _ctx_cap,
    _drop_session_state,
    _last_ctx,
    _session_lock,
    _sse,
    _wresolve,
    load,
)

router = APIRouter()


@router.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    sid = body.get("session", "default")
    message = body.get("message", "")
    try:
        from oceano import workflows
        workflows.fire_keyword(message, "web")        # keyword-trigger workflows (runs in background)
    except Exception:
        pass
    base_url = body.get("base_url")
    data = load()
    api_key = next((e.get("api_key", "") for e in data["endpoints"]
                    if e["base_url"] == base_url), "")

    ag = _agent(sid)
    # Capture the request's model/endpoint; APPLY them to the shared agent INSIDE the session lock
    # (below), so a second turn for the same session can't swap the model out from under this one.
    req_model = body.get("model") or ag.model
    req_base_url, req_api_key = base_url, api_key
    agent_mode = bool(body.get("agent_mode"))
    voice = bool(body.get("voice"))                  # hands-free converse → ask for a short, spoken-friendly reply
    attachments = body.get("attachments") or []      # [{path, name, kind}] from /api/upload
    # so it's verifiable in the journal which mode a message actually ran in (tools
    # are only attached in agent mode) — settles "the toggle was on but it didn't use tools".
    print(f"[chat] model={req_model!r} agent_mode={agent_mode}", flush=True)

    # The agent is blocking (a single LLM step or a slow tool can take 20s+ with no
    # output). Run it in a worker thread and feed events through a queue, so the
    # response generator can emit a keep-alive during any silent gap — otherwise an
    # idle proxy / VS Code port-forward / Tailscale hop drops the stream.
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    now = time.time()
    for _s in [k for k, v in _chat_live.items() if not v.get("running") and now - v.get("ts", now) > _CHAT_LIVE_KEEP]:
        _chat_live.pop(_s, None)                          # prune old finished turns
    _chat_live[sid] = {"running": True, "message": message, "events": [], "ts": now}

    def put(ev):
        if ev is not None:                                # buffer every event so a refresh can reconnect
            b = _chat_live.get(sid)
            if b is not None:
                evs = b["events"]
                t = ev.get("type") if isinstance(ev, dict) else None
                # Coalesce consecutive streamed deltas into ONE buffered event (a fresh dict — never
                # mutate the object already handed to the live queue), so a long answer is a handful
                # of events on reconnect, not thousands. The live consumer still gets every delta.
                if t in ("token", "reasoning") and evs and isinstance(evs[-1], dict) and evs[-1].get("type") == t:
                    evs[-1] = {"type": t, "text": evs[-1].get("text", "") + ev.get("text", "")}
                else:
                    evs.append(ev)
                if t == "tool_progress":
                    # progress events are ephemeral live updates; keep only the most recent so a
                    # long delegation (hundreds of them) can't bloat the reconnect buffer.
                    prog = [e for e in evs if isinstance(e, dict) and e.get("type") == "tool_progress"]
                    if len(prog) > 60:
                        evs.remove(prog[0])
                if isinstance(ev, dict) and ev.get("type") in ("done", "error"):
                    b["running"] = False
        loop.call_soon_threadsafe(q.put_nowait, ev)

    cancel = threading.Event()      # set ONLY by /api/chat/stop (a disconnect no longer cancels)
    _cancels[sid] = cancel

    def worker():
        stream = None
        try:
            # When the user adds chat to the queue (Settings → Execution), this turn waits on
            # the same global gate the background jobs use — so it won't hit the model in
            # parallel with running work. gate=False (default) → chat stays fully responsive.
            from oceano import jobs
            chat_gate = jobs.serialize_chat_enabled()
            if chat_gate and jobs.snapshot()["running"] > 0:
                put({"type": "notice", "text": "⏳ Queued — waiting for current work to finish (chat queue is on)."})
            with jobs.job("chat", (message or "chat")[:60], gate=chat_gate):
                # One turn at a time per session: another tab's turn or a /compact must not
                # mutate ag.messages while this stream is appending to it.
                with _session_lock(sid):
                    ag.model, ag.base_url, ag.api_key = req_model, req_base_url, req_api_key  # apply under the lock
                    cap = _ctx_cap.get(sid)              # /context <n> → auto-compact before the turn
                    if cap and len(ag.messages) > cap:
                        dropped = ag.compact()
                        if dropped:
                            _compactions[sid] = _compactions.get(sid, 0) + 1
                            put({"type": "notice", "text": f"🗜 Auto-compacted {dropped} messages "
                                                            f"(context passed {cap})."})
                    # dropped files become context for the (text-only) local model: text is
                    # extracted inline; images are described by the configured vision target.
                    turn_msg = message
                    if attachments:
                        ctx = _attachment_context(attachments, message, put)
                        if ctx:
                            turn_msg = ctx + message
                    # chat mode still gets the user-chosen memory tools (Settings → Tools) so it can
                    # manage what it knows about you without full agent mode; agent mode → all tools.
                    from oceano import tools as _tools
                    from oceano import mindbridge
                    stream = ag.run_stream(turn_msg, cancel=cancel, voice=voice) if agent_mode else ag.run_stream(turn_msg, only_tools=_tools.chat_tools(), cancel=cancel, voice=voice)
                    # Tag this turn with its conversation for the duration of the run, so a spawn_job
                    # call (local model in-thread, or the Claude/Codex mind via the bridge on another
                    # thread) can route the job's eventual result back to THIS chat.
                    with mindbridge.session(sid):
                        for ev in stream:
                            if isinstance(ev, dict) and ev.get("type") == "stats" and ev.get("ctx"):
                                _last_ctx[sid] = ev["ctx"]   # remember real prompt tokens for /status
                            if cancel.is_set():
                                break           # stop feeding — query was aborted
                            put(ev)
                if not cancel.is_set():
                    put({"type": "done"})
        except Exception as ex:
            traceback.print_exc()   # so it actually lands in the journal, not just the UI
            put({"type": "error", "message": f"{type(ex).__name__}: {ex}"})
        finally:
            # closing the generator unwinds its try/finally → closes the upstream
            # LLM HTTP stream, so the local model stops generating too.
            if cancel.is_set() and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
            b = _chat_live.get(sid)
            if b is not None:
                b["running"] = False                      # turn is over — reconnection stops polling
            put(None)  # sentinel: stream finished

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # SSE comment — ignored by the client parser
                    continue
                if ev is None:
                    break
                yield _sse(ev)
        except (asyncio.CancelledError, GeneratorExit):
            raise                       # client went away — let the turn finish server-side (reconnectable)
        except Exception:
            # never let the response generator die silently — log it and try to
            # send a clean error frame so the client shows a real message.
            traceback.print_exc()
            try:
                yield _sse({"type": "error", "message": "stream closed unexpectedly (see server logs)"})
            except Exception:
                pass
        finally:
            _cancels.pop(sid, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@router.post("/api/chat/stop")
async def chat_stop(request: Request):
    """Abort the in-flight query for a session — the Stop button calls this."""
    sid = (await request.json()).get("session", "default")
    ev = _cancels.get(sid)
    if ev:
        ev.set()
    return {"ok": bool(ev)}


@router.get("/api/chat/live/{sid}")
def chat_live(sid: str, since: int = 0):
    """The in-flight (or just-finished) turn for a session, so a reloaded page can reconnect to it
    and replay what it missed. `since` = how many events the client already has."""
    b = _chat_live.get(sid)
    if not b:
        return {"running": False, "message": "", "events": [], "total": 0}
    evs = b["events"]
    return {"running": b["running"], "message": b["message"], "events": evs[since:], "total": len(evs)}


@router.delete("/api/session/{sid}")
def end_session(sid: str):
    _drop_session_state(sid)
    return {"ok": True}


@router.post("/api/chat/{sid}/truncate")
async def chat_truncate(sid: str, req: Request):
    """Edit-and-regenerate support: drop the persisted chat to its first `keep` messages, then rebuild
    the in-memory Agent from that truncated history — so the follow-up turn re-runs from the edit point
    with a correct context (no stale reply, and no double-added message on the rehydrate path)."""
    from oceano import chats
    if _chat_live.get(sid, {}).get("running"):
        return {"ok": False, "error": "a reply is still streaming — stop it first"}
    keep = max(0, int((await req.json()).get("keep", 0)))
    rec = chats.get(sid)
    if not rec:
        return {"ok": False, "error": "no such chat"}
    msgs = (rec.get("messages") or [])[:keep]
    chats.save(sid, rec.get("title"), msgs, rec.get("created"))
    _drop_session_state(sid)     # forget the live Agent…
    _agent(sid)                  # …and rebuild it from the truncated history NOW (while the file has no
                                 # trailing user msg) so the next /api/chat turn hits the warm, correct path
    return {"ok": True, "kept": len(msgs)}


# ---------------- chat composer slash-commands (mirror Telegram /context /compact /status) ----------------
def _ctx_payload(sid):
    ag = _agent(sid)
    n, approx = ag.context_metrics()
    return {"model": ag.model, "messages": n, "approx_tokens": approx,
            "ctx_tokens": _last_ctx.get(sid), "compactions": _compactions.get(sid, 0),
            "cap": _ctx_cap.get(sid)}


@router.get("/api/chat/context")
def chat_context(session: str = "default"):
    return _ctx_payload(session)


@router.post("/api/chat/context")
async def chat_set_context(req: Request):
    """Set/clear the auto-compact threshold for a session. value: <n> | off."""
    b = await req.json()
    sid = b.get("session", "default")
    raw = str(b.get("value", "")).strip().lower()
    if raw in ("", "off", "0", "none"):
        _ctx_cap.pop(sid, None)
        return {"ok": True, **_ctx_payload(sid)}
    try:
        _ctx_cap[sid] = max(4, int(raw))
    except ValueError:
        return {"ok": False, "error": "usage: /context <n> (messages before auto-compact) or /context off"}
    return {"ok": True, **_ctx_payload(sid)}


@router.post("/api/chat/compact")
async def chat_compact(req: Request):
    b = await req.json()
    sid = b.get("session", "default")
    ag = _agent(sid)
    if len(ag.messages) <= 2:
        return {"ok": False, "error": "nothing to compact yet — the context is already small",
                **_ctx_payload(sid)}
    lock = _session_lock(sid)
    if not lock.acquire(blocking=False):   # a turn is streaming — compacting now would corrupt it
        return {"ok": False, "error": "busy — wait for the current reply to finish (or Stop it) first",
                **_ctx_payload(sid)}
    try:
        # summarising is a blocking LLM call — keep it off the event loop
        dropped = await asyncio.to_thread(ag.compact)
    finally:
        lock.release()
    if dropped:
        _compactions[sid] = _compactions.get(sid, 0) + 1
    return {"ok": True, "dropped": dropped, **_ctx_payload(sid)}


@router.get("/api/chat/status")
def chat_status(session: str = "default"):
    from oceano import tools, memory, rag
    ag = _agent(session)
    try:
        docs = rag.stats().get("files", 0)
    except Exception:
        docs = 0
    try:
        facts = memory.count()
    except Exception:
        facts = 0
    tool_names = sorted(s["function"]["name"] for s in tools.schemas())
    return {**_ctx_payload(session), "tools": tool_names, "tool_count": len(tool_names),
            "memory": facts, "docs": docs}


# ---------------- chats (server-side, dated-folder persistence) ----------------
@router.get("/api/chats")
def chats_list():
    return {"chats": chats.list_all()}


@router.get("/api/chats/{cid}")
def chats_get(cid: str):
    c = chats.get(cid)
    return c or {"id": cid, "title": "New voyage", "messages": []}


@router.post("/api/chats/{cid}")
async def chats_save(cid: str, req: Request):
    b = await req.json()
    # creation date is assigned server-side (never trust the client for a path component);
    # existing chats keep their original date inside chats.save().
    ok = chats.save(cid, b.get("title", ""), b.get("messages", []))
    return {"ok": ok}


@router.delete("/api/chats/{cid}")
def chats_delete(cid: str):
    _drop_session_state(cid)        # also free the in-memory Agent
    return {"ok": chats.delete(cid)}


@router.post("/api/chats/{cid}/to-skill")
async def chat_to_skill(cid: str):
    """Distill this conversation into a reusable skill (delegated to Claude / the improve
    model; saved as a LEARNING skill that enters the independent-review pipeline)."""
    text = chats.transcript(cid)
    if not text.strip():
        return {"ok": False, "error": "no conversation yet — chat a bit first"}
    return await asyncio.to_thread(skills.from_conversation, text)


# ---------------- chat file/image drop ----------------
_UPLOAD_DIR = config.WORKSPACE / "uploads"
_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@router.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Save a dropped file into workspace/uploads and classify it (image / text / other)."""
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "file too large (25 MB max)")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    raw = (file.filename or "file").replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in raw)[:80] or "file"
    dest = _UPLOAD_DIR / safe
    base, suf, n = dest.stem, dest.suffix, 1
    while dest.exists():                                  # don't clobber an existing upload
        dest = _UPLOAD_DIR / f"{base}_{n}{suf}"
        n += 1
    dest.write_bytes(data)
    ext = dest.suffix.lower()
    kind = "image" if ext in _IMG_EXT else ("text" if (ext in rag.TEXT_EXT or ext == ".pdf") else "other")
    return {"ok": True, "name": dest.name, "path": str(dest.relative_to(config.WORKSPACE)), "kind": kind}


@router.post("/api/upload-to")
async def upload_to(dir: str = Form(""), paths: list[str] = Form([]),
                    files: list[UploadFile] = File(...)):
    """Upload many files — or a whole folder (the browser sends each file with its relative path) —
    straight into the workspace. `dir` is the target subfolder (the explorer's current dir); each
    `paths[i]` recreates the picked folder's structure. Everything is confined to the workspace."""
    saved, skipped = [], []
    for i, f in enumerate(files):
        rel = (paths[i] if i < len(paths) else "") or (f.filename or "file")
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p and p not in (".", "..")]
        if not parts:
            skipped.append(str(rel)); continue
        safe = "/".join("".join(c if (c.isalnum() or c in "._- ()") else "_" for c in p)[:120] for p in parts)
        try:
            dest = _wresolve((dir.rstrip("/") + "/" + safe) if dir else safe)
        except HTTPException:
            skipped.append(safe); continue
        data = await f.read()
        if len(data) > 100 * 1024 * 1024:                    # 100 MB per-file cap
            skipped.append(f"{safe} (>100 MB)"); continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        saved.append(str(dest.relative_to(config.WORKSPACE)))
    return {"ok": True, "saved": len(saved), "skipped": skipped, "files": saved[:50]}


def _attachment_context(attachments, question, put=None):
    """Turn dropped files into text context for the (text-only) local model: text files inline,
    images described by the configured vision target. Returns a prefix string ('' if nothing)."""
    from oceano import rag, delegate
    parts = []
    for att in attachments or []:
        try:
            p = _wresolve(att.get("path", ""))
        except Exception:
            continue
        if not p.is_file():
            continue
        name = att.get("name") or p.name
        if att.get("kind") == "image":
            if put:
                put({"type": "notice", "text": f"🖼 analyzing {name} with the vision model…"})
            r = delegate.describe_image(str(p), question, role="vision")
            desc = (r.get("output") or "").strip() if r.get("ok") else f"(couldn't analyze: {r.get('error')})"
            parts.append(f"[Attached image “{name}” — what the vision model sees:]\n{desc}")
        else:
            text = rag._read(p)
            if text.strip():
                parts.append(f"[Attached file “{name}”:]\n{text[:6000]}")
    return ("\n\n".join(parts) + "\n\n") if parts else ""
