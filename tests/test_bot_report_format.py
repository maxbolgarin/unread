"""`/format pdf|md|rich` — how the bot delivers a finished report.

`rich` renders the report as Telegram message(s) instead of a file. The
length limit is NOT a hardcoded 4096: `Config.message_length_max` is a
server-provided value Telegram sends at connect (see the MTProto `config`
constructor), so the splitter reads it from the live client and only
falls back to 4096 when it can't.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from unread.bot.reply import split_for_telegram, telegram_message_limit
from unread.bot.runtime import parse_format_value

# --- /format parsing ----------------------------------------------------------


@pytest.mark.parametrize("arg", ["pdf", "PDF", " pdf "])
def test_parses_pdf(arg) -> None:
    value, msg = parse_format_value(arg)
    assert value == "pdf"
    assert msg


@pytest.mark.parametrize("arg", ["md", "markdown"])
def test_parses_md(arg) -> None:
    assert parse_format_value(arg)[0] == "md"


@pytest.mark.parametrize("arg", ["rich", "text", "msg"])
def test_parses_rich_and_its_aliases(arg) -> None:
    assert parse_format_value(arg)[0] == "rich"


@pytest.mark.parametrize("arg", ["", "none", "default"])
def test_bare_format_clears_the_override(arg) -> None:
    value, msg = parse_format_value(arg)
    assert value == ""
    assert msg


def test_garbage_is_rejected_with_usage() -> None:
    value, msg = parse_format_value("docx")
    assert value is None
    assert "pdf" in msg and "md" in msg and "rich" in msg


# --- the limit is read from the server ---------------------------------------


def test_limit_comes_from_the_live_client_config() -> None:
    client = type("C", (), {"_config": type("Cfg", (), {"message_length_max": 8192})()})()
    assert telegram_message_limit(client) == 8192


def test_limit_falls_back_when_the_client_has_no_config() -> None:
    assert telegram_message_limit(None) == 4096
    assert telegram_message_limit(object()) == 4096


def test_limit_ignores_a_nonsense_server_value() -> None:
    """A zero or negative limit would make the splitter loop forever."""
    client = type("C", (), {"_config": type("Cfg", (), {"message_length_max": 0})()})()
    assert telegram_message_limit(client) == 4096


# --- splitting ----------------------------------------------------------------


def test_short_report_is_one_message() -> None:
    assert split_for_telegram("# Title\n\nshort body", limit=4096) == ["# Title\n\nshort body"]


def test_split_prefers_section_boundaries() -> None:
    """A verdict cut in half mid-sentence is worse than an extra message."""
    body = "## One\n\n" + ("a" * 300) + "\n\n## Two\n\n" + ("b" * 300)
    parts = split_for_telegram(body, limit=400)
    assert len(parts) >= 2
    assert all(p.strip() for p in parts)
    assert parts[1].lstrip().startswith("## Two")


def test_every_part_respects_the_limit() -> None:
    body = "\n\n".join(f"## S{i}\n\n" + ("x" * 200) for i in range(12))
    parts = split_for_telegram(body, limit=500)
    assert parts
    assert all(len(p) <= 500 for p in parts)


def test_a_single_oversized_section_is_still_split() -> None:
    """One giant paragraph has no boundary to split on — it must still be
    chopped rather than sent over the limit."""
    parts = split_for_telegram("x" * 1000, limit=300)
    assert all(len(p) <= 300 for p in parts)
    assert "".join(parts).replace("\n", "") == "x" * 1000


def test_nothing_is_lost_in_the_split() -> None:
    body = "\n\n".join(f"## S{i}\n\nbody {i}" for i in range(8))
    parts = split_for_telegram(body, limit=60)
    rejoined = "\n\n".join(p.strip() for p in parts)
    for i in range(8):
        assert f"body {i}" in rejoined


def test_empty_body_yields_nothing() -> None:
    assert split_for_telegram("", limit=100) == []


# --- /format wiring -----------------------------------------------------------


async def test_format_command_persists_the_choice(tmp_path, monkeypatch) -> None:
    from unread.bot.app import BotApp
    from unread.bot.handlers import cmds
    from unread.config import load_settings, reset_settings
    from unread.db.repo import open_repo

    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    try:
        s = load_settings()
        s.storage.data_path = tmp_path / "d.sqlite"
        app = BotApp(s)

        class _E:
            chat_id = 7
            sender_id = 111

            def __init__(self):
                self.replies = []

            async def reply(self, text, **_kw):
                self.replies.append(text)

        await cmds.handle(_E(), {"name": "format", "args": ["rich"]}, app=app)
        async with open_repo(s.storage.data_path) as repo:
            stored = await repo.get_bot_chat_settings(7)
        assert stored.get("report_format") == "rich"
    finally:
        reset_settings()


def test_effective_format_prefers_the_sticky_value() -> None:
    from unread.bot.runtime import STICKY_REPORT_FORMAT, effective_report_format
    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        s.bot.report_format = "pdf"
        assert effective_report_format({}, s) == "pdf"
        assert effective_report_format({STICKY_REPORT_FORMAT: "rich"}, s) == "rich"
    finally:
        reset_settings()


def test_help_lists_the_available_formats() -> None:
    from unread.bot.handlers.cmds import _SLASH_COMMANDS

    assert "/format" in _SLASH_COMMANDS
    for fmt in ("pdf", "md", "rich"):
        assert fmt in _SLASH_COMMANDS


def test_effective_format_reads_the_app_off_the_event(monkeypatch) -> None:
    """`_run_execute` stashes the app on the event so the reply layer can
    honour a per-admin `/format` without threading `app` through every
    send_* signature."""
    from unread.bot.reply import _effective_format
    from unread.bot.runtime import STICKY_REPORT_FORMAT
    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        load_settings()

        class _App:
            _chat_state: ClassVar[dict] = {7: {STICKY_REPORT_FORMAT: "rich"}}

        class _Event:
            chat_id = 7
            _unread_app = _App()

        assert _effective_format(_Event()) == "rich"
    finally:
        reset_settings()


def test_effective_format_without_an_app_uses_the_config_default() -> None:
    from unread.bot.reply import _effective_format
    from unread.config import get_settings, reset_settings

    reset_settings()
    try:
        # Mutate the SINGLETON — `_effective_format` reads `get_settings()`,
        # and `load_settings()` hands back a fresh object.
        get_settings().bot.report_format = "md"

        class _Event:
            chat_id = 7

        assert _effective_format(_Event()) == "md"
    finally:
        reset_settings()
