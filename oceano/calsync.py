"""Oceano's calendar — one local timeline the agent manages, plus read-only synced feeds.

Two kinds of event live in the same SQLite db:

  • LOCAL events (`local_events`) — created/edited/deleted by you and the agent. This is
    the real "assistant manages my schedule" surface: appointments, activities, anything.
  • FEED events (`events`) — mirrored one-way from external .ics URLs (Google Calendar's
    "Secret address in iCal format", any ICS). READ-ONLY: a sync replaces them wholesale,
    so the agent must not edit them — it works around them, scheduling in the free slots.

So you get one local place that tracks everything; sync just overlays an immovable layer.
Sync runs in the engine every SYNC_INTERVAL seconds (and on demand from the Calendar UI);
recurring feed events are expanded with dateutil's rrule, honouring EXDATE / RECURRENCE-ID.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import requests

import config
from oceano import safety

DB_PATH = config.WORKSPACE.parent / "data" / "calendar.db"
SYNC_INTERVAL = int(os.environ.get("OCEANO_CAL_SYNC", "900"))   # seconds between feed refreshes
WINDOW_PAST = timedelta(days=7)        # keep a little history
WINDOW_FUTURE = timedelta(days=400)    # expand recurrences this far ahead

_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=5000")    # wait (don't error) when another writer holds the db
    con.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer: web+telegram+scheduler+calendar
    con.execute("CREATE TABLE IF NOT EXISTS feeds ("
                "id INTEGER PRIMARY KEY, name TEXT, url TEXT, "
                "last_sync TEXT, last_error TEXT, event_count INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY, feed_id INTEGER, uid TEXT, title TEXT, "
                "location TEXT, description TEXT, start TEXT, end TEXT, all_day INTEGER)")
    # Agent/user-managed events — the editable layer. Kept separate from synced feed events
    # (which a sync replaces wholesale), so local events are never clobbered.
    con.execute("CREATE TABLE IF NOT EXISTS local_events ("
                "id INTEGER PRIMARY KEY, title TEXT NOT NULL, location TEXT, description TEXT, "
                "start TEXT NOT NULL, end TEXT, all_day INTEGER DEFAULT 0, category TEXT, "
                "created TEXT, updated TEXT)")
    return con


# ============================ ICS parsing ============================
def _unfold(text):
    """RFC 5545 line unfolding — a line starting with space/tab continues the previous."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _qsplit(s, sep):
    """Split on sep, but not inside double quotes (params may quote URLs with ':')."""
    out, buf, inq = [], "", False
    for ch in s:
        if ch == '"':
            inq = not inq
        if ch == sep and not inq:
            out.append(buf); buf = ""
        else:
            buf += ch
    out.append(buf)
    return out


def _prop(line):
    """'NAME;PARAM=V;P="a:b":value' -> (name, {param: v}, value), or None."""
    head = _qsplit(line, ":")
    if len(head) < 2:
        return None
    left, value = head[0], ":".join(head[1:])
    parts = _qsplit(left, ";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(s):
    return (s or "").replace("\\n", "\n").replace("\\N", "\n") \
                    .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_dt(value, params):
    """An ICS date/date-time -> (aware datetime in local tz, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d").replace(tzinfo=_LOCAL_TZ)
        return d, True
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    else:
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        tz = _LOCAL_TZ
        if params.get("TZID"):
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(params["TZID"])
            except Exception:
                tz = _LOCAL_TZ
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(_LOCAL_TZ), False


def _parse_ics(text):
    """All VEVENTs in an ICS body -> list of raw event dicts (recurrence unexpanded)."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"uid": "", "title": "", "location": "", "description": "",
                   "dtstart": None, "dtend": None, "all_day": False,
                   "rrule": "", "exdates": set(), "recurrence_id": None}
        elif line == "END:VEVENT":
            if cur and cur["dtstart"]:
                events.append(cur)
            cur = None
        elif cur is not None:
            p = _prop(line)
            if not p:
                continue
            name, params, value = p
            if name == "UID":
                cur["uid"] = value
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "DESCRIPTION":
                cur["description"] = _unescape(value)[:1000]
            elif name == "DTSTART":
                cur["dtstart"], cur["all_day"] = _parse_dt(value, params)
            elif name == "DTEND":
                cur["dtend"], _ = _parse_dt(value, params)
            elif name == "RRULE":
                cur["rrule"] = value
            elif name == "EXDATE":
                for v in value.split(","):
                    try:
                        cur["exdates"].add(_parse_dt(v, params)[0])
                    except ValueError:
                        pass
            elif name == "RECURRENCE-ID":
                try:
                    cur["recurrence_id"] = _parse_dt(value, params)[0]
                except ValueError:
                    pass
    return events


def _expand(events):
    """Expand recurrences into concrete (event, start, end) occurrences in-window.
    RECURRENCE-ID events override that single occurrence of their series."""
    now = datetime.now(_LOCAL_TZ)
    w_start, w_end = now - WINDOW_PAST, now + WINDOW_FUTURE
    overridden = {(e["uid"], e["recurrence_id"]) for e in events if e["recurrence_id"]}
    out = []
    for ev in events:
        dur = (ev["dtend"] - ev["dtstart"]) if ev["dtend"] else None
        if not ev["rrule"] or ev["recurrence_id"]:
            occs = [ev["dtstart"]]
        else:
            try:
                from dateutil.rrule import rrulestr
                rule = rrulestr(ev["rrule"], dtstart=ev["dtstart"])
                occs = list(rule.between(w_start, w_end, inc=True))
            except Exception:
                occs = [ev["dtstart"]]
        for o in occs:
            if o in ev["exdates"] or (not ev["recurrence_id"] and (ev["uid"], o) in overridden):
                continue
            end = (o + dur) if dur else None
            if o > w_end or (end or o) < w_start:
                continue
            out.append((ev, o, end))
    return out


# ============================ feeds ============================
def feeds():
    con = _db()
    rows = con.execute("SELECT id, name, url, last_sync, last_error, event_count "
                       "FROM feeds ORDER BY id").fetchall()
    con.close()
    return [{"id": r[0], "name": r[1], "url": r[2], "last_sync": r[3],
             "last_error": r[4], "event_count": r[5]} for r in rows]


def add_feed(name, url):
    url = (url or "").strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith(("http://", "https://")):
        return None
    if safety.check_url(url):                # SSRF guard — same policy as fetch_url/browser
        return None
    con = _db()
    cur = con.execute("INSERT INTO feeds (name, url) VALUES (?,?)",
                      ((name or "Calendar").strip(), url))
    con.commit()
    fid = cur.lastrowid
    con.close()
    return fid


def delete_feed(fid):
    con = _db()
    con.execute("DELETE FROM events WHERE feed_id=?", (fid,))
    con.execute("DELETE FROM feeds WHERE id=?", (fid,))
    con.commit()
    con.close()
    return True


# ============================ local events (the editable layer) ============================
def _store_dt(value):
    """Normalize a user/agent date or date-time to the stored form 'YYYY-MM-DDTHH:MM'
    (naive local time, matching how feed events are stored). Accepts 'YYYY-MM-DD',
    'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM[:SS]'. Raises ValueError on anything else."""
    s = (value or "").strip().replace(" ", "T", 1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00"
    return datetime.fromisoformat(s).strftime("%Y-%m-%dT%H:%M")


def _is_date_only(value):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", (value or "").strip()))


def get_local_event(eid):
    con = _db()
    r = con.execute("SELECT id, title, location, description, start, end, all_day, category "
                    "FROM local_events WHERE id=?", (eid,)).fetchone()
    con.close()
    if not r:
        return None
    return {"id": r[0], "title": r[1], "location": r[2], "description": r[3],
            "start": r[4], "end": r[5], "all_day": bool(r[6]),
            "calendar": r[7] or "Oceano", "source": "local", "editable": True}


def add_event(title, start, end=None, all_day=False, location="", description="", category=""):
    """Create a local (agent/user-owned) event. Returns {ok, id, event} or {ok:False, error}."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    try:
        start_iso = _store_dt(start)
    except (ValueError, TypeError):
        return {"ok": False, "error": f"bad start {start!r} — use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'"}
    if _is_date_only(start):                      # a bare date means an all-day event
        all_day = True
    end_iso = None
    if end:
        try:
            end_iso = _store_dt(end)
        except (ValueError, TypeError):
            return {"ok": False, "error": f"bad end {end!r} — use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'"}
        if not all_day and end_iso <= start_iso:
            return {"ok": False, "error": "end must be after start"}
    now = datetime.now().isoformat(timespec="seconds")
    con = _db()
    cur = con.execute(
        "INSERT INTO local_events (title, location, description, start, end, all_day, category, created, updated) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (title, (location or "").strip(), (description or "").strip(), start_iso, end_iso,
         1 if all_day else 0, (category or "").strip(), now, now))
    con.commit(); eid = cur.lastrowid; con.close()
    return {"ok": True, "id": eid, "event": get_local_event(eid)}


def update_event(eid, title=None, start=None, end=..., all_day=None,
                 location=None, description=None, category=None):
    """Edit a LOCAL event in place (only the fields you pass). Synced feed events are
    read-only and will be rejected. `end` defaults to a sentinel so passing end=None / ''
    explicitly CLEARS it. Returns {ok, event} or {ok:False, error}."""
    con = _db()
    if not con.execute("SELECT id FROM local_events WHERE id=?", (eid,)).fetchone():
        con.close()
        return {"ok": False, "error": "no editable event with that id (synced feed events are read-only)"}
    sets, vals = [], []
    if title is not None:
        t = str(title).strip()
        if not t:
            con.close(); return {"ok": False, "error": "title cannot be empty"}
        sets.append("title=?"); vals.append(t)
    if location is not None:
        sets.append("location=?"); vals.append(str(location).strip())
    if description is not None:
        sets.append("description=?"); vals.append(str(description).strip())
    if category is not None:
        sets.append("category=?"); vals.append(str(category).strip())
    if start is not None:
        try:
            sets.append("start=?"); vals.append(_store_dt(start))
        except (ValueError, TypeError):
            con.close(); return {"ok": False, "error": f"bad start {start!r}"}
        if _is_date_only(start) and all_day is None:
            all_day = True
    if end is not ...:                            # sentinel → caller didn't touch `end`
        if end in (None, "", False):
            sets.append("end=?"); vals.append(None)
        else:
            try:
                sets.append("end=?"); vals.append(_store_dt(end))
            except (ValueError, TypeError):
                con.close(); return {"ok": False, "error": f"bad end {end!r}"}
    if all_day is not None:
        sets.append("all_day=?"); vals.append(1 if all_day else 0)
    if not sets:
        con.close(); return {"ok": False, "error": "nothing to update"}
    sets.append("updated=?"); vals.append(datetime.now().isoformat(timespec="seconds"))
    vals.append(eid)
    con.execute(f"UPDATE local_events SET {', '.join(sets)} WHERE id=?", vals)
    con.commit(); con.close()
    return {"ok": True, "event": get_local_event(eid)}


def delete_event(eid):
    """Delete a LOCAL event. Synced feed events can't be deleted (read-only)."""
    con = _db()
    cur = con.execute("DELETE FROM local_events WHERE id=?", (eid,))
    con.commit(); n = cur.rowcount; con.close()
    return {"ok": n > 0, "error": None if n > 0
            else "no editable event with that id (synced feed events are read-only)"}


# ============================ sync ============================
def _fetch_ics(url, max_redirects=3):
    """GET a feed with the SSRF guard applied to the URL AND to every redirect hop
    (redirects are followed manually so each target is re-validated)."""
    for _ in range(max_redirects + 1):
        refusal = safety.check_url(url)
        if refusal:
            raise ValueError(refusal)
        r = requests.get(url, timeout=30, allow_redirects=False,
                         headers={"User-Agent": "Oceano-Calendar/1.0"})
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = requests.compat.urljoin(url, loc)
            continue
        r.raise_for_status()
        return r.text
    raise ValueError("too many redirects")


def sync_feed(fid):
    """Fetch + reparse one feed, replacing its local events. Returns a status dict."""
    con = _db()
    row = con.execute("SELECT url FROM feeds WHERE id=?", (fid,)).fetchone()
    if not row:
        con.close()
        return {"ok": False, "error": "no such feed"}
    now = datetime.now(timezone.utc).isoformat()
    try:
        occurrences = _expand(_parse_ics(_fetch_ics(row[0])))
    except Exception as e:
        con.execute("UPDATE feeds SET last_sync=?, last_error=? WHERE id=?",
                    (now, f"{type(e).__name__}: {e}"[:300], fid))
        con.commit(); con.close()
        return {"ok": False, "error": str(e)}
    con.execute("DELETE FROM events WHERE feed_id=?", (fid,))
    for ev, start, end in occurrences:
        con.execute("INSERT INTO events (feed_id, uid, title, location, description, start, end, all_day) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (fid, ev["uid"], ev["title"], ev["location"], ev["description"],
                     start.replace(tzinfo=None).isoformat(timespec="minutes"),
                     end.replace(tzinfo=None).isoformat(timespec="minutes") if end else None,
                     1 if ev["all_day"] else 0))
    con.execute("UPDATE feeds SET last_sync=?, last_error=NULL, event_count=? WHERE id=?",
                (now, len(occurrences), fid))
    con.commit(); con.close()
    return {"ok": True, "events": len(occurrences)}


def sync_all():
    return {f["id"]: sync_feed(f["id"]) for f in feeds()}


def maybe_sync(max_age=None):
    """Sync feeds whose last sync is older than max_age seconds (engine tick)."""
    max_age = SYNC_INTERVAL if max_age is None else max_age
    n = 0
    for f in feeds():
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(f["last_sync"])).total_seconds() if f["last_sync"] else 1e9
        except ValueError:
            age = 1e9
        if age >= max_age:
            sync_feed(f["id"])
            n += 1
    return n


# ============================ queries ============================
def _window(lo, hi):
    """Both layers (feed + local) merged for the [lo, hi) instant window, soonest first.
    Each event carries `source` ('local' | 'feed') and `editable` (True only for local)."""
    con = _db()
    feed_rows = con.execute(
        "SELECT e.id, e.title, e.location, e.description, e.start, e.end, e.all_day, f.name "
        "FROM events e LEFT JOIN feeds f ON f.id=e.feed_id "
        "WHERE COALESCE(e.end, e.start) >= ? AND e.start < ? ORDER BY e.start",
        (lo, hi)).fetchall()
    local_rows = con.execute(
        "SELECT id, title, location, description, start, end, all_day, category "
        "FROM local_events WHERE COALESCE(end, start) >= ? AND start < ? ORDER BY start",
        (lo, hi)).fetchall()
    con.close()
    out = [{"id": r[0], "title": r[1], "location": r[2], "description": r[3],
            "start": r[4], "end": r[5], "all_day": bool(r[6]),
            "calendar": r[7] or "Calendar", "source": "feed", "editable": False}
           for r in feed_rows]
    out += [{"id": r[0], "title": r[1], "location": r[2], "description": r[3],
             "start": r[4], "end": r[5], "all_day": bool(r[6]),
             "calendar": r[7] or "Oceano", "source": "local", "editable": True}
            for r in local_rows]
    out.sort(key=lambda e: e["start"])
    return out


def upcoming(days=30):
    """Merged events from today through +days (for the agenda tool)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today + timedelta(days=days + 1)
    return _window(today.isoformat(timespec="minutes"), horizon.isoformat(timespec="minutes"))


def range_events(start, end):
    """Merged events overlapping the [start, end) DATE range — what the month/week/day grid
    asks for. `start`/`end` are 'YYYY-MM-DD' (end exclusive). Bad input → empty list."""
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", start or "") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end or "")):
        return []
    return _window(start + "T00:00", end + "T00:00")


def _has_local():
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM local_events").fetchone()[0]
    con.close()
    return n > 0


def agenda(days=7):
    """The upcoming agenda as plain text — what the calendar_events tool returns. Editable
    local events are marked `[#id]` (use that id with update/delete_calendar_event); synced
    feed events are marked read-only so the agent knows it can't touch them."""
    evs = upcoming(days)
    if not evs:
        if feeds() or _has_local():
            return f"(no events in the next {days} days — the schedule is clear)"
        return ("(calendar is empty — add events with add_calendar_event, or subscribe to an "
                "external .ics feed in the Calendar section of the web UI)")
    lines, last_day = [], None
    for e in evs:
        day = e["start"][:10]
        if day != last_day:
            label = datetime.fromisoformat(day).strftime("%A %Y-%m-%d")
            lines.append(f"\n{label}:")
            last_day = day
        if e["all_day"]:
            when = "all day"
        else:
            when = e["start"][11:16] + (f"–{e['end'][11:16]}" if e["end"] and e["end"][:10] == day else "")
        where = f" ({e['location']})" if e["location"] else ""
        tag = f"  [#{e['id']}]" if e["editable"] else f"  (read-only · {e['calendar']})"
        lines.append(f"  - {when}  {e['title']}{where}{tag}")
    return "\n".join(lines).strip()
