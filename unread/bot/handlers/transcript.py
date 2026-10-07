"""Voice / audio / video message → transcript, no analysis.

Backs the `📝 Transcript` and `💬 As text` buttons on the media confirm
panel. Downloads the attachment, runs it through the same Whisper path
the file analyzer uses (`extract_audio` / `extract_video`) and either
uploads `transcript.md` or sends the words back as plain messages.
No LLM call either way — Whisper is the only cost.
"""

from __future__ import annotations

import contextlib
import shutil
import time
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.bot.progress import edit_progress
from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


async def execute(
    event: events.NewMessage.Event,
    payload: dict,
    *,
    app: BotApp,
    as_text: bool,
    progress_msg=None,
) -> None:
    """Transcribe the message's audio / video and reply with the transcript.

    `as_text=True` sends the transcript as plain chat messages and nothing
    else; `False` uploads it as `transcript.md` with the usual cost caption.
    """
    from unread.bot import reply
    from unread.bot.confirm import media_label
    from unread.bot.handlers.file import _make_tmp_dir, _materialize_input
    from unread.files.extractors import extract_audio, extract_video

    s = get_settings()
    if progress_msg is None:
        progress_msg = await event.reply("⏳ Transcribing…")
    else:
        await edit_progress(progress_msg, "⏳ Transcribing…")
    started = time.time()
    tmp_dir = _make_tmp_dir()
    try:
        local_path = await _materialize_input(event, payload, tmp_dir, app=app, s=s)
        if local_path is None:
            return  # _materialize_input has already replied with the reason.

        extract = extract_video if payload.get("kind") == "video" else extract_audio
        text = (await extract(local_path)).text.strip()

        if as_text:
            await reply.send_transcript_text(event, text=text)
        else:
            await edit_progress(progress_msg, "📄 Sending transcript…")
            # Strip the markdown backticks the panel label carries.
            title = media_label(payload).replace("`", "")
            transcript = tmp_dir / "transcript.md"
            transcript.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
            await reply.send_transcript_dump(event, transcript=transcript, started=started, title=title)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.transcript_failed")
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_dir, ignore_errors=True)
