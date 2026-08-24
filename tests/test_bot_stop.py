"""`/stop` — cancel the run currently in flight for this chat.

A 90-minute video is a long time to be stuck behind a run you started by
mistake, especially with `concurrency=2` where it also blocks the queue.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unread.bot.app import BotApp
from unread.config import load_settings, reset_settings


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    yield BotApp(load_settings())
    reset_settings()


class _FakeEvent:
    def __init__(self, chat_id: int = 7, sender_id: int = 111) -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw) -> Any:
        self.replies.append(text)
        return None


async def test_stop_cancels_the_running_task(app) -> None:
    started = asyncio.Event()

    async def _long_run():
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_long_run())
    app.register_running(7, task)
    await started.wait()

    from unread.bot.handlers import cmds

    event = _FakeEvent()
    await cmds.handle(event, {"name": "stop", "args": []}, app=app)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert event.replies and "stop" in event.replies[0].lower()


async def test_stop_with_nothing_running_says_so(app) -> None:
    from unread.bot.handlers import cmds

    event = _FakeEvent()
    await cmds.handle(event, {"name": "stop", "args": []}, app=app)
    assert event.replies
    assert "nothing" in event.replies[0].lower()


async def test_stop_only_touches_this_chat(app) -> None:
    """Two admins run concurrently; one stopping must not kill the other."""

    async def _long_run():
        await asyncio.sleep(60)

    mine = asyncio.create_task(_long_run())
    theirs = asyncio.create_task(_long_run())
    app.register_running(7, mine)
    app.register_running(8, theirs)

    from unread.bot.handlers import cmds

    await cmds.handle(_FakeEvent(chat_id=7), {"name": "stop", "args": []}, app=app)
    await asyncio.sleep(0)
    assert mine.cancelled() or mine.done()
    assert not theirs.done()
    theirs.cancel()


async def test_finished_run_is_forgotten(app) -> None:
    """A completed run must not leave a dead task behind that /stop
    reports as cancellable."""

    async def _quick():
        return None

    task = asyncio.create_task(_quick())
    app.register_running(7, task)
    await task
    await asyncio.sleep(0)

    from unread.bot.handlers import cmds

    event = _FakeEvent()
    await cmds.handle(event, {"name": "stop", "args": []}, app=app)
    assert "nothing" in event.replies[0].lower()


def test_stop_is_listed_in_help(app) -> None:
    from unread.bot.handlers.cmds import _build_help_text

    assert "/stop" in _build_help_text(app)


# --- stale panel state -------------------------------------------------------


async def test_ignored_panel_is_pruned_on_the_next_message(app) -> None:
    """Send a video, delete the confirm panel, never tap. Pruning only ran
    on a CALLBACK, so an ignored panel sat in memory until the process
    restarted."""
    import time as _time

    from unread.bot.confirm import PendingRun, RunOptions

    chat_state = app._chat_state.setdefault(7, {})
    chat_state["pending_runs"] = {
        5: PendingRun(
            kind="batch",
            payload={"items": []},
            options=RunOptions(),
            created_at=_time.time() - 7200,
        )
    }

    from unittest.mock import AsyncMock, patch

    event = _FakeEvent()
    with (
        patch("unread.bot.dispatcher.classify", return_value=("youtube", {"url": "x"})),
        patch("unread.bot.burst.add_to_burst", new=AsyncMock()),
    ):
        await app._handle(event)

    assert 5 not in app._chat_state[7]["pending_runs"]


async def test_a_fresh_panel_is_not_pruned(app) -> None:
    from unittest.mock import AsyncMock, patch

    from unread.bot.confirm import PendingRun, RunOptions

    chat_state = app._chat_state.setdefault(7, {})
    chat_state["pending_runs"] = {5: PendingRun(kind="batch", payload={"items": []}, options=RunOptions())}
    event = _FakeEvent()
    with (
        patch("unread.bot.dispatcher.classify", return_value=("youtube", {"url": "x"})),
        patch("unread.bot.burst.add_to_burst", new=AsyncMock()),
    ):
        await app._handle(event)
    assert 5 in app._chat_state[7]["pending_runs"]
