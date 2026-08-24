"""Progress + retry status reaching a non-terminal consumer.

The bot showed one static "Analyzing…" line for the whole run, so a
90-minute video looked identical to a hung process — including while it
sat in a 429 backoff. Both the pipeline's phase progress and the retry
notices now reach an installable sink.
"""

from __future__ import annotations

import asyncio

from unread.util.flood import _user_visible_retry_status, status_sink


def test_sink_receives_retry_status() -> None:
    seen: list[str] = []
    with status_sink(seen.append):
        _user_visible_retry_status("Rate limited — retrying in 13s")
    assert seen == ["Rate limited — retrying in 13s"]


def test_sink_is_removed_on_exit() -> None:
    seen: list[str] = []
    with status_sink(seen.append):
        pass
    _user_visible_retry_status("after")
    assert seen == []


def test_sink_exceptions_never_break_the_retry() -> None:
    """A UI hiccup must not change retry semantics — the whole point of
    the original best-effort try/except."""

    def _explode(_msg):
        raise RuntimeError("sink is broken")

    with status_sink(_explode):
        _user_visible_retry_status("still fine")


def test_nested_sinks_restore_the_outer_one() -> None:
    outer: list[str] = []
    inner: list[str] = []
    with status_sink(outer.append):
        with status_sink(inner.append):
            _user_visible_retry_status("inner")
        _user_visible_retry_status("outer")
    assert inner == ["inner"]
    assert outer == ["outer"]


async def test_sink_is_isolated_between_concurrent_tasks() -> None:
    """Two bot requests run concurrently under one semaphore; one chat's
    progress must not land in another chat's message."""
    a: list[str] = []
    b: list[str] = []

    async def _run(bucket, tag):
        with status_sink(bucket.append):
            await asyncio.sleep(0)
            _user_visible_retry_status(tag)

    await asyncio.gather(_run(a, "chat-a"), _run(b, "chat-b"))
    assert a == ["chat-a"]
    assert b == ["chat-b"]
