"""Bot YouTube "📝 Transcript" button — dump instead of analyze.

Covers the three moving parts the button touches: the callback routing
in `BotApp._handle_callback`, the handler that calls `cmd_dump_youtube`,
and the reply helper that uploads `transcript.md`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from unread.bot.app import BotApp
from unread.bot.burst import BurstItem
from unread.bot.confirm import PendingRun, RunOptions, encode_callback
from unread.config import load_settings, reset_settings


def _fresh_settings():
    reset_settings()
    return load_settings()


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_file(self, chat_id, **kwargs) -> None:
        self.sent.append({"chat_id": chat_id, **kwargs})


class _FakeMessage:
    id = 11


class _FakeEvent:
    """Minimal stand-in for a Telethon NewMessage.Event."""

    def __init__(self) -> None:
        self.chat_id = 7
        self.message = _FakeMessage()
        self.client = _FakeClient()
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs) -> Any:
        self.replies.append(text)
        return _FakeMessage()


class _FakeCallbackEvent:
    def __init__(self, *, data: bytes, sender_id: int = 42, chat_id: int = 7) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text: str = "", **_kwargs) -> None:
        self.answers.append(text)

    async def edit(self, text: str, **_kwargs) -> None:
        self.edits.append(text)

    async def get_message(self):
        return _FakeMessage()


# --- reply.send_transcript_dump ---------------------------------------------


async def test_send_transcript_dump_uploads_the_markdown_file(tmp_path) -> None:
    from unread.bot.reply import send_transcript_dump

    transcript = tmp_path / "transcript.md"
    transcript.write_text("# Hello World\n\n## Transcript\n\nhello world\n", encoding="utf-8")
    event = _FakeEvent()

    await send_transcript_dump(event, transcript=transcript, started=0.0, title="Hello World")

    assert len(event.client.sent) == 1
    sent = event.client.sent[0]
    assert sent["file"] == str(transcript)
    assert sent["force_document"] is True
    assert "✓" in sent["caption"]


async def test_send_transcript_dump_warns_when_the_file_is_missing(tmp_path) -> None:
    from unread.bot.reply import send_transcript_dump

    event = _FakeEvent()
    await send_transcript_dump(
        event,
        transcript=tmp_path / "nope.md",
        started=0.0,
        title="Gone",
    )
    assert event.client.sent == []
    assert event.replies and "⚠️" in event.replies[0]


# --- handlers.youtube.execute_dump ------------------------------------------


async def test_execute_dump_runs_cmd_dump_youtube_in_transcript_mode(tmp_path) -> None:
    from unread.bot.handlers import youtube as yt_handler

    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "transcript.md").write_text("# T\n\nwords\n", encoding="utf-8")

    app = BotApp(_fresh_settings())
    event = _FakeEvent()
    dump_mock = AsyncMock(return_value=dump_dir)
    send_mock = AsyncMock()
    with (
        patch("unread.youtube.dump.cmd_dump_youtube", new=dump_mock),
        patch("unread.bot.reply.send_transcript_dump", new=send_mock),
    ):
        await yt_handler.execute_dump(
            event,
            {"url": "https://youtu.be/abc12345678"},
            RunOptions(),
            app=app,
            progress_msg=None,
        )

    dump_mock.assert_called_once()
    assert dump_mock.call_args.kwargs["mode"] == "transcript"
    assert dump_mock.call_args.kwargs["yes"] is True
    send_mock.assert_called_once()
    assert send_mock.call_args.kwargs["transcript"] == dump_dir / "transcript.md"


async def test_execute_dump_rejects_a_non_youtube_url() -> None:
    from unread.bot.handlers import youtube as yt_handler

    app = BotApp(_fresh_settings())
    event = _FakeEvent()
    dump_mock = AsyncMock()
    with patch("unread.youtube.dump.cmd_dump_youtube", new=dump_mock):
        await yt_handler.execute_dump(
            event,
            {"url": "https://example.com/not-a-video"},
            RunOptions(),
            app=app,
            progress_msg=None,
        )
    dump_mock.assert_not_called()
    assert event.replies and "⚠️" in event.replies[0]


# --- BotApp callback routing -------------------------------------------------


async def _app_with_pending_youtube() -> tuple[BotApp, dict]:
    app = BotApp(_fresh_settings())
    app.owner_id = 42
    item = BurstItem(kind="youtube", payload={"url": "https://youtu.be/abc"}, event=_FakeEvent())
    pending = PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())
    app._chat_state[7] = {"pending_runs": {5: pending}}
    return app, app._chat_state[7]["pending_runs"]


async def test_callback_y_dump_routes_to_the_transcript_dump_path() -> None:
    app, pending_runs = await _app_with_pending_youtube()
    seen: list[Any] = []

    async def _fake_run_youtube_dump(pending, panel_msg):
        seen.append(pending)

    app._run_youtube_dump = _fake_run_youtube_dump  # type: ignore[method-assign]
    event = _FakeCallbackEvent(data=encode_callback("Y_DUMP", 5))

    await app._handle_callback(event)

    assert len(seen) == 1
    # Pending is dropped before the run so a double-tap can't run twice.
    assert 5 not in pending_runs


async def test_callback_y_dump_on_a_stale_panel_reports_expiry() -> None:
    app, _ = await _app_with_pending_youtube()
    event = _FakeCallbackEvent(data=encode_callback("Y_DUMP", 999))
    await app._handle_callback(event)
    assert any("expired" in a.lower() for a in event.answers)


async def test_run_youtube_dump_dispatches_the_first_burst_item() -> None:
    """The real `_run_youtube_dump` must hand the item's own event (not the
    panel's) to the handler so the transcript replies to the user's link."""
    app, _ = await _app_with_pending_youtube()
    item = app._chat_state[7]["pending_runs"][5].payload["items"][0]
    pending = app._chat_state[7]["pending_runs"][5]
    execute_mock = AsyncMock()
    with patch("unread.bot.handlers.youtube.execute_dump", new=execute_mock):
        await app._run_youtube_dump(pending, None)
    execute_mock.assert_called_once()
    assert execute_mock.call_args.args[0] is item.event
    assert execute_mock.call_args.args[1] == item.payload
