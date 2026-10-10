"""Pricing models the catalog doesn't know, from OpenRouter's list (fetch mocked)."""

from __future__ import annotations

import pytest

from unread.ai import live_pricing
from unread.config import get_settings
from unread.util.pricing import chat_cost

REMOTE = {
    "openai/gpt-6-luna-pro": {
        "id": "openai/gpt-6-luna-pro",
        "pricing": {"prompt": "0.000002", "completion": "0.000008", "input_cache_read": "0.0000002"},
    },
    "openrouter/auto": {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(live_pricing, "cache_path", lambda: tmp_path / "live_prices.json")
    live_pricing.reset()
    calls = []

    async def fake_fetch(timeout):
        calls.append(timeout)
        return REMOTE

    monkeypatch.setattr(live_pricing, "_fetch", fake_fetch)
    yield calls
    live_pricing.reset()


async def test_unknown_model_is_priced_and_remembered(_fresh):
    assert chat_cost("openai/gpt-6-luna-pro", 1_000_000, 0, 0) is None
    price = await live_pricing.ensure("openai/gpt-6-luna-pro")
    assert (price.input, price.cached_input, price.output) == (2.0, 0.2, 8.0)
    assert chat_cost("openai/gpt-6-luna-pro", 26599, 7215, 4607, settings=get_settings()) == pytest.approx(
        (19384 * 2 + 7215 * 0.2 + 4607 * 8) / 1e6
    )
    # A new process reads it from disk without fetching.
    live_pricing.reset()
    assert live_pricing.lookup("openai/gpt-6-luna-pro") is not None
    assert len(_fresh) == 1


async def test_miss_and_variable_price_fetch_once(_fresh):
    assert await live_pricing.ensure("nope/never") is None
    assert await live_pricing.ensure("nope/never") is None
    assert await live_pricing.ensure("openrouter/auto") is None
    assert len(_fresh) == 2  # one per unknown model, not per call


def test_bare_vendor_id_maps_to_openrouter_id():
    assert "anthropic/claude-opus-5.5" in live_pricing.candidate_ids("claude-opus-5-5")
