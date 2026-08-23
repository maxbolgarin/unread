"""The report must say whether the fact-check actually searched the web.

Two reasons this earns a metadata row. All three providers bill web
search PER SEARCH, separately from tokens, so the report's own cost line
understates a grounded run — silently, unless it's called out. And a
fact-check run on a provider with no search looks identical to a grounded
one, which is exactly the thing a reader must not be misled about.
"""

from __future__ import annotations

import pytest

from unread.analyzer.commands import _analyze_meta_rows
from unread.analyzer.pipeline import AnalysisResult
from unread.config import load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean():
    reset_settings()
    load_settings()
    yield
    reset_settings()


def _result(**overrides) -> AnalysisResult:
    base = {
        "preset": "factcheck",
        "model": "gpt-5.4",
        "chat_id": 1,
        "thread_id": 0,
        "msg_count": 5,
        "chunk_count": 1,
        "batch_hashes": [],
        "final_result": "body",
        "total_cost_usd": 0.5,
        "cache_hits": 0,
        "cache_misses": 1,
    }
    base.update(overrides)
    return AnalysisResult(**base)


def _rows(result) -> dict[str, str]:
    return dict(_analyze_meta_rows(result, title="X"))


def _find(rows: dict[str, str], needle: str) -> str:
    for label, value in rows.items():
        if needle in label.lower():
            return value
    return ""


def test_grounded_run_reports_that_search_was_used():
    value = _find(_rows(_result(web_search=True, ui_language="en")), "web search")
    assert value
    assert "not included" in value.lower() or "extra" in value.lower()


def test_ungrounded_run_says_search_was_unavailable():
    value = _find(_rows(_result(web_search=False, ui_language="en")), "web search")
    assert value
    assert "unavailable" in value.lower() or "no web" in value.lower()


def test_normal_presets_have_no_web_search_row():
    """`None` means "not applicable" — a summary must not grow a row."""
    rows = _rows(_result(preset="summary", ui_language="en"))
    assert not _find(rows, "web search")


def test_web_search_row_is_localized():
    en = _find(_rows(_result(web_search=True, ui_language="en")), "web search")
    ru_rows = _analyze_meta_rows(_result(web_search=True, ui_language="ru"), title="X")
    ru = ""
    for label, value in ru_rows:
        if "поиск" in label.lower():
            ru = value
    assert ru and ru != en
