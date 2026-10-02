"""`/settings` inline menu — provider, model, and API key from the bot.

The container has its own `~/.unread`, so without this the only way to
repoint a Docker bot at another provider is SSH. Keys are accepted but the
message carrying one is deleted immediately: it still transited Telegram,
but it must not sit in the chat history forever.
"""

from __future__ import annotations

import pytest

from unread.bot.settings_menu import (
    SETTINGS_ACTIONS,
    build_model_menu,
    build_provider_menu,
    build_settings_menu,
    mask_secret,
    parse_settings_callback,
)
from unread.config import load_settings, reset_settings


@pytest.fixture
def settings():
    reset_settings()
    yield load_settings()
    reset_settings()


# --- menus --------------------------------------------------------------------


def test_root_menu_offers_provider_model_and_key(settings) -> None:
    _text, buttons = build_settings_menu(chat_state={}, settings=settings, panel_msg_id=1)
    labels = " ".join(b.text.lower() for row in buttons for b in row)
    assert "provider" in labels
    assert "model" in labels
    assert "key" in labels


def test_provider_menu_lists_every_supported_provider(settings) -> None:
    _text, buttons = build_provider_menu(settings=settings, panel_msg_id=1)
    values = [parse_settings_callback(b.data)[2] for row in buttons for b in row]
    for name in ("openai", "openrouter", "anthropic", "google", "local"):
        assert name in values


def test_provider_menu_marks_the_active_one(settings) -> None:
    settings.ai.chat_provider = "openrouter"
    _text, buttons = build_provider_menu(settings=settings, panel_msg_id=1)
    active = [b.text for row in buttons for b in row if "openrouter" in b.text.lower()]
    assert active and any("✓" in t or "•" in t for t in active)


def test_model_menu_lists_no_model_buttons(settings) -> None:
    """Model ids are typed, not tapped: a hardcoded list goes stale fast."""
    settings.ai.chat_provider = "openrouter"
    _text, buttons = build_model_menu(settings=settings, panel_msg_id=1)
    values = [parse_settings_callback(b.data)[2] for row in buttons for b in row]
    assert [v for v in values if v] == []


@pytest.mark.parametrize("provider", ["openai", "openrouter", "anthropic", "google", "local"])
def test_model_menu_links_the_providers_model_list(settings, provider) -> None:
    from unread.bot.settings_menu import MODEL_LIST_URL

    settings.ai.chat_provider = provider
    text, _buttons = build_model_menu(settings=settings, panel_msg_id=1)
    assert MODEL_LIST_URL[provider] in text


def test_model_menu_shows_the_current_models_price(settings) -> None:
    settings.ai.chat_provider = "openai"
    settings.ai.chat_model = "gpt-5.6-luna"
    text, _buttons = build_model_menu(settings=settings, panel_msg_id=1)
    assert "gpt-5.6-luna" in text
    assert "$0.2/$1.2" in text


def test_model_menu_keeps_preset_default_and_back(settings) -> None:
    _text, buttons = build_model_menu(settings=settings, panel_msg_id=1)
    actions = [parse_settings_callback(b.data)[0] for row in buttons for b in row]
    assert actions == ["S_MODEL", "S_ROOT"]


# --- callback encoding ---------------------------------------------------------


def test_callback_round_trips_an_action_and_value() -> None:
    from unread.bot.settings_menu import encode_settings_callback

    data = encode_settings_callback("S_PROV", 42, "openrouter")
    assert parse_settings_callback(data) == ("S_PROV", 42, "openrouter")


def test_callback_stays_within_telegrams_64_byte_cap() -> None:
    """Telegram hard-caps callback data; a long OpenRouter model id is the
    realistic worst case."""
    from unread.bot.settings_menu import encode_settings_callback

    data = encode_settings_callback("S_MODEL", 999999999, "openai/gpt-5.6-luna-pro:batch")
    assert len(data) <= 64


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_settings_callback(b"NOPE:1:x")


def test_every_action_is_declared() -> None:
    """A button encoding an action the parser doesn't know is a dead tap."""
    assert "S_PROV" in SETTINGS_ACTIONS
    assert "S_MODEL" in SETTINGS_ACTIONS


# --- secret masking ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expect_visible"),
    [("sk-or-v1-abcdef1234567890", "7890"), ("short", ""), ("", "")],
)
def test_mask_shows_at_most_the_last_four(raw, expect_visible) -> None:
    masked = mask_secret(raw)
    assert raw not in masked or raw == ""
    if expect_visible:
        assert masked.endswith(expect_visible)


def test_mask_never_leaks_a_short_key_entirely() -> None:
    """A short string must not round-trip through the mask unchanged."""
    assert mask_secret("abcd") != "abcd"


# --- no drift between the provider tables ------------------------------------


def test_menu_providers_match_the_config_allowlist() -> None:
    """Four copies of this list existed. Adding a sixth provider and
    missing one leaves the menu offering a provider whose key it can't
    store, or the CLI and bot disagreeing about what's valid."""
    from unread.bot.settings_menu import _PROVIDERS
    from unread.config import _VALID_AI_PROVIDERS

    assert set(_PROVIDERS) == set(_VALID_AI_PROVIDERS)


def test_every_menu_provider_has_a_known_secret_field() -> None:
    from unread.bot.settings_menu import _PROVIDERS, secret_key_for_provider

    for name in _PROVIDERS:
        field = secret_key_for_provider(name)
        # "local" legitimately has none; everything else must resolve.
        assert field or name == "local", name


def test_secret_fields_match_the_secrets_allowlist() -> None:
    """A key the secrets table would refuse can't be stored, so offering
    it in the menu is a dead button."""
    from unread.bot.settings_menu import _PROVIDERS, secret_key_for_provider
    from unread.db._keys import SECRET_KEYS

    for name in _PROVIDERS:
        field = secret_key_for_provider(name)
        if field:
            assert field in SECRET_KEYS, f"{name} → {field} is not an allowlisted secret"


@pytest.mark.parametrize(
    "raw",
    ["gpt-5.6-terra", "anthropic/claude-opus-5.5", "qwen2.5:14b", "google/gemini-3.7-flash"],
)
def test_looks_like_model_id_accepts_real_ids(raw) -> None:
    from unread.bot.settings_menu import looks_like_model_id

    assert looks_like_model_id(raw)


@pytest.mark.parametrize("raw", ["", "https://x.com/a", "two words", "/cancel", "a" * 200])
def test_looks_like_model_id_rejects_junk(raw) -> None:
    from unread.bot.settings_menu import looks_like_model_id

    assert not looks_like_model_id(raw)
