"""Routing the fact-check verify call through a web-search-enabled path.

Only the FINAL call searches: the map phase just extracts claims from the
source text, and running a web search per chunk would multiply the bill
for no benefit.
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


def _msg(msg_id: int, text: str = "the economy grew 12 percent last year") -> Message:
    return Message(
        chat_id=1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        sender_name="alice",
        text=text,
    )


class _FakeProvider:
    def __init__(self, *, supports: bool) -> None:
        self.supports_web_search = supports
        self.name = "fake"


async def _run(repo, *, preset, supports_search, chunks, msgs):
    """Run the pipeline with a stubbed provider; return one record per call."""
    calls: list[dict] = []

    async def fake_chat_complete(*_args, **kwargs) -> ChatResult:
        calls.append(
            {
                "phase": kwargs["context"].get("phase"),
                "web_search": kwargs.get("web_search", False),
                "system": kwargs["messages"][0].get("content", ""),
            }
        )
        return ChatResult(text="ok", prompt_tokens=1, cached_tokens=0, completion_tokens=1, cost_usd=0.0)

    old = (pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks)
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = lambda: _FakeProvider(supports=supports_search)
    pipeline.build_chunks = lambda *_a, **_kw: chunks
    try:
        await run_analysis(
            repo=repo,
            chat_id=1,
            thread_id=None,
            title="Chat",
            opts=AnalysisOptions(preset=preset, use_cache=False),
            messages=msgs,
        )
    finally:
        pipeline.chat_complete, pipeline.make_client, pipeline.build_chunks = old
    return calls


async def test_factcheck_reduce_call_requests_web_search(repo: Repo) -> None:
    msgs = [_msg(10), _msg(11)]
    calls = await _run(
        repo,
        preset="factcheck",
        supports_search=True,
        chunks=[Chunk(messages=[msgs[0]]), Chunk(messages=[msgs[1]])],
        msgs=msgs,
    )
    reduce = [c for c in calls if c["phase"] == "analyze_reduce"]
    assert len(reduce) == 1
    assert reduce[0]["web_search"] is True


async def test_factcheck_map_calls_do_not_search(repo: Repo) -> None:
    """Claim extraction is pure reading — searching per chunk would
    multiply a per-search bill for nothing."""
    msgs = [_msg(10), _msg(11)]
    calls = await _run(
        repo,
        preset="factcheck",
        supports_search=True,
        chunks=[Chunk(messages=[msgs[0]]), Chunk(messages=[msgs[1]])],
        msgs=msgs,
    )
    maps = [c for c in calls if c["phase"] == "analyze_map"]
    assert maps
    assert all(c["web_search"] is False for c in maps)


async def test_factcheck_single_pass_run_also_searches(repo: Repo) -> None:
    """A short source skips map/reduce entirely — that one call is the
    verify call and must still search."""
    msgs = [_msg(10)]
    calls = await _run(
        repo, preset="factcheck", supports_search=True, chunks=[Chunk(messages=msgs)], msgs=msgs
    )
    assert [c["phase"] for c in calls] == ["analyze"]
    assert calls[0]["web_search"] is True


async def test_summary_preset_never_searches(repo: Repo) -> None:
    """A normal analysis must never start billing for web searches."""
    msgs = [_msg(10)]
    calls = await _run(repo, preset="summary", supports_search=True, chunks=[Chunk(messages=msgs)], msgs=msgs)
    assert all(c["web_search"] is False for c in calls)


async def test_provider_without_search_gets_a_no_web_access_instruction(repo: Repo) -> None:
    """On OpenRouter/local the run still works, but the model must be told
    it has no web access so it marks claims Unverifiable instead of
    guessing — and says so in the report."""
    msgs = [_msg(10)]
    calls = await _run(
        repo, preset="factcheck", supports_search=False, chunks=[Chunk(messages=msgs)], msgs=msgs
    )
    assert calls[0]["web_search"] is False
    assert "no web" in calls[0]["system"].lower()


async def test_provider_with_search_gets_no_such_instruction(repo: Repo) -> None:
    msgs = [_msg(10)]
    calls = await _run(
        repo, preset="factcheck", supports_search=True, chunks=[Chunk(messages=msgs)], msgs=msgs
    )
    assert "no web" not in calls[0]["system"].lower()


# --- cache key ---------------------------------------------------------------


def test_options_payload_records_whether_web_search_was_available():
    """Without this, an offline fact-check cached on a provider with no
    search would later be served to a search-enabled run."""
    from unread.analyzer.prompts import get_presets

    preset = get_presets("en")["factcheck"]
    on = AnalysisOptions(preset="factcheck", web_search=True).options_payload(preset)
    off = AnalysisOptions(preset="factcheck", web_search=False).options_payload(preset)
    assert on["web_search"] is True
    assert off["web_search"] is False
    assert on != off


def test_options_payload_omits_web_search_for_normal_presets():
    """Existing cache rows must not be re-keyed by this feature."""
    from unread.analyzer.prompts import get_presets

    preset = get_presets("en")["summary"]
    payload = AnalysisOptions(preset="summary").options_payload(preset)
    assert "web_search" not in payload


# --- chat_complete → provider.chat --------------------------------------------


class _RecordingProvider:
    """Records every `chat` call so we can assert the flag actually lands."""

    name = "rec"
    supports_web_search = True

    def __init__(self, *, truncate_first: bool = False) -> None:
        self.calls: list[dict] = []
        self._truncate_first = truncate_first

    async def chat(self, **kwargs):
        from unread.ai.providers import ChatResult as _CR

        self.calls.append(kwargs)
        truncated = self._truncate_first and len(self.calls) == 1
        return _CR(
            text="answer",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            truncated=truncated,
        )


async def test_chat_complete_forwards_web_search_to_the_provider(repo: Repo) -> None:
    """Regression: `chat_complete` accepted `web_search` but dropped it on
    the way to `_one_call`, so every fact-check silently ran ungrounded."""
    from unread.analyzer.openai_client import chat_complete

    provider = _RecordingProvider()
    await chat_complete(
        provider,
        repo=repo,
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        context={"phase": "analyze"},
        web_search=True,
    )
    assert provider.calls[0]["web_search"] is True


async def test_chat_complete_defaults_web_search_off(repo: Repo) -> None:
    from unread.analyzer.openai_client import chat_complete

    provider = _RecordingProvider()
    await chat_complete(
        provider,
        repo=repo,
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        context={"phase": "analyze"},
    )
    # Not passed at all on the default path — see `_one_call`.
    assert provider.calls[0].get("web_search", False) is False


async def test_truncation_retry_keeps_web_search_on(repo: Repo) -> None:
    """The retry re-bills the whole prompt; dropping the flag there would
    quietly downgrade the retried answer to an ungrounded one."""
    from unread.analyzer.openai_client import chat_complete

    provider = _RecordingProvider(truncate_first=True)
    await chat_complete(
        provider,
        repo=repo,
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        context={"phase": "analyze"},
        web_search=True,
    )
    assert len(provider.calls) == 2
    assert all(c["web_search"] is True for c in provider.calls)
