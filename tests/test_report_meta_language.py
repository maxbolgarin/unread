"""Report metadata table must follow the run's UI language.

The `## Sources` / `## Verification` headings already take an explicit
`language` argument, but the metadata table above them resolved from the
process-global `locale.language`. In a multi-admin bot that means one
admin's English report gets a Russian metadata table.
"""

from __future__ import annotations

import pytest

from unread.analyzer.commands import _analyze_meta_rows
from unread.analyzer.pipeline import AnalysisResult
from unread.config import get_settings, load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    load_settings()
    yield
    reset_settings()


def _result(**overrides) -> AnalysisResult:
    base = {
        "preset": "summary",
        "model": "gpt-5.4-mini",
        "chat_id": 123,
        "thread_id": 0,
        "msg_count": 10,
        "chunk_count": 1,
        "batch_hashes": [],
        "final_result": "body",
        "total_cost_usd": 0.01,
        "cache_hits": 0,
        "cache_misses": 1,
    }
    base.update(overrides)
    return AnalysisResult(**base)


def _labels(rows) -> str:
    return " ".join(label for label, _value in rows)


def test_meta_rows_follow_the_results_ui_language():
    get_settings().locale.language = "ru"
    rows = _analyze_meta_rows(_result(ui_language="en"), title="Chat")
    labels = _labels(rows)
    assert "Chat" in labels or "Period" in labels
    assert "Чат" not in labels


def test_meta_rows_fall_back_to_the_global_when_unset():
    """Callers that build an AnalysisResult without a language (tests, older
    code paths) keep today's behavior."""
    get_settings().locale.language = "ru"
    rows = _analyze_meta_rows(_result(), title="Chat")
    labels = _labels(rows)
    assert "Чат" in labels or "Период" in labels


def test_two_languages_produce_different_labels():
    get_settings().locale.language = "ru"
    en = _labels(_analyze_meta_rows(_result(ui_language="en"), title="Chat"))
    ru = _labels(_analyze_meta_rows(_result(ui_language="ru"), title="Chat"))
    assert en != ru
