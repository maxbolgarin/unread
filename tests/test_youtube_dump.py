"""`unread dump <youtube-url>` transcript / audio / video modes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import typer

from unread.youtube.dump import cmd_dump_youtube
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import TranscriptResult


def _meta(video_id: str = "abcdefghijk") -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Hello World",
        channel_id="UC123",
        channel_title="Examples",
        channel_url="https://youtube.com/c/examples",
        description="A test video.",
        upload_date="20250101",
        duration_sec=120,
        view_count=1234,
        like_count=42,
    )


def _trans_with_cues() -> TranscriptResult:
    return TranscriptResult(
        text="hello world",
        source="captions",
        language="en",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=[(0, "hello"), (5, "world")],
    )


def _trans_no_cues() -> TranscriptResult:
    return TranscriptResult(
        text="whisper text only",
        source="audio",
        language=None,
        duration_sec=120,
        cost_usd=0.0123,
        timed_cues=None,
    )


async def test_transcript_mode_writes_metadata_and_transcript(tmp_path) -> None:
    meta = _meta("vid-1cues000")
    out = tmp_path / "out"
    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(return_value=_trans_with_cues()),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "metadata.json").exists()
    assert (out / "transcript.md").exists()
    # Dump-mode transcripts are plain text — per-cue timing stays in the DB
    # for analyze/ask but is NOT emitted into the dump directory.
    assert not (out / "transcript_timed.json").exists()
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "Hello World" in md
    assert "hello world" in md
    assert "[00:00]" not in md and "[00:05]" not in md
    meta_data = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta_data["video_id"] == meta.video_id
    assert meta_data["title"] == "Hello World"
    assert "subtitles" not in meta_data and "automatic_captions" not in meta_data
    # No audio/video files in transcript mode.
    assert not (out / "audio.mp3").exists()
    assert not list(out.glob("video.*"))


async def test_transcript_mode_no_cues_falls_back_to_plain_text(tmp_path) -> None:
    meta = _meta("vid-2nocues00")
    out = tmp_path / "out"
    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(return_value=_trans_no_cues()),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="audio",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "whisper text only" in md
    assert not (out / "transcript_timed.json").exists()


async def test_audio_mode_calls_download_audio(tmp_path) -> None:
    meta = _meta("vid-3audio000")
    out = tmp_path / "out"

    async def _fake_download_audio(metadata, dest_dir):
        local = dest_dir / f"{metadata.video_id}.mp3"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"ID3fake-mp3")
        return local

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.download_audio", new=_fake_download_audio),
        patch("unread.youtube.dump.require_ffmpeg"),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="audio",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "audio.mp3").exists()
    assert (out / "metadata.json").exists()
    assert not (out / "transcript.md").exists()


async def test_video_mode_calls_download_video(tmp_path) -> None:
    meta = _meta("vid-4video000")
    out = tmp_path / "out"

    async def _fake_download_video(metadata, dest_dir):
        local = dest_dir / f"{metadata.video_id}.mp4"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return local

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.download_video", new=_fake_download_video),
        patch("unread.youtube.dump.require_ffmpeg"),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="video",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "video.mp4").exists()
    assert (out / "metadata.json").exists()


async def test_audio_mode_preflight_requires_ffmpeg(tmp_path) -> None:
    meta = _meta("vid-5ffaudio0")
    out = tmp_path / "out"

    def _missing(_reason: str):
        import typer

        raise typer.Exit(1)

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.download_audio",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
        patch("unread.youtube.dump.require_ffmpeg", side_effect=_missing),
        pytest.raises((SystemExit, typer.Exit)),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="audio",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )


async def test_video_mode_preflight_requires_ffmpeg(tmp_path) -> None:
    meta = _meta("vid-6ffvideo0")
    out = tmp_path / "out"

    def _missing(_reason: str):
        import typer

        raise typer.Exit(1)

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.download_video",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
        patch("unread.youtube.dump.require_ffmpeg", side_effect=_missing),
        pytest.raises((SystemExit, typer.Exit)),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="video",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )


async def test_transcript_mode_forwards_transcript_lang_to_get_transcript(tmp_path) -> None:
    """An explicit `transcript_lang` must reach `get_transcript` unchanged —
    this is the Task-3 CLI flag's landing point."""
    meta = _meta("vid-8explic0")
    out = tmp_path / "out"
    get_transcript_mock = AsyncMock(return_value=_trans_with_cues())

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
            transcript_lang="fr",
        )
    get_transcript_mock.assert_called_once()
    assert get_transcript_mock.call_args.kwargs["transcript_lang"] == "fr"


async def test_transcript_mode_uses_repo_cache(tmp_path) -> None:
    """A populated `youtube_videos` row must skip get_transcript entirely."""
    out = tmp_path / "out"
    meta = _meta("vid-7cache000")

    from unread.config import get_settings
    from unread.db.repo import open_repo

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        await repo.put_youtube_video(
            video_id=meta.video_id,
            url=meta.url,
            title=meta.title,
            channel_id=meta.channel_id,
            channel_title=meta.channel_title,
            channel_url=meta.channel_url,
            description=meta.description,
            upload_date=meta.upload_date,
            duration_sec=meta.duration_sec,
            view_count=meta.view_count,
            like_count=meta.like_count,
            tags=meta.tags,
            language="en",
            transcript="cached transcript text",
            transcript_source="captions",
            transcript_model=None,
            transcript_cost_usd=0.0,
            transcript_timed=[(0, "cached"), (3, "transcript text")],
        )

    with (
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
        patch(
            "unread.youtube.dump.fetch_metadata",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )

    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "cached transcript text" in md
    assert "[00:00]" not in md and "[00:03]" not in md
    assert not (out / "transcript_timed.json").exists()


async def _put_cached_row(meta: YoutubeMetadata, *, language: str, transcript: str) -> None:
    from unread.config import get_settings
    from unread.db.repo import open_repo

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        await repo.put_youtube_video(
            video_id=meta.video_id,
            url=meta.url,
            title=meta.title,
            channel_id=meta.channel_id,
            channel_title=meta.channel_title,
            channel_url=meta.channel_url,
            description=meta.description,
            upload_date=meta.upload_date,
            duration_sec=meta.duration_sec,
            view_count=meta.view_count,
            like_count=meta.like_count,
            tags=meta.tags,
            language=language,
            transcript=transcript,
            transcript_source="captions",
            transcript_model=None,
            transcript_cost_usd=0.0,
            transcript_timed=None,
        )


async def test_transcript_mode_cache_bypassed_on_transcript_lang_mismatch(tmp_path) -> None:
    """A cached English transcript must be bypassed and re-fetched when
    `--transcript-lang fr` explicitly asks for a different language —
    mirrors `cmd_analyze_youtube`'s bypass so the CLI flag has the same
    effect in dump mode as in analyze mode."""
    out = tmp_path / "out"
    meta = _meta("vid-9mismatch")
    await _put_cached_row(meta, language="en", transcript="cached english transcript")

    tres = TranscriptResult(
        text="texte francais",
        source="captions",
        language="fr",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=[(0, "bonjour")],
    )
    get_transcript_mock = AsyncMock(return_value=tres)
    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
            transcript_lang="fr",
        )
    get_transcript_mock.assert_called_once()
    assert get_transcript_mock.call_args.kwargs["transcript_lang"] == "fr"
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "texte francais" in md
    assert "cached english transcript" not in md


async def test_transcript_mode_cache_bypass_refetches_metadata_inventory(tmp_path) -> None:
    """When the cache is bypassed on a `--transcript-lang` mismatch, the
    `meta` restored from the DB row has NO caption inventory (the row never
    stored `subtitles` / `automatic_captions`). `_do_transcript_mode` must
    re-fetch metadata via `fetch_metadata` BEFORE the picker / get_transcript
    so the real caption tracks are visible — otherwise `auto` silently bills
    Whisper and `captions` falsely errors "no captions available".

    Mirrors the analyze-path assertion in
    `tests/test_youtube_command.py::test_analyze_youtube_cached_row_bypassed_on_transcript_lang_mismatch`.
    """
    out = tmp_path / "out"
    meta = _meta("vid-refetch1")
    await _put_cached_row(meta, language="en", transcript="cached english transcript")

    # A fresh fetch carries the caption inventory the DB row can't hold.
    fr_inventory = {"fr": [{"url": "https://x/fr.vtt", "ext": "vtt"}]}
    fresh_meta = YoutubeMetadata(
        video_id=meta.video_id,
        url=meta.url,
        title=meta.title,
        channel_id=meta.channel_id,
        channel_title=meta.channel_title,
        channel_url=meta.channel_url,
        description=meta.description,
        upload_date=meta.upload_date,
        duration_sec=meta.duration_sec,
        view_count=meta.view_count,
        like_count=meta.like_count,
        subtitles=fr_inventory,
        automatic_captions={},
    )
    fetch_metadata_mock = AsyncMock(return_value=fresh_meta)
    tres = TranscriptResult(
        text="texte francais",
        source="captions",
        language="fr",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=[(0, "bonjour")],
    )
    get_transcript_mock = AsyncMock(return_value=tres)
    with (
        patch("unread.youtube.dump.fetch_metadata", new=fetch_metadata_mock),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
            transcript_lang="fr",
        )
    # cmd_dump_youtube took the cache path (found a row), so the ONLY
    # fetch_metadata call is the re-fetch inside _do_transcript_mode.
    fetch_metadata_mock.assert_called_once()
    assert fetch_metadata_mock.call_args.args[0] == meta.video_id
    # The re-fetched inventory reaches get_transcript (first positional arg).
    get_transcript_mock.assert_called_once()
    passed_meta = get_transcript_mock.call_args.args[0]
    assert passed_meta.subtitles == fr_inventory
    assert get_transcript_mock.call_args.kwargs["transcript_lang"] == "fr"


async def test_transcript_mode_cache_reused_when_lang_matches(tmp_path) -> None:
    """Same cached row, but `--transcript-lang en` matches the cached
    language (base-prefix aware) — cache must still be used, get_transcript
    must NOT be called."""
    out = tmp_path / "out"
    meta = _meta("vid-9match000")
    await _put_cached_row(meta, language="en-US", transcript="cached english transcript")

    with (
        patch(
            "unread.youtube.dump.fetch_metadata",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
            transcript_lang="en",
        )
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "cached english transcript" in md


async def test_transcript_mode_forwards_preferred_langs_from_cli_overrides(tmp_path) -> None:
    """`--report-language` / `--content-language` must reach `get_transcript`
    as `preferred_langs` — fixes the previously-dead report-language
    plumbing described in the task brief."""
    out = tmp_path / "out"
    meta = _meta("vid-9prefs00")
    get_transcript_mock = AsyncMock(return_value=_trans_with_cues())

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="es",
            source_language="de",
            yes=True,
        )
    get_transcript_mock.assert_called_once()
    preferred = get_transcript_mock.call_args.kwargs["preferred_langs"]
    assert preferred[:3] == ["de", "es", "en"]


async def test_transcript_mode_explicit_audio_source_requires_ffmpeg_early(tmp_path) -> None:
    """Scope addition: explicit `--youtube-source audio` in transcript mode
    must run the ffmpeg preflight early, matching `cmd_analyze_youtube`'s
    top-of-function check and the picker's Whisper row — instead of
    failing deep inside `get_transcript` when ffmpeg is missing."""
    out = tmp_path / "out"
    meta = _meta("vid-9ffaudio1")

    def _missing() -> None:
        raise typer.Exit(1)

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.commands._require_audio_ffmpeg",
            side_effect=_missing,
        ) as ffmpeg_mock,
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
        pytest.raises((SystemExit, typer.Exit)),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="audio",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    ffmpeg_mock.assert_called_once()
