"""TG-link window buttons must survive the tap → handler trip.

`build_tg_choice_panel` offers 📌 Just this msg / 📜 From this msg /
📅 Last day / week / month. The tap stamps `PendingRun.options.tg_window`
and `handlers/tg.py:execute` reads it. Regression guard: the run path
used to rebuild `RunOptions` from settings per item, silently dropping
the choice so every button behaved like the default lookback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unread.bot.app import BotApp
from unread.bot.burst import BurstItem
from unread.bot.confirm import PendingRun, RunOptions, default_options, encode_callback
from unread.config import load_settings, reset_settings

OWNER = 111


def _settings(monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", str(OWNER))
    reset_settings()
    return load_settings()


class _FakeMessage:
    id = 11
    media = None

    async def edit(self, *_args, **_kwargs) -> None:
        return None


class _FakeEvent:
    def __init__(self, sender_id: int = OWNER, chat_id: int = 7) -> None:
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.message = _FakeMessage()
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs) -> Any:
        self.replies.append(text)
        return _FakeMessage()


class _FakeCallbackEvent:
    def __init__(self, *, data: bytes, sender_id: int = OWNER, chat_id: int = 7) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kwargs) -> None:
        self.answers.append(text)

    async def edit(self, *_args, **_kwargs) -> None:
        return None

    async def get_message(self):
        return _FakeMessage()


def _app_with_tg_panel(monkeypatch) -> BotApp:
    app = BotApp(_settings(monkeypatch))
    item = BurstItem(
        kind="tg",
        payload={"url": "https://t.me/c/3853386994/81"},
        event=_FakeEvent(),
    )
    app._chat_state[7] = {
        "pending_runs": {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
    }
    return app


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("T_ONE", "msg"),
        ("T_FRM", "from_msg"),
        ("T_DAY", "1d"),
        ("T_WK", "7d"),
        ("T_MO", "30d"),
    ],
)
async def test_tg_window_button_reaches_the_tg_handler(monkeypatch, action, expected):
    app = _app_with_tg_panel(monkeypatch)
    try:
        execute_mock = AsyncMock()
        with patch("unread.bot.handlers.tg.execute", new=execute_mock):
            await app._handle_callback(_FakeCallbackEvent(data=encode_callback(action, 5)))
        execute_mock.assert_called_once()
        options = execute_mock.call_args.args[2]
        assert options.tg_window == expected
    finally:
        reset_settings()


async def test_plain_run_leaves_tg_window_unset(monkeypatch):
    """▶ Run on a generic batch panel means "no window chosen" — the TG
    handler must fall back to its legacy default, not to a stale value."""
    app = _app_with_tg_panel(monkeypatch)
    try:
        execute_mock = AsyncMock()
        with patch("unread.bot.handlers.tg.execute", new=execute_mock):
            await app._handle_callback(_FakeCallbackEvent(data=encode_callback("R", 5)))
        assert execute_mock.call_args.args[2].tg_window is None
    finally:
        reset_settings()


async def test_kind_defaults_still_apply_alongside_a_panel_choice(monkeypatch):
    """Merging the panel choice must not wipe the per-kind defaults —
    a YouTube item still needs `youtube_source="auto"`."""
    app = BotApp(_settings(monkeypatch))
    try:
        item = BurstItem(kind="youtube", payload={"url": "https://youtu.be/abc"}, event=_FakeEvent())
        app._chat_state[7] = {
            "pending_runs": {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
        }
        execute_mock = AsyncMock()
        with patch("unread.bot.handlers.youtube.execute", new=execute_mock):
            await app._handle_callback(_FakeCallbackEvent(data=encode_callback("R", 5)))
        assert execute_mock.call_args.args[2].youtube_source == "auto"
    finally:
        reset_settings()


# --- the merge helper itself -------------------------------------------------


def test_merge_panel_options_prefers_the_panel_value(monkeypatch):
    from unread.bot.runtime import merge_panel_options

    s = _settings(monkeypatch)
    try:
        merged = merge_panel_options(
            defaults=default_options("tg", s),
            panel=RunOptions(tg_window="30d"),
        )
        assert merged.tg_window == "30d"
    finally:
        reset_settings()


def test_merge_panel_options_keeps_defaults_the_panel_left_alone(monkeypatch):
    from unread.bot.runtime import merge_panel_options

    s = _settings(monkeypatch)
    try:
        merged = merge_panel_options(
            defaults=default_options("youtube", s),
            panel=RunOptions(tg_window="7d"),
        )
        assert merged.youtube_source == "auto"
        assert merged.tg_window == "7d"
    finally:
        reset_settings()
