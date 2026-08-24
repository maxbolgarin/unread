"""Progress line the bot shows while a run is in flight.

"Analyzing video…" is wrong for a fact-check, and the line was carrying
no information about what the run would cost or which model it uses —
the two things worth knowing while you wait.
"""

from __future__ import annotations

import pytest

from unread.bot.progress import run_status_line
from unread.config import load_settings, reset_settings


@pytest.fixture
def settings():
    reset_settings()
    s = load_settings()
    s.ai.chat_provider = "openrouter"
    yield s
    reset_settings()


def test_factcheck_says_fact_checking(settings) -> None:
    line = run_status_line(preset="factcheck", settings=settings, source="video")
    assert "fact-check" in line.lower()
    assert "analyz" not in line.lower()


def test_other_presets_still_say_analyzing(settings) -> None:
    line = run_status_line(preset="summary", settings=settings, source="video")
    assert "analyz" in line.lower()


def test_line_names_the_model_and_provider(settings) -> None:
    """While you're waiting, which model is spending your money is the
    useful fact."""
    line = run_status_line(preset="factcheck", settings=settings, source="video")
    assert "openrouter" in line.lower()
    assert "luna" in line.lower()


def test_line_names_the_preset(settings) -> None:
    line = run_status_line(preset="digest", settings=settings, source="chat")
    assert "digest" in line


def test_line_mentions_web_search_only_for_a_searching_preset(settings) -> None:
    """Web search is billed per search on top of tokens — worth flagging
    while the run is happening, not just afterwards."""
    fact = run_status_line(preset="factcheck", settings=settings, source="video")
    plain = run_status_line(preset="summary", settings=settings, source="video")
    assert "web" in fact.lower()
    assert "web" not in plain.lower()


def test_unknown_preset_does_not_crash(settings) -> None:
    assert run_status_line(preset="nonesuch", settings=settings, source="video")


def test_source_label_is_included(settings) -> None:
    assert "video" in run_status_line(preset="summary", settings=settings, source="video")
    assert "page" in run_status_line(preset="website", settings=settings, source="page")
