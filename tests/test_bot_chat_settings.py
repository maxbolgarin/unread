"""`bot_chat_settings` — per-chat sticky bot settings that survive a restart.

`_chat_state` is in-memory and resets on every bot restart. For a bot with
more than one admin ("I want Russian, you want English") that means
re-sending `/lang` after every redeploy, so the sticky values are mirrored
into `data.sqlite`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.db.repo import Repo


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_put_and_get_round_trip(repo: Repo) -> None:
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="ru")
    assert await repo.get_bot_chat_settings(111) == {"report_language": "ru"}


async def test_settings_are_isolated_per_chat(repo: Repo) -> None:
    """The whole point: each admin's 1:1 chat keeps its own language."""
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="ru")
    await repo.put_bot_chat_setting(chat_id=222, key="report_language", value="en")
    assert (await repo.get_bot_chat_settings(111))["report_language"] == "ru"
    assert (await repo.get_bot_chat_settings(222))["report_language"] == "en"


async def test_put_overwrites_an_existing_key(repo: Repo) -> None:
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="ru")
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="de")
    assert await repo.get_bot_chat_settings(111) == {"report_language": "de"}


async def test_get_for_an_unknown_chat_is_empty(repo: Repo) -> None:
    assert await repo.get_bot_chat_settings(999) == {}


async def test_delete_removes_only_that_key(repo: Repo) -> None:
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="ru")
    await repo.put_bot_chat_setting(chat_id=111, key="preset", value="digest")
    assert await repo.delete_bot_chat_setting(chat_id=111, key="report_language") is True
    assert await repo.get_bot_chat_settings(111) == {"preset": "digest"}


async def test_delete_of_a_missing_key_reports_false(repo: Repo) -> None:
    assert await repo.delete_bot_chat_setting(chat_id=111, key="preset") is False


async def test_unknown_key_is_refused_on_write(repo: Repo) -> None:
    """Allowlist mirrors the sticky constants — a typo must not persist a
    value nothing will ever read."""
    with pytest.raises(ValueError, match="unknown bot chat setting"):
        await repo.put_bot_chat_setting(chat_id=111, key="langauge", value="ru")


async def test_unknown_key_is_filtered_on_read(repo: Repo) -> None:
    """Defense in depth against a hand-edited sqlite row."""
    await repo._conn.execute(
        "INSERT INTO bot_chat_settings(chat_id, key, value, updated_at) VALUES(?,?,?,?)",
        (111, "smuggled", "x", "2026-01-01T00:00:00Z"),
    )
    await repo._conn.commit()
    assert await repo.get_bot_chat_settings(111) == {}


async def test_get_all_returns_every_chat(repo: Repo) -> None:
    """Startup loads the whole table in one query rather than lazily per chat."""
    await repo.put_bot_chat_setting(chat_id=111, key="report_language", value="ru")
    await repo.put_bot_chat_setting(chat_id=222, key="preset", value="digest")
    assert await repo.get_all_bot_chat_settings() == {
        111: {"report_language": "ru"},
        222: {"preset": "digest"},
    }
