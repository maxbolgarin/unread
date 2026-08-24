"""`run_analysis(on_progress=...)` — phase updates for a non-terminal caller.

The Rich spinner is invisible to the bot, so a long run looked identical
to a hung one. These are the events worth surfacing: how many chunks,
which chunk finished, and when the merge starts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from unread.analyzer import pipeline
from unread.analyzer.openai_client import ChatResult
from unread.analyzer.pipeline import AnalysisOptions, run_analysis
from unread.db.repo import Repo
from unread.models import Chunk, Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _fake_provider():
    return type("P", (), {"name": "fake", "supports_web_search": False})()


def _msg(msg_id: int) -> Message:
    return Message(
        chat_id=1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        sender_name="alice",
        text="hello world " * 5,
    )


async def _run(repo, msgs, chunks, *, opts_kw=None, **kw):
    events: list[str] = []

    async def _on_progress(text: str) -> None:
        events.append(text)

    async def fake_chat_complete(*_a, **_kw) -> ChatResult:
        return ChatResult(text="ok", prompt_tokens=1, cached_tokens=0, completion_tokens=1, cost_usd=0.0)

    old = (pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks)
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = _fake_provider
    pipeline.build_chunks = lambda *_a, **_kw: chunks
    try:
        await run_analysis(
            repo=repo,
            chat_id=1,
            thread_id=None,
            title="Chat",
            opts=AnalysisOptions(preset="summary", use_cache=False, **(opts_kw or {})),
            messages=msgs,
            on_progress=_on_progress,
            **kw,
        )
    finally:
        pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks = old
    return events


async def test_multi_chunk_run_reports_each_chunk(repo: Repo) -> None:
    msgs = [_msg(10), _msg(11), _msg(12)]
    chunks = [Chunk(messages=[m]) for m in msgs]
    events = await _run(repo, msgs, chunks)
    joined = " | ".join(events)
    assert "3" in joined, "should say how many chunks there are"
    assert any("1/3" in e or "1 / 3" in e for e in events)
    assert any("3/3" in e or "3 / 3" in e for e in events)


async def test_reduce_phase_is_announced(repo: Repo) -> None:
    msgs = [_msg(10), _msg(11)]
    events = await _run(repo, msgs, [Chunk(messages=[msgs[0]]), Chunk(messages=[msgs[1]])])
    assert any("merg" in e.lower() for e in events)


async def test_single_chunk_run_still_reports_something(repo: Repo) -> None:
    msgs = [_msg(10)]
    events = await _run(repo, msgs, [Chunk(messages=msgs)])
    assert events, "a single-pass run must not be silent either"


async def test_no_callback_is_fine(repo: Repo) -> None:
    """Every existing caller passes nothing."""
    msgs = [_msg(10)]

    async def fake_chat_complete(*_a, **_kw) -> ChatResult:
        return ChatResult(text="ok", prompt_tokens=1, cached_tokens=0, completion_tokens=1, cost_usd=0.0)

    old = (pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks)
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = _fake_provider
    pipeline.build_chunks = lambda *_a, **_kw: [Chunk(messages=msgs)]
    try:
        result = await run_analysis(
            repo=repo,
            chat_id=1,
            thread_id=None,
            title="Chat",
            opts=AnalysisOptions(preset="summary", use_cache=False),
            messages=msgs,
        )
    finally:
        pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks = old
    assert result.final_result


async def test_a_failing_callback_does_not_kill_the_run(repo: Repo) -> None:
    """Progress is decoration. A Telegram edit failing mid-run must not
    lose the analysis the user already paid for."""
    msgs = [_msg(10)]

    async def _boom(_text: str) -> None:
        raise RuntimeError("telegram edit failed")

    async def fake_chat_complete(*_a, **_kw) -> ChatResult:
        return ChatResult(text="ok", prompt_tokens=1, cached_tokens=0, completion_tokens=1, cost_usd=0.0)

    old = (pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks)
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = _fake_provider
    pipeline.build_chunks = lambda *_a, **_kw: [Chunk(messages=msgs)]
    try:
        result = await run_analysis(
            repo=repo,
            chat_id=1,
            thread_id=None,
            title="Chat",
            opts=AnalysisOptions(preset="summary", use_cache=False),
            messages=msgs,
            on_progress=_boom,
        )
    finally:
        pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks = old
    assert result.final_result


async def test_progress_wording_matches_the_source(repo: Repo) -> None:
    """ "Analyzing 54 messages" for a YouTube video — the pipeline's
    progress text leaked the Telegram-first vocabulary the same way the
    report labels did."""
    msgs = [_msg(10)]
    events = await _run(repo, msgs, [Chunk(messages=msgs)], opts_kw={"source_kind": "video"})
    joined = " ".join(events).lower()
    assert "message" not in joined
    assert "segment" in joined


async def test_chat_runs_still_say_messages(repo: Repo) -> None:
    msgs = [_msg(10)]
    events = await _run(repo, msgs, [Chunk(messages=msgs)], opts_kw={"source_kind": "chat"})
    assert any("message" in e.lower() for e in events)
