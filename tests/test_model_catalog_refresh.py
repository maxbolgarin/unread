"""GPT-5.6 model family: catalog entries, defaults, and preset pins.

`gpt-5.6-luna` is cheaper than the old nano tier with 2.6x the context,
so it replaces both the chat and filter defaults. `gpt-5.6-sol` replaces
`gpt-5.4` where a preset pins the flagship.

The load-bearing test here is the last one: every model a preset pins must
exist in the catalog, or cost accounting silently logs
`pricing.chat.unknown_model` and reports $0.
"""

from __future__ import annotations

import pytest

from unread.ai.models import find_model
from unread.analyzer.prompts import get_presets

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"


@pytest.mark.parametrize("model_id", [LUNA, SOL, TERRA])
@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_new_models_are_in_the_catalog(model_id, prefix) -> None:
    """Both id forms matter: bare for OpenAI-direct, `openai/`-prefixed
    for OpenRouter routing."""
    assert find_model(prefix + model_id) is not None


@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_luna_pricing_matches_the_published_rates(prefix) -> None:
    m = find_model(prefix + LUNA)
    assert (m.input_price, m.output_price, m.cached_price) == (0.20, 1.20, 0.02)
    assert m.context_window == 1_050_000


@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_terra_pricing_matches_the_published_rates(prefix) -> None:
    m = find_model(prefix + TERRA)
    assert (m.input_price, m.output_price, m.cached_price) == (2.00, 12.00, 0.20)


@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_sol_pricing_matches_the_published_rates(prefix) -> None:
    m = find_model(prefix + SOL)
    assert (m.input_price, m.output_price, m.cached_price) == (2.00, 10.00, 0.20)


@pytest.mark.parametrize("prefix", ["", "openai/"])
def test_new_models_are_reasoning_class(prefix) -> None:
    """The gpt-5 family rejects a custom `temperature` with a 400."""
    assert find_model(prefix + LUNA).reasoning is True
    assert find_model(prefix + SOL).reasoning is True
    assert find_model(prefix + TERRA).reasoning is True


def test_config_defaults_use_luna() -> None:
    """Asserts the SHIPPED dataclass default, not a resolved Settings —
    `load_settings()` applies persisted `app_settings` overrides, so a
    stale row (or another test's write) would mask a wrong default."""
    from unread.config import OpenAICfg

    assert OpenAICfg().chat_model_default == LUNA
    assert OpenAICfg().filter_model_default == LUNA


def test_provider_class_defaults_use_luna() -> None:
    from unread.ai.openai_provider import OpenAIProvider, OpenRouterProvider

    assert OpenAIProvider.default_chat_model == LUNA
    assert OpenAIProvider.default_filter_model == LUNA
    assert OpenRouterProvider.default_chat_model == "openai/" + LUNA
    assert OpenRouterProvider.default_filter_model == "openai/" + LUNA


@pytest.mark.parametrize("language", ["en", "ru"])
def test_no_preset_still_pins_a_gpt_5_4_model(language) -> None:
    stale = {
        name: (p.filter_model, p.final_model)
        for name, p in get_presets(language).items()
        if "gpt-5.4" in p.filter_model or "gpt-5.4" in p.final_model
    }
    assert stale == {}


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_uses_the_flagship(language) -> None:
    """Verdict quality is the whole point of this preset — it must not
    quietly land on the cheap tier."""
    assert get_presets(language)["factcheck"].final_model == SOL


@pytest.mark.parametrize("language", ["en", "ru"])
def test_every_preset_pinned_model_is_priced(language) -> None:
    """A model missing from the catalog costs nothing visible: cost
    accounting logs `pricing.chat.unknown_model` and reports $0, so the
    run looks free in `unread stats` and the bot's caption."""
    missing = []
    for name, p in get_presets(language).items():
        for slot, model in (("filter", p.filter_model), ("final", p.final_model)):
            if model and find_model(model) is None:
                missing.append((name, slot, model))
    assert missing == []
