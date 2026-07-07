"""Streaming byte-cap in ``safe_fetch.safe_get_capped`` (B4).

A malicious page can stream a multi-GB body; buffering it whole OOMs the
always-on bot. ``safe_get_capped`` must:

* short-circuit when ``Content-Length`` already exceeds the cap (never
  touch the body),
* stop accumulating once the decoded body reaches the cap and surface a
  ``truncated`` flag,
* never buffer redirect-hop bodies,
* pass bodies under the cap through untouched,
* and keep ``safe_get`` (``max_bytes=None``) reading the full body.

All offline: ``httpx.MockTransport`` + a monkeypatched resolver so no DNS
or socket work happens.
"""

from __future__ import annotations

import httpx
import pytest

import unread.util.safe_fetch as sf
from unread.util.safe_fetch import safe_get, safe_get_capped

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _pin_public(monkeypatch):
    """Force every name to resolve to one public IP — no real DNS."""
    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: [_PUBLIC_IP])


class _ChunkStream(httpx.AsyncByteStream):
    """Yields ``chunk`` ``count`` times; records how many bytes were pulled."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.pulled = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self.count):
            self.pulled += len(self.chunk)
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


class _ExplodingStream(httpx.AsyncByteStream):
    """Raises the moment it is iterated — proves the body was never read."""

    def __init__(self) -> None:
        self.iterated = False
        self.closed = False

    async def __aiter__(self):
        self.iterated = True
        raise AssertionError("stream body must not be read")
        yield b""  # pragma: no cover — unreachable, makes this an async-gen

    async def aclose(self) -> None:
        self.closed = True


async def test_over_cap_body_truncated():
    """A body larger than the cap is sliced to the cap and flagged."""
    stream = _ChunkStream(b"x" * 1000, count=1000)  # ~1 MB total

    def handler(request):
        return httpx.Response(200, headers=[("content-type", "text/html")], stream=stream)

    resp, truncated = await safe_get_capped(
        "http://example.com/",
        timeout_sec=5,
        max_bytes=2000,
        transport=httpx.MockTransport(handler),
    )
    assert truncated is True
    assert len(resp.content) == 2000
    # Early stop: we must not have drained the whole ~1 MB stream.
    assert stream.pulled < 10_000
    assert stream.closed is True


async def test_content_length_over_cap_skips_body():
    """Declared Content-Length over the cap → body is never read."""
    stream = _ExplodingStream()

    def handler(request):
        return httpx.Response(
            200,
            headers=[("content-type", "text/html"), ("content-length", "999999999")],
            stream=stream,
        )

    resp, truncated = await safe_get_capped(
        "http://example.com/",
        timeout_sec=5,
        max_bytes=2000,
        transport=httpx.MockTransport(handler),
    )
    assert truncated is True
    assert resp.content == b""
    assert stream.iterated is False
    assert stream.closed is True


async def test_redirect_body_not_buffered():
    """Redirect-hop bodies are closed, never iterated."""
    redirect_stream = _ExplodingStream()
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers=[("location", "http://example.com/final"), ("content-type", "text/html")],
                stream=redirect_stream,
            )
        return httpx.Response(200, headers=[("content-type", "text/html")], content=b"final-body")

    resp, truncated = await safe_get_capped(
        "http://example.com/",
        timeout_sec=5,
        max_bytes=2_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert resp.content == b"final-body"
    assert truncated is False
    assert redirect_stream.iterated is False
    assert redirect_stream.closed is True
    assert len(calls) == 2


async def test_under_cap_passthrough():
    """A body under the cap passes through untouched, not flagged."""
    body = b"<html>ok</html>"

    def handler(request):
        return httpx.Response(200, headers=[("content-type", "text/html")], content=body)

    resp, truncated = await safe_get_capped(
        "http://example.com/",
        timeout_sec=5,
        max_bytes=2_000_000,
        transport=httpx.MockTransport(handler),
    )
    assert truncated is False
    assert resp.content == body
    assert resp.text == "<html>ok</html>"
    assert resp.status_code == 200


async def test_safe_get_wrapper_reads_full_body():
    """``safe_get`` (max_bytes=None) buffers the whole body — no cap."""
    big = b"y" * 5_000_000

    def handler(request):
        return httpx.Response(200, headers=[("content-type", "text/html")], content=big)

    resp = await safe_get(
        "http://example.com/",
        timeout_sec=5,
        transport=httpx.MockTransport(handler),
    )
    assert len(resp.content) == 5_000_000


async def test_body_exactly_at_cap_not_truncated():
    """A body whose size equals the cap is complete, not truncated."""
    body = b"z" * 2000

    def handler(request):
        return httpx.Response(200, headers=[("content-type", "text/html")], content=body)

    resp, truncated = await safe_get_capped(
        "http://example.com/",
        timeout_sec=5,
        max_bytes=2000,
        transport=httpx.MockTransport(handler),
    )
    assert truncated is False
    assert len(resp.content) == 2000
