"""The two YouTube reports: `video` (analysis) and `video_summary` (retelling).

`video` breaks down what the speaker argues and on what grounds;
`video_summary` retells the video in order for someone who won't watch it.
Both are reachable from the bot's YouTube panel and the CLI picker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from unread.analyzer.prompts import get_presets
from unread.bot.confirm import build_youtube_choice_panel, encode_callback, parse_callback
from unread.config import reset_settings
from unread.youtube.commands import SUMMARY_SENTINEL, cmd_analyze_youtube
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import TranscriptResult

from .test_factcheck_surfaces import _app, _FakeCallbackEvent


@pytest.fixture(autouse=True)
def _clean():
    reset_settings()
    yield
    reset_settings()


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("name", ["video", "video_summary"])
def test_both_presets_exist_hidden_with_a_tldr(language, name) -> None:
    """Hidden: picked by the YouTube flow, not the chat-preset wizard. The
    TL;DR is what the bot posts inline above the attached report."""
    preset = get_presets(language)[name]
    assert preset.hidden is True
    assert "## TL;DR" in preset.user_template


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("name", ["video", "video_summary"])
def test_timestamp_links_keep_the_template_query(language, name) -> None:
    """The old prompt showed the link as `URL?t=Ns` while the template is
    `watch?v=ID&t=Ns` — a second `?` breaks the jump-to-moment link."""
    preset = get_presets(language)[name]
    text = preset.system + preset.user_template
    assert "URL?t=" not in text
    assert "watch?v=ID&t=840s" in text


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("name", ["video", "video_summary"])
def test_a_long_video_fits_one_chunk_under_the_tpm_cap(language, name) -> None:
    """A two-hour transcript is ~50-60k tokens. Splitting it map-reduces
    an argument into fragments; the cap still guards low TPM tiers."""
    cap = get_presets(language)[name].max_chunk_input_tokens
    assert cap is not None
    assert 60_000 <= cap <= 100_000


@pytest.mark.parametrize("language", ["en", "ru"])
def test_analysis_breaks_down_claims_and_arguments(language) -> None:
    tpl = get_presets(language)["video"].user_template
    assert ("Claims and arguments" if language == "en" else "Тезисы и аргументы") in tpl


@pytest.mark.parametrize("language", ["en", "ru"])
def test_summary_retells_in_order(language) -> None:
    tpl = get_presets(language)["video_summary"].user_template
    assert ("As it goes" if language == "en" else "По ходу видео") in tpl


def test_bot_panel_offers_summary() -> None:
    _text, buttons = build_youtube_choice_panel(url="https://youtu.be/x", panel_msg_id=4)
    btn = next(b for row in buttons for b in row if "summary" in b.text.lower())
    assert parse_callback(btn.data) == ("Y_SUM", 4, None)


async def test_summary_tap_runs_the_youtube_handler_with_the_preset(monkeypatch) -> None:
    app = _app(monkeypatch)
    execute_mock = AsyncMock()
    with patch("unread.bot.handlers.youtube.execute", new=execute_mock):
        await app._handle_callback(_FakeCallbackEvent(data=encode_callback("Y_SUM", 5)))
    execute_mock.assert_called_once()
    assert execute_mock.call_args.args[2].preset_override == "video_summary"


async def test_cli_picker_summary_row_switches_the_preset() -> None:
    meta = YoutubeMetadata(
        video_id="summary0001",
        url="https://www.youtube.com/watch?v=summary0001",
        title="Talk",
        duration_sec=900,
    )
    tres = TranscriptResult(
        text="today we talk about sleep. " * 20,
        source="captions",
        language="en",
        duration_sec=900,
        cost_usd=0.0,
        timed_cues=[(0, "today we talk about sleep")],
        is_auto=False,
    )
    seen: list[str] = []
    with (
        patch("unread.youtube.commands.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.commands.get_transcript", new=AsyncMock(return_value=tres)),
        patch("unread.youtube.commands._is_interactive", return_value=True),
        patch(
            "unread.youtube.commands._interactive_pick_source",
            new=AsyncMock(return_value=SUMMARY_SENTINEL),
        ),
        patch(
            "unread.analyzer.commands._load_preset_for_commands",
            new=lambda name, *_a, **_kw: seen.append(name),
        ),
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
    assert seen == ["video_summary"]
