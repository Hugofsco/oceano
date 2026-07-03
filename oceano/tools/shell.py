"""Shell / Python execution and Oceano-owned background jobs — with the shared
anti-exfiltration taint gate and the bubblewrap sandbox."""
import os
import subprocess
import sys

import config
from oceano import bgjobs, safety
from oceano.tools.core import _ws, tool

# After this turn read untrusted content (a web page, email, or document), shell/Python execution
# is blocked — the same anti-exfiltration gate ssh_run/mail_send use — so an instruction injected
# into that content can't run a command to read secrets (SSH keys, mail passwords) and curl them
# out. Applies in EVERY channel, including unattended scheduler/Telegram runs where no human is
# watching — which is exactly where this matters most.
_SHELL_TAINTED = ("Blocked for safety: this turn already read external content (a web page, email, or "
                  "document), so running shell/Python is disabled — injected text must not execute "
                  "commands. Ask the user to send a fresh message to run this.")


def _shell_blocked():
    return _SHELL_TAINTED if (safety.untrusted_seen() or safety.bridge_untrusted_seen()) else None


# Defense-in-depth filesystem confinement for the agent's shell (run_shell / python_exec). The
# daemon needs data/ (mail passwords, SSH keys, the mind token), but the agent's shell never does —
# so run it in a bubblewrap sandbox that HIDES data/ and the user's own credential stores, makes the
# rest of the filesystem read-only, keeps the workspace writable, and leaves the network intact. So
# even a shell call that slips past the taint gate can't read secrets to exfiltrate. The sandbox is
# probe-gated: if bwrap is absent or unprivileged user namespaces are blocked on the host, we fall
# back to running the command directly (never break the shell). Force off with OCEANO_SHELL_SANDBOX=0.
_sandbox_probe = None


def _bwrap_base():
    ws = str(config.WORKSPACE)
    data = str(config.WORKSPACE.parent / "data")
    args = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", ws, ws, "--tmpfs", data, "--chdir", ws, "--unshare-pid", "--die-with-parent"]
    home = os.path.expanduser("~")
    for sub in (".ssh", ".aws", ".gnupg", ".config/gcloud"):     # mask the user's own credential stores too
        p = os.path.join(home, sub)
        if os.path.exists(p):
            args += ["--tmpfs", p]
    return args


def _sandbox_ok():
    """True if the bwrap sandbox (with our exact bind set) actually works on this host. Probed once."""
    global _sandbox_probe
    if _sandbox_probe is None:
        try:
            r = subprocess.run(_bwrap_base() + ["--", "true"], capture_output=True, timeout=10)
            _sandbox_probe = (r.returncode == 0)
        except Exception:                            # bwrap absent / userns blocked / any setup error
            _sandbox_probe = False
    return _sandbox_probe


def _sandbox_wrap(inner):
    """Wrap a command argv in the sandbox when it's available; otherwise return it unchanged."""
    if os.environ.get("OCEANO_SHELL_SANDBOX", "auto") == "0" or not _sandbox_ok():
        return inner
    return _bwrap_base() + ["--", *inner]


@tool({
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a bash command in the workspace and return its output. "
                       "Use for builds, scripts, git, etc.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}
        }, "required": ["command"]},
    },
})
def run_shell(command):
    blocked = _shell_blocked()                   # anti-exfiltration: no shell after reading untrusted content
    if blocked:
        return blocked
    refusal = safety.check_shell(command)
    if refusal:
        return refusal
    r = subprocess.run(
        _sandbox_wrap(["bash", "-c", command]), cwd=str(_ws()),   # confined: data/ + home creds hidden
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return f"(exit {r.returncode})\n{out[:8000]}" or f"(exit {r.returncode}, no output)"


# --- background OS jobs — owned by Oceano's daemon, unlike the mind's own native backgrounding ---
@tool({
    "type": "function",
    "function": {
        "name": "spawn_job",
        "description": (
            "Run a bash command as a background job OWNED BY OCEANO. Use this — never your own "
            "native background/async execution — for anything that must keep running after this "
            "turn ends (a build, a long script, a batch job). Your own backgrounding dies or is "
            "orphaned the instant this CLI process exits between turns, so Oceano can't track it "
            "or tell the user it finished, even if you said you would. spawn_job hands the process "
            "to Oceano's own long-lived daemon instead, which keeps running it and proactively "
            "notifies the user when it exits. Poll progress with job_status. Never say 'I'll let "
            "you know when it's done' unless you actually used this tool."
        ),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "the bash command to run in the workspace"},
            "label": {"type": "string", "description": "short name for this job, e.g. 'build frontend'"},
        }, "required": ["command"]},
    },
})
def spawn_job(command, label=""):
    blocked = _shell_blocked()                    # same anti-exfiltration gate as run_shell
    if blocked:
        return blocked
    refusal = safety.check_shell(command)          # same catastrophic-command guard as run_shell
    if refusal:
        return refusal
    argv = _sandbox_wrap(["bash", "-c", command])  # same bubblewrap sandbox as run_shell
    from oceano import mindbridge                   # lazy: mindbridge imports tools (avoid an import cycle)
    rec = bgjobs.spawn(argv, cwd=str(_ws()), display=command, label=label, sid=mindbridge.active_session())
    return f"started job #{rec['id']} ({rec['label']}) — check it with job_status(job_id={rec['id']})"


@tool({
    "type": "function",
    "function": {
        "name": "job_status",
        "description": "Check a background job started with spawn_job: state (running/done/failed/"
                       "lost), exit code, and a tail of its output. Omit job_id to list every job "
                       "Oceano is tracking.",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "integer", "description": "id from spawn_job; omit to list all"},
        }},
    },
})
def job_status(job_id=None):
    if not job_id:
        js = bgjobs.status()
        return "\n".join(f"#{j['id']} [{j['state']}] {j['label']}" for j in js) or "no background jobs"
    rec = bgjobs.status(job_id)
    if rec is None:
        return f"ERROR: no job #{job_id}"
    out = f"#{rec['id']} \"{rec['label']}\" — {rec['state']}"
    if rec["exit_code"] is not None:
        out += f" (exit {rec['exit_code']})"
    if rec.get("tail"):
        out += "\n--- output tail ---\n" + rec["tail"]
    return out


@tool({
    "type": "function",
    "function": {
        "name": "python_exec",
        "description": "Run a Python snippet in the workspace and return stdout/stderr. "
                       "Good for calculations, data wrangling, quick scripts.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}
        }, "required": ["code"]},
    },
})
def python_exec(code):
    blocked = _shell_blocked()                   # anti-exfiltration: no Python after reading untrusted content
    if blocked:
        return blocked
    refusal = safety.check_python(code)          # parity with run_shell — can't shell out to bypass the guard
    if refusal:
        return refusal
    r = subprocess.run(
        _sandbox_wrap([sys.executable, "-"]), input=code, cwd=str(_ws()),   # same confinement as run_shell
        capture_output=True, text=True, timeout=config.SHELL_TIMEOUT,
    )
    out = (r.stdout + r.stderr).strip()
    return out[:8000] or f"(exit {r.returncode}, no output)"
