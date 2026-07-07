"""Usage-log `phase` tag alignment between emitters and consumers.

`Repo.sum_usage_since(phases=...)` (used by the bot's cost caption) filters
`usage_log` rows on `context.phase`. If the analyzer pipeline and the audio
enricher don't emit the exact tags the consumer's `_ANALYZE_PHASES` list
expects, the bot caption silently sums to ~$0 even though real money was
spent. These tests pin the canonical names: `analyze` (single-pass),
`analyze_map`, `analyze_reduce`, and `enrich_<media_type>` for Whisper calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from unread.analyzer import pipeline
from unread.analyzer.openai_client import ChatResult
from unread.analyzer.pipeline import AnalysisOptions, run_analysis
from unread.bot.reply import _ANALYZE_PHASES
from unread.db.repo import Repo
from unread.models import Chunk, Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _msg(msg_id: int, text: str = "hello world") -> Message:
    return Message(
        chat_id=1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        sender_name="alice",
        text=text,
    )


async def _run_and_capture_phases(
    repo: Repo, msgs: list[Message], *, chunks: list[Chunk] | None = None
) -> list[str]:
    """Run `run_analysis` with a stubbed provider/client and return the
    `context["phase"]` seen by every `chat_complete` call, in call order."""
    seen: list[str] = []

    async def fake_chat_complete(*_args, **kwargs) -> ChatResult:
        seen.append(kwargs["context"].get("phase"))
        return ChatResult(
            text="ok",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )

    def fake_make_client() -> object:
        return object()

    opts = AnalysisOptions(preset="summary", use_cache=False)
    old_chat_complete = pipeline.chat_complete
    old_make_client = pipeline.make_client
    old_build_chunks = pipeline.build_chunks
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = fake_make_client
    if chunks is not None:
        pipeline.build_chunks = lambda *_a, **_kw: chunks
    try:
        await run_analysis(
            repo=repo,
            chat_id=1,
            thread_id=None,
            title="Chat",
            opts=opts,
            messages=msgs,
        )
    finally:
        pipeline.chat_complete = old_chat_complete
        pipeline.make_client = old_make_client
        pipeline.build_chunks = old_build_chunks
    return seen


async def test_single_chunk_run_tags_phase_analyze(repo: Repo) -> None:
    msgs = [_msg(10)]
    phases = await _run_and_capture_phases(repo, msgs, chunks=[Chunk(messages=msgs)])
    assert phases == ["analyze"]


async def test_multi_chunk_run_tags_map_then_reduce(repo: Repo) -> None:
    msgs = [_msg(10), _msg(11)]
    chunks = [Chunk(messages=[msgs[0]]), Chunk(messages=[msgs[1]])]
    phases = await _run_and_capture_phases(repo, msgs, chunks=chunks)
    # Two map calls (order not guaranteed under asyncio.gather) + one reduce.
    assert phases.count("analyze_map") == 2
    assert phases.count("analyze_reduce") == 1
    assert phases[-1] == "analyze_reduce"
    assert "map" not in phases
    assert "reduce" not in phases


def test_analyze_phases_are_subset_of_consumer_list() -> None:
    assert {"analyze", "analyze_map", "analyze_reduce"} <= set(_ANALYZE_PHASES)
    # The filter stage makes no LLM calls — it must not be listed.
    assert "filter" not in _ANALYZE_PHASES


def test_consumer_list_includes_enrich_youtube() -> None:
    assert "enrich_youtube" in _ANALYZE_PHASES


# --- Audio enricher: Whisper log_usage context must carry `phase` ---------


class _FakeAudioRepo:
    def __init__(self) -> None:
        self.usage_calls: list[dict] = []

    async def get_media_enrichment(self, *_a, **_kw):
        return None

    async def put_media_enrichment(self, *_a, **_kw):
        return None

    async def set_message_transcript(self, *_a, **_kw):
        return None

    async def log_usage(self, **kwargs):
        self.usage_calls.append(kwargs)


class _FakeTgClient:
    async def get_messages(self, *_a, **_kw):
        return SimpleNamespace(media=object())


async def test_audio_enrich_log_usage_context_carries_phase(monkeypatch, tmp_path: Path) -> None:
    from unread.config import get_settings
    from unread.enrich import audio as audio_mod

    settings = get_settings()
    settings.media.tmp_dir = tmp_path

    downloaded = tmp_path / "downloaded.ogg"
    downloaded.write_bytes(b"fake audio bytes")

    monkeypatch.setattr(audio_mod, "_audio_client_or_none", lambda: ("openai", object()))

    async def fake_download_message(_client, _tel_msg, _out_path):
        return downloaded

    async def fake_transcode_for_openai(path, _media_type, _tmp_dir, *, prefer_mp3=False):
        return [path]

    async def fake_transcribe_file(_oai, _path, _model, _lang):
        return "fake transcript"

    monkeypatch.setattr(audio_mod, "download_message", fake_download_message)
    monkeypatch.setattr(audio_mod, "transcode_for_openai", fake_transcode_for_openai)
    monkeypatch.setattr(audio_mod, "_transcribe_file", fake_transcribe_file)

    msg = Message(
        chat_id=1,
        msg_id=10,
        date=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        media_type="voice",
        media_doc_id=42,
        media_duration=5,
    )
    fake_repo = _FakeAudioRepo()
    result = await audio_mod.enrich_audio(
        msg,
        client=_FakeTgClient(),
        repo=fake_repo,
        model="whisper-1",
        language="en",
    )
    assert result is not None
    assert len(fake_repo.usage_calls) == 1
    assert fake_repo.usage_calls[0]["context"]["phase"] == "enrich_voice"
