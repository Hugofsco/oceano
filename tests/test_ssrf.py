"""SSRF classifier (oceano.safety): the address check that keeps the agent from being tricked
into reaching localhost / your LAN / cloud-metadata (169.254.169.254).

The regression pinned here: an IPv4-mapped (or 6to4 / Teredo) IPv6 address wraps an internal IPv4
whose is_private / is_link_local flags read False on the IPv6 wrapper — so classifying the wrapper
directly let ::ffff:169.254.169.254 through. _internal_ip must unwrap the embedded IPv4 first.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from oceano import livebrowser, safety  # noqa: E402


@pytest.mark.parametrize("addr", [
    "127.0.0.1",                   # loopback
    "10.0.0.5", "192.168.1.1",     # RFC1918
    "169.254.169.254",             # cloud metadata (link-local)
    "0.0.0.0", "::1",              # unspecified / IPv6 loopback
    "::ffff:169.254.169.254",      # IPv4-mapped IPv6 wrapping metadata — the bug
    "::ffff:10.0.0.5",             # IPv4-mapped IPv6 wrapping RFC1918
    "2002:a9fe:aafe::",            # 6to4 wrapping 169.254.170.254
    # CGNAT / Tailscale: NOT is_private, so the named-flag checks alone missed the whole tailnet,
    # including Tailscale's 100.100.100.100 MagicDNS/metadata endpoint.
    "100.64.0.1", "100.100.100.100",
    # Deprecated IPv6 site-local: reports is_global=True AND is_private=False on CPython 3.12,
    # so it needs the explicit is_site_local check.
    "fec0::1",
])
def test_internal_addresses_are_blocked(addr):
    assert safety._internal_ip(addr) is not None, f"{addr} should classify as internal"


@pytest.mark.parametrize("addr", [
    "1.1.1.1", "8.8.8.8",          # public IPv4
    "::ffff:8.8.8.8",              # IPv4-mapped IPv6 wrapping a PUBLIC IPv4 — must stay allowed
    "2606:4700:4700::1111",        # public IPv6 (Cloudflare)
])
def test_public_addresses_are_allowed(addr):
    assert safety._internal_ip(addr) is None, f"{addr} should classify as public/allowed"


def test_check_url_blocks_a_host_resolving_to_mapped_metadata(monkeypatch):
    # Host resolves (via DNS) to the IPv4-mapped-IPv6 form of the metadata address.
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("::ffff:169.254.169.254", 0, 0, 0))])
    refusal = safety.check_url("http://sneaky.example.com/latest/meta-data/")
    assert refusal and "internal address" in refusal


def test_check_url_allows_a_public_host(monkeypatch):
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0, 0, 0))])
    assert safety.check_url("http://example.com/") is None


@pytest.mark.parametrize("url", [
    # A backslash terminates the authority for urllib3 (the parser the HTTP client uses) but not for
    # urllib.parse: this URL is 'example.com' to urlparse (public → allowed) and '127.0.0.1' to the
    # socket that actually connects. Validating one and connecting to the other is a straight bypass.
    "http://127.0.0.1:8899\\@example.com/latest/meta-data/",
    "http://example.com\t/",          # tab — urllib3 keeps it in the host, urlparse strips it
    "http://exa\nmple.com/",
])
def test_check_url_refuses_urls_the_two_parsers_read_differently(url, monkeypatch):
    # Resolve everything to a public address, so a refusal can only come from the parser check.
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0, 0, 0))])
    assert safety.check_url(url) is not None, f"{url!r} must be refused as ambiguous"


@pytest.mark.parametrize("url", [
    "http://example.com/", "https://example.com:8443/a/b?c=d",
    "http://[2606:4700:4700::1111]/",     # bracketed IPv6 must NOT read as parser disagreement
    "http://user:pw@example.com/",        # userinfo is stripped by both parsers
    # A literal space in the PATH or QUERY is ordinary (requests percent-encodes it) and cannot move
    # the host — only the authority is checked for suspicious characters. Scanning the whole URL
    # refused these, which broke plain search/file links.
    "http://example.com/search?q=hello world",
    "http://example.com/my file.pdf",
])
def test_check_url_still_allows_well_formed_public_urls(url, monkeypatch):
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0, 0, 0))])
    assert safety.check_url(url) is None, f"{url!r} should be allowed"


def test_pinned_adapter_fails_closed_when_the_url_host_is_not_the_validated_host():
    # The adapter used to fall through to super().send() on a host mismatch — emitting the request
    # unpinned and unvalidated (reachable via a redirect, or via the parser divergence above, since
    # requests rewrites request.url with urllib3's parser before the adapter sees it).
    adapter = safety._PinnedAdapter("example.com", "93.184.216.34", scheme="http")

    class _Req:
        url = "http://127.0.0.1:8899/%5C@example.com/"
        headers = {}

    with pytest.raises(safety.Blocked):
        adapter.send(_Req())


def test_guarded_request_works_over_plain_http(monkeypatch):
    # assert_hostname/server_hostname are HTTPSConnection-only but landed in connection_pool_kw for
    # BOTH connection classes, so every plain-http request through the guard raised
    # TypeError("unexpected keyword argument 'assert_hostname'") — uncaught by callers, meaning the
    # rebinding-proof path never actually ran for http. Exercise it against a real local socket.
    import http.server
    import socketserver
    import threading

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK-HTTP")

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Pin to loopback deliberately: this test is about the adapter, not the address classifier.
    monkeypatch.setattr(safety, "_safe_ip", lambda host: "127.0.0.1")
    try:
        r = safety.guarded_get(f"http://localhost:{port}/", timeout=5)
        assert r.status_code == 200 and r.text == "OK-HTTP"
    finally:
        srv.shutdown(); srv.server_close()


def test_guarded_get_and_check_url_agree_via_shared_classifier(monkeypatch):
    # _safe_ip (used by the rebinding-proof guarded_get) and check_url must use the SAME predicate,
    # so neither path can drift and allow what the other blocks.
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("::ffff:10.0.0.9", 0, 0, 0))])
    assert safety.check_url("http://internal.example.com/") is not None
    with pytest.raises(safety.Blocked):
        safety._safe_ip("internal.example.com")


class _BrowserRequest:
    def __init__(self, url):
        self.url = url


class _BrowserRoute:
    def __init__(self, url):
        self.request = _BrowserRequest(url)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _BrowserContext:
    def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler

    def route_web_socket(self, pattern, handler):
        self.websocket_pattern = pattern
        self.websocket_handler = handler


class _WebSocketRoute:
    def __init__(self, url):
        self.url = url
        self.closed = False
        self.connected = False

    def close(self):
        self.closed = True

    def connect_to_server(self):
        self.connected = True


def test_browser_guard_blocks_internal_subresources(monkeypatch):
    ctx = _BrowserContext()
    monkeypatch.setattr(
        safety, "check_url", lambda url: "blocked" if "127.0.0.1" in url else None)
    livebrowser._install_ssrf_guard(ctx)
    route = _BrowserRoute("http://127.0.0.1:8080/private.js")
    ctx.handler(route)
    assert route.aborted
    assert not route.continued


def test_browser_guard_preserves_public_subresources(monkeypatch):
    ctx = _BrowserContext()
    monkeypatch.setattr(safety, "check_url", lambda url: None)
    livebrowser._install_ssrf_guard(ctx)
    route = _BrowserRoute("https://cdn.example.test/application.css")
    ctx.handler(route)
    assert route.continued
    assert not route.aborted


def test_browser_network_guard_fails_closed(monkeypatch):
    ctx = _BrowserContext()
    monkeypatch.setattr(safety, "check_url", lambda url: (_ for _ in ()).throw(RuntimeError()))
    livebrowser._install_ssrf_guard(ctx)
    route = _BrowserRoute("https://example.test/application.js")
    ctx.handler(route)
    assert route.aborted
    assert not route.continued


def test_browser_guard_blocks_internal_websockets(monkeypatch):
    ctx = _BrowserContext()
    monkeypatch.setattr(
        safety, "check_url", lambda url: "blocked" if "127.0.0.1" in url else None)
    livebrowser._install_ssrf_guard(ctx)
    route = _WebSocketRoute("ws://127.0.0.1:8080/events")
    ctx.websocket_handler(route)
    assert route.closed
    assert not route.connected


def test_browser_guard_preserves_public_websockets(monkeypatch):
    ctx = _BrowserContext()
    monkeypatch.setattr(safety, "check_url", lambda url: None)
    livebrowser._install_ssrf_guard(ctx)
    route = _WebSocketRoute("wss://events.example.test/socket")
    ctx.websocket_handler(route)
    assert route.connected
    assert not route.closed
