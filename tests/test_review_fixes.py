"""Regressions found by the pre-release review of the fact-check work.

Each test names the failure it locks out; the fixes are small but several
served users a transcript in the wrong language without telling them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from unread.db.repo import Repo
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import TranscriptResult


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _meta(**kw) -> YoutubeMetadata:
    base = {"video_id": "vid1", "url": "https://youtu.be/vid1", "duration_sec": 120}
    base.update(kw)
    return YoutubeMetadata(**base)


# --- transcript_lang_kind survives a cache round-trip ------------------------


async def test_cached_transcript_restores_the_auto_manual_kind(repo: Repo) -> None:
    """Without this the report's `Transcript: ru (auto-captions)` row
    degrades to a bare `ru` on every cached re-run."""
    from unread.youtube.cache import load_exact, save_transcript

    tres = TranscriptResult(
        text="hi",
        source="captions",
        language="ru",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=None,
        is_auto=True,
    )
    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=tres)
    got = await load_exact(repo, video_id="vid1", requested_lang="ru")
    assert got.is_auto is True


async def test_resaving_a_reused_row_keeps_the_kind(repo: Repo) -> None:
    """`put_youtube_transcript` has no COALESCE, so a re-save that derived
    `lang_kind=None` used to overwrite the stored value with NULL."""
    from unread.youtube.cache import load_exact, save_transcript

    tres = TranscriptResult(
        text="hi",
        source="captions",
        language="ru",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=None,
        is_auto=False,
    )
    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=tres)
    reused = await load_exact(repo, video_id="vid1", requested_lang="ru")
    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=reused)
    row = await repo.get_youtube_transcript("vid1", "ru")
    assert row["transcript_lang_kind"] == "manual"


# --- purge clears the new table ---------------------------------------------


async def test_purging_a_youtube_source_also_drops_its_transcripts(repo: Repo) -> None:
    """`unread cache sources purge youtube` reported success while
    `load_exact` kept serving the transcript the user asked to delete."""
    from unread.youtube.cache import save_transcript

    await repo.put_youtube_video(
        video_id="vid1",
        url="https://youtu.be/vid1",
        title="t",
        channel_id=None,
        channel_title=None,
        channel_url=None,
        description=None,
        upload_date=None,
        duration_sec=120,
        view_count=None,
        like_count=None,
        tags=None,
        language="ru",
        transcript="legacy",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
    )
    await save_transcript(
        repo,
        video_id="vid1",
        requested_lang="ru",
        tres=TranscriptResult(
            text="cached", source="captions", language="ru", duration_sec=120, cost_usd=0.0, timed_cues=None
        ),
    )
    await repo.purge_source_cache("youtube", url="https://youtu.be/vid1")
    assert await repo.get_youtube_transcript("vid1", "ru") is None


# --- the fallback notice ------------------------------------------------------


def test_notice_fires_when_a_reused_whisper_row_has_no_language():
    """The audio path stores `language=None` unless `openai.audio_language`
    is set, so the notice used to silently short-circuit — handing the
    user a wrong-language transcript with no warning at all."""
    from unread.youtube.cache import fallback_notice

    msg = fallback_notice(requested="en", delivered=None, source="audio")
    assert msg


def test_no_notice_for_an_audio_row_when_nothing_was_requested():
    from unread.youtube.cache import fallback_notice

    assert fallback_notice(requested="", delivered=None, source="audio") == ""


def test_notice_is_localized():
    """It is written into `transcript.md`, which the bot uploads — an
    English sentence in a Russian admin's file defeats the whole
    per-admin-language feature."""
    from unread.youtube.cache import fallback_notice

    en = fallback_notice(requested="ru", delivered="en", language="en")
    ru = fallback_notice(requested="ru", delivered="en", language="ru")
    assert en and ru
    assert en != ru


# --- the interactive picker ---------------------------------------------------


async def test_picker_without_actions_offers_no_dump_or_factcheck_rows():
    """`unread ask` shares this picker but can only act on a transcript
    source — it used to pass `__factcheck__` straight into
    `get_transcript(source=...)`."""
    from unread.youtube.commands import _interactive_pick_source

    captured: dict[str, Any] = {}

    def _fake_select(_prompt, *, choices, default_value=None):
        captured["values"] = [getattr(c, "value", None) for c in choices]
        return "auto"

    with patch("unread.util.prompt.select", _fake_select):
        await _interactive_pick_source(_meta(subtitles={"en": [{}]}), audio_estimate=0.0, allow_actions=False)
    assert "__dump__" not in captured["values"]
    assert "__factcheck__" not in captured["values"]


async def test_picker_keeps_actions_when_a_transcript_is_already_cached():
    """A cache hit means we don't need to pick a SOURCE — it must not also
    remove the user's choice of WHAT TO DO."""
    from unread.youtube.commands import _interactive_pick_source

    captured: dict[str, Any] = {}

    def _fake_select(_prompt, *, choices, default_value=None):
        captured["values"] = [getattr(c, "value", None) for c in choices]
        return "auto"

    with patch("unread.util.prompt.select", _fake_select):
        await _interactive_pick_source(
            _meta(subtitles={"en": [{}]}), audio_estimate=0.0, source_choices=False
        )
    assert "__dump__" in captured["values"]
    assert "__factcheck__" in captured["values"]


# --- OpenAI Responses privacy -------------------------------------------------


async def test_openai_search_call_opts_out_of_server_side_storage():
    """The Responses API defaults to store=True. Every other call in this
    project goes through Chat Completions, which does not retain prompts —
    a fact-check must not quietly upload a whole chat for 30-day retention."""
    from unread.ai.openai_provider import OpenAIProvider
    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        s.openai.api_key = "sk-test"
        p = OpenAIProvider(s)
        seen: dict[str, Any] = {}

        class _Responses:
            async def create(self, **kwargs):
                seen.update(kwargs)

                class _R:
                    output_text = "ok"
                    usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
                    status = "completed"
                    output: ClassVar[list] = []

                return _R()

        p._client = type("C", (), {"responses": _Responses()})()
        await p.chat(
            model="gpt-5.6-terra",
            messages=[{"role": "user", "content": "check"}],
            max_tokens=10,
            temperature=0.2,
            web_search=True,
        )
        assert seen["store"] is False
    finally:
        reset_settings()


# --- the cache key must reflect what was actually fetched --------------------


async def test_interactive_caption_choice_is_cached_under_its_own_language() -> None:
    """`requested_lang` is computed before the pickers run. Saving the
    picked language under the DEFAULT key poisoned every later default
    run: pick German once, and plain `unread <url>` served German forever
    while never fetching the English captions again."""
    from unread.config import get_settings
    from unread.db.repo import open_repo
    from unread.youtube.commands import cmd_analyze_youtube

    meta = _meta(
        video_id="pickerkey01",
        url="https://www.youtube.com/watch?v=pickerkey01",
        subtitles={"en": [{}], "de": [{}]},
        duration_sec=900,
    )
    german = TranscriptResult(
        text="guten tag welt. " * 20,
        source="captions",
        language="de",
        duration_sec=900,
        cost_usd=0.0,
        timed_cues=[(0, "guten tag")],
        is_auto=False,
    )
    with (
        patch("unread.youtube.commands.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.commands.get_transcript", new=AsyncMock(return_value=german)),
        patch("unread.youtube.commands._is_interactive", return_value=True),
        patch(
            "unread.youtube.commands._interactive_pick_source",
            new=AsyncMock(return_value="auto"),
        ),
        patch(
            "unread.youtube.commands._interactive_pick_caption_lang",
            new=AsyncMock(return_value="de"),
        ),
    ):
        await cmd_analyze_youtube(
            url=meta.url,
            preset=None,
            prompt_file=None,
            model=None,
            filter_model=None,
            output=None,
            console_out=True,
            dry_run=True,
            yes=False,
            language="en",
            report_language="en",
        )

    async with open_repo(get_settings().storage.data_path) as repo:
        de = await repo.get_youtube_transcript("pickerkey01", "de")
        en = await repo.get_youtube_transcript("pickerkey01", "en")
    assert de is not None and "guten" in de["transcript"]
    assert en is None, "German text must not be cached under the English key"


# --- factcheck stays single-pass for realistic sources ------------------------


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_chunk_budget_covers_long_sources(language) -> None:
    """Only the FINAL call searches. When a source chunks, the map calls
    run ungrounded and the reduce merges them with the generic
    `_reduce.md` prompt rather than re-verifying — so the grounded pass
    mostly copies ungrounded verdicts forward. Until per-preset reduce
    prompts exist, the mitigation is a chunk budget big enough that real
    sources (a 3h podcast is ~50k tokens) stay single-pass."""
    from unread.analyzer.prompts import get_presets

    assert get_presets(language)["factcheck"].max_chunk_input_tokens >= 200_000


# --- Anthropic pause_turn -----------------------------------------------------


async def test_anthropic_pause_turn_is_not_reported_as_a_finished_answer():
    """Server-tool turns can stop with `pause_turn`. Treating that as
    complete cached a half-finished verdict table, poisoning every later
    run of the same input."""
    from unread.ai.anthropic_provider import AnthropicProvider
    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        s.anthropic.api_key = "sk-ant-test"
        p = AnthropicProvider(s)

        class _Messages:
            async def create(self, **_kw):
                class _R:
                    content: ClassVar[list] = [type("B", (), {"type": "text", "text": "half"})()]
                    usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
                    stop_reason = "pause_turn"

                return _R()

        p._client = type("C", (), {"messages": _Messages()})()
        res = await p.chat(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "check"}],
            max_tokens=100,
            temperature=0.2,
            web_search=True,
        )
        # `truncated` is what stops `_call_cached` from caching a partial
        # result and triggers one retry.
        assert res.truncated is True
    finally:
        reset_settings()


# --- the search count is actually recorded -----------------------------------


async def test_web_search_count_reaches_the_usage_log(repo: Repo) -> None:
    """The field's docstring claims searches are recorded because they're
    billed separately from tokens. Nothing read it."""
    from unread.ai.providers import ChatResult as _CR
    from unread.analyzer.openai_client import chat_complete

    class _P:
        name = "rec"
        supports_web_search = True

        async def chat(self, **_kw):
            return _CR(text="ok", prompt_tokens=1, cached_tokens=0, completion_tokens=1, web_searches=4)

    await chat_complete(
        _P(),
        repo=repo,
        model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
        context={"phase": "analyze_reduce"},
        web_search=True,
    )
    cur = await repo._conn.execute("SELECT context FROM usage_log")
    rows = [r[0] for r in await cur.fetchall()]
    await cur.close()
    assert any("web_searches" in (r or "") and "4" in (r or "") for r in rows)
