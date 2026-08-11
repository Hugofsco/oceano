"""Remote servers (SSH keychain): list, run commands, transfer files."""
from oceano import safety
from oceano.tools.core import current_channel, tool

# ---------------- remote servers (SSH keychain) ----------------
_HOSTS_OFF = ("Remote host access is turned off (Settings → Security → 'Agent may connect to "
              "your servers'). The user has to enable it before ssh_run/sftp can be used.")
@tool({
    "type": "function",
    "function": {
        "name": "list_hosts",
        "description": "List the servers the user registered for SSH (name, user@host, and each "
                       "host's policy). Call this before ssh_run so you know what's reachable and "
                       "whether a host needs the user to arm it first.",
        "parameters": {"type": "object", "properties": {}},
    },
})
def list_hosts():
    if not safety.remote_hosts_enabled():
        return _HOSTS_OFF
    if current_channel() != "web" and not safety.remote_background_allowed():
        return "(remote hosts are only usable from the web UI — not in this context)"
    from oceano import hosts
    hs = hosts.list_all()
    if not hs:
        return "(no hosts registered — the user adds them in the Hosts panel)"
    def _line(h):
        st = ("armed ✓" if h["armed"] else "needs arming") if h["policy"] == "armed" else h["policy"]
        return (f"- {h['name']} ({h['user']}@{h['host']}:{h['port']}) · policy: {st}"
                + (f" · {h['description']}" if h.get("description") else "")
                + ("" if h["pinned"] else " · ⚠ not yet pinned (user must Test & pin)"))
    return "Registered hosts:\n" + "\n".join(_line(h) for h in hs)


@tool({
    "type": "function",
    "function": {
        "name": "ssh_run",
        "description": "Run one or more shell commands on a registered server over SSH, in ONE "
                       "connection (opened then closed for this call). Returns each command's exit "
                       "code and output. Call list_hosts first for the host name. SAFETY (if it "
                       "refuses, relay the exact reason to the user, don't retry blindly): it only "
                       "works in the web UI with the user present (unless the user allowed "
                       "unattended use in Settings → Security); it will NOT run if this turn has "
                       "read any web page, email, or document (prevents injected text from reaching "
                       "the user's servers); and each host has a policy — read-only hosts reject "
                       "changes, and 'armed' hosts must be unlocked by the user in the Hosts panel.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "host name (or id) from list_hosts"},
            "commands": {"type": "array", "items": {"type": "string"},
                         "description": "shell commands to run in order on that host"},
        }, "required": ["host", "commands"]},
    },
})
def ssh_run(host, commands):
    from oceano import hosts, logs
    cmds = [commands] if isinstance(commands, str) else [str(c) for c in (commands or []) if str(c).strip()]
    if not cmds:
        return "no commands given"
    # --- gates (master switch → channel → injection-taint → host → policy) ---
    if not safety.remote_hosts_enabled():
        return _HOSTS_OFF
    if current_channel() != "web" and not safety.remote_background_allowed():
        return ("ssh_run only runs in the web UI with the user present — it's blocked in "
                "background, scheduled, and Telegram runs. (The user can allow this in "
                "Settings → Security.)")
    if safety.taint_active("remote"):
        return ("Blocked for safety: this turn already read external content (a web page, email, or "
                "document), so connecting to the user's servers is disabled — injected text must not "
                "reach them. Ask the user to send a fresh message to run remote commands.")
    h = hosts._resolve(host)
    if not h:
        avail = ", ".join(x["name"] for x in hosts.list_all()) or "(none registered)"
        return f"no host named {host!r}. Registered: {avail}"
    refusal = hosts.check_policy(h, cmds)
    if refusal:
        return refusal
    # --- run + audit ---
    res = hosts.run(h["id"], cmds)
    if not res.get("ok"):
        logs.log_run("ssh", f"{h['name']}: {cmds[0][:80]}", "error", res.get("error", ""), ref=f"host:{h['id']}")
        return f"SSH to {h['name']} failed: {res.get('error')}"
    blocks = []
    for r in res["results"]:
        b = f"$ {r['cmd']}\n[exit {r['exit']}]"
        if r["stdout"].strip():
            b += "\n" + r["stdout"].strip()
        if r["stderr"].strip():
            b += "\n[stderr] " + r["stderr"].strip()
        blocks.append(b)
    worst = max((r["exit"] for r in res["results"]), default=0)
    logs.log_run("ssh", f"{h['name']}: {len(cmds)} command(s)", "ok" if worst == 0 else "error",
                 "; ".join(f"{r['cmd'][:40]} → exit {r['exit']}" for r in res["results"]), ref=f"host:{h['id']}")
    out = f"Ran on {h['name']} ({h['user']}@{h['host']}):\n\n" + "\n\n".join(blocks)
    return safety.wrap_untrusted(f"ssh:{h['name']}", out, taint=False)   # fence output, but don't block a 2nd host this turn


@tool({
    "type": "function",
    "function": {
        "name": "sftp",
        "description": "Transfer files between the workspace and a registered server over SFTP, or "
                       "list a remote directory. action: 'list' (a remote dir), 'get' (download "
                       "remote → workspace), 'put' (upload workspace → remote). Same safety gates as "
                       "ssh_run (web-only, not after reading untrusted content, per-host policy — "
                       "read-only hosts reject uploads). Local paths are confined to the workspace.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "put"]},
            "host": {"type": "string", "description": "host name (or id) from list_hosts"},
            "remote_path": {"type": "string", "description": "path on the server (dir for list, file for get/put)"},
            "local_path": {"type": "string", "description": "workspace-relative path (download target / upload source)"},
        }, "required": ["action", "host"]},
    },
})
def sftp(action, host, remote_path="", local_path=""):
    from oceano import hosts, logs
    action = (action or "").strip().lower()
    if action not in ("list", "get", "put"):
        return "action must be one of: list, get, put"
    if not safety.remote_hosts_enabled():
        return _HOSTS_OFF
    if current_channel() != "web" and not safety.remote_background_allowed():
        return ("sftp only runs in the web UI with the user present — blocked in background, "
                "scheduled, and Telegram runs. (The user can allow this in Settings → Security.)")
    if safety.taint_active("remote"):
        return ("Blocked for safety: this turn read external content (a web page, email, or document), "
                "so transferring files to/from the user's servers is disabled. Ask for a fresh message.")
    h = hosts._resolve(host)
    if not h:
        avail = ", ".join(x["name"] for x in hosts.list_all()) or "(none registered)"
        return f"no host named {host!r}. Registered: {avail}"
    refusal = hosts.check_sftp_policy(h, action)
    if refusal:
        return refusal
    res = hosts.sftp(h["id"], action, remote_path=remote_path, local_path=local_path)
    logs.log_run("ssh", f"sftp {action}: {h['name']}", "ok" if res.get("ok") else "error",
                 res.get("text") or res.get("error", ""), ref=f"host:{h['id']}")
    if not res.get("ok"):
        return f"sftp {action} failed: {res.get('error')}"
    return safety.wrap_untrusted(f"sftp:{h['name']}", res["text"], taint=False)
