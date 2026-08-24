"""Report labels must match the source.

A YouTube report said "Чат:" and "Сообщений проанализировано" — the
labels are chat-centric because analyze started as a Telegram tool, and
video/page/file runs inherited them.
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


def _result(**kw) -> AnalysisResult:
    base = {
        "preset": "video",
        "model": "m",
        "chat_id": 0,
        "thread_id": 0,
        "msg_count": 11,
        "chunk_count": 1,
        "batch_hashes": [],
        "final_result": "b",
        "total_cost_usd": 0.005,
        "cache_hits": 0,
        "cache_misses": 1,
        "ui_language": "en",
    }
    base.update(kw)
    return AnalysisResult(**base)


def _labels(result) -> str:
    return " ".join(label for label, _ in _analyze_meta_rows(result, title="X"))


def test_video_run_is_not_labelled_as_a_chat() -> None:
    labels = _labels(_result(source_kind="video"))
    assert "Chat" not in labels
    assert "Video" in labels


def test_video_run_counts_segments_not_messages() -> None:
    """A transcript is cut into segments; calling them "messages" is a
    leak of the Telegram-first internals."""
    labels = _labels(_result(source_kind="video"))
    assert "Messages" not in labels
    assert "Segment" in labels


def test_website_run_says_page() -> None:
    labels = _labels(_result(source_kind="website"))
    assert "Page" in labels
    assert "Chat" not in labels


def test_file_run_says_file() -> None:
    labels = _labels(_result(source_kind="file"))
    assert "File" in labels
    assert "Chat" not in labels


def test_chat_run_is_unchanged() -> None:
    labels = _labels(_result(source_kind="chat"))
    assert "Chat" in labels
    assert "Messages" in labels


def test_default_source_kind_still_reads_as_a_chat() -> None:
    """Callers that build a result without the field keep old behaviour."""
    assert "Chat" in _labels(_result())


@pytest.mark.parametrize("kind", ["video", "website", "file", "chat"])
def test_labels_exist_in_both_languages(kind) -> None:
    from unread.i18n import _STRINGS

    for key in (f"report_meta_source_{kind}", f"report_meta_units_{kind}"):
        assert key in _STRINGS, key
        assert _STRINGS[key].get("ru"), key
