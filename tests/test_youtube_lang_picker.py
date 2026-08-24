"""Interactive YouTube caption-language picker.

Covers `list_caption_tracks` (pure inventory, from Task 1),
`_dedup_display_tracks` (one row per base language, manual wins over
auto — regardless of iteration order), and
`_interactive_pick_caption_lang` (the arrow-key menu wired into
analyze / ask / dump).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from unread.youtube.commands import (
    WHISPER_LANG_SENTINEL,
    _dedup_display_tracks,
    _interactive_pick_caption_lang,
)
from unread.youtube.dump import cmd_dump_youtube
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import CaptionTrack, TranscriptResult, list_caption_tracks


def _meta(*, subtitles: dict | None = None, automatic_captions: dict | None = None) -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id="abc123",
        url="https://youtu.be/abc123",
        subtitles=subtitles,
        automatic_captions=automatic_captions,
    )


# --- list_caption_tracks -----------------------------------------------------


def test_list_caption_tracks_manual_and_auto_are_separate_rows() -> None:
    meta = _meta(subtitles={"en": [{}]}, automatic_captions={"en": [{}], "ru": [{}]})
    tracks = list_caption_tracks(meta)
    assert CaptionTrack(lang="en", base="en", is_auto=False) in tracks
    assert CaptionTrack(lang="en", base="en", is_auto=True) in tracks
    assert CaptionTrack(lang="ru", base="ru", is_auto=True) in tracks
    assert len(tracks) == 3


def test_list_caption_tracks_empty_when_no_captions() -> None:
    assert list_caption_tracks(_meta()) == []


def test_list_caption_tracks_manual_first_then_auto_alphabetical() -> None:
    meta = _meta(
        subtitles={"ru": [{}], "en": [{}]},
        automatic_captions={"de": [{}], "cs": [{}]},
    )
    tracks = list_caption_tracks(meta)
    # Manual tracks come first (alphabetical), then auto (alphabetical).
    assert [t.lang for t in tracks] == ["en", "ru", "cs", "de"]
    assert [t.is_auto for t in tracks] == [False, False, True, True]


# --- _dedup_display_tracks ----------------------------------------------------


def test_dedup_display_tracks_manual_wins_over_auto_same_lang() -> None:
    meta = _meta(subtitles={"en": [{}]}, automatic_captions={"en": [{}]})
    tracks = _dedup_display_tracks(meta)
    assert len(tracks) == 1
    assert tracks[0].lang == "en"
    assert tracks[0].is_auto is False


def test_dedup_display_tracks_manual_wins_even_when_auto_is_exact_and_manual_is_variant() -> None:
    """Regression (Task 1 review nuance): an exact auto track for a base
    language must still lose to a prefix-matching manual track for that
    same base, even though `_subtitle_candidates` would list the exact
    auto entry before the prefix manual one."""
    meta = _meta(subtitles={"en-US": [{}]}, automatic_captions={"en": [{}]})
    tracks = _dedup_display_tracks(meta)
    assert len(tracks) == 1
    assert tracks[0].base == "en"
    assert tracks[0].is_auto is False
    assert tracks[0].lang == "en-US"


def test_dedup_display_tracks_one_row_per_base_language() -> None:
    meta = _meta(
        subtitles={"en": [{}], "ru": [{}]},
        automatic_captions={"en": [{}], "de": [{}]},
    )
    tracks = _dedup_display_tracks(meta)
    bases = sorted(t.base for t in tracks)
    assert bases == ["de", "en", "ru"]
    # en stays manual despite an auto en track also existing.
    en_track = next(t for t in tracks if t.base == "en")
    assert en_track.is_auto is False


def test_dedup_display_tracks_empty_when_no_captions() -> None:
    assert _dedup_display_tracks(_meta()) == []


def test_dedup_display_tracks_auto_only_base_stays_auto() -> None:
    meta = _meta(automatic_captions={"fr": [{}]})
    tracks = _dedup_display_tracks(meta)
    assert len(tracks) == 1
    assert tracks[0].is_auto is True
    assert tracks[0].lang == "fr"


# --- _interactive_pick_caption_lang -------------------------------------------


@pytest.mark.asyncio
async def test_pick_caption_lang_choices_include_whisper_and_cancel_rows() -> None:
    meta = _meta(subtitles={"en": [{}]}, automatic_captions={"ru": [{}]})
    with patch("unread.util.prompt.select", return_value="en") as sel:
        result = await _interactive_pick_caption_lang(meta, preselect=["en", "ru"])
    assert result == "en"
    sel.assert_called_once()
    kwargs = sel.call_args.kwargs
    values = [c.value for c in kwargs["choices"] if hasattr(c, "value")]
    assert values == ["en", "ru", WHISPER_LANG_SENTINEL, "__cancel__"]


@pytest.mark.asyncio
async def test_pick_caption_lang_labels_show_auto_vs_manual() -> None:
    meta = _meta(subtitles={"en": [{}]}, automatic_captions={"ru": [{}]})
    with patch("unread.util.prompt.select", return_value="en") as sel:
        await _interactive_pick_caption_lang(meta, preselect=["en"])
    kwargs = sel.call_args.kwargs
    labels = {c.value: c.label for c in kwargs["choices"] if hasattr(c, "value")}
    assert "English" in labels["en"]
    assert "manual" in labels["en"]
    assert "Russian" in labels["ru"]
    assert "auto" in labels["ru"]


@pytest.mark.asyncio
async def test_pick_caption_lang_default_value_is_the_first_row() -> None:
    """The cursor sits on row one regardless of `preselect`. English is
    pinned first, so it wins here even though `ru` is the top preference —
    a highlight that jumps down an auto-translation list is harder to read
    than one at the top. `preselect` still drives `get_transcript`'s
    fallback order."""
    meta = _meta(subtitles={"en": [{}], "ru": [{}]})
    with patch("unread.util.prompt.select", return_value="ru") as sel:
        await _interactive_pick_caption_lang(meta, preselect=["ru", "en"])
    assert sel.call_args.kwargs["default_value"] == "en"


@pytest.mark.asyncio
async def test_pick_caption_lang_default_value_when_no_pinned_language_exists() -> None:
    """Neither pinned language is present, so the first row is whichever
    sorts first BY DISPLAY NAME — French before German."""
    meta = _meta(subtitles={"de": [{}], "fr": [{}]})
    with patch("unread.util.prompt.select", return_value="de") as sel:
        await _interactive_pick_caption_lang(meta, preselect=["ru", "en"])
    assert sel.call_args.kwargs["default_value"] == "fr"


@pytest.mark.asyncio
async def test_pick_caption_lang_cancel_returns_none() -> None:
    meta = _meta(subtitles={"en": [{}], "ru": [{}]})
    with patch("unread.util.prompt.select", return_value="__cancel__"):
        result = await _interactive_pick_caption_lang(meta, preselect=["en"])
    assert result is None


@pytest.mark.asyncio
async def test_pick_caption_lang_whisper_row_returns_sentinel() -> None:
    meta = _meta(subtitles={"en": [{}], "ru": [{}]})
    with patch("unread.util.prompt.select", return_value=WHISPER_LANG_SENTINEL):
        result = await _interactive_pick_caption_lang(meta, preselect=["en"])
    assert result == WHISPER_LANG_SENTINEL


@pytest.mark.asyncio
async def test_pick_caption_lang_keyboard_interrupt_propagates() -> None:
    """Esc/Ctrl-C inside select() raises KeyboardInterrupt — same contract
    as `_interactive_pick_source`; callers translate that into an
    Exit(0) themselves (not this helper's job)."""
    meta = _meta(subtitles={"en": [{}], "ru": [{}]})
    with (
        patch("unread.util.prompt.select", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        await _interactive_pick_caption_lang(meta, preselect=["en"])


# --- skip conditions, exercised end-to-end via `cmd_dump_youtube` -----------
#
# `_do_transcript_mode`'s fresh-fetch branch is the simplest real call site
# to drive (no OpenAI / run_analysis mocking needed, unlike analyze/ask).
# These pin the caller-side skip guards, not just the picker helper.


def _meta_single_track(video_id: str) -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        title="Solo caption video",
        subtitles={"en": [{}]},
        duration_sec=60,
    )


def _meta_multi_track(video_id: str) -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        title="Multi caption video",
        subtitles={"en": [{}], "ru": [{}]},
        duration_sec=60,
    )


def _captions_result(lang: str = "en") -> TranscriptResult:
    return TranscriptResult(
        text="hello world",
        source="captions",
        language=lang,
        duration_sec=60,
        cost_usd=0.0,
        timed_cues=[(0, "hello"), (5, "world")],
        is_auto=False,
    )


def _audio_result() -> TranscriptResult:
    return TranscriptResult(
        text="whisper transcript",
        source="audio",
        language=None,
        duration_sec=60,
        cost_usd=0.01,
        timed_cues=None,
        is_auto=None,
    )


async def test_single_track_video_skips_picker_even_on_tty(tmp_path) -> None:
    """≤1 available caption language on an interactive TTY (yes=False)
    must NOT invoke select() — there is nothing to choose between."""
    meta = _meta_single_track("vid-solo0001")
    out = tmp_path / "out"

    def _boom(*_a, **_kw):
        raise AssertionError("select() must not be called for a single-track video")

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=AsyncMock(return_value=_captions_result())),
        patch("unread.util.prompt.select", side_effect=_boom),
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
            yes=False,
        )
    assert (out / "transcript.md").exists()


async def test_whisper_row_switches_effective_source_to_audio(tmp_path) -> None:
    """Picking the Whisper row forwards `source="audio"` into
    `get_transcript` and runs the ffmpeg preflight — the same guard the
    explicit `--youtube-source audio` branch runs."""
    meta = _meta_multi_track("vid-multi001")
    out = tmp_path / "out"
    get_transcript_mock = AsyncMock(return_value=_audio_result())

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
        patch("unread.util.prompt.select", return_value=WHISPER_LANG_SENTINEL),
        patch("unread.util.preflight.require_ffmpeg") as ffmpeg_check,
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
            yes=False,
        )
    ffmpeg_check.assert_called_once()
    get_transcript_mock.assert_called_once()
    assert get_transcript_mock.call_args.kwargs["source"] == "audio"
    assert get_transcript_mock.call_args.kwargs["transcript_lang"] is None


async def test_non_tty_skips_picker_regardless_of_track_count(tmp_path) -> None:
    """Non-interactive stdin must never invoke select(), even with
    multiple languages available (mirrors `_interactive_pick_source`'s
    own non-TTY guard)."""
    meta = _meta_multi_track("vid-nontty01")
    out = tmp_path / "out"

    def _boom(*_a, **_kw):
        raise AssertionError("select() must not be called on non-TTY stdin")

    with (
        patch("sys.stdin.isatty", return_value=False),
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=AsyncMock(return_value=_captions_result())),
        patch("unread.util.prompt.select", side_effect=_boom),
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
            yes=False,
        )
    assert (out / "transcript.md").exists()


async def test_explicit_transcript_lang_skips_picker(tmp_path) -> None:
    """An explicit `transcript_lang` (the future --transcript-lang flag)
    must skip the picker and forward straight to `get_transcript`."""
    meta = _meta_multi_track("vid-explic01")
    out = tmp_path / "out"
    get_transcript_mock = AsyncMock(return_value=_captions_result("ru"))

    def _boom(*_a, **_kw):
        raise AssertionError("select() must not be called with an explicit transcript_lang")

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.get_transcript", new=get_transcript_mock),
        patch("unread.util.prompt.select", side_effect=_boom),
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
            yes=False,
            transcript_lang="ru",
        )
    get_transcript_mock.assert_called_once()
    assert get_transcript_mock.call_args.kwargs["transcript_lang"] == "ru"
