"""Price models the catalog doesn't know, from OpenRouter's public model list.

A model picked by hand (`openai/gpt-6-luna-pro`) or released after the
installed catalog has no row in `config.toml` `[pricing]` nor in
`catalog.json`, so every run on it used to cost "$0". On the first such
call `ensure()` fetches `GET https://openrouter.ai/api/v1/models` (public,
no key), and remembers the matching price under
`~/.unread/storage/live_prices.json` so later processes price the model
without the network.

Prices from here are estimates: OpenRouter quotes its own rate, which
usually equals the vendor's but can differ. `[pricing]` and the curated
catalog always win; this is only the last resort before "unknown".

Failure is quiet: no network or no match logs a warning, and the model
stays unpriced. A miss is remembered for the process, so an unknown model
costs one request per process, not one per call.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx

from unread.config import ChatPricing
from unread.core.paths import storage_dir
from unread.util.logging import get_logger

log = get_logger(__name__)

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
_MAX_BYTES = 20_000_000

_prices: dict[str, ChatPricing] | None = None
_misses: set[str] = set()
_lock = asyncio.Lock()


def cache_path() -> Path:
    return storage_dir() / "live_prices.json"


def _load() -> dict[str, ChatPricing]:
    global _prices
    if _prices is None:
        _prices = {}
        try:
            raw = json.loads(cache_path().read_text(encoding="utf-8"))
            for model, row in raw.items():
                _prices[model] = ChatPricing(**row)
        except (OSError, ValueError, TypeError) as e:
            if not isinstance(e, FileNotFoundError):
                log.warning("live_pricing.cache_unreadable", path=str(cache_path()), error=str(e))
    return _prices


def _save() -> None:
    path = cache_path()
    data = {m: p.model_dump() for m, p in _load().items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("live_pricing.cache_write_failed", path=str(path), error=str(e))


def lookup(model: str) -> ChatPricing | None:
    """Remembered price for `model`. Never touches the network."""
    return _load().get(model)


def candidate_ids(model: str) -> list[str]:
    """OpenRouter ids that may name `model`."""
    from unread.ai.models import provider_for_model

    out = [model]
    if "/" not in model:
        vendor = provider_for_model(model)
        if vendor in ("openai", "anthropic", "google"):
            bare = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model) if vendor == "anthropic" else model
            out.append(f"{vendor}/{bare}")
    return out


def _per_million(value: object) -> float | None:
    try:
        price = float(value) * 1_000_000  # OpenRouter quotes $ / token
    except (TypeError, ValueError):
        return None
    # Negative = "variable" (routers like openrouter/auto).
    return round(price, 6) if price >= 0 else None


def parse_pricing(row: dict) -> ChatPricing | None:
    pricing = row.get("pricing") or {}
    inp = _per_million(pricing.get("prompt"))
    out = _per_million(pricing.get("completion"))
    if inp is None or out is None:
        return None
    cached = _per_million(pricing.get("input_cache_read"))
    return ChatPricing(input=inp, cached_input=inp if cached is None else cached, output=out)


async def _fetch(timeout: float) -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(OPENROUTER_MODELS)
        resp.raise_for_status()
        if len(resp.content) > _MAX_BYTES:
            raise ValueError(f"response too large: {len(resp.content)} bytes")
        data = resp.json().get("data") or []
    return {m["id"]: m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)}


async def ensure(model: str, *, timeout: float = 10.0) -> ChatPricing | None:
    """Price for `model`, fetching it from OpenRouter once if it isn't remembered."""
    if not model:
        return None
    if (hit := lookup(model)) is not None:
        return hit
    if model in _misses:
        return None
    async with _lock:
        if (hit := lookup(model)) is not None:
            return hit
        if model in _misses:
            return None
        try:
            remote = await _fetch(timeout)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("live_pricing.fetch_failed", model=model, error=str(e))
            _misses.add(model)
            return None
        for rid in candidate_ids(model):
            row = remote.get(rid)
            price = parse_pricing(row) if row else None
            if price is not None:
                _load()[model] = price
                _save()
                log.info("live_pricing.learned", model=model, openrouter_id=rid, **price.model_dump())
                return price
        log.warning("live_pricing.no_match", model=model)
        _misses.add(model)
        return None


def reset() -> None:
    """Forget in-memory state (tests)."""
    global _prices
    _prices = None
    _misses.clear()
