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


async def test_flood_wait_is_slept_through_in_process(app) -> None:
    """Exiting is the wrong response to a WAIT: `restart: unless-stopped`
    turns any exit into a restart loop that re-enters the same limit —
    observed hammering once per second. The process must sleep in place
    and retry, which needs no operator action at all."""
    from telethon.errors import FloodWaitError

    err = FloodWaitError(request=None)
    err.seconds = 120
    attempts: list[int] = []
    slept: list[float] = []

    class _FloodThenOk:
        def __init__(self, *_a, **_kw):
            pass

        async def start(self, **_kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise err
            return self

    async def _fake_sleep(seconds):
        slept.append(seconds)

    with (
        patch("unread.bot.app.TelegramClient", _FloodThenOk),
        patch("unread.bot.app.asyncio.sleep", _fake_sleep),
    ):
        await app._start_bot_client()

    assert len(attempts) == 2, "must retry after the wait, not give up"
    assert slept and slept[0] >= 120, "must sleep at least the requested wait"
    assert app.bot_client is not None


async def test_flood_wait_does_not_exit_on_the_first_failure(app) -> None:
    """A SystemExit here is what produced the restart loop."""
    from telethon.errors import FloodWaitError

    err = FloodWaitError(request=None)
    err.seconds = 5
    calls: list[int] = []

    class _AlwaysFlood:
        def __init__(self, *_a, **_kw):
            pass

        async def start(self, **_kw):
            calls.append(1)
            raise err

    async def _fake_sleep(_s):
        return None

    with (
        patch("unread.bot.app.TelegramClient", _AlwaysFlood),
        patch("unread.bot.app.asyncio.sleep", _fake_sleep),
        pytest.raises(Exception) as caught,
    ):
        await app._start_bot_client()

    # It may eventually give up, but only after genuinely retrying.
    assert len(calls) > 1, "gave up without retrying — that is the restart loop"
    assert not isinstance(caught.value, SystemExit) or len(calls) > 1
