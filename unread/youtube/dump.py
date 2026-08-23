"""`unread dump <youtube-url>` — transcript / audio / video artifacts.

Mirrors :mod:`unread.youtube.commands` shape (fetch metadata, optional
transcript / download) but skips the LLM analysis. Three modes:

- ``transcript`` — ``metadata.json`` + ``transcript.md`` (plain text,
  no per-cue timestamps). Honors the existing ``--youtube-source`` flag.
  Per-cue timing is still cached in the DB for the analyze / ask paths,
  it just isn't emitted into the dump directory.
- ``audio`` — ``metadata.json`` + ``audio.mp3`` (yt-dlp + ffmpeg).
- ``video`` — ``metadata.json`` + ``video.mp4`` / ``.mkv`` /
  ``.webm`` (yt-dlp + ffmpeg merging).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import get_logger
from unread.util.preflight import require_ffmpeg
from unread.youtube.commands import _meta_header, _restore_metadata_from_row
from unread.youtube.download import (
    YoutubeDownloadError,
    download_audio,
    download_video,
)
from unread.youtube.metadata import YoutubeMetadata, fetch_metadata
from unread.youtube.paths import youtube_dump_dir
from unread.youtube.transcript import (
    NoTranscriptAvailable,
    TranscriptResult,
    TranscriptSource,
    YoutubeFetchError,
    get_transcript,
)
from unread.youtube.urls import extract_video_id

console = Console()
log = get_logger(__name__)

YoutubeDumpMode = Literal["transcript", "audio", "video"]


def _metadata_dict(meta: YoutubeMetadata) -> dict:
    """JSON-serializable view of the metadata.

    Drops the bulky ``subtitles`` / ``automatic_captions`` blobs — they
    pull thousands of internal yt-dlp URLs that aren't useful for users
    reading a dump report.
    """
    out = asdict(meta)
    out.pop("subtitles", None)
    out.pop("automatic_captions", None)
    return out


def _build_transcript_md(meta: YoutubeMetadata, tres: TranscriptResult, *, notice: str = "") -> str:
    """Markdown body: meta header + plain transcript text (no per-cue timestamps).

    Per-cue timing is preserved in the DB (and used by the analyze / ask
    paths so the LLM can quote ``[HH:MM:SS]`` markers) but the dump
    artifact is plain reading copy.

    `notice` carries the "you asked for English, this is Russian" warning.
    It goes INTO the file rather than only to the console because the bot
    uploads this file and never shows console output — without it a bot
    user gets a transcript in an unexpected language with no explanation.
    """
    header = _meta_header(meta)
    body = (tres.text or "").strip()
    parts = [header, ""]
    if notice:
        parts += [f"> ⚠️ {notice}", ""]
    parts += ["## Transcript", "", body]
    return "\n".join(parts).rstrip() + "\n"


def _resolve_dump_dir(
    output: Path | None,
    meta: YoutubeMetadata,
    mode: YoutubeDumpMode,
) -> Path:
    if output:
        return output
    return youtube_dump_dir(
        video_id=meta.video_id,
        title=meta.title,
        channel_title=meta.channel_title,
        channel_id=meta.channel_id,
        mode=mode,
        stamp=datetime.now(),
    )


def _write_metadata(meta: YoutubeMetadata, dest: Path) -> None:
    dest.write_text(
        json.dumps(_metadata_dict(meta), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


async def _resolve_metadata(repo, video_id: str) -> YoutubeMetadata:
    cached = await repo.get_youtube_video(video_id)
    if cached:
        return _restore_metadata_from_row(cached)
    try:
        return await fetch_metadata(video_id)
    except YoutubeFetchError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e


async def _do_transcript_mode(
    *,
    repo,
    meta: YoutubeMetadata,
    youtube_source: TranscriptSource,
    dump_dir: Path,
    settings,
    cached_row: dict | None,
    transcript_lang: str | None = None,
    language: str = "en",
    report_language: str = "en",
    source_language: str = "",
    yes: bool = False,
) -> None:
    from unread.youtube.cache import (
        load_cached,
        load_exact,
        resolve_requested_lang,
        save_transcript,
    )
    from unread.youtube.commands import _require_audio_ffmpeg
    from unread.youtube.transcript import _preferred_caption_langs

    # `meta` came from `_restore_metadata_from_row` iff `cmd_dump_youtube`
    # found a cache row (that helper hard-nulls the caption inventory — the
    # DB row doesn't store `subtitles` / `automatic_captions`). Remember it
    # so the fresh-fetch branch below can repopulate the inventory before it
    # runs the picker / `get_transcript`.
    meta_from_cache = cached_row is not None

    # `--youtube-source audio` needs ffmpeg up front — mirrors
    # `cmd_analyze_youtube`'s early preflight so a missing binary surfaces
    # a friendly banner before any network work starts instead of failing
    # deep inside `get_transcript`.
    if youtube_source == "audio":
        _require_audio_ffmpeg()

    # Caption preference doubles as the cache key — see
    # `unread/youtube/cache.py`. Computed before any network work.
    preselect = _preferred_caption_langs(
        settings,
        content_language=source_language or None,
        report_language=report_language or None,
        ui_language=language or None,
    )
    requested_lang = resolve_requested_lang(
        transcript_lang=transcript_lang,
        preferred_langs=preselect,
        source=youtube_source,
    )

    tres = await load_exact(
        repo,
        video_id=meta.video_id,
        requested_lang=requested_lang,
        duration_sec=meta.duration_sec,
    )
    if tres is None:
        from unread.youtube.commands import (
            WHISPER_LANG_SENTINEL,
            _dedup_display_tracks,
            _interactive_pick_caption_lang,
            _is_interactive,
        )

        # A cache-restored `meta` has no caption inventory. We're about to
        # fetch a fresh transcript, so re-fetch metadata to repopulate
        # `subtitles` / `automatic_captions` — otherwise the picker sees zero
        # tracks and `get_transcript` finds no candidates: under `auto` it
        # silently downloads audio and bills Whisper (ignoring the requested
        # caption language); under `captions` it falsely raises "no captions".
        # Must happen before the second-chance lookup, the picker, AND
        # `get_transcript` below — the lookup's whisper-reuse rule reads the
        # same inventory.
        if meta_from_cache and not (meta.subtitles or meta.automatic_captions):
            try:
                meta = await fetch_metadata(meta.video_id)
            except YoutubeFetchError as e:
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e

        tres = await load_cached(repo, video_id=meta.video_id, requested_lang=requested_lang, meta=meta)

    if tres is None:
        effective_source: TranscriptSource = youtube_source
        effective_transcript_lang: str | None = transcript_lang
        if transcript_lang is None and not yes and _is_interactive() and effective_source != "audio":
            tracks = _dedup_display_tracks(meta)
            if len(tracks) > 1:
                picked_lang = await _interactive_pick_caption_lang(meta, preselect=preselect)
                if picked_lang is None:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(0)
                if picked_lang == WHISPER_LANG_SENTINEL:
                    effective_source = "audio"
                    _require_audio_ffmpeg()
                else:
                    effective_transcript_lang = picked_lang

        try:
            tres = await get_transcript(
                meta,
                source=effective_source,
                settings=settings,
                repo=repo,
                transcript_lang=effective_transcript_lang,
                preferred_langs=preselect,
            )
        except NoTranscriptAvailable as e:
            raise typer.BadParameter(str(e)) from e
        except YoutubeFetchError as e:
            console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
            console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
            raise typer.Exit(1) from e

        transcript_lang_kind = None if tres.is_auto is None else ("auto" if tres.is_auto else "manual")
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
            language=tres.language,
            transcript=tres.text,
            transcript_source=tres.source,
            transcript_model=(settings.openai.audio_model_default if tres.source == "audio" else None),
            transcript_cost_usd=tres.cost_usd,
            transcript_timed=tres.timed_cues,
            transcript_lang_kind=transcript_lang_kind,
        )
        # Recomputed from what we're ACTUALLY about to fetch, not from the
        # default preference: the picker may have changed the caption
        # language or switched to Whisper since `requested_lang` was
        # derived. Saving under the default key made one interactive
        # "German" choice serve German to every later default run.
        save_lang = resolve_requested_lang(
            transcript_lang=effective_transcript_lang,
            preferred_langs=preselect,
            source=effective_source,
        )
        await save_transcript(
            repo,
            video_id=meta.video_id,
            requested_lang=save_lang,
            tres=tres,
            transcript_model=(settings.openai.audio_model_default if tres.source == "audio" else None),
        )

    if not (tres.text or "").strip():
        console.print(f"[red]{_t('cli_error_prefix')}[/] {_t('err_files_empty_transcript')}")
        raise typer.Exit(2)

    from unread.youtube.cache import fallback_notice

    notice = fallback_notice(
        requested=requested_lang,
        delivered=tres.language,
        source=tres.source,
        language=language or None,
    )

    _write_metadata(meta, dump_dir / "metadata.json")
    (dump_dir / "transcript.md").write_text(_build_transcript_md(meta, tres, notice=notice), encoding="utf-8")

    if notice:
        console.print(f"[yellow]{notice}[/]")
    console.print(f"[green]{_tf('dump_youtube_transcript_done', path=dump_dir)}[/]")


async def _do_audio_mode(*, meta: YoutubeMetadata, dump_dir: Path) -> None:
    require_ffmpeg("download YouTube audio")
    _write_metadata(meta, dump_dir / "metadata.json")
    try:
        downloaded = await download_audio(meta, dump_dir)
    except YoutubeDownloadError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e
    final = dump_dir / "audio.mp3"
    if downloaded != final:
        downloaded.rename(final)
    console.print(f"[green]{_tf('dump_youtube_audio_done', path=dump_dir)}[/]")


async def _do_video_mode(*, meta: YoutubeMetadata, dump_dir: Path) -> None:
    require_ffmpeg("download YouTube video")
    _write_metadata(meta, dump_dir / "metadata.json")
    try:
        downloaded = await download_video(meta, dump_dir)
    except YoutubeDownloadError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e
    final = dump_dir / f"video{downloaded.suffix}"
    if downloaded != final:
        downloaded.rename(final)
    console.print(f"[green]{_tf('dump_youtube_video_done', path=dump_dir)}[/]")


async def cmd_dump_youtube(
    *,
    url: str,
    mode: YoutubeDumpMode,
    youtube_source: TranscriptSource,
    output: Path | None,
    console_out: bool,
    language: str,
    report_language: str,
    source_language: str,
    yes: bool,
    transcript_lang: str | None = None,
    prefetched_meta: YoutubeMetadata | None = None,
) -> Path:
    """Dump a YouTube video. Mode picks the artifact (transcript / audio / video).

    Returns the directory the artifacts were written into, so callers
    that need the concrete `transcript.md` path (the bot's transcript
    button, the analyze picker's "dump instead" row) don't have to
    re-derive the slug + timestamp.

    `prefetched_meta` short-circuits the metadata lookup for callers
    that already paid for a yt-dlp round-trip — `cmd_analyze_youtube`
    fetches metadata before it shows the source picker, so handing the
    object over avoids a second identical call.
    """
    settings = get_settings()
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    async with open_repo(settings.storage.data_path) as repo:
        cached_row = await repo.get_youtube_video(video_id)
        if prefetched_meta is not None:
            # Caller's object wins over the DB row: `_restore_metadata_from_row`
            # hard-nulls the caption inventory, while a freshly fetched meta
            # still carries `subtitles` / `automatic_captions`.
            meta = prefetched_meta
        elif cached_row:
            meta = _restore_metadata_from_row(cached_row)
        else:
            try:
                meta = await fetch_metadata(video_id)
            except YoutubeFetchError as e:
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e

        dump_dir = _resolve_dump_dir(output, meta, mode)
        dump_dir.mkdir(parents=True, exist_ok=True)

        if mode == "transcript":
            await _do_transcript_mode(
                repo=repo,
                meta=meta,
                youtube_source=youtube_source,
                dump_dir=dump_dir,
                settings=settings,
                cached_row=cached_row,
                transcript_lang=transcript_lang,
                language=language,
                report_language=report_language,
                source_language=source_language,
                yes=yes,
            )
        elif mode == "audio":
            await _do_audio_mode(meta=meta, dump_dir=dump_dir)
        elif mode == "video":
            await _do_video_mode(meta=meta, dump_dir=dump_dir)
        else:
            raise typer.BadParameter(f"Unknown dump mode {mode!r}")

    if console_out and mode == "transcript":
        console.print((dump_dir / "transcript.md").read_text(encoding="utf-8"))

    return dump_dir
