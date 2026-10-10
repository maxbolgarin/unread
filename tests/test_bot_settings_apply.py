"""Applying a settings tap: provider, model, and the API-key flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unread.bot.app import BotApp
from unread.bot.settings_menu import encode_settings_callback
from unread.config import load_settings, reset_settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    s = load_settings()
    s.storage.data_path = tmp_path / "d.sqlite"
    yield BotApp(s)
    reset_settings()


class _Cb:
    def __init__(self, data: bytes, sender_id: int = 111, chat_id: int = 7) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.edit_buttons: list[Any] = []

    async def answer(self, text: str = "", **_kw) -> None:
        self.answers.append(text)

    async def edit(self, text: str | None = None, **kw) -> None:
        self.edits.append(text or "")
        self.edit_buttons.append(kw.get("buttons"))

    async def get_message(self):
        return None


class _Msg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.raw_text = text
        self.deleted = False
        self.media = None

    async def delete(self):
        self.deleted = True


class _Event:
    def __init__(self, text: str = "", chat_id: int = 7, sender_id: int = 111) -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = _Msg(text)
        self.text = text
        self.raw_text = text
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw) -> Any:
        self.replies.append(text)
        return None


async def test_provider_tap_persists_the_choice(app) -> None:
    await app._handle_callback(_Cb(encode_settings_callback("S_PROV", 5, "openrouter")))
    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        stored = await repo.get_all_app_settings()
    assert stored.get("ai.chat_provider") == "openrouter"


async def test_provider_tap_takes_effect_without_a_restart(app) -> None:
    """Persisting but not applying would leave the bot on the old provider
    until someone restarted the container — the exact thing this menu
    exists to avoid."""
    await app._handle_callback(_Cb(encode_settings_callback("S_PROV", 5, "openrouter")))
    from unread.config import get_settings

    assert get_settings().ai.chat_provider == "openrouter"


async def test_model_tap_persists_and_applies(app) -> None:
    await app._handle_callback(_Cb(encode_settings_callback("S_MODEL", 5, "gpt-5.6-sol")))
    from unread.config import get_settings

    assert get_settings().ai.chat_model == "gpt-5.6-sol"


async def test_empty_model_clears_the_override(app) -> None:
    await app._handle_callback(_Cb(encode_settings_callback("S_MODEL", 5, "gpt-5.6-sol")))
    await app._handle_callback(_Cb(encode_settings_callback("S_MODEL", 5, "")))
    from unread.config import get_settings

    assert get_settings().ai.chat_model == ""


async def test_key_tap_arms_the_next_message(app) -> None:
    await app._handle_callback(_Cb(encode_settings_callback("S_KEY", 5)))
    assert app._chat_state.get(7, {}).get("pending_api_key")


async def test_key_message_is_stored_and_deleted(app) -> None:
    """The key still crossed Telegram, but it must not stay in history."""
    from unread.bot import settings_menu  # noqa: F401
    from unread.bot.handlers import cmds

    app._chat_state.setdefault(7, {})["pending_api_key"] = "openrouter"
    event = _Event(text="sk-or-v1-secret-value-1234")
    handled = await cmds.maybe_consume_api_key(event, app=app)
    assert handled is True
    assert event.message.deleted is True

    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        secrets = await repo.get_secrets()
    assert secrets.get("openrouter.api_key") == "sk-or-v1-secret-value-1234"


async def test_key_confirmation_never_echoes_the_key(app) -> None:
    from unread.bot.handlers import cmds

    app._chat_state.setdefault(7, {})["pending_api_key"] = "openrouter"
    event = _Event(text="sk-or-v1-secret-value-1234")
    await cmds.maybe_consume_api_key(event, app=app)
    joined = " ".join(event.replies)
    assert "sk-or-v1-secret-value-1234" not in joined
    assert "1234" in joined  # masked tail is fine


async def test_key_flow_is_disarmed_after_use(app) -> None:
    from unread.bot.handlers import cmds

    app._chat_state.setdefault(7, {})["pending_api_key"] = "openrouter"
    await cmds.maybe_consume_api_key(_Event(text="sk-test"), app=app)
    assert not app._chat_state.get(7, {}).get("pending_api_key")


async def test_normal_message_is_not_swallowed(app) -> None:
    """With nothing armed, a YouTube link must reach the analyze path."""
    from unread.bot.handlers import cmds

    event = _Event(text="https://youtu.be/abc")
    assert await cmds.maybe_consume_api_key(event, app=app) is False


async def test_key_flow_is_primary_owner_only(app) -> None:
    """Credentials are bot-wide; a second admin must not rotate them."""
    cb = _Cb(encode_settings_callback("S_KEY", 5), sender_id=999)
    app.allowed_ids.add(999)
    await app._handle_callback(cb)
    assert not app._chat_state.get(7, {}).get("pending_api_key")


async def test_armed_key_flow_intercepts_before_classification(app) -> None:
    """A key must never reach the analyze path — it would be classified,
    logged, and possibly echoed into a report."""
    app._chat_state.setdefault(7, {})["pending_api_key"] = "openrouter"
    event = _Event(text="sk-or-v1-should-not-be-analyzed")
    with patch("unread.bot.dispatcher.classify") as classify:
        await app._handle(event)
    classify.assert_not_called()
    assert event.message.deleted is True


async def test_unarmed_message_still_reaches_classification(app) -> None:
    event = _Event(text="https://youtu.be/abc")
    with (
        patch("unread.bot.dispatcher.classify", return_value=("youtube", {"url": "x"})) as classify,
        patch("unread.bot.burst.add_to_burst", new=AsyncMock()),
    ):
        await app._handle(event)
    classify.assert_called_once()


# --- custom model id ------------------------------------------------------------


async def test_model_tap_arms_the_next_message(app) -> None:
    cb = _Cb(encode_settings_callback("S_MODELS", 5))
    await app._handle_callback(cb)
    assert app._chat_state.get(7, {}).get("pending_model")
    assert cb.edits and "next message" in cb.edits[-1]


async def test_model_tap_disarms_a_pending_key_prompt(app) -> None:
    """Only one text prompt at a time — a model id must never be stored as a key."""
    await app._handle_callback(_Cb(encode_settings_callback("S_KEY", 5)))
    await app._handle_callback(_Cb(encode_settings_callback("S_MODELS", 5)))
    state = app._chat_state.get(7, {})
    assert state.get("pending_model")
    assert not state.get("pending_api_key")


@pytest.mark.parametrize("action", [("S_ROOT", None), ("S_MODEL", "")])
async def test_leaving_the_model_panel_disarms_the_prompt(app, action) -> None:
    """Otherwise the next ordinary message would become the model."""
    await app._handle_callback(_Cb(encode_settings_callback("S_MODELS", 5)))
    await app._handle_callback(_Cb(encode_settings_callback(action[0], 5, action[1])))
    assert not app._chat_state.get(7, {}).get("pending_model")


async def test_model_prompt_is_primary_owner_only(app) -> None:
    app.allowed_ids.add(999)
    await app._handle_callback(_Cb(encode_settings_callback("S_MODELS", 5), sender_id=999))
    assert not app._chat_state.get(7, {}).get("pending_model")


async def test_custom_model_message_sets_the_model(app) -> None:
    from unread.config import get_settings

    app._chat_state.setdefault(7, {})["pending_model"] = "openrouter"
    event = _Event(text="anthropic/claude-sonnet-5.5")
    with patch("unread.bot.dispatcher.classify") as classify:
        await app._handle(event)
    classify.assert_not_called()
    assert get_settings().ai.chat_model == "anthropic/claude-sonnet-5.5"
    assert not app._chat_state.get(7, {}).get("pending_model")


async def test_unpriced_custom_model_warns_about_cost_reports(app) -> None:
    from unread.bot.handlers import cmds

    app._chat_state.setdefault(7, {})["pending_model"] = "openrouter"
    event = _Event(text="mistralai/mistral-large-2611")
    assert await cmds.maybe_consume_model_name(event, app=app) is True
    assert "$0" in " ".join(event.replies)


@pytest.mark.parametrize("text", ["https://youtu.be/abc", "what is this", "/cancel"])
async def test_non_model_text_leaves_the_model_alone(app, text) -> None:
    from unread.bot.handlers import cmds
    from unread.config import get_settings

    before = get_settings().ai.chat_model
    app._chat_state.setdefault(7, {})["pending_model"] = "openai"
    assert await cmds.maybe_consume_model_name(_Event(text=text), app=app) is True
    assert get_settings().ai.chat_model == before


async def test_expired_model_prompt_lets_the_message_through(app) -> None:
    import time

    from unread.bot.handlers import cmds

    state = app._chat_state.setdefault(7, {})
    state["pending_model"] = "openai"
    state["pending_model_at"] = time.time() - 3600
    assert await cmds.maybe_consume_model_name(_Event(text="gpt-5.6-terra"), app=app) is False
    assert not state.get("pending_model")


# --- per-chat settings taps ------------------------------------------------------


async def test_format_tap_sets_and_persists_the_chat_value(app) -> None:
    cb = _Cb(encode_settings_callback("S_FMT", 5, "rich"))
    await app._handle_callback(cb)
    assert app._chat_state[7]["report_format"] == "rich"
    assert "Message in chat" in cb.edits[-1]  # back on the overview, showing it
    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        stored = await repo.get_all_bot_chat_settings()
    assert stored[7]["report_format"] == "rich"


async def test_default_tap_clears_the_chat_value(app) -> None:
    await app._handle_callback(_Cb(encode_settings_callback("S_LANG", 5, "ru")))
    await app._handle_callback(_Cb(encode_settings_callback("S_LANG", 5, "")))
    assert "report_language" not in app._chat_state[7]


async def test_media_tap_stays_on_the_media_menu(app) -> None:
    cb = _Cb(encode_settings_callback("S_MED", 5, "image"))
    await app._handle_callback(cb)
    assert app._chat_state[7]["enrich_extras"] == {"image"}
    assert "Media" in cb.edits[-1]


async def test_chat_settings_are_open_to_extra_admins(app) -> None:
    """Per-chat values belong to whoever's chat it is; only AI is gated."""
    app.allowed_ids.add(222)
    await app._handle_callback(_Cb(encode_settings_callback("S_CONF", 5), sender_id=222, chat_id=8))
    assert app._chat_state[8]["confirm_disabled"] is True


async def test_forged_value_is_refused(app) -> None:
    cb = _Cb(encode_settings_callback("S_FMT", 5, "docx"))
    await app._handle_callback(cb)
    assert "report_format" not in app._chat_state.get(7, {})
    assert cb.answers and cb.answers[-1]


async def test_close_drops_the_keyboard(app) -> None:
    cb = _Cb(encode_settings_callback("S_CLOSE", 5))
    await app._handle_callback(cb)
    assert cb.edit_buttons == [None]
