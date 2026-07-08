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


def write_text(path, data, encoding="utf-8", mode=0o600):
    """Atomically replace `path`'s contents with `data`. Raises on I/O error (callers
    that already swallow OSError around their write keep doing so).

    `mkstemp` already creates its temp file at 0600 regardless of umask, and `os.replace`
    preserves that mode on the destination — so every store written through this function
    already lands at 0600 today. The explicit `os.fchmod` below just makes that guarantee
    documented and independent of `tempfile`'s implementation, rather than relying on it
    as an implicit side effect."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
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


def secure(path, mode=0o600):
    """Best-effort chmod for files not written through write_text (sqlite DBs, plain
    logs, the heartbeat file) — these are created via sqlite3.connect()/open()/
    Path.write_text() and land at the process umask default instead of 0600."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass
