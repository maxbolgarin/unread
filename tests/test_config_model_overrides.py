"""A newer model must be usable via config alone, without an app release.

Two gaps used to force an update:
  - every shipped preset pins `final_model` / `filter_model`, and the pin
    beat `ai.chat_model` / `ai.filter_model`, so config had no effect on
    analysis;
  - a model missing from the built-in catalog chunked at the 128k
    fallback regardless of its real context window.
"""

from __future__ import annotations

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
