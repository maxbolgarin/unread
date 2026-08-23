"""Tests for `unread.bot.runtime` and the new /lang /enrich /window /settings commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from unread.bot.confirm import RunOptions
from unread.bot.runtime import (
    STICKY_ENRICH_EXTRAS,
    STICKY_REPORT_LANGUAGE,
    STICKY_TG_WINDOW,
    effective_preset,
    effective_preset_for_kind,
    effective_report_language,
    parse_enrich_list,
    parse_lang_value,
    parse_window_value,
    render_settings_overview,
    resolve_options,
    smart_default_preset,
)
from unread.config import load_settings, reset_settings


def _fresh_settings():
    reset_settings()
    return load_settings()


# ---------------------------------------------------------------------------
# resolve_options — per-run wins over sticky
# ---------------------------------------------------------------------------


def test_resolve_options_per_run_wins_over_sticky_window():
    chat_state = {STICKY_TG_WINDOW: "7d"}
    options = RunOptions(tg_window="30d")  # user tapped Last month on the panel
    merged = resolve_options(chat_state=chat_state, settings=_fresh_settings(), options=options)
    assert merged.tg_window == "30d"


def test_resolve_options_sticky_window_used_when_per_run_unset():
    chat_state = {STICKY_TG_WINDOW: "7d"}
    options = RunOptions()
    merged = resolve_options(chat_state=chat_state, settings=_fresh_settings(), options=options)
    assert merged.tg_window == "7d"


def test_resolve_options_merges_sticky_enrich_extras():
    chat_state = {STICKY_ENRICH_EXTRAS: {"image", "link"}}
    options = RunOptions()
    merged = resolve_options(chat_state=chat_state, settings=_fresh_settings(), options=options)
    assert merged.enrich_image is True
    assert merged.enrich_link is True
    assert merged.enrich_doc is False
    assert merged.enrich_video is False


def test_resolve_options_per_run_enrich_overrides_sticky():
    """If panel toggled image off and sticky says on, per-run takes precedence.

    Today the picker has no enrich toggle (gone in the recent simplification)
    so this is mostly future-proofing — but the merge logic stays predictable."""
    chat_state = {STICKY_ENRICH_EXTRAS: {"image"}}
    options = RunOptions(enrich_image=True)
    merged = resolve_options(chat_state=chat_state, settings=_fresh_settings(), options=options)
    assert merged.enrich_image is True


# ---------------------------------------------------------------------------
# effective_* readers
# ---------------------------------------------------------------------------


def test_effective_report_language_sticky_wins():
    chat_state = {STICKY_REPORT_LANGUAGE: "ru"}
    s = _fresh_settings()
    assert effective_report_language(chat_state, s) == "ru"


def test_effective_report_language_falls_back_to_settings():
    chat_state = {}
    s = _fresh_settings()
    # No sticky → uses settings.locale.report_language or .language or "en"
    expected = (s.locale.report_language or s.locale.language or "en").strip()
    assert effective_report_language(chat_state, s) == expected


def test_effective_preset_sticky_wins_over_config_default():
    chat_state = {"preset": "digest"}
    s = _fresh_settings()
    assert effective_preset(chat_state, s) == "digest"


def test_smart_default_preset_file_is_single_msg():
    """A single file / voice / image is one thing — not a chat. Use single_msg."""
    assert smart_default_preset("file") == "single_msg"


def test_smart_default_preset_other_kinds_empty():
    """url / youtube / tg let cmd_analyze_* pick their own kind-specific default."""
    assert smart_default_preset("url") == ""
    assert smart_default_preset("youtube") == ""
    assert smart_default_preset("tg") == ""


def test_effective_preset_for_kind_falls_through_to_smart_default():
    """No sticky, no config preset → file kind picks single_msg."""
    s = _fresh_settings()
    # Make sure no test pollution: stub the config default explicitly.
    s.bot.default_preset = ""
    assert effective_preset_for_kind({}, s, "file") == "single_msg"


def test_effective_preset_for_kind_sticky_wins_over_smart_default():
    s = _fresh_settings()
    s.bot.default_preset = ""
    assert effective_preset_for_kind({"preset": "digest"}, s, "file") == "digest"


def test_effective_preset_for_kind_config_wins_over_smart_default():
    """If operator set `bot.default_preset` in config, that wins."""
    s = _fresh_settings()
    s.bot.default_preset = "highlights"
    assert effective_preset_for_kind({}, s, "file") == "highlights"


# ---------------------------------------------------------------------------
# Slash-command parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arg,expected_value",
    [
        ("image,link", {"image", "link"}),
        ("image, doc, link, video", {"image", "doc", "link", "video"}),
        ("all", {"image", "doc", "link", "video"}),
        ("none", set()),
        ("", set()),  # bare → clear
        ("clear", set()),
    ],
)
def test_parse_enrich_list_valid(arg, expected_value):
    value, _msg = parse_enrich_list(arg)
    assert value == expected_value


def test_parse_enrich_list_rejects_unknown_kind():
    value, msg = parse_enrich_list("voice")  # voice is always-on baseline, not toggleable
    assert value is None
    assert "voice" in msg.lower()


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("day", "1d"),
        ("week", "7d"),
        ("month", "30d"),
        ("1d", "1d"),
        ("7d", "7d"),
        ("30d", "30d"),
        ("msg", "msg"),
        ("this", "msg"),
        ("from", "from_msg"),
        ("from_msg", "from_msg"),
        ("none", ""),
        ("", ""),
    ],
)
def test_parse_window_value_valid(arg, expected):
    value, _msg = parse_window_value(arg)
    assert value == expected


def test_parse_window_value_rejects_garbage():
    value, _msg = parse_window_value("yesterday")
    assert value is None


@pytest.mark.parametrize(
    "arg,expected",
    [
        ("en", "en"),
        ("ru", "ru"),
        ("de", "de"),
        ("", ""),
        ("none", ""),
    ],
)
def test_parse_lang_value_valid(arg, expected):
    value, _msg = parse_lang_value(arg)
    assert value == expected


def test_parse_lang_value_rejects_garbage():
    value, _msg = parse_lang_value("en-US")  # contains dash → invalid
    assert value is None


# ---------------------------------------------------------------------------
# /settings overview rendering
# ---------------------------------------------------------------------------


def test_render_settings_overview_shows_all_knobs():
    chat_state = {
        "preset": "digest",
        STICKY_REPORT_LANGUAGE: "ru",
        STICKY_ENRICH_EXTRAS: {"image", "link"},
        STICKY_TG_WINDOW: "30d",
    }
    text = render_settings_overview(chat_state, _fresh_settings())
    assert "digest" in text
    assert "ru" in text
    assert "image" in text
    assert "link" in text
    assert "30d" in text
    assert "/preset" in text
    assert "/lang" in text


def test_render_settings_overview_no_sticky_shows_defaults():
    text = render_settings_overview({}, _fresh_settings())
    assert "(default)" in text
    assert "none (sticky)" in text or "ask each time" in text


# ---------------------------------------------------------------------------
# /lang /enrich /window /settings slash command handlers
# ---------------------------------------------------------------------------


@dataclass
class _FakeApp:
    _chat_state: dict = field(default_factory=dict)
    user_session_ready: bool = True


@dataclass
class _FakeEvent:
    chat_id: int = 42
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str, **_kw: Any) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_lang_command_sets_sticky_report_language():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "lang", "args": ["ru"]}, app=app)
    assert app._chat_state[42][STICKY_REPORT_LANGUAGE] == "ru"


@pytest.mark.asyncio
async def test_lang_command_bare_clears():
    from unread.bot.handlers import cmds

    app = _FakeApp(_chat_state={42: {STICKY_REPORT_LANGUAGE: "ru"}})
    event = _FakeEvent()
    await cmds.handle(event, {"name": "lang", "args": []}, app=app)
    assert STICKY_REPORT_LANGUAGE not in app._chat_state.get(42, {})


@pytest.mark.asyncio
async def test_enrich_command_sets_extras():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "enrich", "args": ["image,link"]}, app=app)
    assert app._chat_state[42][STICKY_ENRICH_EXTRAS] == {"image", "link"}


@pytest.mark.asyncio
async def test_enrich_command_all_enables_every_extra():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "enrich", "args": ["all"]}, app=app)
    assert app._chat_state[42][STICKY_ENRICH_EXTRAS] == {"image", "doc", "link", "video"}


@pytest.mark.asyncio
async def test_window_command_sets_sticky_window():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "window", "args": ["week"]}, app=app)
    assert app._chat_state[42][STICKY_TG_WINDOW] == "7d"


@pytest.mark.asyncio
async def test_settings_command_replies_with_overview():
    from unread.bot.handlers import cmds

    app = _FakeApp()
    event = _FakeEvent()
    await cmds.handle(event, {"name": "settings", "args": []}, app=app)
    assert event.replies
    assert "Settings" in event.replies[0]


# --- /lang drives both the report language AND the UI language --------------


def test_effective_language_follows_the_sticky_lang_choice():
    """A bot chat has ONE language knob. Without this, an admin who set
    `/lang en` got an English analysis under Russian `## Источники`
    headings, because the report headings resolve from the UI axis."""
    from unread.bot.runtime import STICKY_REPORT_LANGUAGE, effective_language

    s = _fresh_settings()
    s.locale.language = "ru"
    assert effective_language({STICKY_REPORT_LANGUAGE: "en"}, s) == "en"


def test_effective_language_falls_back_to_config_without_a_sticky_choice():
    from unread.bot.runtime import effective_language

    s = _fresh_settings()
    s.locale.language = "ru"
    assert effective_language({}, s) == "ru"


def test_effective_language_ignores_a_blank_sticky_value():
    from unread.bot.runtime import STICKY_REPORT_LANGUAGE, effective_language

    s = _fresh_settings()
    s.locale.language = "ru"
    assert effective_language({STICKY_REPORT_LANGUAGE: ""}, s) == "ru"


def test_two_chats_resolve_to_different_ui_languages():
    from unread.bot.runtime import STICKY_REPORT_LANGUAGE, effective_language

    s = _fresh_settings()
    s.locale.language = "ru"
    assert effective_language({STICKY_REPORT_LANGUAGE: "ru"}, s) == "ru"
    assert effective_language({STICKY_REPORT_LANGUAGE: "en"}, s) == "en"
