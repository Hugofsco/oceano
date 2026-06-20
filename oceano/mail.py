"""Mail — the email accounts Oceano can read, organize, and send from (IMAP + SMTP).

Managed, gated, audited — the same shape as the SSH keychain (oceano/hosts.py):
  • a multi-account store (data/mail.json, 0600, gitignored) with secrets stripped from the API (_public)
  • a PRIMARY account that's the default the agent works on; it may target another by name
  • a per-account policy (readonly | active | trusted) + in-memory "arming" for sending
This module just stores accounts, custodies the app-password, and opens IMAP/SMTP connections. The real
safety gates (web-only channel, injection-taint split, policy/arm) live in the mail_* tools in tools.py.

Storage: one JSON file (atomic, chmod 0600). App passwords are stored alongside the account (the same bar
as the Telegram token in web.json and the SSH keys under data/hosts/); they are NEVER returned by the API.
"""
import email
import email.utils
import html
import imaplib
import json
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "mail.json"
POLICIES = ("readonly", "active", "trusted")
_ARM_TTL = 1800                 # a send-arm lasts 30 minutes
_TIMEOUT = 25                   # IMAP/SMTP connect/op timeout (seconds)
_BODY_CAP = 16000               # cap a fetched body like other tool outputs
_LIST_CAP = 50                  # never list more than this many messages per call

_lock = threading.Lock()
_ARM = {}                       # account id -> expiry epoch (in-memory, never persisted)

# common fallbacks when a server doesn't advertise special-use folders
_TRASH_NAMES = ("Trash", "[Gmail]/Trash", "Deleted Items", "Deleted Messages", "INBOX.Trash")
_JUNK_NAMES = ("Junk", "Spam", "[Gmail]/Spam", "Junk E-mail", "INBOX.Junk")


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------- persistence ----------------
def _load():
    try:
        d = json.loads(STORE.read_text())
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("accounts", [])
    return d


def _save(d):
    atomicio.write_text(STORE, json.dumps(d, indent=2))
    try:
        STORE.chmod(0o600)                      # holds app passwords — restrict to the owner
    except OSError:
        pass


def _next_id(items):
    return max((x["id"] for x in items), default=0) + 1


def _public(a):
    """An account with the password stripped — for the API, UI, and agent."""
    return {"id": a["id"], "name": a["name"], "email": a["email"],
            "imap_host": a["imap_host"], "imap_port": a.get("imap_port", 993),
            "imap_ssl": a.get("imap_ssl", True),
            "smtp_host": a["smtp_host"], "smtp_port": a.get("smtp_port", 465),
            "smtp_ssl": a.get("smtp_ssl", True),
            "user": a.get("user") or a["email"],
            "policy": a.get("policy", "active"),
            "primary": bool(a.get("primary")),
            "has_password": bool(a.get("password")),
            "description": a.get("description", ""), "last_used": a.get("last_used"),
            "armed": is_armed(a["id"])}


# ---------------- CRUD ----------------
def list_all():
    return [_public(a) for a in _load()["accounts"]]


def _raw(aid):
    return next((a for a in _load()["accounts"] if a["id"] == aid), None)


def get(aid):
    a = _raw(aid)
    return _public(a) if a else None


def _resolve(name_or_id):
    """name (case-insensitive), email, or numeric id → the RAW record (with the password)."""
    s = str(name_or_id or "").strip().lower()
    if not s:
        return None
    for a in _load()["accounts"]:
        if (a["name"].strip().lower() == s or a["email"].strip().lower() == s
                or str(a["id"]) == s):
            return a
    return None


def names():
    return ", ".join(a["name"] for a in _load()["accounts"]) or "(none configured)"


def resolve_target(account=None):
    """The mailbox an agent action should act on, honouring Hugo's segregation rule:
      explicit name → that account; else the PRIMARY; else (single account) it; else AMBIGUOUS.
    Returns (raw_record | None, error_message | None). The error is a string the tool relays to the
    user so the agent asks which mailbox to use instead of guessing."""
    accts = _load()["accounts"]
    if account:
        a = _resolve(account)
        if not a:
            return None, f"no mailbox named {account!r}. Configured: {names()}"
        return a, None
    if not accts:
        return None, "no mail accounts are configured — ask the user to add one in Settings → Mail."
    prim = next((a for a in accts if a.get("primary")), None)
    if prim:
        return prim, None
    if len(accts) == 1:
        return accts[0], None
    return None, ("multiple mailboxes are configured and none is set as primary — ask the user which one "
                  f"to use ({names()}), then pass it as the `account` argument.")


def create(name, email_addr, imap_host, smtp_host, user="", password="", imap_port=993,
           smtp_port=465, imap_ssl=True, smtp_ssl=True, policy="active", primary=False, description=""):
    name = (name or "").strip()
    email_addr = (email_addr or "").strip()
    if not name or not email_addr or not (imap_host or "").strip() or not (smtp_host or "").strip():
        return None
    d = _load()
    if any(a["name"].strip().lower() == name.lower() for a in d["accounts"]):
        return None
    rec = {"id": _next_id(d["accounts"]), "name": name, "email": email_addr,
           "imap_host": imap_host.strip(), "imap_port": int(imap_port or 993), "imap_ssl": bool(imap_ssl),
           "smtp_host": smtp_host.strip(), "smtp_port": int(smtp_port or 465), "smtp_ssl": bool(smtp_ssl),
           "user": (user or "").strip() or email_addr, "password": password or "",
           "policy": policy if policy in POLICIES else "active",
           "primary": False, "description": (description or "").strip(),
           "created": _now(), "last_used": None}
    d["accounts"].append(rec)
    # first account becomes primary automatically; or honour an explicit request
    if primary or not any(a.get("primary") for a in d["accounts"][:-1]):
        for a in d["accounts"]:
            a["primary"] = (a["id"] == rec["id"])
    _save(d)
    return _public(rec)


def update(aid, **fields):
    d = _load()
    a = next((x for x in d["accounts"] if x["id"] == aid), None)
    if not a:
        return None
    for k in ("name", "email", "imap_host", "smtp_host", "user", "description"):
        if fields.get(k) is not None:
            a[k] = str(fields[k]).strip()
    for k in ("imap_port", "smtp_port"):
        if fields.get(k) is not None:
            try:
                a[k] = int(fields[k])
            except (TypeError, ValueError):
                pass
    for k in ("imap_ssl", "smtp_ssl"):
        if fields.get(k) is not None:
            a[k] = bool(fields[k])
    if fields.get("policy") in POLICIES:
        a["policy"] = fields["policy"]
    if fields.get("password"):                  # only replace when a new one is actually supplied
        a["password"] = fields["password"]
    _save(d)
    return _public(a)


def set_primary(aid):
    d = _load()
    if not any(a["id"] == aid for a in d["accounts"]):
        return None
    for a in d["accounts"]:
        a["primary"] = (a["id"] == aid)
    _save(d)
    return _public(_raw(aid))


def remove(aid):
    d = _load()
    before = len(d["accounts"])
    was_primary = any(a["id"] == aid and a.get("primary") for a in d["accounts"])
    d["accounts"] = [a for a in d["accounts"] if a["id"] != aid]
    if was_primary and d["accounts"]:           # keep exactly one primary
        d["accounts"][0]["primary"] = True
    _save(d)
    disarm(aid)
    return len(d["accounts"]) < before


def _stamp_used(aid):
    d = _load()
    a = next((x for x in d["accounts"] if x["id"] == aid), None)
    if a:
        a["last_used"] = _now()
        _save(d)


# ---------------- send-arming (in-memory; the human-in-the-loop control for sending) ----------------
def arm(aid):
    if not _raw(aid):
        return False
    with _lock:
        _ARM[aid] = time.time() + _ARM_TTL
    return True


def disarm(aid):
    with _lock:
        _ARM.pop(aid, None)


def is_armed(aid):
    with _lock:
        exp = _ARM.get(aid)
        if exp and exp > time.time():
            return True
        if exp:
            _ARM.pop(aid, None)
        return False


def arm_expiry(aid):
    with _lock:
        return _ARM.get(aid)


# ---------------- policy (per-account gate; channel + taint gates live in the tool) ----------------
def check_policy(a, action):
    """None if `action` ('read' | 'organize' | 'send') is allowed on account a under its policy, else a
    refusal string the agent relays. Assumes the channel + taint gates already passed."""
    pol = a.get("policy", "active")
    if pol == "trusted":
        return None
    if action == "read":
        return None                              # every policy can read
    if pol == "readonly":
        return (f"mailbox '{a['name']}' is read-only — I can read/search/organize-preview but not change or "
                f"send. Ask the user to set its policy to 'active' (or 'trusted') in Settings → Mail.")
    if action == "organize":
        return None                              # 'active' allows in-mailbox organize/delete
    if action == "send":
        if is_armed(a["id"]):
            return None
        return (f"mailbox '{a['name']}' is not armed for sending. Ask the user to open Mail and Arm it "
                f"(grants a {_ARM_TTL // 60}-minute send window), or set its policy to 'trusted'.")
    return f"mailbox '{a['name']}' has an unknown action {action!r}"


# ---------------- MIME helpers ----------------
def _dh(value):
    """Decode an RFC 2047 header (=?utf-8?...?=) to a plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _strip_html(htmltext):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", htmltext)
    t = re.sub(r"(?s)<br\s*/?>", "\n", t)
    t = re.sub(r"(?s)</p\s*>", "\n\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", t)).strip()


def _extract_text(msg):
    """Best-effort plain-text body: prefer text/plain, else strip the text/html part."""
    plain, htmlpart = None, None
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                plain = _payload_text(part)
            elif ctype == "text/html" and htmlpart is None:
                htmlpart = _payload_text(part)
    else:
        if msg.get_content_type() == "text/html":
            htmlpart = _payload_text(msg)
        else:
            plain = _payload_text(msg)
    body = plain if plain else (_strip_html(htmlpart) if htmlpart else "")
    return body[:_BODY_CAP]


def _payload_text(part):
    try:
        raw = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")
    except Exception:
        return ""


def _attachment_names(msg):
    names_ = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                names_.append(_dh(part.get_filename()) or "(unnamed)")
    return names_


# ---------------- IMAP ----------------
def _imap(a):
    """Open + login an IMAP connection (caller closes via _imap_close)."""
    if a.get("imap_ssl", True):
        conn = imaplib.IMAP4_SSL(a["imap_host"], int(a.get("imap_port", 993)),
                                 ssl_context=ssl.create_default_context(), timeout=_TIMEOUT)
    else:
        conn = imaplib.IMAP4(a["imap_host"], int(a.get("imap_port", 143)), timeout=_TIMEOUT)
        try:
            conn.starttls(ssl_context=ssl.create_default_context())
        except Exception:
            pass
    conn.login(a.get("user") or a["email"], a.get("password") or "")
    return conn


def _imap_close(conn):
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


def _q(folder):
    """Quote a mailbox name for IMAP (handles spaces / specials)."""
    return '"%s"' % (folder or "INBOX").replace('"', '\\"')


def _folder_names(conn):
    out = []
    typ, data = conn.list()
    if typ != "OK":
        return out
    for line in data or []:
        s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        m = re.search(r'(?:"([^"]+)"|(\S+))\s*$', s)        # last token = the mailbox name
        if m:
            out.append(m.group(1) or m.group(2))
    return out


def _special_folder(conn, attr, fallbacks):
    """Find a special-use folder (e.g. '\\Trash') from LIST flags; else the first existing fallback name."""
    try:
        typ, data = conn.list()
        if typ == "OK":
            for line in data or []:
                s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                if attr.lower() in s.lower():
                    m = re.search(r'(?:"([^"]+)"|(\S+))\s*$', s)
                    if m:
                        return m.group(1) or m.group(2)
    except Exception:
        pass
    existing = set(_folder_names(conn))
    for name in fallbacks:
        if name in existing:
            return name
    return fallbacks[0]


def _capabilities(conn):
    try:
        return set((conn.capabilities or ()))
    except Exception:
        return set()


def imap_folders(a):
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        return {"ok": True, "folders": _folder_names(conn)}
    finally:
        _imap_close(conn)


def folder_unreads(a, folders=None):
    """{folder_name: unseen_count} for folders with unread mail, via ONE connection (STATUS UNSEEN
    per folder — no message fetch, so it's cheap). Folders with zero unread are omitted."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    out = {}
    try:
        for f in (folders or _folder_names(conn)):
            try:
                typ, data = conn.status(_q(f), "(UNSEEN)")
                if typ == "OK" and data and data[0]:
                    s = data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
                    m = re.search(r"UNSEEN (\d+)", s)
                    if m and int(m.group(1)):
                        out[f] = int(m.group(1))
            except Exception:
                continue
        return {"ok": True, "unreads": out}
    finally:
        _imap_close(conn)


def folder_stats(a):
    """{folder: {'total': n, 'unread': n}} for EVERY folder, via one connection (STATUS — counts only,
    no message fetch). Lets the agent see message counts / which folders are empty in a single call."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    out = {}
    try:
        for f in _folder_names(conn):
            try:
                typ, data = conn.status(_q(f), "(MESSAGES UNSEEN)")
                if typ == "OK" and data and data[0]:
                    s = data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0])
                    mt = re.search(r"MESSAGES (\d+)", s)
                    mu = re.search(r"UNSEEN (\d+)", s)
                    out[f] = {"total": int(mt.group(1)) if mt else 0, "unread": int(mu.group(1)) if mu else 0}
            except Exception:
                continue
        return {"ok": True, "stats": out}
    finally:
        _imap_close(conn)


def _imap_msg(data):
    """Pull a human-readable message out of an imaplib response payload."""
    try:
        if data and data[0]:
            return (data[0].decode("utf-8", "replace") if isinstance(data[0], bytes) else str(data[0]))[:160]
    except Exception:
        pass
    return ""


_SPECIAL_ATTRS = ("\\Sent", "\\Trash", "\\Drafts", "\\Junk", "\\All", "\\Archive", "\\Flagged", "\\Important")


def _special_use_folders(conn):
    """Names of special-use folders (Sent/Trash/Drafts/Junk/All/Starred/…) — never delete these."""
    out = set()
    try:
        typ, data = conn.list()
        if typ == "OK":
            for line in data or []:
                s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                if any(attr.lower() in s.lower() for attr in _SPECIAL_ATTRS):
                    m = re.search(r'(?:"([^"]+)"|(\S+))\s*$', s)
                    if m:
                        out.add(m.group(1) or m.group(2))
    except Exception:
        pass
    return out


def imap_create_folder(a, name):
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "folder name required"}
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, data = conn.create(_q(name))
        if typ != "OK":
            return {"ok": False, "error": _imap_msg(data) or "could not create folder"}
        try:
            conn.subscribe(_q(name))             # so it appears in mail clients
        except Exception:
            pass
        return {"ok": True, "text": f"created folder {name}"}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def imap_rename_folder(a, name, new):
    name, new = (name or "").strip(), (new or "").strip()
    if not name or not new:
        return {"ok": False, "error": "both the current and new name are required"}
    if name.upper() == "INBOX":
        return {"ok": False, "error": "INBOX can't be renamed"}
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        if name in _special_use_folders(conn):
            return {"ok": False, "error": f"'{name}' is a system folder and can't be renamed"}
        typ, data = conn.rename(_q(name), _q(new))
        if typ != "OK":
            return {"ok": False, "error": _imap_msg(data) or "could not rename folder"}
        return {"ok": True, "text": f"renamed {name} → {new}"}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def imap_delete_folder(a, name):
    """Delete a mailbox/folder. Refuses INBOX and special-use folders. NOTE: on most servers this also
    deletes the messages inside it; on Gmail it just removes the label (messages survive in All Mail)."""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "folder name required"}
    if name.upper() == "INBOX" or name == "[Gmail]":
        return {"ok": False, "error": f"'{name}' is a system folder and can't be deleted"}
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        if name in _special_use_folders(conn):
            return {"ok": False, "error": f"'{name}' is a system folder (Sent/Trash/etc.) and can't be deleted"}
        try:
            conn.unsubscribe(_q(name))
        except Exception:
            pass
        typ, data = conn.delete(_q(name))
        if typ != "OK":
            return {"ok": False, "error": _imap_msg(data) or "could not delete folder (system folder?)"}
        return {"ok": True, "text": f"deleted folder {name}"}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def _search_uids(conn, query=None, unread_only=False):
    crit = []
    if unread_only:
        crit.append("UNSEEN")
    if query:
        crit += ["TEXT", '"%s"' % str(query).replace('"', "")]
    if not crit:
        crit = ["ALL"]
    typ, data = conn.uid("SEARCH", None, *crit)
    if typ != "OK" or not data:
        return []
    return data[0].split()


def imap_list(a, folder="INBOX", query=None, limit=20, unread_only=False):
    """Newest-first list of message headers in `folder`. Returns {ok, folder, total, messages:[...]}."""
    limit = max(1, min(int(limit or 20), _LIST_CAP))
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, _ = conn.select(_q(folder), readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"no such folder {folder!r}"}
        uids = _search_uids(conn, query, unread_only)
        total = len(uids)
        chosen = uids[-limit:][::-1]                         # newest first
        msgs = []
        if chosen:
            uid_set = b",".join(chosen)
            typ, data = conn.uid("FETCH", uid_set,
                                 "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            by_uid = {}
            if typ == "OK":
                for item in data or []:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    meta = item[0].decode("utf-8", "replace")
                    um = re.search(r"UID (\d+)", meta)
                    flags = imaplib.ParseFlags(item[0])
                    hdr = email.message_from_bytes(item[1])
                    if um:
                        by_uid[um.group(1)] = (hdr, flags)
            for uid in chosen:
                u = uid.decode()
                hdr, flags = by_uid.get(u, (None, ()))
                if hdr is None:
                    continue
                fl = [f.decode() if isinstance(f, bytes) else str(f) for f in flags]
                msgs.append({"uid": u, "from": _dh(hdr.get("From")), "subject": _dh(hdr.get("Subject")),
                             "date": _dh(hdr.get("Date")), "seen": "\\Seen" in fl,
                             "flagged": "\\Flagged" in fl})
        return {"ok": True, "folder": folder, "total": total, "messages": msgs}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def imap_read(a, uid, folder="INBOX"):
    """Fetch one message's headers + plain-text body WITHOUT marking it read (BODY.PEEK)."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, _ = conn.select(_q(folder), readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"no such folder {folder!r}"}
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return {"ok": False, "error": f"message uid {uid} not found in {folder}"}
        msg = email.message_from_bytes(data[0][1])
        return {"ok": True, "uid": str(uid), "folder": folder,
                "from": _dh(msg.get("From")), "to": _dh(msg.get("To")), "cc": _dh(msg.get("Cc")),
                "subject": _dh(msg.get("Subject")), "date": _dh(msg.get("Date")),
                "attachments": _attachment_names(msg), "body": _extract_text(msg)}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def _reply_context(conn, uid):
    """Headers needed to compose a threaded reply (To, Subject, Message-ID, References)."""
    typ, data = conn.uid("FETCH", str(uid),
                         "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT MESSAGE-ID REFERENCES)])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    h = email.message_from_bytes(data[0][1])
    to = _dh(h.get("Reply-To") or h.get("From"))
    subj = _dh(h.get("Subject"))
    if not re.match(r"(?i)\s*re:", subj):
        subj = "Re: " + subj
    return {"to": to, "subject": subj, "message_id": (h.get("Message-ID") or "").strip(),
            "references": (h.get("References") or "").strip()}


def imap_move(a, uid, dest, folder="INBOX"):
    """Move a message from `folder` to `dest` (UID MOVE, or COPY+\\Deleted+EXPUNGE fallback)."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, _ = conn.select(_q(folder))
        if typ != "OK":
            return {"ok": False, "error": f"no such folder {folder!r}"}
        if "MOVE" in _capabilities(conn):
            typ, _ = conn.uid("MOVE", str(uid), _q(dest))
            if typ != "OK":
                return {"ok": False, "error": f"move to {dest!r} failed"}
        else:
            typ, _ = conn.uid("COPY", str(uid), _q(dest))
            if typ != "OK":
                return {"ok": False, "error": f"copy to {dest!r} failed (does it exist?)"}
            conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            conn.expunge()
        _stamp_used(a["id"])
        return {"ok": True, "text": f"moved uid {uid} from {folder} → {dest}"}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


def imap_delete(a, uid, folder="INBOX"):
    """Delete = move to the account's Trash (reversible). No permanent expunge in v1."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        trash = _special_folder(conn, "\\Trash", _TRASH_NAMES)
    finally:
        _imap_close(conn)
    if (folder or "").strip().lower() == trash.lower():
        return {"ok": False, "error": f"message is already in {trash}; permanent delete is disabled in v1"}
    res = imap_move(a, uid, trash, folder)
    if res.get("ok"):
        res["text"] = f"deleted uid {uid} (moved {folder} → {trash})"
    return res


def imap_flag(a, uid, flag, folder="INBOX"):
    """Mark a message: flag ∈ read|unread|flagged|unflagged|spam. 'spam' moves it to the Junk folder."""
    flag = (flag or "").strip().lower()
    if flag == "spam":
        try:
            conn = _imap(a)
            junk = _special_folder(conn, "\\Junk", _JUNK_NAMES)
        except Exception as e:
            return {"ok": False, "error": _clean_err(e)}
        finally:
            try:
                _imap_close(conn)
            except Exception:
                pass
        res = imap_move(a, uid, junk, folder)
        if res.get("ok"):
            res["text"] = f"marked uid {uid} as spam (moved → {junk})"
        return res
    op_map = {"read": ("+FLAGS", "\\Seen"), "unread": ("-FLAGS", "\\Seen"),
              "flagged": ("+FLAGS", "\\Flagged"), "unflagged": ("-FLAGS", "\\Flagged")}
    if flag not in op_map:
        return {"ok": False, "error": f"unknown flag {flag!r} (use read|unread|flagged|unflagged|spam)"}
    sign, fl = op_map[flag]
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, _ = conn.select(_q(folder))
        if typ != "OK":
            return {"ok": False, "error": f"no such folder {folder!r}"}
        typ, _ = conn.uid("STORE", str(uid), sign, f"({fl})")
        if typ != "OK":
            return {"ok": False, "error": f"could not set {flag} on uid {uid}"}
        _stamp_used(a["id"])
        return {"ok": True, "text": f"marked uid {uid} {flag}"}
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)


# ---------------- SMTP ----------------
def _build_message(a, to, subject, body, cc=None, in_reply_to="", references="", html=None):
    msg = EmailMessage()
    from_name = a.get("name") or ""
    msg["From"] = email.utils.formataddr((from_name, a["email"])) if from_name else a["email"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject or "(no subject)"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    msg.set_content(body or "")                  # text/plain (the fallback)
    if html:
        msg.add_alternative(html, subtype="html")  # multipart/alternative: clients prefer the HTML part
    return msg


def _smtp_send_msg(a, msg, recipients):
    if a.get("smtp_ssl", True):
        srv = smtplib.SMTP_SSL(a["smtp_host"], int(a.get("smtp_port", 465)),
                               context=ssl.create_default_context(), timeout=_TIMEOUT)
    else:
        srv = smtplib.SMTP(a["smtp_host"], int(a.get("smtp_port", 587)), timeout=_TIMEOUT)
        srv.ehlo()
        try:
            srv.starttls(context=ssl.create_default_context())
            srv.ehlo()
        except Exception:
            pass
    try:
        srv.login(a.get("user") or a["email"], a.get("password") or "")
        srv.send_message(msg, from_addr=a["email"], to_addrs=recipients)
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def _recipients(to, cc=None):
    out = [addr for _n, addr in email.utils.getaddresses([to or ""]) if addr]
    if cc:
        out += [addr for _n, addr in email.utils.getaddresses([cc]) if addr]
    return out


def smtp_send(a, to, subject, body, cc=None, html=None):
    recips = _recipients(to, cc)
    if not recips:
        return {"ok": False, "error": "no valid recipient address"}
    msg = _build_message(a, to, subject, body, cc=cc, html=html)
    try:
        _smtp_send_msg(a, msg, recips)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    _stamp_used(a["id"])
    return {"ok": True, "text": f"sent '{subject or '(no subject)'}' to {', '.join(recips)}"}


def smtp_reply(a, uid, body, folder="INBOX", html=None):
    """Reply to message `uid`: pulls the thread headers over IMAP, then sends via SMTP."""
    try:
        conn = _imap(a)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    try:
        typ, _ = conn.select(_q(folder), readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"no such folder {folder!r}"}
        ctx = _reply_context(conn, uid)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    finally:
        _imap_close(conn)
    if not ctx or not ctx["to"]:
        return {"ok": False, "error": f"could not load message uid {uid} to reply to"}
    recips = _recipients(ctx["to"])
    if not recips:
        return {"ok": False, "error": "original message had no replyable address"}
    msg = _build_message(a, ctx["to"], ctx["subject"], body,
                         in_reply_to=ctx["message_id"], references=ctx["references"], html=html)
    try:
        _smtp_send_msg(a, msg, recips)
    except Exception as e:
        return {"ok": False, "error": _clean_err(e)}
    _stamp_used(a["id"])
    return {"ok": True, "text": f"replied to {', '.join(recips)} re: {ctx['subject']}"}


# ---------------- connectivity test (used by the UI "Test connection" button) ----------------
def test(a):
    """Verify IMAP login + SMTP login. Returns {ok, imap, smtp, error?}."""
    out = {"ok": False, "imap": False, "smtp": False}
    try:
        conn = _imap(a)
        _imap_close(conn)
        out["imap"] = True
    except Exception as e:
        out["error"] = "IMAP: " + _clean_err(e)
        return out
    try:
        if a.get("smtp_ssl", True):
            srv = smtplib.SMTP_SSL(a["smtp_host"], int(a.get("smtp_port", 465)),
                                   context=ssl.create_default_context(), timeout=_TIMEOUT)
        else:
            srv = smtplib.SMTP(a["smtp_host"], int(a.get("smtp_port", 587)), timeout=_TIMEOUT)
            srv.ehlo()
            try:
                srv.starttls(context=ssl.create_default_context())
                srv.ehlo()
            except Exception:
                pass
        try:
            srv.login(a.get("user") or a["email"], a.get("password") or "")
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        out["smtp"] = True
    except Exception as e:
        out["error"] = "SMTP: " + _clean_err(e)
        return out
    out["ok"] = True
    _stamp_used(a["id"])
    return out


def _clean_err(e):
    if isinstance(e, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)):
        return "authentication failed (wrong username/app-password, or the provider needs an app password)"
    if isinstance(e, ssl.SSLError):
        return f"TLS error: {str(e)[:160]} (check SSL/port)"
    if isinstance(e, (TimeoutError, OSError)):
        return f"connection failed: {str(e)[:160]} (check host/port)"
    return f"{type(e).__name__}: {str(e)[:200]}"
