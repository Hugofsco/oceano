"""Content routes: skills, the model-eval harness, notes/kanban, the voice
console, workflows (named, schedulable recipes), and the researcher
(scheduled deep-dives → living docs)."""
import asyncio
import json
import os
import tempfile
import threading
import traceback

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from oceano import evals, policies, researcher, skills, suggestions, traces
from oceano.web.state import _sse

router = APIRouter()


# ---------------- skills ----------------
@router.get("/api/skills")
def get_skills():
    return skills.all_skills()


@router.post("/api/skills")
async def post_skill(req: Request):
    b = await req.json()
    slug = skills.save_skill(b["name"], b.get("description", ""), b.get("body", ""), b.get("dir"),
                             status=b.get("status", "published"), notes=b.get("notes", ""))
    return {"ok": True, "dir": slug}


@router.patch("/api/skills/{dir}")
async def patch_skill(dir: str, req: Request):
    """Move a skill through the lifecycle (publish / send back to learning)."""
    b = await req.json()
    return {"ok": skills.set_status(dir, b.get("status", ""), b.get("notes"))}


@router.delete("/api/skills/{dir}")
def remove_skill(dir: str):
    return {"ok": skills.delete_skill(dir)}


@router.post("/api/skills/evaluate")
def evaluate_skills_api():
    """Kick off review → staging → publish in the background (it shells out to
    Claude Code, which can take minutes)."""
    if skills.eval_state()["running"]:
        return {"ok": False, "running": True, "error": "an evaluation is already running"}
    threading.Thread(target=skills.evaluate_all, daemon=True).start()
    return {"ok": True, "running": True}


@router.get("/api/skills-eval")
def skills_eval_state():
    return skills.eval_state()


# ---------------- suggestions: the self-evolution queue ----------------
# Nightly reflection files proposals here (suggestions.add); until now they were only
# reachable by asking the agent in chat, so the queue accumulated unseen. Brain →
# Suggestions renders these.
@router.get("/api/suggestions")
def suggestions_list(status: str = "pending"):
    """Also reports the health of the queue's ONLY producer — the [ SELF ] reflection task —
    so the panel can warn when it's switched off (a disabled producer starves the queue
    silently; that must be loud, not indistinguishable from 'no ideas')."""
    from oceano import reflect, scheduler
    t = next((t for t in scheduler.all_tasks() if t.get("source") == reflect.SOURCE), None)
    return {"suggestions": suggestions.all_suggestions(status or "pending"),
            "pending": len(suggestions.all_suggestions("pending")),
            "reflection": {"exists": bool(t), "enabled": bool(t and t.get("enabled")),
                           "last_filed": suggestions.last_filed()}}


@router.post("/api/suggestions/{sid}/accept")
def suggestions_accept(sid: int):
    """Accept = ACT: auto-creates the artifact for the safe kinds (research topic /
    workflow draft / memory); skill & setting are marked for manual follow-up."""
    return suggestions.accept(sid)


@router.post("/api/suggestions/{sid}/dismiss")
def suggestions_dismiss(sid: int):
    return suggestions.dismiss(sid)


# ---------------- evals: model eval harness ----------------
@router.get("/api/evals/cases")
def evals_cases():
    return {"cases": evals.all_cases(), "categories": list(evals.CATEGORIES),
            "grader_types": list(evals.GRADER_TYPES)}


@router.post("/api/evals/cases")
async def evals_save_case(req: Request):
    b = await req.json()
    rid = evals.save_case(b.get("id"), b.get("name", ""), b.get("category", "qa"),
                          b.get("prompt", ""), b.get("rubric", ""), b.get("graders", []),
                          b.get("seed"), b.get("timeout"), b.get("weight", 1.0),
                          bool(b.get("enabled", True)))
    return {"ok": True, "id": rid}


@router.delete("/api/evals/cases/{cid}")
def evals_delete_case(cid: int):
    return {"ok": evals.delete_case(cid)}


@router.get("/api/evals/models")
def evals_models():
    """Available local models + which are selected as eval targets (drives Run-now
    AND the scheduled run), plus the locked schedule for context."""
    return evals.models_config()


@router.post("/api/evals/models")
async def evals_set_models(req: Request):
    b = await req.json()
    return {"ok": True, "selected": evals.set_selected_models(b.get("models") or [])}


@router.post("/api/evals/run")
async def evals_run(req: Request):
    if evals.state()["running"]:
        return {"ok": False, "running": True, "error": "an eval run is already in progress"}
    b = await req.json()
    evals.run_all_bg(b.get("models") or None)   # None → use the saved selection
    return {"ok": True, "running": True}


@router.post("/api/evals/cancel")
def evals_cancel():
    """Stop an in-progress run (after the current case). The ✕ Cancel button calls this."""
    return {"ok": evals.cancel()}


@router.get("/api/evals/state")
def evals_state():
    return evals.state()


@router.get("/api/evals/leaderboard")
def evals_leaderboard(run_id: int = None, category: str = None):
    return evals.leaderboard(run_id, category=category or None)


@router.get("/api/evals/runs")
def evals_runs():
    return {"runs": evals.runs()}


@router.delete("/api/evals/runs/{run_id}")
def evals_delete_run(run_id: int):
    if not evals.delete_run(run_id):
        return {"ok": False, "error": "that run is still executing — cancel it first"}
    return {"ok": True}


@router.post("/api/evals/runs/clear")
def evals_clear_runs():
    removed = evals.clear_runs()
    if removed is None:
        return {"ok": False, "error": "an eval run is in progress — cancel it or let it finish first"}
    return {"ok": True, "removed": removed}


@router.get("/api/evals/results")
def evals_results(run_id: int):
    return {"results": evals.results(run_id)}


# ---------------- notes / kanban board ----------------
@router.get("/api/notes")
def notes_get():
    from oceano import notes
    return notes.board()


@router.post("/api/notes")
async def notes_add(req: Request):
    from oceano import notes
    b = await req.json()
    card = notes.add(b.get("title", b.get("text", "")), b.get("body", ""), b.get("tags"), b.get("col"))
    if not card:
        raise HTTPException(400, "no column to add to")
    return {"ok": True, "card": card}


@router.post("/api/notes/columns")
async def notes_add_column(req: Request):
    from oceano import notes
    b = await req.json()
    board = notes.add_column(b.get("name", ""), b.get("after"))
    if not board:
        raise HTTPException(400, "blank/duplicate column name, or the board is already full")
    return {"ok": True, **board}


@router.patch("/api/notes/columns/{name}")
async def notes_rename_column(name: str, req: Request):
    from oceano import notes
    b = await req.json()
    if not notes.rename_column(name, b.get("name", "")):
        raise HTTPException(400, "no such column, or the new name is blank/taken")
    return {"ok": True, **notes.board()}


@router.post("/api/notes/columns/{name}/move")
async def notes_move_column(name: str, req: Request):
    from oceano import notes
    b = await req.json()
    if not notes.move_column(name, int(b.get("direction", 0) or 0)):
        raise HTTPException(400, "no such column, or already at that edge")
    return {"ok": True, **notes.board()}


@router.delete("/api/notes/columns/{name}")
def notes_delete_column(name: str, move_to: str = ""):
    from oceano import notes
    if not notes.remove_column(name, move_to or None):
        raise HTTPException(400, "no such column, it's the last one, or its cards need a move_to")
    return {"ok": True, **notes.board()}


@router.patch("/api/notes/{cid}")
async def notes_update(cid: int, req: Request):
    from oceano import notes
    b = await req.json()
    ok = notes.update(cid, b.get("title", b.get("text")), b.get("body"), b.get("tags"), b.get("col"))
    if not ok:
        raise HTTPException(404, "no such card")
    return {"ok": True}


@router.delete("/api/notes/{cid}")
def notes_delete(cid: int):
    from oceano import notes
    return {"ok": notes.remove(cid)}


# ---------------- notebook — longer-form Markdown notes ----------------
@router.get("/api/notebook")
def notebook_list(q: str = "", tag: str = ""):
    from oceano import notebook
    return {"notes": notebook.list_all(q, tag), "tags": notebook.all_tags()}


@router.post("/api/notebook")
async def notebook_create(req: Request):
    from oceano import notebook
    b = await req.json()
    return {"ok": True, "note": notebook.create(b.get("title", ""), b.get("body", ""), b.get("tags"))}


@router.patch("/api/notebook/{nid}")
async def notebook_update(nid: int, req: Request):
    from oceano import notebook
    b = await req.json()
    ok = notebook.update(nid, b.get("title"), b.get("body"), b.get("tags"), b.get("pinned"))
    if not ok:
        raise HTTPException(404, "no such note")
    return {"ok": True, "note": notebook.get(nid)}


@router.delete("/api/notebook/{nid}")
def notebook_delete(nid: int):
    from oceano import notebook
    return {"ok": notebook.remove(nid)}


# ---------------- voice console (web) — reuses the Telegram speech stack ----------------
@router.get("/api/voice/status")
def voice_status():
    from oceano import voice
    return voice.status()


@router.get("/api/voice/voices")
def voice_voices():
    """Available Kokoro voices, installed Piper voices, and current TTS settings — for the Voice tab."""
    from oceano import voice
    return {"voices": voice.list_voices(), "settings": voice.get_settings(),
            "piper_installed": voice.piper_installed()}


@router.post("/api/voice/settings")
async def voice_settings(req: Request):
    """Set the active TTS engine / voice / speed / Piper voice and the wake-word config (persisted;
    takes effect on the next utterance / the next time conversation mode is started)."""
    from oceano import voice
    b = await req.json()
    return {"ok": True, "settings": voice.set_settings(engine=b.get("engine"), voice=b.get("voice"),
                                                       speed=b.get("speed"), wake=b.get("wake"),
                                                       wake_word=b.get("wake_word"),
                                                       piper_voice=b.get("piper_voice"))}


@router.get("/api/voice/piper/languages")
def voice_piper_languages():
    """Languages available in the Piper voice catalog (+ what's already installed) — for Browse."""
    from oceano import voice
    return {"languages": voice.piper_languages(), "installed": voice.piper_installed()}


@router.get("/api/voice/piper/voices")
def voice_piper_voices(lang: str = ""):
    """Piper catalog voices, filtered to one language code (e.g. ?lang=en_US)."""
    from oceano import voice
    return {"voices": voice.piper_list(lang or None)}


@router.post("/api/voice/piper/download")
async def voice_piper_download(req: Request):
    """Download a Piper voice from the catalog into assets/voice/ (md5-verified). Runs in a worker
    thread so the (sync, ~tens-of-MB) download doesn't block the event loop."""
    from starlette.concurrency import run_in_threadpool
    from oceano import voice
    key = (await req.json()).get("key", "")
    if not key:
        return {"ok": False, "error": "no voice key"}
    return await run_in_threadpool(voice.piper_download, key)


@router.post("/api/voice/stt")
async def voice_stt(req: Request):
    """Transcribe an uploaded audio blob (the browser's MediaRecorder gives webm/opus;
    faster-whisper decodes it via ffmpeg). Body is the raw audio bytes."""
    from oceano import voice
    data = await req.body()
    if not data:
        return {"text": ""}
    fd, tmp = tempfile.mkstemp(suffix=".webm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        text = await asyncio.to_thread(voice.transcribe, tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return {"text": text}


@router.post("/api/voice/tts")
async def voice_tts(req: Request):
    """Render text to an OGG/Opus clip the browser can play. The temp file is unlinked
    after the response is sent (BackgroundTask)."""
    from oceano import voice
    text = ((await req.json()).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "no text")
    path = await asyncio.to_thread(voice.synthesize, text)
    if not path:
        raise HTTPException(503, "TTS unavailable on this machine")

    def _cleanup():
        try:
            os.remove(path)
        except OSError:
            pass

    return FileResponse(path, media_type="audio/ogg", background=BackgroundTask(_cleanup))


# ---------------- workflows (named, schedulable multi-step recipes) ----------------
@router.get("/api/workflows")
def workflows_list():
    from oceano import workflows
    return [{**w, "schedule": workflows.schedule_info(w["id"])} for w in workflows.list_all()]


@router.get("/api/workflows/live")
def workflows_live():
    """In-progress (and just-finished) runs — lets the UI reconnect to a running workflow's
    live state after a browser refresh, and mark which workflows are running."""
    from oceano import workflows
    return {"running": workflows.live()}


@router.get("/api/workflows/checkpoints")
def workflows_checkpoints():
    """Workflow id -> {ts, status} for every resumable checkpoint (from a paused ⏸ or failed run)
    — the list view uses this to show a ▶ Resume button AND a status badge (paused vs failed) per
    card, without a /resume GET for every workflow."""
    from oceano import workflows
    info = workflows.resumable_info()
    return {"ids": list(info.keys()), "info": {str(k): v for k, v in info.items()}}


# NB: registered BEFORE the /{wid}/… routes — "secrets" must never be parsed as a wid
@router.get("/api/workflows/secrets")
def wf_secrets_list():
    """Names only. Values are write-only: they never come back out through the API."""
    from oceano import workflows
    return {"secrets": workflows.list_secrets()}


@router.put("/api/workflows/secrets/{name}")
async def wf_secrets_set(name: str, req: Request):
    from oceano import workflows
    b = await req.json()
    if not workflows.set_secret(name, str(b.get("value") or "")):
        raise HTTPException(400, "a value is required, and names are letters/digits/._- "
                                 "starting with a letter (max 64)")
    return {"ok": True, "secrets": workflows.list_secrets()}


@router.delete("/api/workflows/secrets/{name}")
def wf_secrets_delete(name: str):
    from oceano import workflows
    if not workflows.delete_secret(name):
        raise HTTPException(404, "no such secret")
    return {"ok": True, "secrets": workflows.list_secrets()}


@router.get("/api/workflows/{wid}/triggers")
def workflows_triggers_get(wid: int):
    from oceano import workflows
    return {"triggers": workflows.get_triggers(wid)}


@router.put("/api/workflows/{wid}/triggers")
async def workflows_triggers_set(wid: int, req: Request):
    from oceano import workflows
    b = await req.json()
    return {"ok": True, "triggers": workflows.set_triggers(wid, b.get("triggers", []))}


@router.post("/api/workflows/{wid}/webhook/{token}")
async def workflows_webhook(wid: int, token: str, req: Request, wait: int = 0):
    """Fire a workflow from an external POST. Auth-exempt — the secret token IS the auth.
    The server is localhost-bound by default; only reachable remotely if you tunnel it.
    An optional input value (the workflow's argument) is read from the body: JSON {"input": …}
    or the raw request body as text. `?wait=1` runs it synchronously and returns the final
    output (202 with the run still going if it outlasts the 120s budget) — a workflow as an API."""
    from oceano import workflows
    inp = ""
    try:
        raw = await req.body()
        if raw:
            try:
                inp = str((json.loads(raw) or {}).get("input", "") or "")
            except (json.JSONDecodeError, AttributeError, TypeError):
                inp = raw.decode("utf-8", "replace")[:4000]
    except Exception:
        inp = ""
    if wait:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, lambda: workflows.webhook_run_sync(wid, token, inp=inp))
        try:
            rec = await asyncio.wait_for(asyncio.shield(fut), timeout=120)
        except asyncio.TimeoutError:                   # keep running detached; it's still recorded
            return JSONResponse({"ok": True, "started": True,
                                 "note": "still running — result not ready within 120s"}, status_code=202)
        if rec is None:
            raise HTTPException(404, "no matching/enabled webhook trigger")
        return {"ok": rec.get("status") == "ok", "status": rec.get("status"),
                "summary": rec.get("summary", ""), "output": rec.get("output", "")}
    wf = workflows.webhook_run(wid, token, inp=inp)
    if not wf:
        raise HTTPException(404, "no matching/enabled webhook trigger")
    return JSONResponse({"ok": True, "started": wf["name"]}, status_code=202)


@router.get("/api/workflows/{wid}/export")
def workflows_export(wid: int):
    """The workflow as a portable JSON document (webhook secrets stripped — see export_wf)."""
    from oceano import workflows
    data = workflows.export_wf(wid)
    if not data:
        raise HTTPException(404, "no such workflow")
    return data


@router.post("/api/workflows/import")
async def workflows_import(req: Request, replace: int = 0):
    """?replace=1: a name collision updates the existing workflow in place (same id, run
    history kept) instead of creating a de-duped copy."""
    from oceano import workflows
    wf = workflows.import_wf(await req.json(), replace=bool(replace))
    if not wf:
        raise HTTPException(400, "not a workflow export (a graph is required)")
    return {"ok": True, "workflow": wf}


@router.post("/api/workflows/{wid}/duplicate")
def workflows_duplicate(wid: int):
    from oceano import workflows
    wf = workflows.duplicate(wid)
    if not wf:
        raise HTTPException(404, "no such workflow")
    return {"ok": True, "workflow": wf}


@router.post("/api/workflows")
async def workflows_create(req: Request):
    from oceano import workflows
    b = await req.json()
    return {"ok": True, "workflow": workflows.create(b.get("name", "Untitled"),
                                                      b.get("description", ""), b.get("graph"),
                                                      input_cfg=b.get("input"),
                                                      overlap=b.get("overlap"))}


@router.patch("/api/workflows/{wid}")
async def workflows_update(wid: int, req: Request):
    from oceano import workflows
    b = await req.json()
    wf = workflows.update(wid, name=b.get("name"), description=b.get("description"),
                          graph=b.get("graph"), input_cfg=b.get("input"),
                          overlap=b.get("overlap"))
    return {"ok": wf is not None, "workflow": wf}


@router.delete("/api/workflows/{wid}")
def workflows_delete(wid: int):
    from oceano import workflows
    return {"ok": workflows.remove(wid)}


@router.post("/api/workflows/{wid}/schedule")
async def workflows_schedule(wid: int, req: Request):
    from oceano import workflows
    workflows.set_schedule(wid, ((await req.json()).get("cron") or "").strip())
    return {"ok": True, "schedule": workflows.schedule_info(wid)}


@router.get("/api/workflows/{wid}/runs")
def workflows_runs(wid: int):
    from oceano import workflows
    return workflows.runs(wid)


@router.post("/api/workflows/{wid}/pause")
def workflows_pause(wid: int):
    """Ask a running workflow to stop after its current node finishes. Reuses the same signal
    as a jobs-popup ✕ cancel — the run already keeps its checkpoint on a 'cancelled' status
    (only ok/empty/skipped clear it), so this is a cancel that stays resumable via /resume.
    False if the workflow isn't actually running right now."""
    from oceano import jobs
    return {"ok": jobs.cancel_by_ref(f"workflow:{wid}")}


@router.get("/api/workflows/{wid}/resume")
def workflows_resume_state(wid: int):
    from oceano import workflows
    return {"checkpoint": workflows.resume_state(wid)}


@router.post("/api/workflows/{wid}/resume")
async def workflows_resume_run(wid: int):
    from oceano import workflows
    rec = await asyncio.to_thread(workflows.resume, wid)
    if rec is None:
        raise HTTPException(404, "no resumable checkpoint for that workflow")
    return {"ok": rec.get("status") == "ok", "run": rec}


@router.get("/api/workflows/{wid}/traces")
def workflows_traces(wid: int, run_id: str = ""):
    return {"events": traces.query(run_id=run_id or None, workflow_id=wid)}


@router.get("/api/policies")
def runtime_policies_get():
    return {"policies": policies.get(), "capabilities": list(policies.CAPABILITIES), "modes": list(policies.MODES)}


@router.post("/api/policies")
async def runtime_policies_set(req: Request):
    b = await req.json()
    if not policies.set_all((b or {}).get("policies") or {}):
        raise HTTPException(500, "could not save policies")
    return {"ok": True, "policies": policies.get()}


@router.get("/api/workflows/approvals")
def workflows_approvals():
    """Approval-node pauses waiting on a human decision (issue 8 D)."""
    from oceano import workflows
    return {"pending": workflows.pending_approvals()}


@router.post("/api/workflows/approve")
async def workflows_approve(req: Request):
    """Resolve an approval-node pause: {token, approved: bool} → the run continues down the
    approved/rejected branch."""
    from oceano import workflows
    b = await req.json()
    ok = workflows.resolve_approval(b.get("token", ""), bool(b.get("approved")))
    return {"ok": ok}


@router.post("/api/workflows/{wid}/run")
async def workflows_run(wid: int, req: Request):
    """Run a workflow now, streaming step-by-step progress as SSE. The engine runs in a
    worker thread (it blocks on the local model + tools); events feed through a queue so
    the response can keep-alive during quiet steps — same shape as /api/chat.
    An optional JSON body {"input": …} supplies the workflow's argument."""
    from oceano import workflows
    wf = workflows.get(wid)
    if not wf:
        raise HTTPException(404, "no such workflow")
    inp = ""
    try:
        b = await req.json()
        inp = str((b or {}).get("input", "") or "")
    except Exception:
        inp = ""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    put = lambda ev: loop.call_soon_threadsafe(q.put_nowait, ev)

    def worker():
        try:
            workflows.run(wf, trigger="manual", on_step=put, inp=inp)
        except Exception as ex:
            traceback.print_exc()
            put({"event": "error", "message": f"{type(ex).__name__}: {ex}"})
        finally:
            put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=10)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if ev is None:
                break
            yield _sse(ev)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ---------------- researcher (scheduled deep-dives → living docs) -------------
@router.get("/api/research")
def get_research():
    return researcher.all_topics()


@router.post("/api/research")
async def add_research(req: Request):
    b = await req.json()
    rid = researcher.add_topic(b.get("topic", ""), b.get("focus", ""), b.get("cron", "0 8 * * *"),
                               b.get("model", ""), b.get("base_url", ""))
    return {"ok": rid is not None, "id": rid,
            **({} if rid is not None else {"error": "topic and a valid cron are required"})}


@router.patch("/api/research/{rid}")
async def update_research(rid: int, req: Request):
    b = await req.json()
    ok = researcher.update_topic(rid, b.get("topic"), b.get("focus"), b.get("cron"), b.get("enabled"),
                                 b.get("model"), b.get("base_url"))
    return {"ok": ok}


@router.delete("/api/research/{rid}")
def delete_research(rid: int):
    return {"ok": researcher.delete_topic(rid)}


@router.post("/api/research/{rid}/run")
def run_research_now(rid: int):
    researcher.run_topic_bg(rid)        # long-running — fire in the background
    return {"ok": True, "started": True}
