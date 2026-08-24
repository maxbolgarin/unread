"""The uploaded `.md` must announce its encoding.

Telegram's in-app text viewer on iOS guessed Latin-1 for a UTF-8 report
and showed the whole Russian document as mojibake ("Ð Ñ€Ðµ…", "â€\"").
The file was fine; nothing told the viewer how to read it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unread.bot.reply import _send_full_report
from unread.config import get_settings, reset_settings


@pytest.fixture(autouse=True)
def _md_format():
    reset_settings()
    get_settings().bot.report_format = "md"
    yield
    reset_settings()


class _Client:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_file(self, chat_id, **kw):
        self.sent.append({"chat_id": chat_id, **kw})


class _Event:
    def __init__(self) -> None:
        self.chat_id = 7
        self.client = _Client()
        self.message = type("M", (), {"id": 1})()
        self.replies: list[str] = []

    async def reply(self, text, **_kw) -> Any:
        self.replies.append(text)
        return None


def _report(tmp_path: Path) -> Path:
    p = tmp_path / "отчёт-factcheck.md"
    p.write_text("# Отчёт\n\nВремени не существует — проверка.\n", encoding="utf-8")
    return p


async def test_md_upload_declares_utf8(tmp_path) -> None:
    event = _Event()
    report = _report(tmp_path)
    await _send_full_report(event, report=report, md_text=report.read_text(encoding="utf-8"), caption="✓ 1s")
    sent = event.client.sent[0]
    mime = (sent.get("mime_type") or "").lower()
    assert "charset=utf-8" in mime, sent


async def test_md_upload_starts_with_a_bom(tmp_path) -> None:
    """The MIME type alone doesn't reach every viewer; a BOM is the
    in-band signal that survives being saved and reopened."""
    event = _Event()
    report = _report(tmp_path)
    await _send_full_report(event, report=report, md_text=report.read_text(encoding="utf-8"), caption="✓ 1s")
    payload = event.client.sent[0]["file"]
    data = payload.getvalue() if hasattr(payload, "getvalue") else Path(payload).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), data[:12]


async def test_uploaded_filename_keeps_the_md_extension(tmp_path) -> None:
    event = _Event()
    report = _report(tmp_path)
    await _send_full_report(event, report=report, md_text=report.read_text(encoding="utf-8"), caption="✓ 1s")
    attrs = event.client.sent[0].get("attributes") or []
    names = [getattr(a, "file_name", "") for a in attrs]
    assert any(n.endswith(".md") for n in names), names


async def test_saved_report_on_disk_is_not_given_a_bom(tmp_path) -> None:
    """The BOM is for the upload only — the on-disk report stays clean so
    tooling that reads it back isn't tripped by a stray \\ufeff."""
    event = _Event()
    report = _report(tmp_path)
    await _send_full_report(event, report=report, md_text=report.read_text(encoding="utf-8"), caption="✓ 1s")
    assert not report.read_bytes().startswith(b"\xef\xbb\xbf")


async def test_transcript_upload_also_declares_utf8(tmp_path) -> None:
    """The transcript is markdown too, and hits the same iOS viewer that
    mangled the report."""
    from unread.bot.reply import send_transcript_dump

    transcript = tmp_path / "transcript.md"
    transcript.write_text("# Т\n\nПодождите, мы не можем начать.\n", encoding="utf-8")
    event = _Event()
    await send_transcript_dump(event, transcript=transcript, started=0.0, title="Т")

    sent = event.client.sent[0]
    assert "charset=utf-8" in (sent.get("mime_type") or "").lower()
    payload = sent["file"]
    data = payload.getvalue() if hasattr(payload, "getvalue") else Path(payload).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")


async def test_transcript_upload_keeps_the_md_name(tmp_path) -> None:
    from unread.bot.reply import send_transcript_dump

    transcript = tmp_path / "transcript.md"
    transcript.write_text("x", encoding="utf-8")
    event = _Event()
    await send_transcript_dump(event, transcript=transcript, started=0.0, title="Т")
    attrs = event.client.sent[0].get("attributes") or []
    assert any(getattr(a, "file_name", "").endswith(".md") for a in attrs)
