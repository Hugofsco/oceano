"""Server-side chat persistence, organized into dated folders.

Each conversation is one JSON file at  data/chats/<YYYY-MM-DD>/<id>.json  where the
date is the day the chat STARTED (stable — a chat keeps its folder as it continues).
This survives browser clears and groups history by day. Chats live until deleted.
"""
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone

import config
from oceano import atomicio, embeddings

CHATS_DIR = config.WORKSPACE.parent / "data" / "chats"
VEC_DB = config.WORKSPACE.parent / "data" / "chatvec.db"   # one embedding per conversation (incremental)
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")          # session ids come from the client → validate
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # the dated-folder component must be a real date (ASCII)


def _safe_id(sid):
    return bool(sid) and _ID_RE.fullmatch(sid or "") is not None


def _now():
    return datetime.now().isoformat(timespec="seconds")   # local time → folders match the user's day


def _to_utc(s):
    """Parse a stored ISO timestamp to an aware UTC datetime (created/updated are local-naive; a
    message `ts` is UTC). Returns None if unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)  # naive → local → UTC
    except (TypeError, ValueError):
        return None


def _fill_ts(messages, lo, hi, start, end):
    """Give every user/assistant message in [lo, hi) that lacks a `ts` an interpolated UTC timestamp,
    spread evenly between `start` and `end` by position — so each message gets its OWN, monotonic
    time rather than all sharing one. Idempotent: messages that already have a ts are untouched."""
    span, denom = (end - start), max(1, hi - lo - 1)
    for j in range(max(0, lo), min(hi, len(messages))):
        m = messages[j]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and not m.get("ts"):
            m["ts"] = (start + span * ((j - lo) / denom)).isoformat(timespec="seconds")


def _stamp(messages, prev_n, created, prev_updated):
    """Ensure every displayed (user/assistant) message carries an individual `ts`. Messages newly
    appended in THIS save get the current time; pre-existing un-stamped history is spread across the
    chat's real active window [created, prev_updated] so each old message gets a plausible, distinct
    time. The browser stamps its own messages, but turns from the scheduler, Telegram, the Claude
    mind, a reconnected stream — and the whole backlog from before timestamps existed — flow here."""
    now = datetime.now(timezone.utc)
    c = _to_utc(created) or now
    pu = _to_utc(prev_updated) or c
    if pu < c:
        pu = c
    _fill_ts(messages, 0, prev_n, c, pu)                 # historical: within [created, last activity]
    nowiso = now.isoformat(timespec="seconds")
    for j in range(max(0, prev_n), len(messages)):       # appended this save → now
        m = messages[j]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and not m.get("ts"):
            m["ts"] = nowiso
    return messages


def backfill_timestamps():
    """One-time migration: give every existing user/assistant message a `ts`. For chats from before
    per-message timestamps existed (all ts missing), spread each message's time across the chat's real
    [created, updated] window so every message shows an individual, plausible time. Idempotent —
    re-running only touches messages still lacking a ts, and preserves created/updated."""
    if not CHATS_DIR.exists():
        return {"chats": 0, "stamped": 0}
    n_chats = n_stamped = 0
    for f in sorted(CHATS_DIR.glob("*/*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        msgs = rec.get("messages") or []
        missing = sum(1 for m in msgs if isinstance(m, dict)
                      and m.get("role") in ("user", "assistant") and not m.get("ts"))
        if not missing:
            continue
        c = _to_utc(rec.get("created")) or _to_utc(rec.get("updated")) or datetime.now(timezone.utc)
        u = _to_utc(rec.get("updated")) or c
        if u < c:
            u = c
        _fill_ts(msgs, 0, len(msgs), c, u)
        rec["messages"] = msgs
        atomicio.write_text(f, json.dumps(rec))          # preserve created/updated; only fill ts
        n_chats += 1
        n_stamped += missing
    return {"chats": n_chats, "stamped": n_stamped}


def _find(sid):
    """Path of an existing session file (searched across all dated folders), or None."""
    if not _safe_id(sid):
        return None
    return next(iter(sorted(CHATS_DIR.glob(f"*/{sid}.json"))), None)


def save(sid, title, messages, created=None):
    """Create or update a chat. Keeps the original creation date/folder on update."""
    if not _safe_id(sid):
        return False
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    existing = _find(sid)
    prev_n = 0                                            # how many messages were already on disk
    prev_updated = None                                   # the chat's last-activity time before this save
    if existing is not None:
        try:
            old = json.loads(existing.read_text())
            created = old.get("created") or created or _now()
            prev_n = len(old.get("messages") or [])
            prev_updated = old.get("updated")
        except (OSError, json.JSONDecodeError):
            created = created or _now()
        path = existing
    else:
        day = (created or "")[:10]                   # the dated folder = YYYY-MM-DD
        if not _DATE_RE.fullmatch(day):              # anything not a clean ASCII date → today
            created = _now(); day = created[:10]
        folder = CHATS_DIR / day
        # defense in depth: the day folder must be a direct child of CHATS_DIR
        if folder.resolve().parent != CHATS_DIR.resolve():
            return False
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.json"
    rec = {"id": sid, "title": (title or "New voyage")[:120], "created": created,
           "updated": _now(), "messages": _stamp(messages or [], prev_n, created, prev_updated)}
    atomicio.write_text(path, json.dumps(rec))   # atomic: a torn write can't lose the conversation
    return True


def get(sid):
    p = _find(sid)
    if p is None:
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def list_all():
    """All chats as lightweight metadata (no message bodies), newest activity first."""
    if not CHATS_DIR.exists():
        return []
    out = []
    for p in CHATS_DIR.glob("*/*.json"):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": d.get("id"), "title": d.get("title") or "Untitled",
                    "created": d.get("created"), "updated": d.get("updated"),
                    "date": (d.get("created") or "")[:10], "count": len(d.get("messages") or [])})
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return out


def history_messages(sid, limit=400):
    """The stored conversation as agent-ready chat messages: a list of
    {role: 'user'|'assistant', content} so a continued chat (or any chat after a
    restart) gets its real history back into the Agent. Thinking and tool-trace
    entries are dropped — they're display-only, and reconstructing valid
    tool_call/tool_result pairs from the saved shape is fragile; the user+assistant
    turns carry the actual context. Keeps only the last `limit` turns as a backstop
    against an enormous history blowing the prompt (the user can /compact further)."""
    rec = get(sid)
    if not rec:
        return []
    out = []
    for m in rec.get("messages", []) or []:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            c = m.get("content")
            if c:                                    # skip empty assistant turns (pure tool steps)
                out.append({"role": "assistant", "content": c})
        # 'thinking' / 'tool' / 'tools' are display-only — not part of the model's context
    return out[-limit:]


def transcript(sid, limit_chars=12000):
    """A readable transcript of a conversation (User / Assistant turns + brief tool traces),
    for distilling into a skill. Reasoning is dropped; tool results are truncated."""
    rec = get(sid)
    if not rec:
        return ""
    lines = []
    for m in rec.get("messages", []) or []:
        role = m.get("role")
        if role == "user":
            lines.append("User: " + (m.get("content") or ""))
        elif role == "assistant":
            lines.append("Assistant: " + (m.get("content") or ""))
        elif role == "tool":
            lines.append(f"[tool {m.get('name')}] → {str(m.get('result', ''))[:300]}")
    return "\n".join(l for l in lines if l.strip())[:limit_chars]


def delete(sid):
    p = _find(sid)
    if p is None:
        return False
    try:
        p.unlink()
    except OSError:
        return False
    _prune_empty()
    return True


def wipe():
    """Delete every stored chat. Returns how many were removed."""
    n = 0
    if CHATS_DIR.exists():
        for p in CHATS_DIR.glob("*/*.json"):
            try:
                p.unlink(); n += 1
            except OSError:
                pass
        _prune_empty()
    return n


def _prune_empty():
    for d in CHATS_DIR.glob("*"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


# ============================ semantic search over past conversations ============================
# One embedding per conversation (title + concatenated message text), in a small SQLite store.
# Incremental: a chat is re-embedded only when its `updated` timestamp changes, so reindex() is
# cheap to call right before a search.
def _vdb():
    VEC_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(VEC_DB)
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS chatvec ("
                "id TEXT PRIMARY KEY, title TEXT, date TEXT, updated TEXT, snippet TEXT, embedding TEXT)")
    return con


_EMBED_CLIP = 1400          # the embed server is ~512-token limited; clip to a safe char budget

def _chat_text(rec):
    """A compact, topic-dense representation for embedding: the title and the user's OWN
    turns first (they carry the topic), then assistant/other text. Callers clip this to
    _EMBED_CLIP — putting the user's words first keeps the gist within the budget."""
    title = (rec.get("title") or "").strip()
    users, others = [], []
    for m in rec.get("messages", []) or []:
        t = m.get("content") or m.get("text") or ""
        if not isinstance(t, str) or not t.strip():
            continue
        (users if m.get("role") == "user" else others).append(t.strip())
    return "\n".join(p for p in ([title] + users + others) if p)


def reindex(force=False):
    """Embed conversations whose content changed since last index; prune deleted ones. Returns how
    many were (re)embedded. No-op cost when nothing changed — unless force=True re-embeds EVERY
    conversation (used after an embedding model/convention change)."""
    con = _vdb()
    have = {r[0]: r[1] for r in con.execute("SELECT id, updated FROM chatvec").fetchall()}
    done, seen = 0, set()
    for meta in list_all():                              # newest first; {id, title, date, updated, count}
        sid = meta["id"]
        seen.add(sid)
        if not force and have.get(sid) == meta["updated"]:
            continue                                     # unchanged since last index
        rec = get(sid)
        if not rec:
            continue
        text = _chat_text(rec)
        if not text.strip():
            continue                                     # nothing to embed (e.g. an empty new chat)
        vec = embeddings.embed(text[:_EMBED_CLIP])
        if not vec:
            break                                        # embed server down → leave the rest for next time
        snippet = " ".join(text[:260].split())
        con.execute("INSERT OR REPLACE INTO chatvec (id,title,date,updated,snippet,embedding) VALUES (?,?,?,?,?,?)",
                    (sid, meta["title"], meta["date"], meta["updated"], snippet, json.dumps(vec)))
        done += 1
    for gone in set(have) - seen:                        # chat deleted on disk → drop its vector
        con.execute("DELETE FROM chatvec WHERE id=?", (gone,))
    con.commit()
    con.close()
    return done


def search(query, k=8):
    """Semantic search over past conversations: [{id, title, date, snippet, score}], best first.
    Keyword fallback over title+snippet when the embed server is down."""
    reindex()                                            # keep fresh (incremental, cheap)
    con = _vdb()
    rows = con.execute("SELECT id, title, date, snippet, embedding FROM chatvec").fetchall()
    con.close()
    if not rows:
        return []
    qv = embeddings.embed(query, "query")
    scored = []
    if qv:
        for sid, title, date, snip, emb in rows:
            try:
                scored.append((embeddings.cosine(qv, json.loads(emb)), sid, title, date, snip))
            except (ValueError, TypeError):
                continue
    else:                                                # keyword fallback
        words = set(query.lower().split())
        for sid, title, date, snip, _ in rows:
            hay = (title + " " + snip).lower()
            scored.append((float(sum(w in hay for w in words)), sid, title, date, snip))
    scored.sort(reverse=True)
    return [{"id": sid, "title": title, "date": date, "snippet": snip, "score": round(max(s, 0.0), 3)}
            for s, sid, title, date, snip in scored[:k]]
