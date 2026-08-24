"""Provider / model selection from the environment.

The container has its own `~/.unread`, so `ai.chat_provider` set on a
laptop doesn't reach it. Without an env var the only ways to point a
Docker bot at a different provider were SSH-ing in to run
`unread settings` or baking a config.toml into the image.
"""

from __future__ import annotations

import pytest

from unread.config import load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean():
    reset_settings()
    yield
    reset_settings()


def test_chat_provider_from_env(monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_AI_CHAT_PROVIDER", "openrouter")
    assert load_settings().ai.chat_provider == "openrouter"


def test_chat_model_from_env(monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_AI_CHAT_MODEL", "openai/gpt-5.6-luna")
    assert load_settings().ai.chat_model == "openai/gpt-5.6-luna"


def test_filter_slot_from_env(monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_AI_FILTER_PROVIDER", "openrouter")
    monkeypatch.setenv("UNREAD_AI_FILTER_MODEL", "openai/gpt-5.6-luna")
    s = load_settings()
    assert s.ai.filter_provider == "openrouter"
    assert s.ai.filter_model == "openai/gpt-5.6-luna"


def test_unset_env_leaves_the_defaults(monkeypatch) -> None:
    for key in ("UNREAD_AI_CHAT_PROVIDER", "UNREAD_AI_CHAT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings()
    assert s.ai.chat_provider == ""
    assert s.ai.chat_model == ""


def test_provider_value_is_validated(monkeypatch) -> None:
    """A typo'd provider should fail at load with the valid names, not
    surface later as a confusing 'Unknown AI provider' mid-run."""
    monkeypatch.setenv("UNREAD_AI_CHAT_PROVIDER", "openrouterr")
    with pytest.raises(ValueError, match="UNREAD_AI_CHAT_PROVIDER"):
        load_settings()


def test_provider_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("UNREAD_AI_CHAT_PROVIDER", "OpenRouter")
    assert load_settings().ai.chat_provider == "openrouter"
