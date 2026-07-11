"""Ops routes: the scheduler (cron tasks), hosts (the SSH keychain of
registered servers), and the calendar (ICS feeds + the editable local layer)."""
import asyncio

from fastapi import APIRouter, File, Request, UploadFile

from oceano import calsync, safety, scheduler

router = APIRouter()


# ---------------- scheduler ----------------
@router.get("/api/scheduler")
def get_scheduler():
    import time as _t
    lb = scheduler.last_beat()
    return {"beat_ago": (_t.time() - lb) if lb else None, "tasks": scheduler.all_tasks()}


@router.get("/api/cron/preview")
def cron_preview_api(cron: str = "", n: int = 5):
    """Validate a cron expression and list its next few fire times — the task editor's
    live schedule preview."""
    return scheduler.cron_preview(cron, n)


@router.post("/api/tasks")
async def add_task_api(req: Request):
    b = await req.json()
    tid = scheduler.add_task(b["cron"], b["instruction"], model=b.get("model"), base_url=b.get("base_url"))
    return {"ok": tid is not None, "id": tid}


@router.patch("/api/tasks/{tid}")
async def update_task_api(tid: int, req: Request):
    b = await req.json()
    ok = scheduler.update_task(tid, b.get("cron"), b.get("instruction"), b.get("enabled"),
                               model=b.get("model"), base_url=b.get("base_url"))
    return {"ok": ok, **({} if ok else {"error": "invalid cron expression (format: min hr day mon wkday)"})}


@router.delete("/api/tasks/{tid}")
def delete_task_api(tid: int):
    ok = scheduler.delete_task(tid)
    return {"ok": ok, **({} if ok else {"error": "this task is delete-protected — the nightly "
            "[ SELF ] reflection is what fills Brain → Suggestions. Switch it OFF instead."})}


@router.post("/api/tasks/{tid}/run")
async def run_task_api(tid: int):
    """Run a scheduled task right now, on demand. Off the event loop — a task can block
    (it may call the model, delegate, or run a workflow)."""
    return await asyncio.to_thread(scheduler.run_task, tid)


# ---------------- hosts (SSH keychain — registered servers the agent can ssh_run on) ----------------
@router.get("/api/hosts")
def hosts_list():
    from oceano import hosts
    return hosts.list_all()


@router.post("/api/hosts")
async def hosts_create(req: Request):
    from oceano import hosts
    b = await req.json()
    h = hosts.create(b.get("name", ""), b.get("host", ""), b.get("user", ""),
                     port=b.get("port", 22), auth=b.get("auth"),
                     policy=b.get("policy", "armed"), description=b.get("description", ""))
    return {"ok": h is not None, "host": h,
            **({} if h else {"error": "name, host and user are required (and the name must be unique)"})}


@router.patch("/api/hosts/{hid}")
async def hosts_update(hid: int, req: Request):
    from oceano import hosts
    b = await req.json()
    h = hosts.update(hid, **{k: b.get(k) for k in ("name", "host", "user", "port", "policy", "description", "auth")})
    return {"ok": h is not None, "host": h}


@router.delete("/api/hosts/{hid}")
def hosts_delete(hid: int):
    from oceano import hosts
    return {"ok": hosts.remove(hid)}


@router.post("/api/hosts/{hid}/key")
async def hosts_key(hid: int, file: UploadFile = File(...)):
    """Custody a private key for this host (written 0600 under data/hosts/)."""
    from oceano import hosts
    pem = (await file.read()).decode("utf-8", "replace")
    if "PRIVATE KEY" not in pem:
        return {"ok": False, "error": "that file doesn't look like an SSH private key (no PRIVATE KEY header)"}
    return {"ok": hosts.set_key(hid, pem), "host": hosts.get(hid)}


@router.post("/api/hosts/{hid}/test")
async def hosts_test(hid: int, req: Request):
    """Connect once and pin the server's host key (TOFU). Off the event loop — it blocks on the network."""
    from oceano import hosts
    secret = ""
    try:
        secret = (await req.json()).get("secret", "")
    except Exception:
        pass
    return await asyncio.to_thread(hosts.test_and_pin, hid, secret)


@router.post("/api/hosts/{hid}/arm")
async def hosts_arm(hid: int, req: Request):
    from oceano import hosts
    secret = ""
    try:
        secret = (await req.json()).get("secret", "")
    except Exception:
        pass
    ok = hosts.arm(hid, secret or None)
    return {"ok": ok, "host": hosts.get(hid), "expires": hosts.arm_expiry(hid)}


@router.post("/api/hosts/{hid}/disarm")
def hosts_disarm(hid: int):
    from oceano import hosts
    hosts.disarm(hid)
    return {"ok": True, "host": hosts.get(hid)}


# ---------------- calendar (local copy, synced from ICS feeds) ----------------
@router.get("/api/calendar")
def get_calendar(days: int = 30, start: str = "", end: str = ""):
    # start+end (YYYY-MM-DD) → the month/week/day grid asks for an explicit range; otherwise
    # fall back to "next N days from today" (the agenda).
    events = calsync.range_events(start, end) if (start and end) else calsync.upcoming(max(1, min(days, 365)))
    return {"feeds": calsync.feeds(), "events": events}


@router.post("/api/calendar/feeds")
async def add_calendar_feed(req: Request):
    b = await req.json()
    refusal = safety.check_url((b.get("url") or "").strip().replace("webcal://", "https://", 1))
    if refusal:
        return {"ok": False, "error": refusal}
    fid = calsync.add_feed(b.get("name", ""), b.get("url", ""))
    if fid is None:
        return {"ok": False, "error": "invalid URL — paste the calendar's secret .ics address"}
    result = await asyncio.to_thread(calsync.sync_feed, fid)   # first sync right away
    return {"ok": True, "id": fid, "sync": result}


@router.delete("/api/calendar/feeds/{fid}")
def delete_calendar_feed(fid: int):
    return {"ok": calsync.delete_feed(fid)}


@router.post("/api/calendar/sync")
async def sync_calendar():
    results = await asyncio.to_thread(calsync.sync_all)
    return {"ok": all(r.get("ok") for r in results.values()) if results else True, "results": results}


# ---- local events: the editable layer (synced feed events stay read-only) ----
@router.post("/api/calendar/events")
async def add_calendar_event_api(req: Request):
    b = await req.json()
    return calsync.add_event(b.get("title", ""), b.get("start", ""), end=b.get("end"),
                             all_day=bool(b.get("all_day")), location=b.get("location", ""),
                             description=b.get("description", ""), category=b.get("category", ""))


@router.put("/api/calendar/events/{eid}")
async def update_calendar_event_api(eid: int, req: Request):
    b = await req.json()
    # only override fields the client actually sent (so omitted ones aren't wiped); `end`
    # uses calsync's sentinel default so it's only touched when present in the body.
    kw = {k: b[k] for k in ("title", "start", "all_day", "location", "description", "category") if k in b}
    if "end" in b:
        kw["end"] = b["end"]
    return calsync.update_event(eid, **kw)


@router.delete("/api/calendar/events/{eid}")
def delete_calendar_event_api(eid: int):
    return calsync.delete_event(eid)
