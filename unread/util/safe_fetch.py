"""SSRF guard for outbound URL fetches.

The link enricher (``unread.enrich.link``) and the website analyzer
(``unread.website.content``) follow redirects on user-supplied URLs.
Without validation, a malicious page could redirect to:

* ``http://169.254.169.254/...`` — AWS / GCP / Azure instance-metadata
  endpoint, leaking IAM credentials.
* ``http://localhost:N`` / ``http://127.0.0.1:N`` — local services
  (admin panels, dev servers, dashboards).
* ``http://10.x.y.z`` / ``http://192.168.x.y`` — internal LAN hosts.

The fetched body is then summarized by an LLM and pasted into the
user's report — exfiltrating private data.

This module exposes:

* :func:`resolve_public_ips` — DNS resolves a host and returns its
  addresses only when *every* address is public (loopback / RFC1918 /
  link-local / unique-local / unspecified are rejected). A host that
  returns a mix is treated as private (DNS-rebinding guard).
* :func:`is_public_address` — a bool wrapper over ``resolve_public_ips``.
* :func:`safe_get_capped` / :func:`safe_get` — drop-in GET helpers that
  validate the initial URL and every redirect hop, refuse non-http(s)
  schemes, stream the body under a hard byte cap, and **pin** each
  connection to the validated IP.
* :func:`safe_validate` — a validate-only gate for callers that delegate
  the actual fetch to a third-party library.

DNS-rebinding (TOCTOU) hardening
--------------------------------

``getaddrinfo`` validation alone is racy: httpx re-resolves the host
independently at connect time, so a rebinding attacker can pass
validation with a public ``A`` record and then serve a private one to
the real connection. :func:`safe_get_capped` closes this by *pinning*:
after validating, it rewrites the request URL's netloc to the validated
IP, keeps the original hostname in the ``Host`` header, and (for HTTPS)
sets ``sni_hostname`` in the request extensions so both the TLS SNI and
the certificate hostname check still use the real name. Each redirect
hop is re-validated and re-pinned.

**Residual risk — :func:`safe_validate`.** The trafilatura path
(``unread.website.content``) validates via :func:`safe_validate` but then
hands the URL to ``trafilatura.fetch_url``, which re-resolves the host
itself. That fetch *cannot* be pinned from here, so it remains vulnerable
to DNS rebinding between validation and connect. Prefer :func:`safe_get`
/ :func:`safe_get_capped` wherever the body is fetched in-process.

Both link.py and website/content.py call into this module instead of
constructing ``httpx.AsyncClient`` directly.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from unread.util.logging import get_logger

log = get_logger(__name__)


class BlockedURLError(Exception):
    """Raised when a target URL resolves to a private / forbidden address."""


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
DEFAULT_MAX_REDIRECTS: int = 10


def _addr_is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except (TypeError, ValueError):
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return False
    return not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except (TypeError, ValueError):
        return False
    return True


def resolve_public_ips(host: str) -> list[str]:
    """Resolve ``host`` and return its addresses iff *all* are public.

    Returns the list of resolved IP strings when every address is public;
    an empty list when the host is non-public, unresolvable, or returns a
    *mix* of public and private addresses. The all-or-nothing rule is the
    DNS-rebinding guard: a mixed result could otherwise smuggle a private
    target past validation.

    For an IP literal, returns ``[host]`` when public, else ``[]``.
    """
    if _is_ip_literal(host):
        return [host] if _addr_is_public(host) else []

    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return []
    # Explicit private TLDs by convention.
    if host.endswith(".local") or host.endswith(".internal"):
        return []

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    if not infos:
        return []
    ips: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if not _addr_is_public(addr):
            return []
        ips.append(addr)
    return ips


def is_public_address(host: str) -> bool:
    """True iff ``host`` resolves *exclusively* to public addresses."""
    return bool(resolve_public_ips(host))


def _prefer_ipv4(ips: list[str]) -> str:
    """Pick the pinning target — prefer an IPv4 address when present."""
    for ip in ips:
        try:
            if ipaddress.ip_address(ip).version == 4:
                return ip
        except ValueError:
            continue
    return ips[0]


def _ensure_safe_url(url: str) -> tuple[str, str | None]:
    """Validate ``url`` and choose a pinned IP.

    Returns ``(hostname, pinned_ip)``. ``pinned_ip`` is ``None`` for
    IP-literal URLs (nothing to pin) and the validated address to connect
    to for name-based URLs. Raises :class:`BlockedURLError` on a
    forbidden scheme, a missing host, or a non-public target.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"refusing non-http(s) scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise BlockedURLError(f"URL has no hostname: {url!r}")

    if _is_ip_literal(host):
        if not _addr_is_public(host):
            raise BlockedURLError(f"refusing fetch to non-public host {host!r}")
        # Already an IP — DNS rebinding is not applicable; skip pinning.
        return host, None

    ips = resolve_public_ips(host)
    if not ips:
        raise BlockedURLError(f"refusing fetch to non-public host {host!r}")
    return host, _prefer_ipv4(ips)


def _pin_url(url: str, pinned_ip: str) -> str:
    """Rebuild ``url`` with its netloc host replaced by ``pinned_ip``.

    Preserves scheme, port, path, params, query, fragment and any
    userinfo. IPv6 literals are bracketed.
    """
    parsed = urlparse(url)
    host_part = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = host_part
    if parsed.port is not None:
        netloc = f"{host_part}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _host_header(url: str) -> str:
    """The ``Host`` header value for ``url`` — original name, port iff non-default."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parsed.port
    default = _DEFAULT_PORTS.get(parsed.scheme.lower())
    if port is not None and port != default:
        return f"{host}:{port}"
    return host


def _build_pinned_request(
    client: httpx.AsyncClient,
    url: str,
    hostname: str,
    pinned_ip: str | None,
) -> httpx.Request:
    """Build a GET request that connects to ``pinned_ip`` but presents ``hostname``.

    For IP-literal URLs (``pinned_ip is None``) the request is built as-is
    — httpx's auto ``Host`` (the IP) is correct and there is nothing to
    pin. For name-based URLs the netloc becomes the IP, ``Host`` carries
    the real name, and HTTPS additionally sets ``sni_hostname`` so SNI and
    certificate verification use the real hostname.
    """
    if pinned_ip is None:
        return client.build_request("GET", url)

    pinned_url = _pin_url(url, pinned_ip)
    req_headers = {"Host": _host_header(url)}
    extensions: dict[str, Any] = {}
    if urlparse(url).scheme.lower() == "https":
        extensions["sni_hostname"] = hostname
    return client.build_request("GET", pinned_url, headers=req_headers, extensions=extensions)


async def _read_capped(resp: httpx.Response, max_bytes: int | None) -> tuple[bytes, bool]:
    """Read a response body, capping at ``max_bytes`` (``None`` → no cap).

    Returns ``(body, truncated)``. When ``max_bytes`` is set:

    * a declared ``Content-Length`` over the cap short-circuits — the body
      is closed unread and ``(b"", True)`` is returned;
    * otherwise decoded bytes accumulate until they exceed the cap, at
      which point iteration stops (bounding memory to ~cap + one chunk),
      the body is sliced to the cap and ``truncated`` is ``True``.
    """
    if max_bytes is None:
        body = await resp.aread()
        return body, False

    content_length = resp.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                await resp.aclose()
                return b"", True
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            truncated = True
            break
    await resp.aclose()
    body = b"".join(chunks)
    if len(body) > max_bytes:
        body = body[:max_bytes]
    return body, truncated


def _rebuild(resp: httpx.Response, body: bytes, final_url: str) -> httpx.Response:
    """Rebuild a fully-read :class:`httpx.Response` from capped ``body``.

    The original streaming response is drained/closed by the caller; this
    returns a fresh, non-streaming response whose ``.content`` / ``.text``
    are the capped bytes. Content-coding / length headers are dropped
    because ``body`` is already decoded and re-sized.
    """
    headers = httpx.Headers(resp.headers)
    for stale in ("content-encoding", "content-length", "transfer-encoding"):
        if stale in headers:
            del headers[stale]
    return httpx.Response(
        status_code=resp.status_code,
        headers=headers,
        content=body,
        request=httpx.Request("GET", final_url),
    )


async def safe_get_capped(
    url: str,
    *,
    timeout_sec: float,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_bytes: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[httpx.Response, bool]:
    """GET ``url`` with per-hop SSRF validation, IP pinning and a byte cap.

    Returns ``(response, truncated)`` where ``response`` is a rebuilt,
    fully-read response carrying at most ``max_bytes`` of body and
    ``truncated`` says whether the body was cut short (``False`` when
    ``max_bytes`` is ``None``).

    Implementation notes:

    * ``follow_redirects=False``; the chain is walked manually so each hop
      is validated *and* re-pinned before the next request.
    * every hop is sent with ``stream=True`` — redirect bodies are closed
      unread and the final body is streamed under the cap.
    * ``max_redirects`` defaults to 10 (tighter than httpx's 20).
    * ``transport`` injects an ``httpx.AsyncBaseTransport`` (test seam).

    Raises :class:`BlockedURLError` when validation fails; other errors
    propagate as the underlying httpx exceptions.
    """
    hostname, pinned_ip = _ensure_safe_url(url)
    client_kwargs: dict[str, Any] = {
        "timeout": timeout_sec,
        "follow_redirects": False,
        "headers": dict(headers or {}),
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        current = url
        cur_host = hostname
        cur_pin = pinned_ip
        for _ in range(max_redirects + 1):
            request = _build_pinned_request(client, current, cur_host, cur_pin)
            resp = await client.send(request, stream=True)
            if resp.is_redirect:
                target = resp.headers.get("location")
                await resp.aclose()  # never buffer redirect bodies
                if not target:
                    return _rebuild(resp, b"", current), False
                # Resolve relative redirects against the current URL.
                next_url = str(httpx.URL(current).join(target))
                cur_host, cur_pin = _ensure_safe_url(next_url)
                current = next_url
                continue
            body, truncated = await _read_capped(resp, max_bytes)
            return _rebuild(resp, body, current), truncated
        raise BlockedURLError(f"redirect chain exceeded {max_redirects} hops for {url!r}")


async def safe_get(
    url: str,
    *,
    timeout_sec: float,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Uncapped GET — thin wrapper over :func:`safe_get_capped`.

    Buffers the full body (``max_bytes=None``); kept for callers that
    apply their own size handling. Validates the initial URL and every
    redirect hop and pins each connection to the validated IP.
    """
    resp, _ = await safe_get_capped(
        url,
        timeout_sec=timeout_sec,
        headers=headers,
        max_redirects=max_redirects,
        max_bytes=None,
        transport=transport,
    )
    return resp


def safe_validate(url: str) -> None:
    """Assert ``url`` is safe to fetch; raise :class:`BlockedURLError` if not.

    Use when the caller delegates the fetch to a third-party library (e.g.
    ``trafilatura.fetch_url``) that doesn't expose a redirect hook. Note
    the residual DNS-rebinding risk documented in the module docstring:
    this validates but cannot *pin* the subsequent connection.
    """
    _ensure_safe_url(url)


__all__: tuple[str, ...] = (
    "BlockedURLError",
    "is_public_address",
    "resolve_public_ips",
    "safe_get",
    "safe_get_capped",
    "safe_validate",
)
