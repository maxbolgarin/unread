"""Keep the model catalog (names, prices, context windows) fresh without a release.

The wheel ships `unread/ai/catalog.json`. A long-running bot would
otherwise price every run with whatever that file said on release day,
so this module fetches the same file from the repo's `main` branch
(`ai.catalog_url`), validates it, caches it under
`~/.unread/storage/model_catalog.json`, and overlays it on the bundled
catalog (`unread.ai.models.apply_overlay`).

Who refreshes:
  * `unread bot` — `refresh_loop` runs for the bot's lifetime, checking
    every `ai.catalog_refresh_hours` (default 24, 0 disables).
  * `unread update` — refreshes once, alongside the PyPI check.
  * every other command — only *reads* the cached copy (`load_cached`),
    so a CLI run never waits on the network for this.

Failure is always quiet: no network, a 404, a malformed or older file —
the catalog already in force stays, and a warning is logged.

What lands in `main`'s `catalog.json` is reviewed: the weekly
`model-prices` workflow opens a PR, it never pushes. So the remote file
is no more trusted than the code itself, and `parse_catalog` still
rejects anything out of shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from unread.ai.models import CatalogError, apply_overlay, parse_catalog
from unread.core.paths import storage_dir
from unread.util.logging import get_logger

log = get_logger(__name__)

# The whole catalog is ~15 KB; anything past this isn't our file.
_MAX_BYTES = 1_000_000


def cache_path() -> Path:
    return storage_dir() / "model_catalog.json"


def _apply(raw: bytes | str, *, source: str) -> str | None:
    """Validate + overlay. Returns the catalog date if it was applied."""
    try:
        updated, registry = parse_catalog(json.loads(raw))
    except (ValueError, CatalogError) as e:  # JSONDecodeError is a ValueError
        log.warning("catalog.invalid", source=source, error=str(e))
        return None
    if not apply_overlay(updated, registry):
        log.info("catalog.older_than_bundled", source=source, updated=updated)
        return None
    return updated


def load_cached() -> str | None:
    """Overlay the last downloaded catalog, if any. Never touches the network."""
    path = cache_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _apply(raw, source=str(path))


def cache_age_hours() -> float | None:
    try:
        return (time.time() - cache_path().stat().st_mtime) / 3600
    except OSError:
        return None


async def refresh(url: str, *, timeout: float = 10.0) -> str | None:
    """Fetch `url`, apply it, and cache it. Returns the applied catalog date.

    Only `https://` URLs are fetched. The cache is written only after the
    file validates, so a bad download never replaces a good cached copy.
    """
    if not url.startswith("https://"):
        log.warning("catalog.url_not_https", url=url)
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as e:
        log.warning("catalog.fetch_failed", url=url, error=str(e))
        return None
    if len(raw) > _MAX_BYTES:
        log.warning("catalog.too_large", url=url, size=len(raw))
        return None
    updated = _apply(raw, source=url)
    if updated is None:
        return None
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    except OSError as e:
        # Applied in memory already; only the next process misses out.
        log.warning("catalog.cache_write_failed", path=str(path), error=str(e))
    log.info("catalog.refreshed", url=url, updated=updated)
    return updated


async def refresh_loop(url: str, every_hours: float) -> None:
    """Bot background task: refresh now if the cache is stale, then every N hours.

    Starts from the cached copy so a restart doesn't need the network to
    get back to the last known prices. Cancelled by the bot's shutdown.
    """
    if not url or every_hours <= 0:
        return
    load_cached()
    interval = every_hours * 3600
    age = cache_age_hours()
    delay = 0.0 if age is None else max(0.0, interval - age * 3600)
    while True:
        if delay:
            await asyncio.sleep(delay)
        try:
            await refresh(url)
        except Exception as e:  # never let a refresh bug kill the bot
            log.warning("catalog.refresh_crashed", error=str(e))
        delay = interval
