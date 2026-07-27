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

from oceano import safety  # noqa: E402


@pytest.mark.parametrize("addr", [
    "127.0.0.1",                   # loopback
    "10.0.0.5", "192.168.1.1",     # RFC1918
    "169.254.169.254",             # cloud metadata (link-local)
    "0.0.0.0", "::1",              # unspecified / IPv6 loopback
    "::ffff:169.254.169.254",      # IPv4-mapped IPv6 wrapping metadata — the bug
    "::ffff:10.0.0.5",             # IPv4-mapped IPv6 wrapping RFC1918
    "2002:a9fe:aafe::",            # 6to4 wrapping 169.254.170.254
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


def test_guarded_get_and_check_url_agree_via_shared_classifier(monkeypatch):
    # _safe_ip (used by the rebinding-proof guarded_get) and check_url must use the SAME predicate,
    # so neither path can drift and allow what the other blocks.
    monkeypatch.setattr(safety.socket, "getaddrinfo",
                        lambda host, *a, **k: [(0, 0, 0, "", ("::ffff:10.0.0.9", 0, 0, 0))])
    assert safety.check_url("http://internal.example.com/") is not None
    with pytest.raises(safety.Blocked):
        safety._safe_ip("internal.example.com")
