"""YouTube URL handler — `cmd_analyze_youtube` / `cmd_dump_youtube` wrappers.

Two entry points behind the confirm panel's two buttons:
`execute` (▶ Analyze) runs the full analysis pipeline; `execute_dump`
(📝 Transcript) writes `transcript.md` and uploads it without any
chat-completion call.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.bot.confirm import RunOptions
from unread.bot.progress import edit_progress
from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


async def execute(
    event: events.NewMessage.Event,
    payload: dict,
    options: RunOptions,
    *,
    app: BotApp,
    progress_msg=None,
) -> None:
    from unread.bot.handlers.file import _effective_preset
    from unread.bot.runtime import (
        effective_language,
        effective_report_language,
        effective_source_language,
    )
    from unread.youtube.commands import cmd_analyze_youtube
    from unread.youtube.urls import extract_video_id

    s = get_settings()
    url = payload["url"]
    chat_state = app._chat_state.get(event.chat_id) or {}
    # An explicit button tap beats a sticky `/preset` set some other day.
    preset = options.preset_override or _effective_preset(s, app, event.chat_id)
    started = time.time()

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        await event.reply(f"⚠️ Not a recognizable YouTube video URL: {e}")
        return

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Pulling transcript for `{video_id}`…")
    else:
        await edit_progress(progress_msg, f"⏳ Pulling transcript for `{video_id}`…")
    try:
        from unread.bot.progress import LiveProgress, run_status_line
        from unread.util.flood import status_sink

        header = run_status_line(preset=preset, settings=s, source="video")
        await edit_progress(progress_msg, header)
        live = LiveProgress(progress_msg)

        async def _on_progress(text: str) -> None:
            await live(f"{header}\n{text}")

        def _on_status(text: str) -> None:
            # Retry notices arrive from a sync context deep in the SDK
            # retry loop, so schedule the edit rather than awaiting it.
            import asyncio as _asyncio

            with contextlib.suppress(RuntimeError):
                _asyncio.get_running_loop().create_task(live.flush(f"{header}\n⏳ {text}"))

        language = effective_language(chat_state, s)
        report_language = effective_report_language(chat_state, s)
        with status_sink(_on_status):
            await cmd_analyze_youtube(
                url=url,
                preset=preset or None,
                prompt_file=None,
                model=None,
                filter_model=None,
                output=None,
                console_out=False,
                no_console=True,
                no_cache=False,
                max_cost=None,
                dry_run=False,
                self_check=False,
                cite_context=0,
                post_to=None,
                post_saved=False,
                language=language,
                report_language=report_language,
                source_language=effective_source_language(chat_state, s),
                youtube_source=options.youtube_source or "auto",
                yes=True,
                on_progress=_on_progress,
            )
        await edit_progress(progress_msg, "📄 Sending report…")
        from unread.bot import reply

        await reply.send_youtube_report(event, preset=preset, started=started, hint=video_id)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.youtube_handler_failed", url=url)
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise


async def execute_dump(
    event: events.NewMessage.Event,
    payload: dict,
    options: RunOptions,
    *,
    app: BotApp,
    progress_msg=None,
) -> None:
    """`📝 Transcript` button — write `transcript.md` and upload it.

    Skips the analysis pipeline entirely: no chunking, no map-reduce, no
    chat-completion call. The only thing that can cost money here is
    Whisper, and only when the video has no usable captions.
    """
    from unread.bot.runtime import (
        effective_language,
        effective_report_language,
        effective_source_language,
    )
    from unread.youtube.dump import cmd_dump_youtube
    from unread.youtube.urls import extract_video_id

    s = get_settings()
    url = payload["url"]
    chat_state = app._chat_state.get(event.chat_id) or {}
    started = time.time()

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        await event.reply(f"⚠️ Not a recognizable YouTube video URL: {e}")
        return

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Pulling transcript for `{video_id}`…")
    else:
        await edit_progress(progress_msg, f"⏳ Pulling transcript for `{video_id}`…")

    try:
        dump_dir = await cmd_dump_youtube(
            url=url,
            mode="transcript",
            youtube_source=options.youtube_source or "auto",
            output=None,
            console_out=False,
            language=effective_language(chat_state, s),
            report_language=effective_report_language(chat_state, s),
            source_language=effective_source_language(chat_state, s),
            yes=True,
        )
        await edit_progress(progress_msg, "📄 Sending transcript…")
        from unread.bot import reply

        await reply.send_transcript_dump(
            event,
            transcript=dump_dir / "transcript.md",
            started=started,
            title=_dump_title(dump_dir, fallback=video_id),
        )
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.youtube_dump_failed", url=url)
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise


def _dump_title(dump_dir, *, fallback: str) -> str:
    """Video title from the dump's `metadata.json`; `fallback` if unreadable."""
    import json

    try:
        meta = json.loads((dump_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    return str(meta.get("title") or fallback)
