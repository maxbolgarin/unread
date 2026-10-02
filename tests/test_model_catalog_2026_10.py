"""October 2026 catalog refresh: Claude 5.5, Gemini 3.7 Flash, OpenRouter ids.

OpenRouter spells Claude versions with dots (`anthropic/claude-opus-5.5`);
Anthropic's own API uses dashes (`claude-opus-5-5`). Both must resolve, and
Claude 5.x must never be sent a `temperature` — it 400s.
"""

from __future__ import annotations

import pytest

from unread.ai.models import find_model, models_for_provider, rejects_temperature


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "gemini-3.7-flash",
        "anthropic/claude-opus-5.5",
        "anthropic/claude-sonnet-5.5",
        "google/gemini-3.7-flash",
    ],
)
def test_new_models_are_in_the_catalog(model_id) -> None:
    assert find_model(model_id) is not None


def test_opus_5_5_pricing() -> None:
    m = find_model("claude-opus-5-5")
    assert (m.input_price, m.cached_price, m.output_price) == (4.00, 0.20, 20.00)


@pytest.mark.parametrize(
    ("alias", "upstream"),
    [
        ("anthropic/claude-opus-4-7", "claude-opus-4-7"),
        ("anthropic/claude-opus-4.7", "claude-opus-4-7"),
        ("openai/gpt-5.5", "gpt-5.5"),
        ("google/gemini-2.5-flash", "gemini-2.5-flash"),
    ],
)
def test_vendor_alias_falls_back_to_the_upstream_row(alias, upstream) -> None:
    """Keeps pricing for OpenRouter ids the picker no longer lists."""
    assert find_model(alias) is find_model(upstream)


def test_unknown_vendor_alias_is_still_unknown() -> None:
    assert find_model("mistralai/mistral-large-2611") is None


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-5-5",
        "anthropic/claude-sonnet-5.5",
        "claude-opus-4-7",
        "claude-sonnet-5-7",  # not catalogued — name-shape fallback
        "gpt-5.6-luna",
    ],
)
def test_sampling_locked_models_reject_temperature(model_id) -> None:
    assert rejects_temperature(model_id)


@pytest.mark.parametrize("model_id", ["claude-haiku-4-5", "claude-sonnet-4-6", "gemini-3.7-flash"])
def test_older_models_keep_temperature(model_id) -> None:
    assert not rejects_temperature(model_id)


def test_openrouter_picker_lists_current_models() -> None:
    ids = [m.id for m in models_for_provider("openrouter", role="chat")]
    assert "anthropic/claude-opus-5.5" in ids
    assert "google/gemini-3.7-flash" in ids
    assert "google/gemini-2.5-flash" not in ids


async def test_anthropic_adapter_omits_temperature_for_claude_5() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from unread.ai.anthropic_provider import AnthropicProvider
    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        settings = load_settings()
        settings.anthropic.api_key = "sk-ant-test"
        provider = AnthropicProvider(settings)
        create = AsyncMock(side_effect=RuntimeError("stop"))
        provider._client = SimpleNamespace(messages=SimpleNamespace(create=create))
        for model, expect_temperature in (("claude-opus-5-5", False), ("claude-haiku-4-5", True)):
            create.reset_mock()
            with pytest.raises(RuntimeError):
                await provider.chat(
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=10,
                    temperature=0.2,
                )
            assert ("temperature" in create.call_args.kwargs) is expect_temperature
    finally:
        reset_settings()
