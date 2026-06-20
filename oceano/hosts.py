"""Keychain & Hosts — the servers Oceano can SSH into and run command batches on.

Managed, gated, audited. Private keys are custodied under data/hosts/ (0600, gitignored); a host's
server key is PINNED on first connect (trust-on-first-use) and verified on every later connect, so a
changed key (MITM) fails loudly. The agent reaches a host ONLY through the ssh_run / list_hosts tools,
which add the real safety gates on top of this module:
  • web channel only (never scheduler / telegram / background)
  • not in a turn that ingested untrusted web/email/doc content (blocks prompt-injection → remote exec)
  • the host's policy (readonly | armed | trusted)
This module just stores hosts, custodies keys, pins host keys, and opens the connection.

Storage: one JSON file (atomic). Keys live as data/hosts/<id>.key. Passphrases/passwords are NOT
stored unless the user opts to "remember" them — normally they're entered when a host is *armed* and
held only in memory for the arm window.
"""
import base64
import hashlib
import io
import json
import re
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from oceano import atomicio

STORE = config.WORKSPACE.parent / "data" / "hosts.json"
KEY_DIR = config.WORKSPACE.parent / "data" / "hosts"
POLICIES = ("readonly", "armed", "trusted")
_OUT_CAP = 8000                 # cap stdout/stderr per command (like other tool outputs)
_ARM_TTL = 1800                 # an arm lasts 30 minutes
_CONNECT_TIMEOUT = 15

_lock = threading.Lock()
_ARM = {}                       # hid -> expiry epoch        (in-memory, never persisted)
_ARM_SECRET = {}                # hid -> passphrase/password (in-memory, never persisted)


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------- persistence ----------------
def _load():
    try:
        d = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("hosts", [])
    return d


def _save(d):
    atomicio.write_text(STORE, json.dumps(d, indent=2))


def _next_id(items):
    return max((x["id"] for x in items), default=0) + 1


def _norm_auth(a):
    a = a or {}
    t = a.get("type") if a.get("type") in ("key", "password") else "key"
    return {"type": t,
            "key_file": a.get("key_file") or None,
            "key_path": (a.get("key_path") or None),
            "has_passphrase": bool(a.get("has_passphrase")),
            "passphrase": a.get("passphrase") or None,   # only if user chose to remember
            "password": a.get("password") or None}        # only if user chose to remember


def _needs_secret(h):
    """True if connecting requires a secret the user must supply when arming (not stored)."""
    a = h.get("auth") or {}
    if a.get("type") == "password":
        return not a.get("password")
    return bool(a.get("has_passphrase")) and not a.get("passphrase")


def _public(h):
    """A host record with every secret stripped — for the API, UI, and agent."""
    a = h.get("auth") or {}
    return {"id": h["id"], "name": h["name"], "host": h["host"], "port": h.get("port", 22),
            "user": h["user"], "policy": h.get("policy", "armed"),
            "auth_type": a.get("type", "key"),
            "has_key": bool(a.get("key_file") or a.get("key_path")),
            "needs_secret": _needs_secret(h), "pinned": bool(h.get("host_key")),
            "fingerprint": _fingerprint(h.get("host_key", "")),
            "description": h.get("description", ""), "last_used": h.get("last_used"),
            "armed": is_armed(h["id"])}


# ---------------- CRUD ----------------
def list_all():
    return [_public(h) for h in _load()["hosts"]]


def _raw(hid):
    return next((h for h in _load()["hosts"] if h["id"] == hid), None)


def get(hid):
    h = _raw(hid)
    return _public(h) if h else None


def get_by_name(name):
    name = (name or "").strip().lower()
    h = next((x for x in _load()["hosts"] if x["name"].strip().lower() == name), None)
    return _public(h) if h else None


def _resolve(name_or_id):
    """name or numeric id → the RAW record (with secrets), for the connector."""
    s = str(name_or_id or "").strip()
    for h in _load()["hosts"]:
        if h["name"].strip().lower() == s.lower() or str(h["id"]) == s:
            return h
    return None


def create(name, host, user, port=22, auth=None, policy="armed", description=""):
    name = (name or "").strip()
    if not name or not (host or "").strip() or not (user or "").strip():
        return None
    d = _load()
    if any(h["name"].strip().lower() == name.lower() for h in d["hosts"]):
        return None
    try:
        port = int(port or 22)
    except (TypeError, ValueError):
        port = 22
    rec = {"id": _next_id(d["hosts"]), "name": name, "host": host.strip(), "port": port,
           "user": user.strip(), "auth": _norm_auth(auth),
           "policy": policy if policy in POLICIES else "armed",
           "host_key": "", "description": (description or "").strip(),
           "created": _now(), "last_used": None}
    d["hosts"].append(rec)
    _save(d)
    return _public(rec)


def update(hid, **fields):
    d = _load()
    h = next((x for x in d["hosts"] if x["id"] == hid), None)
    if not h:
        return None
    for k in ("name", "host", "user", "description", "host_key"):
        if fields.get(k) is not None:
            h[k] = str(fields[k]).strip() if k != "host_key" else fields[k]
    if fields.get("port") is not None:
        try:
            h["port"] = int(fields["port"])
        except (TypeError, ValueError):
            pass
    if fields.get("policy") in POLICIES:
        h["policy"] = fields["policy"]
    if fields.get("auth") is not None:
        # merge: keep a previously-custodied key_file unless a new auth supplies one
        merged = {**(h.get("auth") or {}), **_norm_auth(fields["auth"])}
        h["auth"] = _norm_auth(merged)
    _save(d)
    return _public(h)


def remove(hid):
    d = _load()
    before = len(d["hosts"])
    d["hosts"] = [h for h in d["hosts"] if h["id"] != hid]
    _save(d)
    disarm(hid)
    try:
        (KEY_DIR / f"{hid}.key").unlink()
    except OSError:
        pass
    return len(d["hosts"]) < before


def set_key(hid, pem):
    """Custody a private key for this host: write data/hosts/<id>.key (0600) and point auth at it.
    Detects whether the key is passphrase-protected so arming can prompt for it."""
    d = _load()
    h = next((x for x in d["hosts"] if x["id"] == hid), None)
    if not h:
        return False
    pem = pem if pem.endswith("\n") else pem + "\n"
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    atomicio.write_text(KEY_DIR / f"{hid}.key", pem)        # mkstemp 0600 → file ends up 0600
    a = h.setdefault("auth", {})
    a["type"] = "key"
    a["key_file"] = f"hosts/{hid}.key"
    a["key_path"] = None
    a["has_passphrase"] = _pem_encrypted(pem)
    _save(d)
    return True


# ---------------- session arming (in-memory; the human-in-the-loop control) ----------------
def arm(hid, secret=None):
    if not _raw(hid):
        return False
    with _lock:
        _ARM[hid] = time.time() + _ARM_TTL
        if secret:
            _ARM_SECRET[hid] = secret
    return True


def disarm(hid):
    with _lock:
        _ARM.pop(hid, None)
        _ARM_SECRET.pop(hid, None)


def is_armed(hid):
    with _lock:
        exp = _ARM.get(hid)
        if exp and exp > time.time():
            return True
        if exp:                                 # expired → forget the secret too
            _ARM.pop(hid, None)
            _ARM_SECRET.pop(hid, None)
        return False


def arm_expiry(hid):
    with _lock:
        return _ARM.get(hid)


def _armed_secret(hid):
    with _lock:
        return _ARM_SECRET.get(hid)


# ---------------- policy (the per-host gate; channel + taint gates live in the tool) ----------------
_WRITE_RE = re.compile(
    r"(^|[\s;&|`(])(rm|mv|cp|dd|mkfs\w*|tee|truncate|shred|chmod|chown|chgrp|ln|install|patch|"
    r"apt|apt-get|yum|dnf|pacman|zypper|snap|pip\d?|npm|cargo|gem|make|systemctl|service|"
    r"reboot|shutdown|halt|poweroff|kill|pkill|killall|useradd|userdel|usermod|passwd|"
    r"crontab|iptables|nft|ufw|mount|umount|mkdir|rmdir|touch|git|docker|kubectl|"
    r"sed\s+-i|tar\s+[^|]*-[a-z]*x|curl[^|]*-O|wget)(\s|$)", re.IGNORECASE)


def looks_write(cmd):
    """Best-effort: does this command change the remote host? Used for the `readonly` policy.
    NOT a security boundary — the real boundary is the remote SSH user's own permissions."""
    c = (cmd or "").strip()
    return bool(_WRITE_RE.search(c)) or ">" in c.replace(">=", "").replace("2>&1", "").replace("->", "")


def check_policy(h, commands):
    """None if `commands` may run on host h under its policy, else a refusal string the agent relays.
    Assumes the channel + taint gates already passed."""
    pol = h.get("policy", "armed")
    if pol == "trusted":
        return None
    if pol == "armed":
        if is_armed(h["id"]):
            return None
        return (f"host '{h['name']}' is not armed. Ask the user to open the Hosts panel and Arm it "
                f"(grants a {_ARM_TTL // 60}-minute window); I can't run anything until they do.")
    if pol == "readonly":
        bad = [c for c in commands if looks_write(c)]
        if bad:
            return (f"host '{h['name']}' is read-only — these would change it: "
                    + "; ".join(b.strip()[:60] for b in bad[:3])
                    + ". Ask the user to Arm it (or set its policy to trusted) for write commands.")
        return None
    return f"host '{h['name']}' has an unknown policy"


# ---------------- the connector (paramiko + host-key pinning) ----------------
def _hk_name(host, port):
    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def _fingerprint(keyline):
    """OpenSSH-style SHA256 fingerprint of a stored 'type base64' host-key line."""
    try:
        blob = base64.b64decode(keyline.split()[1])
        return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    except (IndexError, ValueError, AttributeError):
        return ""


def _pem_encrypted(pem):
    import paramiko
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            cls.from_private_key(io.StringIO(pem))
            return False                        # loaded with no passphrase → not encrypted
        except paramiko.PasswordRequiredException:
            return True
        except paramiko.SSHException:
            continue
    return True                                 # couldn't parse unencrypted → assume it needs one


def _load_pkey(text, passphrase):
    import paramiko
    last = None
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException as e:
            last = "this key needs a passphrase — arm the host and provide it"
        except paramiko.SSHException as e:
            last = str(e)
    raise ValueError(last or "could not load private key (unsupported format or wrong passphrase)")


def _read_key_text(h):
    a = h.get("auth") or {}
    if a.get("key_path"):
        return Path(a["key_path"]).read_text()
    if a.get("key_file"):
        return (config.WORKSPACE.parent / "data" / a["key_file"]).read_text()
    raise ValueError("no SSH key configured for this host")


class _Capture:
    """First-connect host-key capture (TOFU). Only used in the explicit test/pin flow."""
    def __init__(self):
        self.key = None

    def missing_host_key(self, client, hostname, key):
        self.key = key                          # capture + allow (then the caller pins it)


def _open(h, secret, pin_mode):
    import paramiko
    a = h.get("auth") or {}
    cli = paramiko.SSHClient()
    cap = None
    if pin_mode and not h.get("host_key"):
        cap = _Capture()
        cli.set_missing_host_key_policy(cap)    # TOFU: capture the server key this once
    else:
        from paramiko.hostkeys import HostKeyEntry
        name = _hk_name(h["host"], h.get("port", 22))
        entry = HostKeyEntry.from_line(f"{name} {h['host_key']}")
        if entry:
            cli.get_host_keys().add(name, entry.key.get_name(), entry.key)
        cli.set_missing_host_key_policy(paramiko.RejectPolicy())   # any OTHER key → reject (MITM)
    kw = {"hostname": h["host"], "port": int(h.get("port", 22)), "username": h["user"],
          "timeout": _CONNECT_TIMEOUT, "auth_timeout": _CONNECT_TIMEOUT,
          "banner_timeout": _CONNECT_TIMEOUT, "look_for_keys": False, "allow_agent": False}
    if a.get("type") == "password":
        kw["password"] = secret or a.get("password") or ""
    else:
        kw["pkey"] = _load_pkey(_read_key_text(h), secret or a.get("passphrase"))
    cli.connect(**kw)
    captured = f"{cap.key.get_name()} {cap.key.get_base64()}" if (cap and cap.key) else None
    return cli, captured


def _clean_err(e):
    import paramiko
    if isinstance(e, paramiko.BadHostKeyException):
        return "SERVER HOST KEY CHANGED — refusing to connect (possible MITM). Re-pin only if you trust it."
    if isinstance(e, paramiko.AuthenticationException):
        return "authentication failed (wrong key/passphrase/password or user)"
    return f"{type(e).__name__}: {str(e)[:200]}"


def test_and_pin(hid, secret=None):
    """Connect once and PIN the server's host key (TOFU). Returns {ok, fingerprint} or {ok:False,error}."""
    h = _raw(hid)
    if not h:
        return {"ok": False, "error": "no such host"}
    try:
        cli, captured = _open(h, secret, pin_mode=True)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": _clean_err(e)}
    try:
        cli.close()
    except Exception:
        pass
    if captured:
        update(hid, host_key=captured)
    line = captured or h.get("host_key", "")
    return {"ok": True, "fingerprint": _fingerprint(line)}


def run(name_or_id, commands, secret=None, timeout=60):
    """Open ONE connection, run `commands` in order, close. Returns
    {ok, host, results:[{cmd, exit, stdout, stderr}]} or {ok:False, error}. No gating here —
    the tool enforces channel/taint/policy before calling this."""
    h = _resolve(name_or_id)
    if not h:
        return {"ok": False, "error": f"no host named {name_or_id!r}"}
    if not h.get("host_key"):
        return {"ok": False, "error": f"host '{h['name']}' has no pinned key — Test & pin it first"}
    sec = secret or _armed_secret(h["id"])
    try:
        cli, _ = _open(h, sec, pin_mode=False)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": _clean_err(e)}
    results = []
    try:
        for cmd in commands:
            try:
                _in, out, err = cli.exec_command(cmd, timeout=timeout)
                so = out.read().decode("utf-8", "replace")[:_OUT_CAP]
                se = err.read().decode("utf-8", "replace")[:_OUT_CAP]
                code = out.channel.recv_exit_status()
                results.append({"cmd": cmd, "exit": code, "stdout": so, "stderr": se})
            except Exception as e:               # noqa: BLE001 — one bad command shouldn't drop the rest
                results.append({"cmd": cmd, "exit": -1, "stdout": "", "stderr": _clean_err(e)})
    finally:
        try:
            cli.close()
        except Exception:
            pass
    d = _load()                                  # stamp last_used
    rec = next((x for x in d["hosts"] if x["id"] == h["id"]), None)
    if rec:
        rec["last_used"] = _now()
        _save(d)
    return {"ok": True, "host": h["name"], "results": results}


# ---------------- SFTP (file transfer, reuses the keychain) ----------------
def _ws_path(local):
    """Resolve a LOCAL path under the workspace; reject anything that escapes it."""
    p = (config.WORKSPACE / (local or "")).resolve()
    if not (p == config.WORKSPACE.resolve() or str(p).startswith(str(config.WORKSPACE.resolve()) + "/")):
        raise ValueError("local path escapes the workspace")
    return p


def check_sftp_policy(h, action):
    """None if `action` ('list'|'get'|'put') is allowed on host h under its policy, else a refusal.
    (Channel + taint gates run in the tool first.) 'put' writes the remote; 'get'/'list' only read it."""
    pol = h.get("policy", "armed")
    if pol == "trusted":
        return None
    if pol == "armed":
        return None if is_armed(h["id"]) else (
            f"host '{h['name']}' is not armed — ask the user to Arm it in the Hosts panel "
            f"(grants a {_ARM_TTL // 60}-minute window).")
    if pol == "readonly":
        if action == "put":
            return (f"host '{h['name']}' is read-only — uploads are blocked. "
                    f"Ask the user to Arm it (or set its policy to trusted).")
        return None
    return f"host '{h['name']}' has an unknown policy"


def sftp(name_or_id, action, remote_path="", local_path="", secret=None):
    """SFTP against a registered host. action: 'list' (a remote dir) | 'get' (download remote →
    workspace) | 'put' (upload workspace → remote). LOCAL paths are confined to the workspace.
    No gating here — the tool enforces channel/taint/policy before calling this."""
    h = _resolve(name_or_id)
    if not h:
        return {"ok": False, "error": f"no host named {name_or_id!r}"}
    if not h.get("host_key"):
        return {"ok": False, "error": f"host '{h['name']}' has no pinned key — Test & pin it first"}
    try:
        cli, _ = _open(h, secret or _armed_secret(h["id"]), pin_mode=False)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": _clean_err(e)}
    try:
        sf = cli.open_sftp()
        if action == "list":
            items = sorted(sf.listdir_attr(remote_path or "."), key=lambda a: a.filename)[:300]
            rows = []
            for a in items:
                d = stat.S_ISDIR(a.st_mode or 0)
                rows.append(("📁 " if d else "   ") + a.filename + ("/" if d else f"   {a.st_size}B"))
            return {"ok": True, "host": h["name"],
                    "text": f"{remote_path or '.'} on {h['name']}:\n" + ("\n".join(rows) or "(empty)")}
        if action == "get":
            if not remote_path:
                return {"ok": False, "error": "remote_path is required for get"}
            dest = _ws_path(local_path or Path(remote_path).name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            sf.get(remote_path, str(dest))
            return {"ok": True, "host": h["name"],
                    "text": f"downloaded {remote_path} → workspace/{dest.relative_to(config.WORKSPACE)} "
                            f"({dest.stat().st_size}B)"}
        if action == "put":
            src = _ws_path(local_path)
            if not src.is_file():
                return {"ok": False, "error": f"workspace file not found: {local_path}"}
            dst = remote_path or Path(local_path).name
            sf.put(str(src), dst)
            return {"ok": True, "host": h["name"],
                    "text": f"uploaded workspace/{src.relative_to(config.WORKSPACE)} → {dst} on "
                            f"{h['name']} ({src.stat().st_size}B)"}
        return {"ok": False, "error": f"unknown action {action!r} (use list, get, or put)"}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": _clean_err(e)}
    finally:
        try:
            cli.close()
        except Exception:
            pass
