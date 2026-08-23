"""Ask-over-YouTube adapter. Wraps unread.youtube.transcript."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from unread.ask.sources.core import DocCitation, cmd_ask_document
from unread.ask.sources.file import _prompt_question  # reuse the same TTY-prompt helper
from unread.config import get_settings

console = Console()


def _fmt_hms(seconds: int | None) -> str:
    sec = max(0, int(seconds or 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _render_timed(cues: list[tuple[int, str]]) -> str:
    """Format captions as ``[HH:MM:SS] line`` rows so the LLM can quote seekable timecodes."""
    return "\n".join(f"[{_fmt_hms(start)}] {body}".strip() for start, body in cues)


async def cmd_ask_youtube(
    ref: str,
    question: str | None,
    *,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    no_console: bool = False,
    no_save: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
    no_followup: bool = False,
    semantic: bool = False,
    build_index: bool = False,
    rerank: bool | None = None,
    limit: int = 200,
    show_retrieved: bool = False,
    youtube_source: str = "auto",
    transcript_lang: str | None = None,
) -> None:
    """Fetch a YouTube transcript and ask a question over it.

    Mirrors `cmd_analyze_youtube`'s pre-LLM flow: render the metadata
    panel, run the interactive captions-vs-Whisper picker on a TTY when
    `youtube_source="auto"`, log the transcript-ready status. Without
    this the user typed a question and waited blindly while Whisper
    transcoded a multi-megabyte audio stream — even though the video
    had captions readily available.
    """
    from unread.db.repo import open_repo
    from unread.util.pricing import audio_cost
    from unread.youtube.commands import (
        WHISPER_LANG_SENTINEL,
        _dedup_display_tracks,
        _interactive_pick_caption_lang,
        _interactive_pick_source,
        _is_interactive,
        _render_metadata_panel,
        _require_audio_ffmpeg,
        _restore_metadata_from_row,
    )
    from unread.youtube.metadata import fetch_metadata
    from unread.youtube.transcript import (
        NoTranscriptAvailable,
        TranscriptSource,
        YoutubeFetchError,
        _preferred_caption_langs,
        get_transcript,
    )
    from unread.youtube.urls import extract_video_id

    settings = get_settings()
    video_id = extract_video_id(ref)
    if not video_id:
        console.print(f"[red]Could not extract a YouTube video id from: {ref}[/]")
        raise typer.Exit(2)

    # Forced-Whisper path needs ffmpeg up front; bail with a friendly
    # message instead of dying mid-pipeline. Mirrors analyze.
    if youtube_source == "audio":
        _require_audio_ffmpeg()

    async with open_repo(settings.storage.data_path) as repo:
        from unread.youtube.cache import (
            fallback_notice,
            load_cached,
            load_exact,
            resolve_requested_lang,
            save_transcript,
        )

        # Caption preference doubles as the transcript cache key, and is
        # settings-only — computed before any network work. See
        # `unread/youtube/cache.py` for why the key is the REQUESTED
        # language rather than the delivered one.
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

        cached = await repo.get_youtube_video(video_id)
        cached_tres = await load_exact(
            repo,
            video_id=video_id,
            requested_lang=requested_lang,
            duration_sec=(cached or {}).get("duration_sec"),
        )
        if cached and cached_tres is not None:
            # Cached transcript path — read text directly from the row,
            # do NOT re-call get_transcript (which would re-download
            # captions / re-transcode audio even though we already have
            # the text). Mirrors `cmd_analyze_youtube`'s cached branch.
            console.print(f"[grey70]Using cached YouTube metadata + transcript ({video_id})[/]")
            meta = _restore_metadata_from_row(cached)
            console.print(_render_metadata_panel(meta, audio_estimate=0.0))
            timed_cues = cached_tres.timed_cues
            text = _render_timed(timed_cues).strip() if timed_cues else (cached_tres.text or "").strip()
            console.print(f"[green]Transcript ready[/] ({cached_tres.source}, {len(text):,} chars, cached)")
            if notice := fallback_notice(requested=requested_lang, delivered=cached_tres.language):
                console.print(f"[yellow]{notice}[/]")
        else:
            if cached:
                meta = _restore_metadata_from_row(cached)
            else:
                console.print(f"[grey70]Fetching YouTube metadata for {video_id}…[/]")
                try:
                    meta = await fetch_metadata(video_id)
                except YoutubeFetchError as e:
                    console.print(f"[red]YouTube fetch failed: {str(e)[:300]}[/]")
                    raise typer.Exit(1) from e

            audio_estimate = float(audio_cost(settings.openai.audio_model_default, meta.duration_sec) or 0.0)
            console.print(_render_metadata_panel(meta, audio_estimate=audio_estimate))

            # Second chance now that `meta` carries a real caption
            # inventory: reuses a Whisper transcript somebody already paid
            # for when this request has no caption track of its own.
            reuse = await load_cached(repo, video_id=video_id, requested_lang=requested_lang, meta=meta)
            if reuse is not None:
                console.print(f"[grey70]Reusing a cached transcript ({reuse.source})[/]")

            effective_source: TranscriptSource = youtube_source  # type: ignore[assignment]
            if reuse is None and youtube_source == "auto" and not yes and _is_interactive():
                picked = await _interactive_pick_source(meta, audio_estimate=audio_estimate)
                if picked is None:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(0)
                effective_source = picked

            # Caption-language picker — identical skip guards / wiring as
            # `cmd_analyze_youtube`'s fresh-fetch branch. `preselect` is
            # computed once at the top of this repo block (it also feeds
            # the cache key).
            effective_transcript_lang: str | None = transcript_lang
            if (
                reuse is None
                and transcript_lang is None
                and not yes
                and _is_interactive()
                and effective_source != "audio"
            ):
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

            if reuse is not None:
                tres = reuse
            else:
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
                    console.print(f"[red]YouTube fetch failed: {str(e)[:300]}[/]")
                    raise typer.Exit(1) from e
            text = _render_timed(tres.timed_cues).strip() if tres.timed_cues else (tres.text or "").strip()
            cost_str = f", ${tres.cost_usd:.4f}" if tres.cost_usd > 0 else ""
            console.print(f"[green]Transcript ready[/] ({tres.source}, {len(text):,} chars{cost_str})")
            if notice := fallback_notice(requested=requested_lang, delivered=tres.language):
                console.print(f"[yellow]{notice}[/]")

            # `ask` used to read the transcript cache without ever writing
            # it, so an ask-first workflow re-fetched every time. Cheap to
            # fix now that there is a per-language table to write into.
            await save_transcript(
                repo,
                video_id=video_id,
                requested_lang=requested_lang,
                tres=tres,
                transcript_model=(settings.openai.audio_model_default if tres.source == "audio" else None),
            )

    if not text:
        console.print("[red]Transcript is empty — nothing to answer over.[/]")
        raise typer.Exit(2)

    source_label = f"YouTube · {meta.title or video_id}"
    source_id = f"yt:{video_id}"
    content_hash = f"yt:{video_id}:{len(text)}"
    citations = [
        DocCitation(
            uri=f"https://youtu.be/{video_id}",
            label=meta.title or video_id,
            offset_start=0,
            offset_end=len(text),
        )
    ]
    used_question = question if question else await _prompt_question(source_label)
    await cmd_ask_document(
        extracted_text=text,
        citations=citations,
        source_label=source_label,
        source_id=source_id,
        source_kind="youtube",
        content_hash=content_hash,
        question=used_question,
        model=model,
        output=output,
        console_out=console_out,
        no_console=no_console,
        no_save=no_save,
        max_cost=max_cost,
        yes=yes,
        language=language,
        report_language=report_language,
        source_language=source_language,
        no_followup=no_followup,
        semantic=semantic,
        build_index=build_index,
        rerank=rerank,
        limit=limit,
        show_retrieved=show_retrieved,
    )
