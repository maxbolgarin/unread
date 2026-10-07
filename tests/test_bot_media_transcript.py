"""Bot voice / video message → transcript (`📝 Transcript` / `💬 As text`).

Covers the panel routing in `burst.render_burst_panel`, the dispatcher's
voice / round-video tagging, the plain-text splitter and sender, the
handler, and the callback routing in `BotApp._handle_callback`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

from unread.bot.app import BotApp
from unread.bot.burst import BurstItem, render_burst_panel
from unread.bot.confirm import PendingRun, RunOptions, encode_callback, parse_callback
from unread.config import load_settings, reset_settings
from unread.files.extractors import ExtractResult


def _fresh_settings():
    reset_settings()
    return load_settings()


def _voice_payload() -> dict:
    return {
        "source": "media",
        "kind": "audio",
        "subtype": "voice",
        "mime": "audio/ogg",
        "size": 1000,
        "name": "attachment.ogg",
    }


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.files: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_file(self, chat_id, **kwargs) -> None:
        self.files.append({"chat_id": chat_id, **kwargs})


class _FakeMessage:
    id = 11

    async def edit(self, *_a, **_kw) -> None:
        return None

    async def delete(self) -> None:
        return None


class _FakeEvent:
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

    async def answer(self, text: str = "", **_kwargs) -> None:
        self.answers.append(text)

    async def edit(self, *_a, **_kw) -> None:
        return None

    async def get_message(self):
        return _FakeMessage()


def _buttons_by_action(buttons) -> dict[str, Any]:
    return {parse_callback(b.data)[0]: b for row in buttons for b in row}


# --- dispatcher ---------------------------------------------------------------


def _doc_media(mime: str, attrs: list) -> MessageMediaDocument:
    doc = Document(
        id=1,
        access_hash=2,
        file_reference=b"",
        date=None,
        mime_type=mime,
        size=1234,
        dc_id=1,
        attributes=attrs,
    )
    return MessageMediaDocument(document=doc)


def test_dispatcher_tags_voice_and_round_video() -> None:
    from unread.bot.dispatcher import _classify_media

    voice = _classify_media(_doc_media("audio/ogg", [DocumentAttributeAudio(duration=3, voice=True)]))
    assert voice["kind"] == "audio"
    assert voice["subtype"] == "voice"

    round_video = _classify_media(
        _doc_media("video/mp4", [DocumentAttributeVideo(duration=3, w=240, h=240, round_message=True)])
    )
    assert round_video["kind"] == "video"
    assert round_video["subtype"] == "videonote"

    song = _classify_media(_doc_media("audio/mpeg", [DocumentAttributeAudio(duration=3)]))
    assert "subtype" not in song


# --- panels -------------------------------------------------------------------


def test_single_voice_gets_the_media_choice_panel() -> None:
    items = [BurstItem(kind="file", payload=_voice_payload(), event=None)]
    text, buttons = render_burst_panel(items=items, panel_msg_id=3)
    assert "Voice message" in text
    actions = _buttons_by_action(buttons)
    assert set(actions) == {"R", "V_DUMP", "V_TEXT"}
    assert parse_callback(actions["V_TEXT"].data) == ("V_TEXT", 3, None)


def test_single_pdf_keeps_the_plain_run_button() -> None:
    payload = {"source": "media", "kind": "pdf", "name": "a.pdf"}
    items = [BurstItem(kind="file", payload=payload, event=None)]
    _text, buttons = render_burst_panel(items=items, panel_msg_id=3)
    assert set(_buttons_by_action(buttons)) == {"R"}


def test_two_voices_fall_back_to_the_batch_panel() -> None:
    items = [BurstItem(kind="file", payload=_voice_payload(), event=None) for _ in range(2)]
    text, buttons = render_burst_panel(items=items, panel_msg_id=3)
    assert "voice message" in text
    assert "V_TEXT" not in _buttons_by_action(buttons)


def test_forwarded_voice_offers_transcript_buttons() -> None:
    payload = {**_voice_payload(), "fwd_channel_id": 100, "fwd_msg_id": 5, "fwd_title": "Chan"}
    items = [BurstItem(kind="file", payload=payload, event=None)]
    _text, buttons = render_burst_panel(items=items, panel_msg_id=3)
    actions = _buttons_by_action(buttons)
    assert {"F_FULL", "V_DUMP", "V_TEXT", "F_DAY"} <= set(actions)
    assert "F_TXT" not in actions


# --- splitter / sender --------------------------------------------------------


def test_split_plain_text_prefers_sentence_and_word_boundaries() -> None:
    from unread.bot.reply import split_plain_text

    text = "One two three. Four five six. Seven eight nine."
    parts = split_plain_text(text, limit=20)
    assert all(len(p) <= 20 for p in parts)
    assert parts[0] == "One two three."
    assert " ".join(parts) == text


def test_split_plain_text_chops_a_wordless_run() -> None:
    from unread.bot.reply import split_plain_text

    assert split_plain_text("x" * 25, limit=10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_plain_text("   ", limit=10) == []


async def test_send_transcript_text_sends_only_the_words() -> None:
    from unread.bot.reply import send_transcript_text

    event = _FakeEvent()
    await send_transcript_text(event, text="hello *world* _there_")

    assert event.replies == []
    assert len(event.client.sent) == 1
    sent = event.client.sent[0]
    # Verbatim — no header, no caption, no markdown parsing.
    assert sent["text"] == "hello *world* _there_"
    assert sent["parse_mode"] is None
    assert sent["reply_to"] == 11


async def test_send_transcript_text_only_first_part_replies(monkeypatch) -> None:
    from unread.bot import reply

    monkeypatch.setattr(reply, "telegram_message_limit", lambda _c: 10)
    event = _FakeEvent()
    await reply.send_transcript_text(event, text="aaaa bbbb cccc dddd")
    assert [m["reply_to"] for m in event.client.sent] == [11, None]


# --- handler ------------------------------------------------------------------


async def _run_handler(tmp_path: Path, *, as_text: bool, payload: dict):
    from unread.bot.handlers import transcript as transcript_handler

    app = BotApp(_fresh_settings())
    event = _FakeEvent()
    audio_file = tmp_path / "voice.ogg"
    audio_file.write_bytes(b"x")
    audio_mock = AsyncMock(return_value=ExtractResult(text="the words", extra={}))
    video_mock = AsyncMock(return_value=ExtractResult(text="the words", extra={}))
    text_mock = AsyncMock()
    dump_mock = AsyncMock()
    with (
        patch("unread.bot.handlers.file._materialize_input", new=AsyncMock(return_value=audio_file)),
        patch("unread.files.extractors.extract_audio", new=audio_mock),
        patch("unread.files.extractors.extract_video", new=video_mock),
        patch("unread.bot.reply.send_transcript_text", new=text_mock),
        patch("unread.bot.reply.send_transcript_dump", new=dump_mock),
    ):
        await transcript_handler.execute(event, payload, app=app, as_text=as_text, progress_msg=None)
    return event, audio_mock, video_mock, text_mock, dump_mock


async def test_handler_as_text_sends_plain_transcript(tmp_path) -> None:
    _event, audio_mock, video_mock, text_mock, dump_mock = await _run_handler(
        tmp_path, as_text=True, payload=_voice_payload()
    )
    audio_mock.assert_called_once()
    video_mock.assert_not_called()
    text_mock.assert_called_once()
    assert text_mock.call_args.kwargs["text"] == "the words"
    dump_mock.assert_not_called()


async def test_handler_file_mode_uploads_transcript_md(tmp_path) -> None:
    payload = {**_voice_payload(), "kind": "video", "subtype": "videonote"}
    _event, audio_mock, video_mock, text_mock, dump_mock = await _run_handler(
        tmp_path, as_text=False, payload=payload
    )
    video_mock.assert_called_once()
    audio_mock.assert_not_called()
    text_mock.assert_not_called()
    dump_mock.assert_called_once()
    assert dump_mock.call_args.kwargs["title"] == "⭕ Video message"
    assert dump_mock.call_args.kwargs["transcript"].name == "transcript.md"


# --- callback routing ---------------------------------------------------------


def _app_with_pending_voice() -> tuple[BotApp, dict]:
    app = BotApp(_fresh_settings())
    app.owner_id = 42
    item = BurstItem(kind="file", payload=_voice_payload(), event=_FakeEvent())
    pending = PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())
    app._chat_state[7] = {"pending_runs": {5: pending}}
    return app, app._chat_state[7]["pending_runs"]


async def test_callback_v_text_routes_to_the_transcript_path() -> None:
    app, pending_runs = _app_with_pending_voice()
    seen: list[bool] = []

    async def _fake(pending, panel_msg, *, as_text):
        seen.append(as_text)

    app._run_media_transcript = _fake  # type: ignore[method-assign]
    await app._handle_callback(_FakeCallbackEvent(data=encode_callback("V_TEXT", 5)))
    assert seen == [True]
    assert 5 not in pending_runs


async def test_callback_v_dump_routes_to_the_transcript_path() -> None:
    app, _ = _app_with_pending_voice()
    seen: list[bool] = []

    async def _fake(pending, panel_msg, *, as_text):
        seen.append(as_text)

    app._run_media_transcript = _fake  # type: ignore[method-assign]
    await app._handle_callback(_FakeCallbackEvent(data=encode_callback("V_DUMP", 5)))
    assert seen == [False]


async def test_run_media_transcript_uses_the_items_own_event() -> None:
    app, pending_runs = _app_with_pending_voice()
    pending = pending_runs[5]
    item = pending.payload["items"][0]
    execute_mock = AsyncMock()
    with patch("unread.bot.handlers.transcript.execute", new=execute_mock):
        await app._run_media_transcript(pending, None, as_text=True)
    execute_mock.assert_called_once()
    assert execute_mock.call_args.args[0] is item.event
    assert execute_mock.call_args.kwargs["as_text"] is True
