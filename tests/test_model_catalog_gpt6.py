"""Catalog refresh 2026-10-10: GPT-6, Claude Haiku 5.5, Gemini 3.8 Flash.

A model missing from the catalog fails quietly in three ways at once:
cost reports $0, the chunker falls back to a 128k window (so an hour-long
video gets map-reduced for nothing), and the temperature guard can send
a sampling parameter the model 400s on.
"""

from __future__ import annotations

import pytest

from unread.ai.models import find_model, rejects_temperature
from unread.analyzer.chunker import model_context_window

GPT6 = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
}


@pytest.mark.parametrize("model_id", list(GPT6))
@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_gpt6_family_is_priced(model_id, prefix) -> None:
    m = find_model(prefix + model_id)
    assert m is not None
    assert (m.input_price, m.cached_price, m.output_price) == GPT6[model_id]
    assert m.context_window == 1_050_000
    assert m.reasoning is True


@pytest.mark.parametrize("model_id", ["claude-haiku-5-5", "anthropic/claude-haiku-5.5"])
def test_haiku_5_5_is_priced(model_id) -> None:
    m = find_model(model_id)
    assert m is not None
    assert (m.input_price, m.cached_price, m.output_price) == (0.10, 0.01, 0.50)
    assert m.context_window == 1_000_000


@pytest.mark.parametrize("model_id", ["gemini-3.8-flash", "google/gemini-3.8-flash"])
def test_gemini_3_8_flash_records_the_list_rate(model_id) -> None:
    """The $0.75/$3.75 intro rate ends 2026-12-31; err high, not low."""
    m = find_model(model_id)
    assert m is not None
    assert (m.input_price, m.output_price) == (1.50, 7.50)


def test_gpt6_luna_gets_its_real_context_window() -> None:
    assert model_context_window("openai/gpt-6-luna") >= 1_000_000


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-haiku-5-5",
        "anthropic/claude-haiku-5.5",
        # Ids the catalog doesn't know yet still get the guard.
        "gpt-6-luna-pro",
        "openai/gpt-7-luna",
        "gpt-10-sol",
        "claude-haiku-5-6",
    ],
)
def test_new_families_drop_temperature(model_id) -> None:
    assert rejects_temperature(model_id) is True


@pytest.mark.parametrize("model_id", ["gpt-4o", "gpt-4o-mini", "claude-haiku-4-5", "claude-sonnet-4-6"])
def test_older_models_keep_temperature(model_id) -> None:
    assert rejects_temperature(model_id) is False
