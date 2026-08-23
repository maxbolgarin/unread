"""`unread.youtube.cache` — the request→delivery cache policy.

Shared by analyze / dump / ask so all three agree on what counts as a hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import TranscriptResult


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _meta(*, subtitles=None, automatic_captions=None) -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id="vid1",
        url="https://youtu.be/vid1",
        subtitles=subtitles,
        automatic_captions=automatic_captions,
        duration_sec=120,
    )


def _tres(text="hello", source="captions", language="en") -> TranscriptResult:
    return TranscriptResult(
        text=text,
        source=source,
        language=language,
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=[(0, text)],
    )


# --- requested-language resolution ------------------------------------------


def test_requested_lang_prefers_an_explicit_transcript_lang():
    from unread.youtube.cache import resolve_requested_lang

    assert resolve_requested_lang(transcript_lang="fr", preferred_langs=["ru"], source="auto") == "fr"


def test_requested_lang_falls_back_to_the_top_preference():
    from unread.youtube.cache import resolve_requested_lang

    assert resolve_requested_lang(transcript_lang=None, preferred_langs=["ru", "en"], source="auto") == "ru"


def test_requested_lang_normalizes_a_variant_tag():
    from unread.youtube.cache import resolve_requested_lang

    assert resolve_requested_lang(transcript_lang="en-US", preferred_langs=[], source="auto") == "en"


def test_requested_lang_is_language_independent_for_forced_audio():
    """Whisper output doesn't depend on the requested language, so every
    `--youtube-source audio` request shares one cache row."""
    from unread.youtube.cache import AUDIO_CACHE_KEY, resolve_requested_lang

    assert resolve_requested_lang(transcript_lang="fr", preferred_langs=["ru"], source="audio") == (
        AUDIO_CACHE_KEY
    )


def test_requested_lang_is_empty_without_any_preference():
    from unread.youtube.cache import resolve_requested_lang

    assert resolve_requested_lang(transcript_lang=None, preferred_langs=[], source="auto") == ""


# --- lookup ------------------------------------------------------------------


async def test_lookup_hits_an_exact_language_row(repo: Repo) -> None:
    from unread.youtube.cache import load_cached, save_transcript

    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=_tres("привет", language="ru"))
    hit = await load_cached(repo, video_id="vid1", requested_lang="ru", meta=_meta())
    assert hit is not None
    assert hit.text == "привет"


async def test_lookup_misses_a_different_language(repo: Repo) -> None:
    from unread.youtube.cache import load_cached, save_transcript

    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=_tres(language="ru"))
    assert await load_cached(repo, video_id="vid1", requested_lang="en", meta=_meta()) is None


async def test_lookup_reuses_a_whisper_row_when_the_language_has_no_captions(repo: Repo) -> None:
    """Second admin asks for EN; the video has no EN captions, so their
    request would end in Whisper anyway — reuse instead of re-billing."""
    from unread.youtube.cache import load_cached, save_transcript

    await save_transcript(
        repo, video_id="vid1", requested_lang="ru", tres=_tres("spoken", source="audio", language="ru")
    )
    hit = await load_cached(
        repo,
        video_id="vid1",
        requested_lang="en",
        meta=_meta(),
    )
    assert hit is not None
    assert hit.source == "audio"


async def test_lookup_does_not_reuse_whisper_when_captions_exist_for_the_request(repo: Repo) -> None:
    """The video DOES have EN captions, so the EN request must fetch them
    rather than inherit somebody else's Russian Whisper transcript."""
    from unread.youtube.cache import load_cached, save_transcript

    await save_transcript(
        repo, video_id="vid1", requested_lang="ru", tres=_tres("spoken", source="audio", language="ru")
    )
    meta = _meta(subtitles={"en": [{}]})
    assert await load_cached(repo, video_id="vid1", requested_lang="en", meta=meta) is None


async def test_lookup_falls_back_to_a_legacy_youtube_videos_row(repo: Repo) -> None:
    """Caches written before this table existed must not all miss at once."""
    from unread.youtube.cache import load_cached

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
        transcript="legacy text",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
    )
    hit = await load_cached(repo, video_id="vid1", requested_lang="ru", meta=_meta())
    assert hit is not None
    assert hit.text == "legacy text"


async def test_legacy_row_is_ignored_for_a_different_language(repo: Repo) -> None:
    from unread.youtube.cache import load_cached

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
        transcript="legacy text",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
    )
    meta = _meta(subtitles={"en": [{}]})
    assert await load_cached(repo, video_id="vid1", requested_lang="en", meta=meta) is None


async def test_save_then_load_preserves_timed_cues(repo: Repo) -> None:
    from unread.youtube.cache import load_cached, save_transcript

    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=_tres("привет", language="ru"))
    hit = await load_cached(repo, video_id="vid1", requested_lang="ru", meta=_meta())
    assert hit.timed_cues == [(0, "привет")]


# --- the fallback notice -----------------------------------------------------


def test_fallback_notice_when_delivery_differs_from_request():
    from unread.youtube.cache import fallback_notice

    msg = fallback_notice(requested="en", delivered="ru")
    assert msg
    assert "en" in msg.lower() or "english" in msg.lower()
    assert "ru" in msg.lower() or "russian" in msg.lower()


def test_no_fallback_notice_when_they_match():
    from unread.youtube.cache import fallback_notice

    assert fallback_notice(requested="ru", delivered="ru") == ""


def test_no_fallback_notice_for_a_variant_of_the_same_language():
    from unread.youtube.cache import fallback_notice

    assert fallback_notice(requested="en", delivered="en-US") == ""


def test_no_fallback_notice_without_a_request():
    from unread.youtube.cache import fallback_notice

    assert fallback_notice(requested="", delivered="ru") == ""


async def test_load_exact_hits_without_metadata(repo: Repo) -> None:
    from unread.youtube.cache import load_exact, save_transcript

    await save_transcript(repo, video_id="vid1", requested_lang="ru", tres=_tres("привет", language="ru"))
    hit = await load_exact(repo, video_id="vid1", requested_lang="ru", duration_sec=120)
    assert hit is not None and hit.text == "привет"


async def test_load_exact_does_not_reuse_a_whisper_row(repo: Repo) -> None:
    """The whisper-reuse rule needs a real caption inventory to be safe,
    so the metadata-free lookup must not apply it."""
    from unread.youtube.cache import load_exact, save_transcript

    await save_transcript(
        repo, video_id="vid1", requested_lang="ru", tres=_tres(source="audio", language="ru")
    )
    assert await load_exact(repo, video_id="vid1", requested_lang="en") is None


async def test_load_exact_falls_back_to_a_legacy_row(repo: Repo) -> None:
    """Pre-upgrade installs keep their cache without a metadata round-trip."""
    from unread.youtube.cache import load_exact

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
        language="en-US",
        transcript="legacy english",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
    )
    # Base-prefix aware: request "en" matches a stored "en-US".
    hit = await load_exact(repo, video_id="vid1", requested_lang="en")
    assert hit is not None and hit.text == "legacy english"


async def test_load_exact_ignores_a_legacy_row_in_another_language(repo: Repo) -> None:
    from unread.youtube.cache import load_exact

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
        transcript="legacy russian",
        transcript_source="captions",
        transcript_model=None,
        transcript_cost_usd=0.0,
        transcript_timed=None,
    )
    assert await load_exact(repo, video_id="vid1", requested_lang="en") is None
