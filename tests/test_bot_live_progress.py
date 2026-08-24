"""Live progress edits and /stop for an in-flight bot run."""

from __future__ import annotations

import asyncio

from unread.bot.progress import LiveProgress


class _FakeMsg:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit(self, text, **_kw):
        self.texts.append(text)


async def test_first_update_is_immediate() -> None:
    """Waiting out the throttle before the FIRST edit would leave the
    user staring at the stale line for the whole window."""
    msg = _FakeMsg()
    live = LiveProgress(msg, min_interval=60.0)
    await live("Analyzing 3 chunks… 0/3")
    assert msg.texts == ["Analyzing 3 chunks… 0/3"]


async def test_rapid_updates_are_throttled() -> None:
    """Telegram rate-limits edits; a 30-chunk run must not send 30 in a
    burst and get the bot flood-waited."""
    msg = _FakeMsg()
    live = LiveProgress(msg, min_interval=60.0)
    for i in range(10):
        await live(f"chunk {i}")
    assert len(msg.texts) == 1


async def test_update_passes_after_the_interval() -> None:
    msg = _FakeMsg()
    live = LiveProgress(msg, min_interval=0.01)
    await live("first")
    await asyncio.sleep(0.02)
    await live("second")
    assert msg.texts == ["first", "second"]


async def test_identical_text_is_not_resent() -> None:
    """Telegram rejects an edit that changes nothing with
    MESSAGE_NOT_MODIFIED — don't spend a request on it."""
    msg = _FakeMsg()
    live = LiveProgress(msg, min_interval=0.0)
    await live("same")
    await live("same")
    assert msg.texts == ["same"]


async def test_edit_failures_are_swallowed() -> None:
    """Progress is decoration; a failed edit must not kill the run."""

    class _Broken:
        async def edit(self, *_a, **_kw):
            raise RuntimeError("telegram said no")

    live = LiveProgress(_Broken(), min_interval=0.0)
    await live("anything")


async def test_final_flush_bypasses_the_throttle() -> None:
    """The last line must always land, even if it arrives inside the
    throttle window — otherwise the message ends on a stale count."""
    msg = _FakeMsg()
    live = LiveProgress(msg, min_interval=60.0)
    await live("0/3")
    await live.flush("3/3 done")
    assert msg.texts[-1] == "3/3 done"


async def test_no_message_is_a_noop() -> None:
    live = LiveProgress(None, min_interval=0.0)
    await live("nothing to edit")
    await live.flush("still nothing")
