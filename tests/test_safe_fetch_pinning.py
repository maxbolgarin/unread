"""IP-pinning against DNS-rebinding TOCTOU in ``safe_fetch`` (B5).

The host is validated once, then the connection is *pinned* to the
validated IP: the request URL's netloc becomes the IP, the ``Host``
header keeps the original name, and (for HTTPS) ``sni_hostname`` in the
request extensions drives both SNI and certificate verification. Every
redirect hop re-validates and re-pins. IP-literal URLs skip pinning.

Pure builder tests cover ``_pin_url`` / ``_host_header``; MockTransport
tests assert the request that reaches the wire has the IP as its host
while the ``Host`` header (and SNI) carry the real name. All offline.
"""

from __future__ import annotations

import httpx
import pytest

import unread.util.safe_fetch as sf
from unread.util.safe_fetch import (
    _host_header,
    _pin_url,
    is_public_address,
    resolve_public_ips,
    safe_get_capped,
)

# ---------------------------------------------------------------------------
# Pure builder tests
# ---------------------------------------------------------------------------


def test_pin_url_preserves_path_query_fragment_port():
    out = _pin_url("http://example.com:8443/a/b?x=1&y=2#frag", "1.2.3.4")
    assert out == "http://1.2.3.4:8443/a/b?x=1&y=2#frag"


def test_pin_url_default_port_dropped():
    out = _pin_url("https://example.com/path", "1.2.3.4")
    assert out == "https://1.2.3.4/path"


def test_pin_url_ipv6_bracketed():
    out = _pin_url("https://example.com/x", "2606:4700::1111")
    assert out == "https://[2606:4700::1111]/x"


def test_pin_url_ipv6_with_port():
    out = _pin_url("http://example.com:9000/y?z=1", "2606:4700::1111")
    assert out == "http://[2606:4700::1111]:9000/y?z=1"


def test_host_header_name_only_for_default_port():
    assert _host_header("https://example.com/x") == "example.com"
    assert _host_header("http://example.com/x") == "example.com"


def test_host_header_includes_nondefault_port():
    assert _host_header("https://example.com:8443/x") == "example.com:8443"


def test_is_public_address_bool_wrapper():
    assert is_public_address("1.1.1.1") is True
    assert is_public_address("127.0.0.1") is False


def test_resolve_public_ips_ip_literal():
    assert resolve_public_ips("8.8.8.8") == ["8.8.8.8"]
    assert resolve_public_ips("10.0.0.1") == []


# ---------------------------------------------------------------------------
# Transport-level pinning
# ---------------------------------------------------------------------------


async def test_https_request_pinned_to_ip_with_host_and_sni(monkeypatch):
    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: ["93.184.216.34"])
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"ok")

    resp, _ = await safe_get_capped(
        "https://example.com/page",
        timeout_sec=5,
        max_bytes=1_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert resp.content == b"ok"
    assert seen["host"] == "93.184.216.34"
    assert seen["host_header"] == "example.com"
    assert seen["sni"] == "example.com"


async def test_http_request_pinned_no_sni(monkeypatch):
    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: ["93.184.216.34"])
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"ok")

    await safe_get_capped(
        "http://example.com/page",
        timeout_sec=5,
        max_bytes=1_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert seen["host"] == "93.184.216.34"
    assert seen["host_header"] == "example.com"
    # No TLS on plain http → no SNI override.
    assert seen["sni"] is None


async def test_ip_literal_url_skips_pinning():
    seen = {}

    def handler(request):
        seen["host"] = request.url.host
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"ok")

    await safe_get_capped(
        "http://93.184.216.34/page",
        timeout_sec=5,
        max_bytes=1_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert seen["host"] == "93.184.216.34"
    # IP-literal: nothing to pin, no SNI override, httpx auto-Host is the IP.
    assert seen["sni"] is None


async def test_redirect_hop_revalidated_and_repinned(monkeypatch):
    # First host → IP1, redirect target host → IP2. Each hop re-pins.
    ip_map = {"a.example": "11.11.11.11", "b.example": "22.22.22.22"}
    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: [ip_map[host]])
    seen: list[tuple[str, str | None]] = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("host")))
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers=[("location", "https://b.example/final"), ("content-type", "text/html")],
                content=b"",
            )
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"done")

    resp, _ = await safe_get_capped(
        "http://a.example/start",
        timeout_sec=5,
        max_bytes=1_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert resp.content == b"done"
    assert seen[0] == ("11.11.11.11", "a.example")
    assert seen[1] == ("22.22.22.22", "b.example")


async def test_http_to_https_redirect_pin_and_sni_survive(monkeypatch):
    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: ["44.44.44.44"])
    seen: list[dict] = []

    def handler(request):
        seen.append(
            {
                "scheme": request.url.scheme,
                "host": request.url.host,
                "host_header": request.headers.get("host"),
                "sni": request.extensions.get("sni_hostname"),
            }
        )
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers=[("location", "https://secure.example/x"), ("content-type", "text/html")],
                content=b"",
            )
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"secure")

    resp, _ = await safe_get_capped(
        "http://secure.example/x",
        timeout_sec=5,
        max_bytes=1_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert resp.content == b"secure"
    # http hop: no SNI. https hop: pinned IP host, real Host + SNI.
    assert seen[0]["scheme"] == "http"
    assert seen[0]["sni"] is None
    assert seen[1]["scheme"] == "https"
    assert seen[1]["host"] == "44.44.44.44"
    assert seen[1]["host_header"] == "secure.example"
    assert seen[1]["sni"] == "secure.example"


async def test_redirect_to_private_ip_blocked(monkeypatch):
    """A rebinding redirect to a private host is rejected mid-chain."""
    from unread.util.safe_fetch import BlockedURLError

    def resolver(host):
        return [] if host == "evil.example" else ["55.55.55.55"]

    monkeypatch.setattr(sf, "resolve_public_ips", resolver)

    def handler(request):
        return httpx.Response(
            302,
            headers=[("location", "http://evil.example/meta"), ("content-type", "text/html")],
            content=b"",
        )

    with pytest.raises(BlockedURLError):
        await safe_get_capped(
            "http://good.example/start",
            timeout_sec=5,
            max_bytes=1_000_000,
            transport=httpx.MockTransport(handler),
        )
