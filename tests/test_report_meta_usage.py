"""Report header: tokens, time, honest cache and cost rows."""

from __future__ import annotations

import pytest

from unread.analyzer.commands import _analyze_meta_rows
from unread.analyzer.pipeline import AnalysisResult
from unread.config import load_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    load_settings()
    yield
    reset_settings()


def _rows(**overrides) -> dict[str, str]:
    base = {
        "preset": "video_summary",
        "model": "gpt-5.4-mini",
        "chat_id": 123,
        "thread_id": 0,
        "msg_count": 7,
        "chunk_count": 1,
        "batch_hashes": [],
        "final_result": "body",
        "total_cost_usd": 0.0123,
        "cache_hits": 0,
        "cache_misses": 1,
        "ui_language": "en",
        "source_kind": "video",
    }
    base.update(overrides)
    return dict(_analyze_meta_rows(AnalysisResult(**base), title="Some video"))


def test_video_report_has_no_period_row():
    assert "**Period:**" not in _rows()
    assert "**Period:**" in _rows(source_kind="chat")


def test_tokens_row_shows_provider_cache_and_time():
    rows = _rows(prompt_tokens=26599, cached_tokens=7215, completion_tokens=4607, elapsed_s=34.6)
    assert rows["**Tokens:**"] == "26 599 in (7 215 from provider cache, 27%) + 4 607 out"
    assert rows["**Time:**"] == "34.6s"


def test_local_cache_row_only_when_it_served_something():
    assert "**Cache:**" not in _rows()
    assert _rows(cache_hits=1, cache_misses=0, total_cost_usd=0.0)["**Cache:**"].startswith("1/1")


def test_unpriced_model_is_not_reported_as_free():
    rows = _rows(total_cost_usd=0.0, unpriced_models=["openai/gpt-6-luna-pro"])
    assert rows["**Cost:**"].startswith("unknown")
    assert "openai/gpt-6-luna-pro" in rows["**Cost:**"]
