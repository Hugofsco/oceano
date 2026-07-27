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
    monkeypatch.setattr(webcontrol, "MAX_CONCURRENCY", 4)
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
