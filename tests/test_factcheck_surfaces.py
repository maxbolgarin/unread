"""Fact-check reachable from both surfaces: the bot panel and the CLI.

`--preset factcheck` works everywhere by virtue of being a preset; these
cover the one-tap / one-keypress paths that make it discoverable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unread.bot.app import BotApp
from unread.bot.burst import BurstItem
from unread.bot.confirm import (
    PendingRun,
    RunOptions,
    build_youtube_choice_panel,
    encode_callback,
    parse_callback,
)
from unread.config import load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean():
    reset_settings()
    yield
    reset_settings()


# --- bot panel ---------------------------------------------------------------


def test_youtube_panel_offers_analyze_transcript_and_factcheck():
    _text, buttons = build_youtube_choice_panel(url="https://youtu.be/x", panel_msg_id=5)
    flat = [b for row in buttons for b in row]
    labels = " ".join(b.text.lower() for b in flat)
    assert "analyze" in labels
    assert "transcript" in labels
    assert "fact" in labels
    assert len(flat) == 3


def test_factcheck_button_encodes_its_own_action():
    _text, buttons = build_youtube_choice_panel(url="https://youtu.be/x", panel_msg_id=9)
    flat = [b for row in buttons for b in row]
    btn = next(b for b in flat if "fact" in b.text.lower())
    assert parse_callback(btn.data) == ("Y_FACT", 9, None)


# --- options plumbing --------------------------------------------------------


def test_merge_panel_options_carries_the_preset_override():
    """Regression shape: `_run_batch_separately` rebuilds RunOptions from
    settings, so anything the panel chose must be in the merge or it is
    silently dropped — exactly how the TG window buttons broke."""
    from unread.bot.confirm import default_options
    from unread.bot.runtime import merge_panel_options

    s = load_settings()
    merged = merge_panel_options(
        defaults=default_options("youtube", s),
        panel=RunOptions(preset_override="factcheck"),
    )
    assert merged.preset_override == "factcheck"
    # And the kind default survives the merge.
    assert merged.youtube_source == "auto"


class _FakeMessage:
    id = 11
    media = None

    async def edit(self, *_a, **_kw) -> None:
        return None


class _FakeEvent:
    def __init__(self, sender_id: int = 111, chat_id: int = 7) -> None:
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.message = _FakeMessage()
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw) -> Any:
        self.replies.append(text)
        return _FakeMessage()


class _FakeCallbackEvent:
    def __init__(self, *, data: bytes, sender_id: int = 111, chat_id: int = 7) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kw) -> None:
        self.answers.append(text)

    async def edit(self, *_a, **_kw) -> None:
        return None

    async def get_message(self):
        return _FakeMessage()


def _app(monkeypatch) -> BotApp:
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    app = BotApp(load_settings())
    item = BurstItem(kind="youtube", payload={"url": "https://youtu.be/abc"}, event=_FakeEvent())
    app._chat_state[7] = {
        "pending_runs": {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
    }
    return app


async def test_factcheck_tap_reaches_the_youtube_handler_with_the_preset(monkeypatch) -> None:
    app = _app(monkeypatch)
    execute_mock = AsyncMock()
    with patch("unread.bot.handlers.youtube.execute", new=execute_mock):
        await app._handle_callback(_FakeCallbackEvent(data=encode_callback("Y_FACT", 5)))
    execute_mock.assert_called_once()
    assert execute_mock.call_args.args[2].preset_override == "factcheck"


async def test_plain_run_tap_carries_no_preset_override(monkeypatch) -> None:
    app = _app(monkeypatch)
    execute_mock = AsyncMock()
    with patch("unread.bot.handlers.youtube.execute", new=execute_mock):
        await app._handle_callback(_FakeCallbackEvent(data=encode_callback("R", 5)))
    assert execute_mock.call_args.args[2].preset_override is None


async def test_youtube_handler_prefers_the_preset_override(monkeypatch) -> None:
    """A sticky `/preset digest` must not beat an explicit Fact-check tap."""
    from unread.bot.handlers import youtube as yt

    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    app = BotApp(load_settings())
    app._chat_state[7] = {"preset": "digest"}
    event = _FakeEvent()
    analyze_mock = AsyncMock()
    with (
        patch("unread.youtube.commands.cmd_analyze_youtube", new=analyze_mock),
        patch("unread.bot.reply.send_youtube_report", new=AsyncMock()),
    ):
        await yt.execute(
            event,
            {"url": "https://youtu.be/abc12345678"},
            RunOptions(preset_override="factcheck"),
            app=app,
            progress_msg=None,
        )
    assert analyze_mock.call_args.kwargs["preset"] == "factcheck"


# --- CLI picker ---------------------------------------------------------------


async def test_cli_picker_factcheck_row_switches_the_preset() -> None:
    """The YouTube source picker's third row runs the analysis pipeline
    with the factcheck preset instead of `video`."""
    from unread.youtube.commands import FACTCHECK_SENTINEL, cmd_analyze_youtube
    from unread.youtube.metadata import YoutubeMetadata
    from unread.youtube.transcript import TranscriptResult

    meta = YoutubeMetadata(
        video_id="factcheck01",
        url="https://www.youtube.com/watch?v=factcheck01",
        title="Claims",
        duration_sec=900,
    )
    tres = TranscriptResult(
        text="the economy grew twelve percent. " * 20,
        source="captions",
        language="en",
        duration_sec=900,
        cost_usd=0.0,
        timed_cues=[(0, "the economy grew twelve percent")],
        is_auto=False,
    )
    seen: list[str] = []

    def _record_preset(name, *_a, **_kw):
        seen.append(name)

    with (
        patch("unread.youtube.commands.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.commands.get_transcript", new=AsyncMock(return_value=tres)),
        patch("unread.youtube.commands._is_interactive", return_value=True),
        patch(
            "unread.youtube.commands._interactive_pick_source",
            new=AsyncMock(return_value=FACTCHECK_SENTINEL),
        ),
        # Imported lazily inside `cmd_analyze_youtube`, so patch the source.
        patch("unread.analyzer.commands._load_preset_for_commands", new=_record_preset),
    ):
        await cmd_analyze_youtube(
            url=meta.url,
            preset=None,
            prompt_file=None,
            model=None,
            filter_model=None,
            output=None,
            console_out=True,
            dry_run=True,
            yes=False,
        )
    assert seen == ["factcheck"]
