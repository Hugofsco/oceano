"""Calendar tools: the local timeline + read-only synced feeds, single events and
batch / optimized scheduling."""
from oceano.tools.core import tool

# --- calendar (one local timeline you manage + read-only synced feeds) -------
# You can fully manage LOCAL events (create/edit/delete). Events synced from an external
# .ics feed are READ-ONLY (a sync would overwrite them) — schedule AROUND them, never try
# to change them. In calendar_events output, editable events are marked `[#id]`; that id is
# what you pass to update_calendar_event / delete_calendar_event.
@tool({
    "type": "function",
    "function": {
        "name": "calendar_events",
        "description": "Read the user's schedule: upcoming events for the next N days, across "
                       "their local Oceano calendar AND any synced external feeds. Use it "
                       "whenever they ask about their schedule/availability/plans, AND before "
                       "you add or move anything (to find free slots and the ids of existing "
                       "events). Editable local events show as `[#id]`; feed events are marked "
                       "read-only and must not be changed.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "how many days ahead to look (default 7)"},
        }},
    },
})
def calendar_events(days=7):
    from oceano import calsync
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        days = 7
    return calsync.agenda(days)


@tool({
    "type": "function",
    "function": {
        "name": "add_calendar_event",
        "description": "Add an event to the user's local calendar (appointments, activities, "
                       "reminders, blocks of time). Check calendar_events first so you don't "
                       "clash with an existing event. Times are the user's local time.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "what the event is, e.g. 'Dentist'"},
            "start": {"type": "string", "description": "start as 'YYYY-MM-DD HH:MM' (timed) or "
                                                       "'YYYY-MM-DD' (all-day)"},
            "end": {"type": "string", "description": "optional end as 'YYYY-MM-DD HH:MM'"},
            "all_day": {"type": "boolean", "description": "true for an all-day event"},
            "location": {"type": "string", "description": "optional place"},
            "description": {"type": "string", "description": "optional notes"},
        }, "required": ["title", "start"]},
    },
})
def add_calendar_event(title, start, end=None, all_day=False, location="", description=""):
    from oceano import calsync
    r = calsync.add_event(title, start, end=end, all_day=all_day,
                          location=location, description=description)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    e = r["event"]
    when = e["start"][:10] if e["all_day"] else f"{e['start'][:10]} {e['start'][11:16]}"
    return f"Added '{e['title']}' on {when} (id {e['id']})."


@tool({
    "type": "function",
    "function": {
        "name": "update_calendar_event",
        "description": "Edit an existing LOCAL calendar event (reschedule, rename, change "
                       "location, etc.). Pass the event's id (the `[#id]` from calendar_events) "
                       "and only the fields you want to change. Synced feed events are "
                       "read-only and cannot be edited.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the event id (from the `[#id]` marker)"},
            "title": {"type": "string"},
            "start": {"type": "string", "description": "new start 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'"},
            "end": {"type": "string", "description": "new end, or empty string to clear it"},
            "all_day": {"type": "boolean"},
            "location": {"type": "string"},
            "description": {"type": "string"},
        }, "required": ["id"]},
    },
})
def update_calendar_event(id, title=None, start=None, end=..., all_day=None,
                          location=None, description=None):
    from oceano import calsync
    r = calsync.update_event(id, title=title, start=start, end=end, all_day=all_day,
                             location=location, description=description)
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    e = r["event"]
    when = e["start"][:10] if e["all_day"] else f"{e['start'][:10]} {e['start'][11:16]}"
    return f"Updated '{e['title']}' → {when} (id {e['id']})."


@tool({
    "type": "function",
    "function": {
        "name": "delete_calendar_event",
        "description": "Delete a LOCAL calendar event by its id (the `[#id]` from "
                       "calendar_events). Synced feed events are read-only and cannot be "
                       "deleted. Confirm with the user before deleting if there's any doubt.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "the event id (from the `[#id]` marker)"},
        }, "required": ["id"]},
    },
})
def delete_calendar_event(id):
    from oceano import calsync
    r = calsync.delete_event(id)
    return "Event deleted." if r.get("ok") else f"ERROR: {r.get('error')}"


# --- batch / optimized scheduling (build a whole plan in one call) -----------
def _format_ops(r):
    """Render a calsync plan/manage result (create / move / delete) as a readable per-day
    summary for the model + user."""
    from datetime import datetime as D
    items, c, commit = r["items"], r["counts"], r["commit"]
    lines = ["✅ Calendar updated:" if commit else "📋 PLAN (preview — nothing saved yet):"]
    timed = [x for x in items if x.get("start") and x.get("action") in ("create", "move")]
    last = None
    for it in sorted(timed, key=lambda x: x["start"]):
        day = it["start"][:10]
        if day != last:
            try:
                lab = D.fromisoformat(day).strftime("%A %Y-%m-%d")
            except ValueError:
                lab = day
            lines.append(f"\n{lab}:"); last = day
        when = "all day" if it.get("all_day") else it["start"][11:16] + (f"–{it['end'][11:16]}" if it.get("end") else "")
        mark = {"placed": "✓", "conflict": "⚠", "skipped": "–", "error": "✗"}.get(it["status"], "•")
        verb = "moved → " if it.get("action") == "move" else ""
        extra = (f" (id {it['id']})" if it.get("id") else "") + (f"  — {it['note']}" if it.get("note") else "")
        lines.append(f"  {mark} {verb}{when}  {it.get('title', '')}{extra}")
    dels = [x for x in items if x.get("action") == "delete"]
    if dels:
        lines.append("\nRemoved:" if commit else "\nWill remove:")
        for it in dels:
            label = it.get("title") or f"id {it.get('id')}"
            note = f"  — {it['note']}" if it["status"] == "error" and it.get("note") else ""
            lines.append(f"  {'✗' if it['status'] == 'error' else '🗑'} {label}{note}")
    probs = [x for x in items if not x.get("start") and x.get("action") != "delete"]
    if probs:
        lines.append("\nCould not place:")
        for it in probs:
            lines.append(f"  ✗ {it.get('title') or '(untitled)'} — {it.get('note', '')}")
    tail = f"{c['applied']} applied · {c['conflict']} conflict · {c['unplaceable']} unplaceable"
    if c["error"]:
        tail += f" · {c['error']} error"
    if not commit:
        tail += ".\nTo apply, call the same tool again with the same input and commit=true."
    lines.append("\n" + tail)
    return "\n".join(lines).strip()


@tool({
    "type": "function",
    "function": {
        "name": "add_calendar_events",
        "description": "Schedule MANY calendar events in ONE call — use this for any multi-event PLAN "
                       "(a study schedule, a trip itinerary, a week of workouts) instead of calling "
                       "add_calendar_event over and over. Each event either has an exact `start`, OR a "
                       "`duration_minutes` (+ optional window) to be AUTO-PLACED into the first free slot "
                       "that doesn't clash with existing events. By DEFAULT this only PREVIEWS the plan "
                       "(commit=false) and writes nothing — show the preview to the user, and once they "
                       "confirm, call again with the SAME events and commit=true. If the user gave exact "
                       "times and clearly wants them booked now, you may pass commit=true directly. Times "
                       "are the user's local time; resolve relative dates ('next week') to concrete "
                       "YYYY-MM-DD using the current date first.",
        "parameters": {"type": "object", "properties": {
            "events": {
                "type": "array", "description": "the events to schedule",
                "items": {"type": "object", "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "exact start 'YYYY-MM-DD HH:MM' (or "
                              "'YYYY-MM-DD' for all-day). Omit to auto-place via duration_minutes."},
                    "end": {"type": "string", "description": "optional exact end 'YYYY-MM-DD HH:MM'"},
                    "duration_minutes": {"type": "integer", "description": "length in minutes; with no "
                                         "start the event is auto-placed into a free slot"},
                    "window_start": {"type": "string", "description": "auto-place search start "
                                     "'YYYY-MM-DD' (default today)"},
                    "window_end": {"type": "string", "description": "auto-place search end 'YYYY-MM-DD' "
                                   "(default +14 days)"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                }, "required": ["title"]},
            },
            "commit": {"type": "boolean", "description": "false (default) = PREVIEW only, nothing saved; "
                       "true = actually create the events"},
            "day_start": {"type": "string", "description": "working-hours start 'HH:MM' for auto-placement (default 09:00)"},
            "day_end": {"type": "string", "description": "working-hours end 'HH:MM' for auto-placement (default 18:00)"},
            "skip_conflicts": {"type": "boolean", "description": "on commit, skip events that clash with "
                               "an existing one instead of creating them"},
        }, "required": ["events"]},
    },
})
def add_calendar_events(events, commit=False, day_start="", day_end="", skip_conflicts=False):
    from oceano import calsync
    r = calsync.plan_events(events, commit=bool(commit), day_start=day_start or None,
                            day_end=day_end or None, skip_conflicts=bool(skip_conflicts))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return _format_ops(r)


@tool({
    "type": "function",
    "function": {
        "name": "find_free_slots",
        "description": "Find open time slots of a given length in the user's calendar, avoiding "
                       "existing events (local AND synced feeds) and the past. Use this before building "
                       "a plan to see real availability, then schedule with add_calendar_events.",
        "parameters": {"type": "object", "properties": {
            "window_start": {"type": "string", "description": "search from this date 'YYYY-MM-DD' (inclusive)"},
            "window_end": {"type": "string", "description": "search until this date 'YYYY-MM-DD' (inclusive)"},
            "duration_minutes": {"type": "integer", "description": "how long each slot must be"},
            "count": {"type": "integer", "description": "how many slots to return (default 5)"},
            "day_start": {"type": "string", "description": "working-hours start 'HH:MM' (default 09:00)"},
            "day_end": {"type": "string", "description": "working-hours end 'HH:MM' (default 18:00)"},
        }, "required": ["window_start", "window_end", "duration_minutes"]},
    },
})
def find_free_slots(window_start, window_end, duration_minutes, count=5, day_start="", day_end=""):
    from datetime import datetime as D
    from oceano import calsync
    try:
        count = max(1, min(int(count), 50))
    except (TypeError, ValueError):
        count = 5
    slots = calsync.find_free_slots(window_start, window_end, duration_minutes, count=count,
                                    day_start=day_start or None, day_end=day_end or None)
    if not slots:
        return f"(no free {duration_minutes}-min slots in {window_start}..{window_end} within working hours)"
    out = []
    for s in slots:
        try:
            lab = D.fromisoformat(s["start"]).strftime("%a %Y-%m-%d %H:%M")
        except ValueError:
            lab = s["start"]
        out.append(f"- {lab}–{s['end'][11:16]}")
    return "Free slots:\n" + "\n".join(out)


@tool({
    "type": "function",
    "function": {
        "name": "manage_calendar",
        "description": "Apply MULTIPLE calendar changes — create, move (reschedule), and delete — in "
                       "ONE atomic call. Use this to RESHUFFLE or replan a schedule (e.g. 'clear "
                       "Tuesday and spread those sessions across Wednesday', 'push everything an hour "
                       "later'). Each operation has an `action`: 'create' (same fields as "
                       "add_calendar_events — an exact `start`, or `duration_minutes` (+ optional "
                       "window) to auto-place into a free slot), 'move' (the event `id` + a new "
                       "`start`/`end` or `duration_minutes`; omit both to keep its current length), or "
                       "'delete' (the event `id`). Deletes free up slots that later creates can reuse. "
                       "By DEFAULT this only PREVIEWS (commit=false, nothing saved) — show the plan, "
                       "and once the user confirms, call again with the SAME operations and "
                       "commit=true. ALL changes apply together or not at all (no half-done reshuffles, "
                       "and concurrent edits can't interleave). Get event ids from calendar_events.",
        "parameters": {"type": "object", "properties": {
            "operations": {
                "type": "array", "description": "the changes to apply, in one batch",
                "items": {"type": "object", "properties": {
                    "action": {"type": "string", "enum": ["create", "move", "delete"]},
                    "id": {"type": "integer", "description": "event id (required for move/delete)"},
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "'YYYY-MM-DD HH:MM' (or 'YYYY-MM-DD' all-day)"},
                    "end": {"type": "string"},
                    "duration_minutes": {"type": "integer", "description": "length; on create with no "
                                         "start, auto-places into a free slot"},
                    "window_start": {"type": "string", "description": "auto-place search start 'YYYY-MM-DD'"},
                    "window_end": {"type": "string", "description": "auto-place search end 'YYYY-MM-DD'"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                }, "required": ["action"]},
            },
            "commit": {"type": "boolean", "description": "false (default) = PREVIEW only; true = apply all changes atomically"},
            "day_start": {"type": "string", "description": "working-hours start 'HH:MM' for auto-placement (default 09:00)"},
            "day_end": {"type": "string", "description": "working-hours end 'HH:MM' for auto-placement (default 18:00)"},
            "skip_conflicts": {"type": "boolean", "description": "on commit, skip create/move ops that clash instead of applying them"},
        }, "required": ["operations"]},
    },
})
def manage_calendar(operations, commit=False, day_start="", day_end="", skip_conflicts=False):
    from oceano import calsync
    r = calsync.manage(operations, commit=bool(commit), day_start=day_start or None,
                       day_end=day_end or None, skip_conflicts=bool(skip_conflicts))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return _format_ops(r)
