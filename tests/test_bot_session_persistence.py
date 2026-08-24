"""The bot-mode Telethon session must survive a restart.

An in-memory session meant every start re-ran `ImportBotAuthorization`,
which Telegram rate-limits. Under `restart: unless-stopped` that is
self-sustaining: the container crashes, restarts, re-authorizes, gets a
longer flood wait, crashes again. Observed in production as
"A wait of 790 seconds is required" repeating every ~60s.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from unread.bot.app import BotApp
from unread.config import load_settings, reset_settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    s = load_settings()
    s.telegram.api_id = 1
    s.telegram.api_hash = "h"
    s.telegram.session_path = tmp_path / "session.sqlite"
    s.bot.token = "123:abc"
    yield BotApp(s)
    reset_settings()


def test_bot_session_path_lives_beside_the_user_session() -> None:
    """It has to sit inside the mounted `~/.unread` volume, or a container
    restart loses it and we're back to re-authorizing."""
    from unread.core.paths import default_bot_session_path, default_session_path

    assert default_bot_session_path().parent == default_session_path().parent
    assert default_bot_session_path() != default_session_path()
    assert "bot" in default_bot_session_path().name


async def test_start_uses_a_persistent_session_not_an_in_memory_one(app) -> None:
    seen: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, session, **kwargs):
            seen["session"] = session
            seen["kwargs"] = kwargs

        async def start(self, **kw):
            seen["start"] = kw
            return self

    with patch("unread.bot.app.TelegramClient", _FakeClient):
        await app._start_bot_client()

    # A str/Path session is an on-disk SQLite session; StringSession is not.
    assert isinstance(seen["session"], (str,))
    assert "bot" in seen["session"]
    assert seen["start"]["bot_token"] == "123:abc"


async def test_start_raises_a_clear_error_on_flood_wait(app) -> None:
    """Crash-looping on a flood wait re-triggers the same limit. The
    operator needs to be told to STOP restarting, with the wait time."""
    from telethon.errors import FloodWaitError

    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        async def start(self, **_kw):
            raise FloodWaitError(request=None)

    err = FloodWaitError(request=None)
    err.seconds = 790

    class _FloodClient(_FakeClient):
        async def start(self, **_kw):
            raise err

    with patch("unread.bot.app.TelegramClient", _FloodClient), pytest.raises(SystemExit):
        await app._start_bot_client()


async def test_flood_wait_message_names_the_wait_and_the_cause(app, capsys) -> None:
    from telethon.errors import FloodWaitError

    err = FloodWaitError(request=None)
    err.seconds = 790

    class _FloodClient:
        def __init__(self, *_a, **_kw):
            pass

        async def start(self, **_kw):
            raise err

    with patch("unread.bot.app.TelegramClient", _FloodClient), pytest.raises(SystemExit):
        await app._start_bot_client()
    out = capsys.readouterr().out.lower()
    assert "790" in out or "13" in out
    assert "restart" in out
