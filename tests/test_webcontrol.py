import threading
import time

import pytest

from oceano import livebrowser, webcontrol
from oceano.tools import web


@pytest.fixture(autouse=True)
def isolated_governor(monkeypatch):
    monkeypatch.setattr(webcontrol, "MIN_INTERVAL", 0)
    monkeypatch.setattr(webcontrol, "CACHE_TTL", 300)
    monkeypatch.setattr(webcontrol, "BLOCK_COOLDOWN", 10)
    monkeypatch.setattr(webcontrol, "MAX_COOLDOWN", 60)
    monkeypatch.setattr(webcontrol, "MAX_CONCURRENCY", 4)
    monkeypatch.setattr(webcontrol, "MAX_ORIGIN_STATES", 32)
    webcontrol.reset_for_tests()
    yield
    webcontrol.reset_for_tests()


def test_same_origin_operations_are_serialized():
    active = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Event()

    def worker():
        nonlocal active, peak
        with webcontrol.permit("https://example.test/page"):
            with lock:
                active += 1
                peak = max(peak, active)
                entered.set()
            time.sleep(0.03)
            with lock:
                active -= 1

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert entered.wait(1)
    second.start()
    first.join()
    second.join()
    assert peak == 1


def test_per_origin_start_interval_is_enforced(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(webcontrol, "MIN_INTERVAL", 1.5)
    monkeypatch.setattr(webcontrol.time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(webcontrol.time, "sleep", fake_sleep)
    with webcontrol.permit("https://example.test/one"):
        pass
    with webcontrol.permit("https://example.test/two"):
        pass
    assert sleeps == [1.5]


def test_429_arms_fast_host_cooldown():
    url = "https://example.test/page"
    with webcontrol.permit(url):
        assert webcontrol.observe_response(url, 429, {"Retry-After": "30"}) == 30
    with pytest.raises(webcontrol.CoolingDown) as exc:
        with webcontrol.permit(url):
            pass
    assert exc.value.seconds >= 29


def test_hostile_retry_after_is_finite_and_capped():
    assert webcontrol.observe_response(
        "https://large.test", 429, {"Retry-After": "999999999999"}) == 60
    assert webcontrol.observe_response(
        "https://infinite.test", 429, {"Retry-After": "1e309"}) == 10


def test_origin_state_collection_is_bounded(monkeypatch):
    monkeypatch.setattr(webcontrol, "MAX_ORIGIN_STATES", 16)
    for number in range(40):
        with webcontrol.permit(f"https://host-{number}.test/page"):
            pass
    assert len(webcontrol._states) <= 16


def test_malformed_port_does_not_crash_governor_keying():
    url = "https://example.test:notaport/page"
    assert webcontrol.origin(url) == "invalid://"
    assert webcontrol.cache_key(url).startswith("invalid:")


def test_concurrent_cache_loads_are_coalesced():
    started = threading.Event()
    release = threading.Event()
    calls = 0
    values = []

    def loader():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(1)
        return "loaded"

    def worker():
        values.append(webcontrol.cached("same", loader))

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert started.wait(1)
    second.start()
    release.set()
    first.join()
    second.join()
    assert calls == 1
    assert values == ["loaded", "loaded"]


def test_url_cache_key_ignores_fragment_but_not_query():
    assert (webcontrol.cache_key("HTTPS://Example.Test/a?q=1#first")
            == webcontrol.cache_key("https://example.test/a?q=1#second"))
    assert (webcontrol.cache_key("https://example.test/a?q=1")
            != webcontrol.cache_key("https://example.test/a?q=2"))


def test_interactive_fetch_uses_reader_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "live_browser_available", lambda: True)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(web.livebrowser, "session_fingerprint", lambda: "session-a")
    monkeypatch.setattr(
        web.livebrowser, "fetch",
        lambda url: calls.append(url) or {
            "ok": True, "text": "rendered", "status": 200, "headers": {}, "error": ""})
    assert web.fetch_url("https://example.test/page") == "rendered"
    assert web.fetch_url("https://example.test/page#section") == "rendered"
    assert calls == ["https://example.test/page"]


def test_interactive_429_is_not_cached_and_blocks_immediate_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "live_browser_available", lambda: True)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(web.livebrowser, "session_fingerprint", lambda: "session-a")
    monkeypatch.setattr(
        web.livebrowser, "fetch",
        lambda url: calls.append(url) or {
            "ok": False, "text": "", "status": 429,
            "headers": {"Retry-After": "20"}, "error": "blocked"})
    first = web.fetch_url("https://example.test/page")
    second = web.fetch_url("https://example.test/page")
    assert "HTTP 429" in first
    assert "cooling down" in second
    assert len(calls) == 1


@pytest.mark.parametrize("cache_meta", [
    {"authenticated": True},
    {"sets_cookie": True},
    {"cache_control": "private, max-age=60"},
    {"cache_control": "no-store"},
])
def test_private_browser_responses_are_not_cached(monkeypatch, cache_meta):
    calls = []
    monkeypatch.setattr(web, "live_browser_available", lambda: True)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(web.livebrowser, "session_fingerprint", lambda: "session-a")
    monkeypatch.setattr(
        web.livebrowser, "fetch",
        lambda url: calls.append(url) or {
            "ok": True, "text": "private", "status": 200, "headers": {},
            "cache": cache_meta, "error": ""})
    assert web.fetch_url("https://example.test/private") == "private"
    assert web.fetch_url("https://example.test/private") == "private"
    assert len(calls) == 2


def test_browser_cache_is_partitioned_by_cookie_state(monkeypatch):
    calls = []
    sessions = iter(("anonymous", "authenticated"))
    monkeypatch.setattr(web, "live_browser_available", lambda: True)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(web.livebrowser, "session_fingerprint", lambda: next(sessions))
    monkeypatch.setattr(
        web.livebrowser, "fetch",
        lambda url: calls.append(url) or {
            "ok": True, "text": "rendered", "status": 200, "headers": {},
            "cache": {}, "error": ""})
    web.fetch_url("https://example.test/page")
    web.fetch_url("https://example.test/page")
    assert len(calls) == 2


def test_browser_cache_fails_closed_without_session_fingerprint(monkeypatch):
    calls = []
    monkeypatch.setattr(web, "live_browser_available", lambda: True)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(web.livebrowser, "session_fingerprint", lambda: "")
    monkeypatch.setattr(
        web.livebrowser, "fetch",
        lambda url: calls.append(url) or {
            "ok": True, "text": "rendered", "status": 200, "headers": {},
            "cache": {}, "error": ""})
    web.fetch_url("https://example.test/page")
    web.fetch_url("https://example.test/page")
    assert len(calls) == 2


def test_cross_origin_302_drops_headers_and_body(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, location=None):
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.reason = "OK"
            self.text = "done"

    responses = iter((Response(302, "https://other.test/next"), Response(200)))
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(
        web.safety, "guarded_request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses))
    result = web.http_request(
        "https://origin.test/start", method="POST",
        headers={"Authorization": "Bearer secret", "X-Custom": "value"},
        json={"secret": "payload"})
    assert "done" in result
    assert calls[1][0][0] == "GET"
    assert calls[1][1]["headers"] == {}
    assert calls[1][1]["json"] is None
    assert calls[1][1]["data"] is None


def test_cross_origin_307_refuses_body_replay(monkeypatch):
    class Response:
        status_code = 307
        headers = {"Location": "https://other.test/next"}
        reason = "Temporary Redirect"
        text = ""

    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "guarded_request", lambda *args, **kwargs: Response())
    result = web.http_request(
        "https://origin.test/start", method="POST", json={"secret": "payload"})
    assert "REFUSED cross-origin HTTP 307 redirect" in result


def test_same_origin_redirect_preserves_existing_request(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, location=None):
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.reason = "OK"
            self.text = "done"

    responses = iter((Response(307, "/next"), Response(200)))
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(
        web.safety, "guarded_request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses))
    web.http_request(
        "https://origin.test/start", method="POST",
        headers={"Authorization": "Bearer secret"}, json={"value": 1})
    assert calls[1][0][0] == "POST"
    assert calls[1][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[1][1]["json"] == {"value": 1}


def test_background_fetch_uses_guarded_http_cache(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}
        text = "<html><body><main>Readable page</main></body></html>"

    monkeypatch.setattr(web, "live_browser_available", lambda: False)
    monkeypatch.setattr(web.safety, "check_url", lambda url: None)
    monkeypatch.setattr(web.safety, "wrap_untrusted", lambda source, text: text)
    monkeypatch.setattr(
        web.safety, "guarded_get", lambda *a, **k: calls.append((a, k)) or Response())
    assert web.fetch_url("https://example.test/page") == "Readable page"
    assert web.fetch_url("https://example.test/page") == "Readable page"
    assert len(calls) == 1


def test_web_search_reuses_short_cache(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"results": [{"title": "A", "url": "https://example.test", "content": "B"}]}

    monkeypatch.setattr(web, "live_browser_available", lambda: False)
    monkeypatch.setattr(web.requests, "get", lambda *a, **k: calls.append((a, k)) or Response())
    first = web.web_search("same query")
    second = web.web_search("same   query")
    assert "https://example.test" in first
    assert "https://example.test" in second
    assert len(calls) == 1
