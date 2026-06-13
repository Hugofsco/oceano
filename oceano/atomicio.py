"""Atomic file writes for the small durable stores.

write_text() writes to a temp file in the SAME directory, fsyncs it, then os.replace()s
it onto the target. os.replace is atomic on POSIX, and a same-directory temp keeps the
rename on one filesystem — so a crash (or a second writer racing) never leaves a
half-written file: a reader sees either the old contents whole or the new ones whole.

Used by the JSON/YAML/config stores several threads can touch at once (web, Telegram,
scheduler, calendar) — the companion to the busy_timeout/WAL settings in each DB's _db().
Not for the SQLite files themselves (sqlite does its own atomicity) or throwaway scratch
like the scheduler heartbeat.
"""
import os
import tempfile
from pathlib import Path


def write_text(path, data, encoding="utf-8"):
    """Atomically replace `path`'s contents with `data`. Raises on I/O error (callers
    that already swallow OSError around their write keep doing so)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)          # don't leave the temp behind on failure
        except OSError:
            pass
        raise
