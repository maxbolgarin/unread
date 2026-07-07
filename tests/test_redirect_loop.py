"""HTTP redirect loops surface cleanly, not as raw HTTPError.

httpx caps redirect chains at 20 by default and raises
`httpx.TooManyRedirects` (a subclass of `httpx.HTTPError`). Two paths
need to handle this distinctly so users can grep for `redirect_loop`:

  - `unread/website/content.py:_http_get` — raises a typed
    `WebsiteFetchError` whose message names the loop, not just the
    generic "Fetch failed".
  - `unread/enrich/link.py:_fetch` — logs `enrich.link.redirect_loop`
    and returns None so the rest of the enrich phase keeps going.

`safe_fetch` now streams via ``client.send(build_request(...), stream=True)``
and pins the connection to a validated IP, so these tests mock ``send``
(not ``get``) and monkeypatch the resolver to stay offline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import unread.util.safe_fetch as sf


def _fake_toomanyredirects_client() -> AsyncMock:
    """An AsyncClient stand-in whose ``send`` raises TooManyRedirects."""
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False
    # build_request is sync and must return a real Request.
    fake_client.build_request = MagicMock(return_value=httpx.Request("GET", "https://example.com/loop"))
    fake_client.send = AsyncMock(side_effect=httpx.TooManyRedirects("21 redirects"))
    return fake_client


@pytest.mark.asyncio
async def test_website_fetch_raises_typed_error_on_redirect_loop(monkeypatch) -> None:
    """`_http_get` → WebsiteFetchError mentioning a redirect loop."""
    from unread.website.content import WebsiteFetchError, _http_get

    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: ["93.184.216.34"])

    with (
        patch("unread.util.safe_fetch.httpx.AsyncClient", return_value=_fake_toomanyredirects_client()),
        pytest.raises(WebsiteFetchError, match="redirect"),
    ):
        await _http_get(
            "https://example.com/loop",
            timeout_sec=5,
            user_agent="ua",
            max_bytes=1_000_000,
        )


@pytest.mark.asyncio
async def test_link_enricher_returns_none_on_redirect_loop(caplog, monkeypatch) -> None:
    """`_fetch` → returns None and logs the typed `redirect_loop` key."""
    import logging

    from unread.enrich.link import _fetch

    monkeypatch.setattr(sf, "resolve_public_ips", lambda host: ["93.184.216.34"])

    with (
        patch("unread.util.safe_fetch.httpx.AsyncClient", return_value=_fake_toomanyredirects_client()),
        caplog.at_level(logging.DEBUG, logger="unread.enrich.link"),
    ):
        result = await _fetch("https://example.com/loop", timeout_sec=5)

    assert result is None
