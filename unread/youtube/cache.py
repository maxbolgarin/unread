"""Transcript cache policy shared by analyze / dump / ask.

The three entry points used to each carry their own copy of "is this
cached row good enough?", keyed on the transcript's *delivered* language.
That works for one user and breaks for two: with a single row per video,
a Russian request and an English request evict each other on every
alternating call, re-downloading captions and — when there are none —
re-billing Whisper.

The rule here keys on what the caller **asked for**:

* exact `(video_id, requested_lang)` row → hit;
* no row, and the video has no captions in the requested language, but a
  Whisper transcript exists → hit, because transcription output doesn't
  depend on the request and re-running it would just cost money again;
* still nothing → fall back to a pre-upgrade `youtube_videos` row when
  its language matches, so existing installs don't lose their cache;
* otherwise miss.

Keying on the request is also what makes the fallback case cacheable at
all: ask for English on a Russian-only video and you get Russian back,
stored under `'en'`. The next English request is a hit rather than a
permanent miss that re-fetches forever.
"""

from __future__ import annotations

import json
from typing import Any

from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import (
    TranscriptResult,
    _lang_base,
    list_caption_tracks,
)

# Cache key for transcripts that don't depend on the requested language.
# Every `--youtube-source audio` run shares this row: Whisper hears what
# it hears regardless of which language the caller would have preferred.
AUDIO_CACHE_KEY = "__audio__"


def resolve_requested_lang(
    *,
    transcript_lang: str | None,
    preferred_langs: list[str] | None,
    source: str,
) -> str:
    """The cache key for this run's language request.

    An explicit `--transcript-lang` wins; otherwise the top caption
    preference (which the bot derives from the admin's `/lang`). Empty
    when the caller expressed no preference at all — a scripted `--yes`
    run on an install with no locale configured.
    """
    if source == "audio":
        return AUDIO_CACHE_KEY
    if transcript_lang:
        return _lang_base(transcript_lang)
    if preferred_langs:
        return _lang_base(preferred_langs[0])
    return ""


def _has_captions_for(meta: YoutubeMetadata, lang: str) -> bool:
    """True when the video offers any caption track in `lang`."""
    if not lang or lang == AUDIO_CACHE_KEY:
        return False
    base = _lang_base(lang)
    return any(track.base == base for track in list_caption_tracks(meta))


def _row_to_result(row: dict[str, Any], *, duration_sec: int | None) -> TranscriptResult:
    timed: list[tuple[int, str]] | None = None
    raw = row.get("transcript_timed_json")
    if raw:
        try:
            timed = [(int(s), str(t)) for s, t in json.loads(raw)]
        except (TypeError, ValueError):
            timed = None
    # Restore manual-vs-auto. Without it every cached run derived
    # `is_auto=None`, which degraded the report's `Transcript:` row to a
    # bare language code AND — because `save_transcript` recomputes the
    # column from this field with no COALESCE — overwrote the stored
    # value with NULL on the next save.
    kind = row.get("transcript_lang_kind")
    is_auto = True if kind == "auto" else (False if kind == "manual" else None)
    return TranscriptResult(
        text=row.get("transcript") or "",
        source=row.get("transcript_source") or "captions",  # type: ignore[arg-type]
        language=row.get("language"),
        duration_sec=duration_sec,
        cost_usd=float(row.get("transcript_cost_usd") or 0.0),
        timed_cues=timed,
        is_auto=is_auto,
    )


async def load_exact(
    repo: Any,
    *,
    video_id: str,
    requested_lang: str,
    duration_sec: int | None = None,
) -> TranscriptResult | None:
    """Cache hit reachable without metadata: exact row, then legacy row.

    Separate from `load_cached` so callers can try the common case before
    paying for a yt-dlp metadata call. That separation is load-bearing in
    two directions:

    * a cache-restored `YoutubeMetadata` has its caption inventory
      nulled, so judging caption availability with one would wrongly
      conclude "no captions here" and reuse a Whisper row — hence the
      whisper-reuse rule lives in `load_cached`, which demands real
      metadata;
    * the legacy `youtube_videos` fallback needs no inventory, so it
      belongs here. Leaving it out made every pre-upgrade install re-fetch
      metadata once per video despite having the transcript on disk.
    """
    row = await repo.get_youtube_transcript(video_id, requested_lang)
    if row is None:
        row = await _legacy_row(repo, video_id=video_id, requested_lang=requested_lang)
    if row is None or not (row.get("transcript") or "").strip():
        return None
    return _row_to_result(row, duration_sec=duration_sec)


async def load_cached(
    repo: Any,
    *,
    video_id: str,
    requested_lang: str,
    meta: YoutubeMetadata,
) -> TranscriptResult | None:
    """Best cached transcript for this request, or None to go fetch one.

    `meta` must carry a real caption inventory — pass a freshly fetched
    one, not a `_restore_metadata_from_row` reconstruction.
    """
    row = await repo.get_youtube_transcript(video_id, requested_lang)

    if row is None and not _has_captions_for(meta, requested_lang):
        # This request has no caption track to fetch, so it would end in
        # Whisper. If somebody already paid for that, reuse it.
        row = await repo.find_youtube_transcript_by_source(video_id, "audio")

    if row is None:
        row = await _legacy_row(repo, video_id=video_id, requested_lang=requested_lang)

    if row is None or not (row.get("transcript") or "").strip():
        return None
    return _row_to_result(row, duration_sec=meta.duration_sec)


async def _legacy_row(repo: Any, *, video_id: str, requested_lang: str) -> dict[str, Any] | None:
    """Pre-upgrade `youtube_videos.transcript`, when its language fits.

    Without this every existing install would re-fetch every video once
    after upgrading. Only accepted when the stored language matches the
    request (or the caller has no preference) — otherwise it is exactly
    the wrong-language row this module exists to stop serving.
    """
    legacy = await repo.get_youtube_video(video_id)
    if not legacy or not (legacy.get("transcript") or "").strip():
        return None
    if not requested_lang or requested_lang == AUDIO_CACHE_KEY:
        return legacy
    if _lang_base(legacy.get("language") or "") == _lang_base(requested_lang):
        return legacy
    return None


async def save_transcript(
    repo: Any,
    *,
    video_id: str,
    requested_lang: str,
    tres: TranscriptResult,
    transcript_model: str | None = None,
) -> None:
    """Cache a freshly fetched transcript under the requested language."""
    lang_kind = None if tres.is_auto is None else ("auto" if tres.is_auto else "manual")
    await repo.put_youtube_transcript(
        video_id=video_id,
        requested_lang=requested_lang,
        language=tres.language,
        transcript=tres.text or "",
        transcript_source=tres.source,
        transcript_lang_kind=lang_kind,
        transcript_model=transcript_model,
        transcript_cost_usd=tres.cost_usd,
        transcript_timed=tres.timed_cues,
    )


def fallback_notice(
    *,
    requested: str,
    delivered: str | None,
    source: str | None = None,
    language: str | None = None,
) -> str:
    """One-line warning when the transcript isn't in the requested language.

    Returns "" when they match, when the request was a variant of the
    delivered language (`en` vs `en-US`), or when nothing was requested.
    The caller decides where to show it — a bot caption, a console line,
    the top of `transcript.md`.

    `delivered=None` is NOT silence when the transcript came from Whisper:
    the audio path stores no language unless `openai.audio_language` is
    set, so short-circuiting there handed the user a wrong-language
    transcript with no warning — precisely the case this exists for.

    `language` selects the message's own language. It is written into a
    file the bot uploads, so an English sentence in a Russian admin's
    transcript would defeat the per-admin language feature.
    """
    from unread.i18n import tf as _tf
    from unread.util.languages import language_display_name

    if not requested or requested == AUDIO_CACHE_KEY:
        return ""

    req_base = _lang_base(requested)
    if not delivered:
        if source == "audio":
            return _tf("transcript_lang_unknown", language, requested=language_display_name(req_base))
        return ""

    got_base = _lang_base(delivered)
    if req_base == got_base:
        return ""
    return _tf(
        "transcript_lang_fallback",
        language,
        requested=language_display_name(req_base),
        delivered=f"{language_display_name(got_base)} ({got_base})",
    )
