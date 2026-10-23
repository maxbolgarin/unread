"""A newer model must be usable via config alone, without an app release.

Two gaps used to force an update:
  - every shipped preset pins `final_model` / `filter_model`, and the pin
    beat `ai.chat_model` / `ai.filter_model`, so config had no effect on
    analysis;
  - a model missing from the built-in catalog chunked at the 128k
    fallback regardless of its real context window.
"""

from __future__ import annotations

import pytest

from unread.analyzer.chunker import model_context_window
from unread.analyzer.pipeline import effective_models
from unread.analyzer.prompts import Preset
from unread.config import ChatPricing, Settings, get_settings


def _preset() -> Preset:
    return Preset(
        name="t",
        prompt_version="v1",
        system="s",
        user_template="u",
        filter_model="preset-filter",
        final_model="preset-final",
    )


def test_preset_pin_used_when_config_empty():
    assert effective_models(_preset(), Settings()) == ("preset-final", "preset-filter")


def test_config_models_beat_preset_pin():
    s = Settings()
    s.ai.chat_model = "cfg-final"
    s.ai.filter_model = "cfg-filter"
    assert effective_models(_preset(), s) == ("cfg-final", "cfg-filter")


def test_explicit_override_beats_config():
    s = Settings()
    s.ai.chat_model = "cfg-final"
    s.ai.filter_model = "cfg-filter"
    got = effective_models(_preset(), s, model_override="cli-final", filter_model_override="cli-filter")
    assert got == ("cli-final", "cli-filter")


def test_openai_default_used_when_preset_has_no_pin():
    p = _preset()
    p.final_model = ""
    p.filter_model = ""
    s = Settings()
    assert effective_models(p, s) == (s.openai.chat_model_default, s.openai.filter_model_default)


def _openai_pinned_preset() -> Preset:
    """A preset pinned like every shipped one: to an OpenAI catalog id."""
    p = _preset()
    p.final_model = "gpt-5.6-luna"
    p.filter_model = "gpt-5.6-luna"
    return p


def _on(provider: str) -> Settings:
    s = Settings()
    s.ai.chat_provider = provider
    s.ai.filter_provider = provider
    return s


@pytest.mark.parametrize("provider", ["anthropic", "google", "openrouter"])
def test_foreign_preset_pin_falls_back_to_the_provider_default(provider):
    """A provider picked without a model (`unread init`, or
    `UNREAD_AI_CHAT_PROVIDER` alone) used to get the preset's OpenAI pin —
    Anthropic and Gemini reject `gpt-5.6-luna`, OpenRouter wants `openai/…`."""
    from unread.ai.providers import _provider_class_defaults

    assert effective_models(_openai_pinned_preset(), _on(provider)) == _provider_class_defaults(provider)


def test_preset_pin_owned_by_the_provider_is_kept():
    p = _preset()
    p.final_model = "claude-haiku-4-5"
    p.filter_model = "claude-haiku-4-5"
    assert effective_models(p, _on("anthropic")) == ("claude-haiku-4-5", "claude-haiku-4-5")


def test_unknown_preset_pin_is_trusted_on_any_provider():
    """A custom preset's own pick isn't in the catalog — never second-guess it."""
    assert effective_models(_preset(), _on("google")) == ("preset-final", "preset-filter")


def test_local_keeps_the_preset_pin():
    """The local server names its own models; leave its pins alone."""
    assert effective_models(_openai_pinned_preset(), _on("local")) == ("gpt-5.6-luna", "gpt-5.6-luna")


def test_config_model_still_beats_the_provider_default():
    s = _on("anthropic")
    s.ai.chat_model = "claude-opus-5-5"
    final, filt = effective_models(_openai_pinned_preset(), s)
    assert final == "claude-opus-5-5"
    assert filt.startswith("claude-")


def test_slots_resolve_independently():
    """Chat on Anthropic with the cheap pass left on OpenAI keeps the pin
    for the filter slot only."""
    from unread.ai.providers import _provider_class_defaults

    s = Settings()
    s.ai.chat_provider = "anthropic"
    final, filt = effective_models(_openai_pinned_preset(), s)
    assert final == _provider_class_defaults("anthropic")[0]
    assert filt == "gpt-5.6-luna"


def test_env_provider_alone_gives_models_that_provider_serves(monkeypatch):
    """The Docker path end to end: only the provider comes from the
    environment, every shipped preset still has to resolve to a model the
    provider actually serves."""
    from unread.ai.models import models_for_provider
    from unread.analyzer.prompts import get_presets
    from unread.config import load_settings, reset_settings

    monkeypatch.setenv("UNREAD_AI_CHAT_PROVIDER", "anthropic")
    monkeypatch.setenv("UNREAD_AI_FILTER_PROVIDER", "anthropic")
    monkeypatch.delenv("UNREAD_AI_CHAT_MODEL", raising=False)
    monkeypatch.delenv("UNREAD_AI_FILTER_MODEL", raising=False)
    reset_settings()
    try:
        s = load_settings()
        served = {m.id for m in models_for_provider("anthropic")}
        for preset in get_presets("en").values():
            final, filt = effective_models(preset, s)
            assert final in served, (preset.name, final)
            assert filt in served, (preset.name, filt)
    finally:
        reset_settings()


def test_context_window_from_pricing_config():
    get_settings().pricing.chat["brand-new-model"] = ChatPricing(
        input=1.0, cached_input=0.1, output=2.0, context_window=1_000_000
    )
    assert model_context_window("brand-new-model") == 1_000_000


def test_context_window_config_overrides_catalog():
    get_settings().pricing.chat["gpt-5.4-mini"] = ChatPricing(
        input=1.0, cached_input=0.1, output=2.0, context_window=50_000
    )
    assert model_context_window("gpt-5.4-mini") == 50_000


def test_pricing_entry_without_context_window_keeps_catalog():
    get_settings().pricing.chat["gpt-5.4-mini"] = ChatPricing(input=1.0, cached_input=0.1, output=2.0)
    assert model_context_window("gpt-5.4-mini") == 400_000
