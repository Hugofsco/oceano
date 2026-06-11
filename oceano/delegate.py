"""Delegate a bounded subtask to Claude Code (the `claude` CLI), running headless.

Why: the local model must not validate its own work (skill review), and some
subtasks benefit from a stronger coding model. The user already runs Claude Code
on this box, so `claude -p` uses their existing subscription — no API key.

Containment: the subprocess runs with cwd inside the workspace (or a caller-chosen
folder) and ONLY the tools listed in `tools` are allowed — no Bash unless a caller
explicitly grants it. Claude Code itself blocks edits outside its working directory.
"""
import shutil
import subprocess

import config

DEFAULT_TOOLS = "Read,Glob,Grep,Write,Edit"


def available():
    return shutil.which("claude") is not None


def to_claude(instructions, cwd=None, tools=DEFAULT_TOOLS, timeout=600, max_turns=30):
    """Run one headless Claude Code task. Returns {ok, output, error}."""
    binary = shutil.which("claude")
    if not binary:
        return {"ok": False, "output": "",
                "error": "claude CLI not found on PATH — install Claude Code to use delegation"}
    cmd = [binary, "-p", instructions, "--output-format", "text",
           "--max-turns", str(int(max_turns))]
    if tools:
        cmd += ["--allowedTools", tools]
    try:
        r = subprocess.run(cmd, cwd=str(cwd or config.WORKSPACE),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": f"claude timed out after {timeout}s"}
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not launch claude: {e}"}
    if r.returncode != 0:
        return {"ok": False, "output": (r.stdout or "").strip(),
                "error": (r.stderr or f"claude exited {r.returncode}").strip()[:400]}
    return {"ok": True, "output": (r.stdout or "").strip(), "error": ""}
