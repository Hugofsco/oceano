"""Server-side chat persistence, organized into dated folders.

Each conversation is one JSON file at  data/chats/<YYYY-MM-DD>/<id>.json  where the
date is the day the chat STARTED (stable — a chat keeps its folder as it continues).
This survives browser clears and groups history by day. Chats live until deleted.
"""
import json
import re
import shutil
import sqlite3
from datetime import datetime

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
    if existing is not None:
        try:
            created = (json.loads(existing.read_text()).get("created")) or created or _now()
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
           "updated": _now(), "messages": messages or []}
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


def reindex():
    """Embed conversations whose content changed since last index; prune deleted ones.
    Returns how many were (re)embedded. No-op cost when nothing changed."""
    con = _vdb()
    have = {r[0]: r[1] for r in con.execute("SELECT id, updated FROM chatvec").fetchall()}
    done, seen = 0, set()
    for meta in list_all():                              # newest first; {id, title, date, updated, count}
        sid = meta["id"]
        seen.add(sid)
        if have.get(sid) == meta["updated"]:
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
    qv = embeddings.embed(query)
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
