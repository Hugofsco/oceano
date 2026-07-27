"""Shared outbound-web pacing, caching, deduplication, and host cooldowns.

This controls tool-level requests and browser navigations. Chromium subresources are reduced
separately in livebrowser, but every top-level operation still passes through this governor,
including concurrent Claude/Codex MCP calls running on independent daemon threads.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit


def _float_env(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return float(default)


def _int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


MIN_INTERVAL = _float_env("OCEANO_WEB_MIN_INTERVAL", "1.5")
MAX_CONCURRENCY = _int_env("OCEANO_WEB_MAX_CONCURRENCY", "4")
CACHE_TTL = _float_env("OCEANO_WEB_CACHE_TTL", "300")
BLOCK_COOLDOWN = _float_env("OCEANO_WEB_BLOCK_COOLDOWN", "120")
MAX_CACHE_ENTRIES = _int_env("OCEANO_WEB_CACHE_ENTRIES", "256", minimum=16)


class CoolingDown(RuntimeError):
    def __init__(self, origin, seconds):
        self.origin = origin
        self.seconds = max(1, int(seconds + 0.999))
        super().__init__(f"{origin} is cooling down for {self.seconds}s after a rate-limit/block response")


@dataclass
class _OriginState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    next_start: float = 0.0
    cooldown_until: float = 0.0


_state_lock = threading.RLock()
_states = {}
_global_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
_cache = {}                         # key -> (expires_monotonic, value)
_inflight = {}                      # key -> Event


def origin(url):
    """Canonical scheme://host:port identity used for pacing and cooldowns."""
    p = urlsplit(str(url or ""))
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if not scheme or not host:
        return "invalid://"
    default = (scheme == "http" and p.port in (None, 80)) or (scheme == "https" and p.port in (None, 443))
    return f"{scheme}://{host}" + ("" if default else f":{p.port}")


def cache_key(url):
    """Normalize a URL for safe GET deduplication; fragments never affect the response body."""
    p = urlsplit(str(url or ""))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def _state(url):
    key = origin(url)
    with _state_lock:
        return key, _states.setdefault(key, _OriginState())


@contextmanager
def permit(url):
    """Serialize one operation per origin, pace starts, and cap global web concurrency.

    Active cooldowns fail fast rather than sleeping a model/tool worker for minutes.
    """
    key, state = _state(url)
    with state.lock:
        now = time.monotonic()
        if state.cooldown_until > now:
            raise CoolingDown(key, state.cooldown_until - now)
        delay = state.next_start - now
        if delay > 0:
            time.sleep(delay)
        _global_slots.acquire()
        state.next_start = time.monotonic() + MIN_INTERVAL
        try:
            yield
        finally:
            _global_slots.release()


def _retry_after_seconds(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def observe_response(url, status, headers=None):
    """Arm a host cooldown for 429/403. Returns cooldown seconds (0 when not armed)."""
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        return 0
    if code not in (403, 429):
        return 0
    headers = headers or {}
    retry = _retry_after_seconds(headers.get("Retry-After") or headers.get("retry-after"))
    seconds = max(BLOCK_COOLDOWN, retry or 0.0)
    _, state = _state(url)
    with state.lock:
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + seconds)
    return max(1, int(seconds + 0.999))


def cached(key, loader, ttl=None, cache_if=None):
    """Return a cached value or coalesce concurrent loads of the same safe GET key."""
    ttl = CACHE_TTL if ttl is None else max(0.0, float(ttl))
    if ttl <= 0:
        return loader()
    while True:
        with _state_lock:
            now = time.monotonic()
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
            event = _inflight.get(key)
            if event is None:
                event = _inflight[key] = threading.Event()
                owner = True
            else:
                owner = False
        if owner:
            try:
                value = loader()
                if cache_if is None or cache_if(value):
                    with _state_lock:
                        if len(_cache) >= MAX_CACHE_ENTRIES:
                            oldest = min(_cache, key=lambda item: _cache[item][0])
                            _cache.pop(oldest, None)
                        _cache[key] = (time.monotonic() + ttl, value)
                return value
            finally:
                with _state_lock:
                    _inflight.pop(key, None)
                    event.set()
        event.wait(60)


def reset_for_tests():
    """Clear process state. Tests may also monkeypatch module constants for zero-delay runs."""
    global _global_slots
    with _state_lock:
        _states.clear()
        _cache.clear()
        for event in _inflight.values():
            event.set()
        _inflight.clear()
        _global_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
