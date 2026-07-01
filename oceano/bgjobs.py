"""OS-process registry for jobs the mind (Claude/Codex) spawns via tools.spawn_job, so they're
owned by the DAEMON — not the ephemeral `claude -p` / `codex exec` process that asked for them.

Why this exists: when Claude/Codex runs as the resident mind (agent.py's _claude_mind_stream /
_codex_mind_stream), its OWN native background execution (Bash run_in_background and the like) is
a child of the CLI subprocess for THAT turn, which exits the moment the turn ends — killing or
orphaning the job invisibly. "I'll let you know when it's done" is then a promise the harness has
no way to keep. spawn_job hands the process to THIS registry instead, which lives as long as the
daemon does (oceano.engine — one process, never exits between turns), so it can actually track the
job and notify the user for real. See oceano.tools.spawn_job / job_status for the safety-gated
entry points — this module has no safety logic of its own, by design (see tools.py).

Modeled on jobs.py's dict+snapshot+persisted-JSON shape, but jobs.job()'s with-block lifetime (same
Python thread, same process) doesn't fit an OS process that must outlive both the calling thread
and the CLI invocation that created it.
"""
import itertools
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import config
from oceano import atomicio

STATE_PATH = config.WORKSPACE.parent / "data" / "bgjobs.json"
LOG_DIR = config.WORKSPACE.parent / "data" / "job-logs"
TAIL_CHARS = 4000       # bounded tail, matches tools.run_shell's [:8000] truncation convention
MAX_KEPT = 200          # prune finished jobs beyond this (oldest-ended first); running ones are never pruned
POLL_SECS = 5           # how often a post-restart watcher polls a job it didn't spawn itself

_mx = threading.Lock()  # guards _jobs AND brackets every persist (snapshot + disk write together)
_jobs = {}              # id(int) -> record
_on_complete = None     # optional hook(rec) fired once per job at its terminal transition (set by the web layer)


def set_on_complete(cb):
    """Register a callback fired once with the job record when a job reaches a terminal state
    (done/failed/lost). The web layer uses this to deliver the result into the conversation that
    spawned the job. Best-effort — a raising callback never disturbs job bookkeeping."""
    global _on_complete
    _on_complete = cb


def _load():
    try:
        d = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        d = {}
    out = {}
    for k, v in (d.get("jobs") or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return out


_jobs = _load()
_counter = itertools.count(max(_jobs, default=0) + 1)   # ids keep climbing across restarts


def _persist():
    """Snapshot + disk write happen under the SAME lock acquisition, not split — two jobs
    finishing concurrently must not race each other's writes out of order on disk (the
    in-memory registry would stay correct either way, but a crash right after would then
    reconcile from a stale file)."""
    with _mx:
        finished = sorted((j for j in _jobs.values() if j["state"] not in ("running", "starting")),
                          key=lambda j: j.get("ended") or 0)
        for j in finished[:max(0, len(finished) - MAX_KEPT)]:
            _jobs.pop(j["id"], None)
        snapshot = json.dumps({"jobs": {str(k): v for k, v in _jobs.items()}})
    try:
        atomicio.write_text(STATE_PATH, snapshot)
    except OSError:
        pass


def _tail_file(path, max_chars=TAIL_CHARS):
    """The last `max_chars` of a log file, without ever loading the whole thing — a build log
    can be huge, and we only ever want to show/report a bounded tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars * 4))   # UTF-8 worst case ~4 bytes/char
            data = f.read()
        return data.decode("utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _proc_start_ticks(pid):
    """Field 22 (starttime, in clock ticks since boot) of /proc/<pid>/stat — used to tell
    'our job is still alive' apart from 'an unrelated process later reused this pid', which a
    bare os.kill(pid, 0) can't distinguish. None if the pid is gone or /proc is unavailable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        # the comm field (2nd, in parens) can itself contain spaces/parens, so split after the
        # LAST ')' rather than naively splitting on whitespace from the start.
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[19])              # fields[0] is stat's field 3 (state), so field 22 is fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid, start_ticks):
    if not pid:
        return False
    now_ticks = _proc_start_ticks(pid)
    if now_ticks is None:
        return False
    return start_ticks is None or now_ticks == start_ticks


def spawn(argv, cwd, display, label="", sid=None):
    """Launch argv as a detached child (its own session, so it outlives both the calling
    thread and — via start_new_session — any signal sent to the daemon's process group), log
    its output to a file (NEVER a pipe: a build's output can exceed the OS pipe buffer, and
    with nothing draining it the child — and our reaper — would hang forever), and start a
    reaper thread so it's never left a zombie. `sid` is the conversation that spawned it (so its
    result can be delivered back there); None when spawned outside a chat (telegram/scheduler).
    Returns the job record dict."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jid = next(_counter)
    log_path = LOG_DIR / f"{jid}.log"
    rec = {"id": jid, "label": (label or display)[:140], "command": display,
           "pid": None, "start_ticks": None, "state": "starting",
           "started": time.time(), "ended": None, "exit_code": None,
           "log_path": str(log_path), "sid": sid, "delivered": False}
    with _mx:
        _jobs[jid] = rec
    logf = open(log_path, "wb")
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        logf.close()
        with _mx:
            rec["state"], rec["ended"] = "failed", time.time()
        _persist()
        raise
    with _mx:
        rec["pid"] = proc.pid
        rec["start_ticks"] = _proc_start_ticks(proc.pid)
        rec["state"] = "running"
    _persist()
    threading.Thread(target=_reap, args=(jid, proc, logf), daemon=True).start()
    return dict(rec)


def _reap(jid, proc, logf):
    """Owns the real Popen for this job's lifetime — the only legitimate way to reap our own
    child and retrieve its true exit code."""
    try:
        code = proc.wait()
    finally:
        try:
            logf.close()
        except OSError:
            pass
    with _mx:
        rec = _jobs.get(jid)
        if rec is None:
            return
        rec["exit_code"], rec["ended"] = code, time.time()
        rec["state"] = "done" if code == 0 else "failed"
        snap = dict(rec)
    _persist()
    _announce(snap)


def _announce(rec):
    """Proactively tell the user the job's outcome — this is what actually fulfills 'I'll let
    you know when it's done', from the daemon side, independent of whether anyone is watching
    or asking. Best-effort: a notify/log failure must never break job bookkeeping."""
    try:
        from oceano import scheduler, logs
        dur = round((rec["ended"] or time.time()) - rec["started"], 1)
        if rec["state"] == "done":
            msg = f'Job "{rec["label"]}" finished after {dur}s.'
        elif rec["state"] == "failed":
            msg = (f'Job "{rec["label"]}" FAILED (exit {rec["exit_code"]}) after {dur}s.\n'
                  + _tail_file(rec["log_path"], 800))
        else:                                # "lost" — reconciled after a daemon restart
            msg = (f'Job "{rec["label"]}" is no longer running (Oceano restarted while it was '
                  f'in flight, so its result could not be recovered) after {dur}s. Last output:\n'
                  + _tail_file(rec["log_path"], 800))
        scheduler.notify(msg, title="Oceano — background job")
        logs.log_run(kind="bgjob", title=rec["label"], status="ok" if rec["state"] == "done" else "error",
                    summary=_tail_file(rec["log_path"], 2000), duration=dur, ref=f"bgjob:{rec['id']}")
    except Exception:
        pass
    if _on_complete is not None:                 # deliver the result into the spawning conversation
        try:
            _on_complete(rec)
        except Exception:
            pass


def pending_for(sid):
    """Terminal jobs for conversation `sid` whose result hasn't been delivered into the chat yet,
    each with a bounded tail — the web layer polls this to print completions into the conversation."""
    if not sid:
        return []
    with _mx:
        recs = [dict(j) for j in _jobs.values()
                if j.get("sid") == sid and not j.get("delivered")
                and j["state"] in ("done", "failed", "lost")]
    for r in recs:
        r["tail"] = _tail_file(r["log_path"]) if r.get("log_path") else ""
    return recs


def mark_delivered(job_id):
    """Flag a job's result as delivered into its conversation, so it's never printed twice.
    Persisted, so a daemon restart doesn't re-deliver. Returns True if a job was flagged."""
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return False
    with _mx:
        rec = _jobs.get(jid)
        if rec is None or rec.get("delivered"):
            return False
        rec["delivered"] = True
    _persist()
    return True


def status(job_id=None):
    """No job_id → every tracked job (most recent first). A job_id → that job's record plus a
    bounded tail of its log. None if the id doesn't exist."""
    if not job_id:
        with _mx:
            return [dict(j) for j in sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)]
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return None
    with _mx:
        rec = _jobs.get(jid)
        rec = dict(rec) if rec else None
    if rec is not None:
        rec["tail"] = _tail_file(rec["log_path"]) if rec.get("log_path") else ""
    return rec


def _watch(jid):
    """Poll a job THIS process did not spawn itself (i.e. one still 'running' after a daemon
    restart) until it disappears. We can never recover its real exit code — once the old daemon
    process exited, the OS reparented the orphaned job to init/a subreaper, and only that
    process can retrieve it via wait() — so the terminal state here is honestly 'lost', not a
    guessed pass/fail."""
    while True:
        with _mx:
            rec = _jobs.get(jid)
            if rec is None or rec["state"] not in ("running", "starting"):
                return
            pid, start_ticks = rec.get("pid"), rec.get("start_ticks")
        if not _alive(pid, start_ticks):
            with _mx:
                rec = _jobs.get(jid)
                if rec and rec["state"] in ("running", "starting"):
                    rec["state"], rec["ended"] = "lost", time.time()
                    snap = dict(rec)
                else:
                    snap = None
            _persist()
            if snap:
                _announce(snap)
            return
        time.sleep(POLL_SECS)


def _reconcile():
    """Run once at import (daemon startup). Any job left 'running'/'starting' in the persisted
    state belongs to a PREVIOUS daemon process, not this one — so it's picked up by _watch
    (poll-only), never by _reap (which requires owning the real Popen)."""
    with _mx:
        pending = [dict(j) for j in _jobs.values() if j["state"] in ("running", "starting")]
    for rec in pending:
        if _alive(rec.get("pid"), rec.get("start_ticks")):
            threading.Thread(target=_watch, args=(rec["id"],), daemon=True).start()
        else:
            with _mx:
                r = _jobs.get(rec["id"])
                if r:
                    r["state"], r["ended"] = "lost", r.get("ended") or time.time()
                    snap = dict(r)
                else:
                    snap = None
            _persist()
            if snap:
                _announce(snap)         # best-effort: tell the user it's gone, exit code unrecoverable


_reconcile()      # must be last: everything it calls must already be defined
