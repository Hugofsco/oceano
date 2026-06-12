"""Server-side chat persistence, organized into dated folders.

Each conversation is one JSON file at  data/chats/<YYYY-MM-DD>/<id>.json  where the
date is the day the chat STARTED (stable — a chat keeps its folder as it continues).
This survives browser clears and groups history by day. Chats live until deleted.
"""
import json
import re
import shutil
from datetime import datetime

import config

CHATS_DIR = config.WORKSPACE.parent / "data" / "chats"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")          # session ids come from the client → validate


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
        created = created or _now()
        folder = CHATS_DIR / created[:10]            # YYYY-MM-DD
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.json"
    rec = {"id": sid, "title": (title or "New voyage")[:120], "created": created,
           "updated": _now(), "messages": messages or []}
    path.write_text(json.dumps(rec))
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
