"""`youtube_transcripts` — one cached transcript per (video, requested language).

`youtube_videos` holds a single transcript per video, so two admins with
different languages evict each other: A asks for RU, B asks for EN, and
every alternating request re-fetches (and can re-bill Whisper). Keying on
the REQUESTED language — not the delivered one — also makes "asked for EN,
video only has RU" a cache hit next time instead of a permanent miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unread.db.repo import Repo


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def _put(repo: Repo, **overrides):
    base = {
        "video_id": "vid1",
        "requested_lang": "ru",
        "language": "ru",
        "transcript": "привет",
        "transcript_source": "captions",
        "transcript_lang_kind": "manual",
        "transcript_model": None,
        "transcript_cost_usd": 0.0,
        "transcript_timed": [(0, "привет")],
    }
    base.update(overrides)
    await repo.put_youtube_transcript(**base)


async def test_put_and_get_round_trip(repo: Repo) -> None:
    await _put(repo)
    row = await repo.get_youtube_transcript("vid1", "ru")
    assert row is not None
    assert row["transcript"] == "привет"
    assert row["language"] == "ru"
    assert row["transcript_source"] == "captions"


async def test_two_languages_coexist_for_one_video(repo: Repo) -> None:
    """The headline fix — RU and EN admins stop evicting each other."""
    await _put(repo, requested_lang="ru", language="ru", transcript="привет")
    await _put(repo, requested_lang="en", language="en", transcript="hello")
    assert (await repo.get_youtube_transcript("vid1", "ru"))["transcript"] == "привет"
    assert (await repo.get_youtube_transcript("vid1", "en"))["transcript"] == "hello"


async def test_miss_for_an_unrequested_language(repo: Repo) -> None:
    await _put(repo, requested_lang="ru")
    assert await repo.get_youtube_transcript("vid1", "de") is None


async def test_fallback_delivery_is_cached_under_the_requested_language(repo: Repo) -> None:
    """Asked for EN, video is RU-only → store under 'en' with language='ru'.
    Re-asking for EN must HIT, not re-fetch forever."""
    await _put(repo, requested_lang="en", language="ru", transcript="привет")
    row = await repo.get_youtube_transcript("vid1", "en")
    assert row is not None
    assert row["language"] == "ru"
    assert row["requested_lang"] == "en"


async def test_put_is_idempotent_for_the_same_key(repo: Repo) -> None:
    await _put(repo, transcript="first")
    await _put(repo, transcript="second")
    assert (await repo.get_youtube_transcript("vid1", "ru"))["transcript"] == "second"


async def test_empty_requested_lang_is_its_own_key(repo: Repo) -> None:
    """Scripted runs (`--yes`, no locale) express no preference."""
    await _put(repo, requested_lang="", language="ru")
    assert await repo.get_youtube_transcript("vid1", "") is not None
    assert await repo.get_youtube_transcript("vid1", "ru") is None


async def test_timed_cues_round_trip_as_json(repo: Repo) -> None:
    await _put(repo, transcript_timed=[(0, "a"), (5, "b")])
    row = await repo.get_youtube_transcript("vid1", "ru")
    import json

    assert json.loads(row["transcript_timed_json"]) == [[0, "a"], [5, "b"]]


async def test_find_audio_transcript_returns_a_whisper_row(repo: Repo) -> None:
    """Whisper output doesn't depend on the requested language, so a second
    admin must reuse it instead of paying for the same transcription."""
    await _put(repo, requested_lang="ru", language="ru", transcript_source="audio")
    found = await repo.find_youtube_transcript_by_source("vid1", "audio")
    assert found is not None
    assert found["transcript_source"] == "audio"


async def test_find_audio_transcript_ignores_caption_rows(repo: Repo) -> None:
    await _put(repo, requested_lang="ru", transcript_source="captions")
    assert await repo.find_youtube_transcript_by_source("vid1", "audio") is None


async def test_find_audio_transcript_is_scoped_to_the_video(repo: Repo) -> None:
    await _put(repo, video_id="other", transcript_source="audio")
    assert await repo.find_youtube_transcript_by_source("vid1", "audio") is None
