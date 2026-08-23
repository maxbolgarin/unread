"""`unread.bot.prefs` — the bridge between `_chat_state` and the DB.

Sticky settings live in memory for the request path (hot, sync) and are
mirrored to `bot_chat_settings` so a restart doesn't wipe them.
"""

from __future__ import annotations

import pytest

from unread.bot.app import BotApp
from unread.bot.runtime import (
    STICKY_CONFIRM_DISABLED,
    STICKY_ENRICH_EXTRAS,
    STICKY_PRESET,
    STICKY_REPORT_LANGUAGE,
)
from unread.config import load_settings, reset_settings


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111,222")
    reset_settings()
    s = load_settings()
    s.storage.data_path = tmp_path / "data.sqlite"
    yield BotApp(s)
    reset_settings()


async def test_set_sticky_updates_memory_and_survives_a_restart(app) -> None:
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_REPORT_LANGUAGE, value="ru")
    assert app._chat_state[111][STICKY_REPORT_LANGUAGE] == "ru"

    # "Restart": a brand-new BotApp with an empty _chat_state, same DB.
    fresh = BotApp(app.settings)
    assert fresh._chat_state == {}
    await prefs.load_all(fresh)
    assert fresh._chat_state[111][STICKY_REPORT_LANGUAGE] == "ru"


async def test_each_admin_keeps_their_own_language_across_a_restart(app) -> None:
    """The headline case: owner wants Russian, second admin wants English."""
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_REPORT_LANGUAGE, value="ru")
    await prefs.set_sticky(app, chat_id=222, key=STICKY_REPORT_LANGUAGE, value="en")

    fresh = BotApp(app.settings)
    await prefs.load_all(fresh)
    assert fresh._chat_state[111][STICKY_REPORT_LANGUAGE] == "ru"
    assert fresh._chat_state[222][STICKY_REPORT_LANGUAGE] == "en"


async def test_clear_sticky_removes_from_memory_and_db(app) -> None:
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_PRESET, value="digest")
    await prefs.clear_sticky(app, chat_id=111, key=STICKY_PRESET)
    assert STICKY_PRESET not in app._chat_state.get(111, {})

    fresh = BotApp(app.settings)
    await prefs.load_all(fresh)
    assert STICKY_PRESET not in fresh._chat_state.get(111, {})


async def test_enrich_extras_round_trips_as_a_set(app) -> None:
    """`_chat_state` holds a set; the DB column is text. Restoring the
    wrong type would break `resolve_options`' membership tests."""
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_ENRICH_EXTRAS, value={"image", "link"})
    fresh = BotApp(app.settings)
    await prefs.load_all(fresh)
    restored = fresh._chat_state[111][STICKY_ENRICH_EXTRAS]
    assert isinstance(restored, set)
    assert restored == {"image", "link"}


async def test_confirm_disabled_round_trips_as_a_bool(app) -> None:
    """Stored as text — restoring the string "0" would be truthy and
    silently disable the confirm panel for that admin."""
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_CONFIRM_DISABLED, value=True)
    fresh = BotApp(app.settings)
    await prefs.load_all(fresh)
    assert fresh._chat_state[111][STICKY_CONFIRM_DISABLED] is True

    await prefs.set_sticky(app, chat_id=111, key=STICKY_CONFIRM_DISABLED, value=False)
    fresh2 = BotApp(app.settings)
    await prefs.load_all(fresh2)
    assert fresh2._chat_state[111][STICKY_CONFIRM_DISABLED] is False


async def test_load_all_preserves_non_persisted_runtime_state(app) -> None:
    """`_chat_state` also holds ephemeral things (pending_runs, the burst
    accumulator). Loading must merge, not replace the whole dict."""
    from unread.bot import prefs

    await prefs.set_sticky(app, chat_id=111, key=STICKY_REPORT_LANGUAGE, value="ru")
    fresh = BotApp(app.settings)
    fresh._chat_state[111] = {"pending_runs": {7: "sentinel"}}
    await prefs.load_all(fresh)
    assert fresh._chat_state[111]["pending_runs"] == {7: "sentinel"}
    assert fresh._chat_state[111][STICKY_REPORT_LANGUAGE] == "ru"


async def test_load_all_on_an_empty_db_is_a_noop(app) -> None:
    from unread.bot import prefs

    await prefs.load_all(app)
    assert app._chat_state == {}


# --- slash commands write through -------------------------------------------


class _FakeEvent:
    def __init__(self, chat_id: int = 111, sender_id: int = 111) -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs) -> None:
        self.replies.append(text)


async def _stored(app, chat_id: int) -> dict:
    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        return await repo.get_bot_chat_settings(chat_id)


async def test_lang_command_persists_the_choice(app) -> None:
    from unread.bot.handlers import cmds

    event = _FakeEvent()
    await cmds.handle(event, {"name": "lang", "args": ["ru"]}, app=app)
    assert app._chat_state[111][STICKY_REPORT_LANGUAGE] == "ru"
    assert await _stored(app, 111) == {"report_language": "ru"}


async def test_bare_lang_command_clears_the_stored_choice(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(), {"name": "lang", "args": ["ru"]}, app=app)
    await cmds.handle(_FakeEvent(), {"name": "lang", "args": []}, app=app)
    assert await _stored(app, 111) == {}


async def test_invalid_lang_command_persists_nothing(app) -> None:
    from unread.bot.handlers import cmds

    event = _FakeEvent()
    await cmds.handle(event, {"name": "lang", "args": ["not-a-code!"]}, app=app)
    assert await _stored(app, 111) == {}


async def test_two_admins_persist_independent_languages(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(chat_id=111), {"name": "lang", "args": ["ru"]}, app=app)
    await cmds.handle(_FakeEvent(chat_id=222), {"name": "lang", "args": ["en"]}, app=app)
    assert await _stored(app, 111) == {"report_language": "ru"}
    assert await _stored(app, 222) == {"report_language": "en"}


async def test_preset_command_persists_and_clears(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(), {"name": "preset", "args": ["digest"]}, app=app)
    assert await _stored(app, 111) == {"preset": "digest"}
    await cmds.handle(_FakeEvent(), {"name": "preset", "args": []}, app=app)
    assert await _stored(app, 111) == {}


async def test_enrich_command_persists(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(), {"name": "enrich", "args": ["image,link"]}, app=app)
    stored = await _stored(app, 111)
    assert set(stored["enrich_extras"].split(",")) == {"image", "link"}


async def test_window_command_persists(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(), {"name": "window", "args": ["week"]}, app=app)
    assert await _stored(app, 111) == {"tg_window": "7d"}


async def test_confirm_command_persists(app) -> None:
    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(), {"name": "confirm", "args": ["off"]}, app=app)
    assert await _stored(app, 111) == {"confirm_disabled": "1"}
    # `/confirm on` is "back to the default", so it clears the row rather
    # than storing a falsey one — same shape as every other clear.
    await cmds.handle(_FakeEvent(), {"name": "confirm", "args": ["on"]}, app=app)
    assert await _stored(app, 111) == {}
