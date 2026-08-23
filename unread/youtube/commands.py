"""Top-level handler for `unread analyze <youtube-url>`.

Mirrors the post-`prepare_chat_run` half of `_run_single` (no Telegram
backfill, no mark_read). Splits the transcript into multiple synthetic
`Message` rows so the existing chunker / map-reduce flow can summarize
long videos without hitting `formatter._BODY_CAP` (4000 chars/msg).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from unread.analyzer.pipeline import (
    AnalysisOptions,
    estimate_cost,
    run_analysis,
)
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.models import Message
from unread.util.logging import get_logger
from unread.util.pricing import audio_cost
from unread.youtube.metadata import YoutubeMetadata, fetch_metadata
from unread.youtube.paths import youtube_report_path
from unread.youtube.transcript import (
    CaptionTrack,
    NoTranscriptAvailable,
    TranscriptSource,
    YoutubeFetchError,
    _lang_base,
    _preferred_caption_langs,
    get_transcript,
    list_caption_tracks,
)
from unread.youtube.urls import extract_video_id

console = Console()
log = get_logger(__name__)


# Each synthetic message body must stay below `formatter._BODY_CAP` (4000)
# or the formatter will truncate with `…`. 3500 leaves headroom for any
# label additions and keeps cue-aligned splits readable.
_SEGMENT_CHARS = 3500
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _parse_upload_date(yyyymmdd: str | None) -> datetime:
    """yt-dlp gives `upload_date` as YYYYMMDD; default to now() on miss."""
    if yyyymmdd and len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        try:
            return datetime.strptime(yyyymmdd, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _meta_header(meta: YoutubeMetadata) -> str:
    """Compact metadata block prepended as the first synthetic message."""
    bits: list[str] = [f"YouTube video: {meta.title or meta.video_id}"]
    if meta.channel_title:
        bits.append(f"Channel: {meta.channel_title}")
    if meta.upload_date:
        bits.append(f"Uploaded: {meta.upload_date}")
    if meta.duration_sec:
        bits.append(f"Duration: {_fmt_hms(meta.duration_sec)}")
    if meta.view_count is not None:
        bits.append(f"Views: {meta.view_count:,}")
    if meta.like_count is not None:
        bits.append(f"Likes: {meta.like_count:,}")
    bits.append(f"URL: {meta.url}")
    if meta.description:
        desc = meta.description.strip()
        # Cap at ~1500 chars so the header itself fits comfortably under
        # _BODY_CAP even with a verbose description; long descriptions
        # rarely add detail the transcript doesn't.
        if len(desc) > 1500:
            desc = desc[:1500].rstrip() + "…"
        bits.append("")
        bits.append("Description:")
        bits.append(desc)
    return "\n".join(bits)


def _fmt_hms(seconds: int | None) -> str:
    """Format seconds as `HH:MM:SS` (or `MM:SS` when under an hour)."""
    sec = max(0, int(seconds or 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _segment_transcript(text: str, *, max_chars: int = _SEGMENT_CHARS) -> list[str]:
    """Split transcript into ≤max_chars chunks, preferring sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    out: list[str] = []
    sentences = _SENTENCE_END.split(text)
    buf = ""
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        candidate = (buf + " " + s) if buf else s
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(s) <= max_chars:
            buf = s
            continue
        # Sentence itself longer than budget — hard cut.
        tail = s
        while len(tail) > max_chars:
            out.append(tail[:max_chars])
            tail = tail[max_chars:]
        buf = tail
    if buf:
        out.append(buf)
    return out


def _segment_timed_cues(
    cues: list[tuple[int, str]],
    *,
    max_chars: int = _SEGMENT_CHARS,
) -> list[tuple[int, str]]:
    """Pack (start_sec, text) cues into segments of ≤max_chars chars.

    Each output entry's `start_sec` is the start of the FIRST cue in
    that segment — so a citation `[#start_sec]` lands the user at the
    moment the segment's content begins, not somewhere in the middle.
    """
    out: list[tuple[int, str]] = []
    cur_start: int | None = None
    cur_lines: list[str] = []
    cur_chars = 0
    for start, line in cues:
        prefix_chars = len(line) + 1  # +1 for join separator
        if cur_start is None:
            cur_start = start
            cur_lines = [line]
            cur_chars = len(line)
            continue
        if cur_chars + prefix_chars > max_chars:
            out.append((cur_start, " ".join(cur_lines)))
            cur_start = start
            cur_lines = [line]
            cur_chars = len(line)
        else:
            cur_lines.append(line)
            cur_chars += prefix_chars
    if cur_start is not None and cur_lines:
        out.append((cur_start, " ".join(cur_lines)))
    return out


def _build_synthetic_messages(
    meta: YoutubeMetadata,
    transcript_text: str,
    *,
    timed_cues: list[tuple[int, str]] | None = None,
) -> list[Message]:
    """Header + per-segment `Message` list keyed off `chat_id=0`.

    msg_id strategy: the metadata header is `msg_id=0` (so a citation to
    `#0` is a clear "header marker, not the speaker"). Transcript segments
    use `msg_id = max(prev+1, start_sec)` so each msg_id is the second-
    offset of the segment's first cue — citations like `[#754]` resolve to
    `?t=754s` via the link template override. When timed cues are not
    available (Whisper path), offsets get spread uniformly across
    `meta.duration_sec`.
    """
    upload_dt = _parse_upload_date(meta.upload_date)
    duration = max(1, int(meta.duration_sec or 0))
    sender = meta.channel_title or "YouTube"

    msgs: list[Message] = [
        Message(
            chat_id=0,
            msg_id=0,
            date=upload_dt,
            sender_name=sender,
            text=_meta_header(meta),
        )
    ]

    if timed_cues:
        timed_segments = _segment_timed_cues(timed_cues)
    else:
        plain_segments = _segment_transcript(transcript_text)
        n = max(1, len(plain_segments))
        timed_segments = [(int((i / n) * duration), seg) for i, seg in enumerate(plain_segments)]

    prev_id = 0
    for start_sec, seg in timed_segments:
        # Enforce strictly-increasing msg_ids — two short cues at the same
        # second would otherwise collide.
        msg_id = max(prev_id + 1, int(start_sec))
        prev_id = msg_id
        body = f"[{_fmt_hms(start_sec)}] {seg}"
        msgs.append(
            Message(
                chat_id=0,
                msg_id=msg_id,
                date=upload_dt + timedelta(seconds=int(start_sec)),
                sender_name=sender,
                text=body,
            )
        )
    return msgs


def _has_any_captions(meta: YoutubeMetadata) -> bool:
    return bool(meta.subtitles or meta.automatic_captions)


def _render_metadata_panel(
    meta: YoutubeMetadata, *, audio_estimate: float, captions_known: bool = True
) -> Panel:
    """Pretty-print metadata + caption availability + Whisper estimate.

    `captions_known=False` when `meta` was rebuilt from a `youtube_videos`
    row: that helper doesn't store the caption inventory, and a fresh
    fetch ALSO collapses "no captions" to None (`dict(...) or None` in
    `metadata.py`), so the object alone can't distinguish "none" from
    "not recorded". Guessing "none" printed `Captions none (Whisper
    required)` directly above a cached English caption transcript.
    """
    rows: list[str] = []
    if meta.channel_title:
        rows.append(f"[bold]Channel[/] {meta.channel_title}")
    rows.append(f"[bold]Title[/]   {meta.title or meta.video_id}")
    if meta.duration_sec:
        rows.append(f"[bold]Duration[/] {_fmt_hms(meta.duration_sec)}")
    if meta.upload_date:
        rows.append(f"[bold]Uploaded[/] {meta.upload_date}")
    if meta.view_count is not None:
        rows.append(f"[bold]Views[/]    {meta.view_count:,}")
    if meta.like_count is not None:
        rows.append(f"[bold]Likes[/]    {meta.like_count:,}")
    if captions_known:
        cap_label = "[green]available[/]" if _has_any_captions(meta) else "[yellow]none[/] (Whisper required)"
    else:
        cap_label = "[grey70]not recorded[/] (metadata from cache)"
    rows.append(f"[bold]Captions[/] {cap_label}")
    if audio_estimate > 0:
        rows.append(
            f"[bold]Whisper estimate[/] ~${audio_estimate:.4f} (audio transcription only; analysis cost is extra)"
        )
    rows.append(f"[bold]URL[/]      {meta.url}")
    if meta.description:
        desc = meta.description.strip().splitlines()[0][:200]
        rows.append("")
        rows.append(f"[grey70]{desc}…[/]" if len(meta.description) > 200 else f"[grey70]{desc}[/]")
    return Panel("\n".join(rows), title="YouTube video", border_style="cyan")


def _is_interactive() -> bool:
    """True if stdin is an interactive terminal (not piped / non-tty)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


# Sentinel returned by `_interactive_pick_source` for the "just dump the
# transcript" row. The caller translates it into a `cmd_dump_youtube`
# hand-off instead of running the analysis pipeline. Mirrors the shape of
# `WHISPER_LANG_SENTINEL` below.
DUMP_SENTINEL = "__dump__"

# Sentinel for the "fact-check instead" row. Switches the run's preset to
# `factcheck` rather than handing off to another command — fact-checking
# IS an analysis, just a different one.
FACTCHECK_SENTINEL = "__factcheck__"


async def _interactive_pick_source(
    meta: YoutubeMetadata,
    *,
    audio_estimate: float,
    allow_actions: bool = True,
    source_choices: bool = True,
) -> TranscriptSource | str | None:
    """Prompt the user to confirm + pick a transcript source.

    Returns the chosen TranscriptSource ("auto" / "captions" / "audio"),
    `DUMP_SENTINEL` for "skip the analysis, just write the transcript",
    `FACTCHECK_SENTINEL` for "run the fact-check preset instead", or
    `None` to signal cancel.

    `allow_actions=False` drops the dump / fact-check rows for callers
    that can only act on a transcript SOURCE — `unread ask` passes the
    return value straight to `get_transcript(source=...)`, where a
    sentinel would be a garbage source value.

    `source_choices=False` drops the source rows instead, for when a
    cached transcript already exists: there is nothing to fetch, but the
    user must still get to choose what to DO with it. Skipping the whole
    picker on a cache hit silently removed the transcript and fact-check
    options.
    """
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    has_captions = _has_any_captions(meta)
    audio_label = (
        f"Audio + Whisper — ~${audio_estimate:.4f} + analysis"
        if audio_estimate > 0
        else "Audio + Whisper + analysis"
    )
    choices: list = []
    if source_choices:
        choices.append(
            Choice(value="auto", label="Auto — captions if available, otherwise Whisper (recommended)")
        )
        if has_captions:
            choices.append(
                Choice(
                    value="captions",
                    label="Captions only — cheaper (skips Whisper; analysis still costs)",
                )
            )
        choices.append(Choice(value="audio", label=audio_label))
    else:
        # Transcript already cached — "analyze it" is the only source-ish
        # row left, and it's the default.
        choices.append(Choice(value="auto", label="▶ Analyze (transcript already cached)"))
    choices.append(_sep())
    if allow_actions:
        choices.append(
            Choice(
                value=DUMP_SENTINEL,
                label="📝 Transcript only — save the text as Markdown, no analysis (no LLM cost)",
            )
        )
        choices.append(
            Choice(
                value=FACTCHECK_SENTINEL,
                label="🔎 Fact-check — extract the claims and verify them (slower, costs more)",
            )
        )
        choices.append(_sep())
    choices.append(Choice(value="__cancel__", label="Cancel"))

    answer = _select(
        "Continue analysis? Pick the transcript source:",
        choices=choices,
        default_value="auto",
    )
    if answer is None or answer == "__cancel__":
        return None
    return answer


# Sentinel returned by `_interactive_pick_caption_lang` for the "None of
# these — transcribe audio with Whisper" row. Shared across the analyze /
# ask / dump wiring so callers all recognize the same value.
WHISPER_LANG_SENTINEL = "__whisper__"


def _dedup_display_tracks(meta: YoutubeMetadata) -> list[CaptionTrack]:
    """One row per base language for the picker; manual wins over auto.

    `list_caption_tracks` emits SEPARATE rows for a manual and an auto
    track of the same base language (e.g. manual `en` + auto `en-US`
    both have base `en`) — useful for the full inventory, but the
    picker should show exactly one row per base language. The winner
    is decided independently of iteration/candidate order: a manual
    track for a base never loses to an auto track for that base, even
    if (per `_subtitle_candidates`' ordering elsewhere) an exact auto
    track would sort before a prefix-matching manual track.
    """
    by_base: dict[str, CaptionTrack] = {}
    for track in list_caption_tracks(meta):
        existing = by_base.get(track.base)
        if existing is None or (existing.is_auto and not track.is_auto):
            by_base[track.base] = track
    return [by_base[base] for base in sorted(by_base)]


def _require_audio_ffmpeg() -> None:
    """ffmpeg preflight for the Whisper/audio transcript path.

    Shared by the explicit `--youtube-source audio` branch and the
    caption-language picker's "transcribe with Whisper" row so both
    surface the same friendly missing-ffmpeg banner before any
    network work starts.
    """
    from unread.util.preflight import require_ffmpeg

    require_ffmpeg("download and transcribe YouTube audio")


async def _interactive_pick_caption_lang(
    meta: YoutubeMetadata,
    *,
    preselect: list[str],
) -> str | None:
    """Prompt the user to pick which caption-language track to use.

    Returns the chosen track's `lang` code (forward as
    `get_transcript(transcript_lang=...)`), the sentinel
    :data:`WHISPER_LANG_SENTINEL` when the user picks "transcribe
    audio with Whisper" instead, or `None` on cancel.

    `preselect` is an ordered language preference (e.g.
    `_preferred_caption_langs(settings)`); the first entry whose base
    language has an available track pre-highlights that row.
    """
    from unread.util.languages import language_display_name
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    tracks = _dedup_display_tracks(meta)
    suffix = {
        True: _t("youtube_lang_pick_auto_suffix"),
        False: _t("youtube_lang_pick_manual_suffix"),
    }
    choices: list = [
        Choice(
            value=track.lang,
            label=f"{language_display_name(track.base)}{suffix[track.is_auto]}",
        )
        for track in tracks
    ]

    default_value: str | None = None
    for lang in preselect:
        base = _lang_base(lang)
        match = next((t for t in tracks if t.base == base), None)
        if match is not None:
            default_value = match.lang
            break

    choices.append(_sep())
    choices.append(Choice(value=WHISPER_LANG_SENTINEL, label=_t("youtube_lang_pick_whisper_row")))
    choices.append(Choice(value="__cancel__", label=_t("wiz_cancel_choice")))

    answer = _select(
        _t("youtube_lang_pick_title"),
        choices=choices,
        default_value=default_value,
    )
    if answer is None or answer == "__cancel__":
        return None
    return answer


def _restore_metadata_from_row(row: dict) -> YoutubeMetadata:
    """Rebuild YoutubeMetadata from a `youtube_videos` cache row."""
    import json

    tags_raw = row.get("tags")
    try:
        tags = list(json.loads(tags_raw)) if tags_raw else None
    except (TypeError, ValueError):
        tags = None
    return YoutubeMetadata(
        video_id=row["video_id"],
        url=row["url"],
        title=row.get("title"),
        channel_id=row.get("channel_id"),
        channel_title=row.get("channel_title"),
        channel_url=row.get("channel_url"),
        description=row.get("description"),
        upload_date=row.get("upload_date"),
        duration_sec=row.get("duration_sec"),
        view_count=row.get("view_count"),
        like_count=row.get("like_count"),
        tags=tags,
        language=row.get("language"),
        subtitles=None,
        automatic_captions=None,
    )


async def cmd_analyze_youtube(
    *,
    url: str,
    preset: str | None,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_console: bool = False,
    no_cache: bool = False,
    max_cost: float | None = None,
    dry_run: bool = False,
    self_check: bool = False,
    cite_context: int = 0,
    post_to: str | None = None,
    post_saved: bool = False,
    language: str = "en",
    report_language: str = "en",
    source_language: str = "",
    youtube_source: TranscriptSource = "auto",
    yes: bool = False,
    transcript_lang: str | None = None,
) -> None:
    """Analyze one YouTube video. Captions-first, Whisper fallback."""
    from unread.analyzer.commands import (
        _load_preset_for_commands,
        _post_to_chat,
        _print_and_write,
        _self_check,
    )

    settings = get_settings()
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    # `youtube_source="audio"` forces Whisper transcription, which needs
    # ffmpeg to extract / transcode the audio. Catch the missing-binary
    # case here instead of mid-pipeline after metadata fetch. ("auto"
    # only uses ffmpeg if captions are unavailable; we let that fail
    # naturally with the existing transcript-side error so a captions-
    # only run on a machine without ffmpeg still works.)
    if youtube_source == "audio":
        _require_audio_ffmpeg()

    # YouTube videos default to the `video` preset (system prompt tuned
    # for transcripts, time-stamped citations, no chat semantics). User
    # `--preset summary` etc. still wins.
    effective_preset = preset or "video"

    async with open_repo(settings.storage.data_path) as repo:
        from unread.youtube.cache import (
            fallback_notice,
            load_cached,
            load_exact,
            resolve_requested_lang,
            save_transcript,
        )

        # Caption preference is settings-only (no network), so the cache
        # key can be computed before we touch yt-dlp. `requested_lang` is
        # what this run ASKED for — see `unread/youtube/cache.py` for why
        # the cache keys on that rather than on what came back.
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

        # Metadata row and transcript row are looked up separately now:
        # one video has one metadata row but a transcript per requested
        # language.
        cached = await repo.get_youtube_video(video_id)
        cached_tres = (
            None
            if no_cache
            else await load_exact(
                repo,
                video_id=video_id,
                requested_lang=requested_lang,
                duration_sec=(cached or {}).get("duration_sec"),
            )
        )
        timed_cues: list[tuple[int, str]] | None = None
        transcript_lang_kind: str | None = None
        # Language of the transcript we actually end up with (the fetched
        # caption track / Whisper detection). Kept distinct from the
        # `transcript_lang` PARAMETER, which carries the user's requested
        # `--transcript-lang` and must never be overwritten mid-run.
        fetched_lang: str | None = None
        if cached and cached_tres is not None:
            console.print(f"[grey70]Using cached YouTube metadata + transcript ({video_id})[/]")
            metadata = _restore_metadata_from_row(cached)
            transcript_text = cached_tres.text
            transcript_source: str = cached_tres.source
            transcript_cost = float(cached_tres.cost_usd or 0.0)
            fetched_lang = cached_tres.language
            transcript_lang_kind = (
                None if cached_tres.is_auto is None else ("auto" if cached_tres.is_auto else "manual")
            )
            timed_cues = cached_tres.timed_cues
            # Match the fresh-fetch UX: render the metadata panel for
            # cached runs too. Audio cost estimate is meaningless on the
            # cached path (the transcript already exists, no Whisper
            # call coming) so we pass 0.0 — the panel still shows title /
            # channel / duration / language, which is the useful part.
            console.print(_render_metadata_panel(metadata, audio_estimate=0.0, captions_known=False))
            if notice := fallback_notice(
                requested=requested_lang,
                delivered=fetched_lang,
                source=cached_tres.source,
                language=language or None,
            ):
                console.print(f"[yellow]{notice}[/]")
        else:
            console.print(f"[grey70]Fetching YouTube metadata for {video_id}…[/]")
            try:
                metadata = await fetch_metadata(video_id)
            except YoutubeFetchError as e:
                # yt-dlp couldn't reach the video at all — friendly banner
                # plus an upgrade hint, since this is the most common
                # symptom of yt-dlp lagging behind a YouTube format change.
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e

            audio_estimate = float(
                audio_cost(settings.openai.audio_model_default, metadata.duration_sec) or 0.0
            )
            console.print(_render_metadata_panel(metadata, audio_estimate=audio_estimate))

            # Second-chance lookup, now that we hold metadata with a real
            # caption inventory. Catches the two cases the metadata-free
            # lookup above can't judge: a Whisper transcript somebody else
            # already paid for (when this request has no caption track to
            # fetch either), and a pre-upgrade `youtube_videos` row. A hit
            # here also means no pickers — asking the user to choose a
            # transcript source we're not going to fetch is just noise.
            reuse = (
                None
                if no_cache
                else await load_cached(repo, video_id=video_id, requested_lang=requested_lang, meta=metadata)
            )
            if reuse is not None:
                console.print(f"[grey70]Reusing a cached transcript ({reuse.source})[/]")

            # Interactive picker only when:
            #   - stdin is a TTY (not piped),
            #   - --yes wasn't passed (scripted runs skip prompts), and
            #   - --youtube-source was left at the default "auto".
            # Explicit `--youtube-source captions|audio` is honoured as-is.
            effective_source: TranscriptSource = youtube_source
            if youtube_source == "auto" and not yes and _is_interactive():
                picked = await _interactive_pick_source(
                    metadata,
                    audio_estimate=audio_estimate,
                    # A cache hit means there's no source left to choose,
                    # but the user must still reach dump / fact-check.
                    source_choices=reuse is None,
                )
                if picked is None:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(0)
                if picked == DUMP_SENTINEL:
                    # Hand off to the dump path and stop — no chunking, no
                    # map-reduce, no OpenAI call. `cmd_dump_youtube` opens
                    # its own repo connection; the outer one is idle here
                    # and SQLite runs in WAL mode, so the nested open is
                    # safe. `prefetched_meta` reuses the yt-dlp round-trip
                    # we already paid for above.
                    from unread.youtube.dump import cmd_dump_youtube

                    await cmd_dump_youtube(
                        url=url,
                        mode="transcript",
                        youtube_source="auto",
                        output=None,
                        console_out=False,
                        language=language,
                        report_language=report_language,
                        source_language=source_language,
                        yes=False,
                        transcript_lang=transcript_lang,
                        prefetched_meta=metadata,
                    )
                    return
                if picked == FACTCHECK_SENTINEL:
                    # Same pipeline, different preset — and `auto` stays
                    # the transcript source, since fact-checking needs the
                    # words either way.
                    effective_preset = "factcheck"
                    picked = "auto"
                effective_source = picked

            # `preselect` (the caption-language preference list, CLI
            # overrides winning over saved `[locale]` settings) is
            # computed once at the top of this repo block — it also feeds
            # the cache key. It drives both the picker's preselect and
            # `get_transcript`'s fallback order.

            # Caption-language picker: runs AFTER the source picker so a
            # user who picked "audio" (or was given --youtube-source
            # audio) never sees a language menu for a path that isn't
            # going to use captions. Skipped when: non-interactive, --yes,
            # an explicit --transcript-lang was already given, there's
            # ≤1 distinct caption language to choose from, or the
            # effective source is "audio".
            effective_transcript_lang: str | None = transcript_lang
            if (
                reuse is None
                and transcript_lang is None
                and not yes
                and _is_interactive()
                and effective_source != "audio"
            ):
                tracks = _dedup_display_tracks(metadata)
                if len(tracks) > 1:
                    picked_lang = await _interactive_pick_caption_lang(metadata, preselect=preselect)
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
                        metadata,
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
            transcript_text = tres.text
            transcript_source = tres.source
            transcript_cost = tres.cost_usd
            fetched_lang = tres.language
            timed_cues = tres.timed_cues
            transcript_lang_kind = None if tres.is_auto is None else ("auto" if tres.is_auto else "manual")

            cost_str = f", ${transcript_cost:.4f}" if transcript_cost > 0 else ""
            console.print(
                f"[green]Transcript ready[/] ({tres.source}, {len(transcript_text):,} chars{cost_str})"
            )
            if notice := fallback_notice(
                requested=requested_lang,
                delivered=fetched_lang,
                source=tres.source,
                language=language or None,
            ):
                console.print(f"[yellow]{notice}[/]")

            # Per-requested-language cache. Written even on a `reuse` hit:
            # cheap, and it means the next request for THIS language is an
            # exact hit instead of re-deriving the reuse each time.
            # Recomputed from what we ACTUALLY fetched, not from the
            # default preference: the pickers may have changed the caption
            # language or switched to Whisper since `requested_lang` was
            # derived. Saving under the default key made one interactive
            # "German" choice serve German to every later default run,
            # while the English captions were never fetched again.
            save_lang = resolve_requested_lang(
                transcript_lang=effective_transcript_lang,
                preferred_langs=preselect,
                source=effective_source,
            )
            await save_transcript(
                repo,
                video_id=video_id,
                requested_lang=save_lang,
                tres=tres,
                transcript_model=(
                    settings.openai.audio_model_default if transcript_source == "audio" else None
                ),
            )

            await repo.put_youtube_video(
                video_id=video_id,
                url=metadata.url,
                title=metadata.title,
                channel_id=metadata.channel_id,
                channel_title=metadata.channel_title,
                channel_url=metadata.channel_url,
                description=metadata.description,
                upload_date=metadata.upload_date,
                duration_sec=metadata.duration_sec,
                view_count=metadata.view_count,
                like_count=metadata.like_count,
                tags=metadata.tags,
                language=fetched_lang,
                transcript=transcript_text,
                transcript_source=transcript_source,
                transcript_model=(
                    settings.openai.audio_model_default if transcript_source == "audio" else None
                ),
                transcript_cost_usd=transcript_cost,
                transcript_timed=timed_cues,
                transcript_lang_kind=transcript_lang_kind,
            )

        if not transcript_text.strip():
            console.print(f"[red]{_t('cli_error_prefix')}[/] {_t('err_files_empty_transcript')}")
            raise typer.Exit(2)

        messages = _build_synthetic_messages(metadata, transcript_text, timed_cues=timed_cues)
        loaded_preset = _load_preset_for_commands(effective_preset, prompt_file, language=report_language)

        if dry_run:
            n = len(messages)
            if loaded_preset is None:
                console.print(f"[bold]Dry run: {n} synthetic msgs / preset={effective_preset}[/]")
                return
            lo, hi = estimate_cost(
                n_messages=n,
                preset=loaded_preset,
                settings=settings,
            )
            console.print(
                f"[bold]Dry run: video={video_id} "
                f"chars={len(transcript_text):,} "
                f"segments={n} preset={effective_preset} "
                f"final={loaded_preset.final_model} filter={loaded_preset.filter_model}[/]"
            )
            if hi is not None:
                analysis_hi = hi + transcript_cost
                console.print(
                    f"  Estimated cost: ${(lo or 0.0) + transcript_cost:.4f} – "
                    f"${analysis_hi:.4f} "
                    f"(transcript ${transcript_cost:.4f} + analysis ${lo or 0:.4f}–${hi:.4f})"
                )
            else:
                console.print("  [yellow]Cost estimate unavailable (missing pricing entry)[/]")
            return

        if loaded_preset is not None:
            from unread.analyzer.commands import enforce_cost_gates

            lo, hi = estimate_cost(
                n_messages=len(messages),
                preset=loaded_preset,
                settings=settings,
            )
            # Whisper spend is already committed by this point but still
            # belongs in the number the user is asked about — on a long
            # podcast it can dominate the bill.
            enforce_cost_gates(
                lo=lo,
                hi=hi,
                extra_cost=transcript_cost,
                max_cost=max_cost,
                yes=yes,
                n_messages=len(messages),
                preset_name=effective_preset,
                settings=settings,
            )

        opts = AnalysisOptions(
            preset=effective_preset,
            prompt_file=prompt_file,
            model_override=model,
            filter_model_override=filter_model,
            use_cache=not no_cache,
            include_transcripts=True,
            min_msg_chars=0,  # synthetic header may be short; never drop it
            youtube_video_id=video_id,
            source_kind="video",
        )

        # Citations like `[#754]` resolve through `?t=754s`, so a click on
        # any citation in the report jumps straight to that moment in the
        # video. Subbed in by the formatter via `link_template`.
        link_template = f"https://www.youtube.com/watch?v={video_id}&t={{msg_id}}s"

        console.print(f"[grey70]{_t('running_analysis')}[/]")
        result = await run_analysis(
            repo=repo,
            chat_id=0,
            thread_id=None,
            title=metadata.title or video_id,
            opts=opts,
            messages=messages,
            language=language,
            report_language=report_language,
            source_language=source_language,
            link_template_override=link_template,
        )

        # Reflect transcript cost in the totals shown to the user. The
        # underlying analysis_cache rows are unaffected — they only hold
        # LLM-side cost.
        if transcript_cost:
            result.total_cost_usd += transcript_cost
            result.enrich_cost_usd += transcript_cost
            result.enrich_kinds = list({*result.enrich_kinds, transcript_source})

        # Surface which captions track the analysis is based on so the user
        # can spot a Russian-channel-with-English-manual-subs mismatch.
        if fetched_lang:
            result.transcript_lang = fetched_lang
        if transcript_source == "audio":
            result.transcript_lang_kind = "audio"
        elif transcript_lang_kind:
            result.transcript_lang_kind = transcript_lang_kind

        if self_check and result.final_result and messages:
            verification, verification_err = await _self_check(
                result=result,
                messages=messages,
                repo=repo,
                report_language=report_language,
            )
            heading = _t("verification_heading", language)
            if verification:
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + verification
            elif verification_err:
                failure_line = _t("verification_failed", language).format(err=verification_err)
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + failure_line

        if cite_context > 0 and result.final_result:
            # No Telegram chat → citations have no surrounding context to
            # expand against. Skip silently rather than emitting an empty
            # Sources section.
            log.info("youtube.cite_context_skipped", reason="no telegram chat")

        # Pull every cited `?t=Ns` back a few seconds so a click on a
        # citation lands the listener slightly before the cited segment
        # boundary instead of mid-phrase. Done post-cache so cached LLM
        # output produces shifted links on every render.
        if result.final_result:
            from unread.youtube.citations import shift_citation_timestamps

            result.final_result = shift_citation_timestamps(result.final_result)

        # Compute output path: explicit --output wins; else a youtube/<channel>/...
        # report file — never the chat-shaped default path.
        if output is None and not console_out:
            output_path: Path | None = youtube_report_path(
                video_id=video_id,
                title=metadata.title,
                channel_title=metadata.channel_title,
                channel_id=metadata.channel_id,
                preset=effective_preset,
            )
        else:
            output_path = output

        _print_and_write(
            result,
            output=output_path,
            title=metadata.title or video_id,
            console_out=not no_console,
            no_save=console_out,
        )

        post_target = post_to if post_to else ("me" if post_saved else None)
        if post_target and result.msg_count > 0:
            from unread.tg.client import tg_client

            try:
                async with tg_client(settings) as client:
                    await _post_to_chat(
                        client,
                        repo,
                        result,
                        title=metadata.title or video_id,
                        target=post_target,
                    )
            except Exception as e:
                log.warning("youtube.post_failed", target=post_target, err=str(e)[:200])
                console.print(f"[yellow]{_tf('couldnt_post_to', target=post_target, err=e)}[/]")
