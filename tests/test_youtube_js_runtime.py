"""YouTube audio downloads need a JS runtime to solve the nsig challenge.

Without one yt-dlp still extracts metadata, but every media URL it
builds is signed wrong and the first byte of the download comes back
403 Forbidden. That is what the bot hit, and the warning that names the
cause was logged at debug, so the log showed only the 403.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from unread.youtube import transcript
from unread.youtube.metadata import _ydl_options

ROOT = Path(__file__).resolve().parents[1]


def test_image_ships_a_js_runtime() -> None:
    assert "/usr/local/bin/deno" in (ROOT / "Dockerfile").read_text()


def test_ytdlp_comes_with_the_challenge_solver() -> None:
    """`yt-dlp[default]` pulls yt-dlp-ejs, the script deno runs."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.replace(" ", "").startswith("yt-dlp[default]") for d in deps)


def test_metadata_extraction_routes_warnings_through_our_logger() -> None:
    """Extraction is where yt-dlp notices the missing runtime."""
    assert isinstance(_ydl_options().get("logger"), transcript._YtDlpLogger)


@pytest.mark.parametrize(
    "msg",
    [
        "[youtube] No supported JavaScript runtime could be found. Only deno is enabled by default",
        "[youtube] abc: n challenge solving failed: Some formats may be missing",
        "[youtube] abc: Signature solving failed: Some formats may be missing",
        "Your yt-dlp version (2026.03.17) is older than 90 days!",
    ],
)
def test_actionable_warnings_are_raised_to_warning(msg, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(transcript.log, "warning", lambda event, **_kw: calls.append(event))
    monkeypatch.setattr(transcript.log, "debug", lambda event, **_kw: None)
    transcript._YtDlpLogger().warning(msg)
    assert calls == ["ytdlp.warning"]


def test_routine_warnings_stay_at_debug(monkeypatch) -> None:
    warned: list[str] = []
    monkeypatch.setattr(transcript.log, "warning", lambda event, **_kw: warned.append(event))
    monkeypatch.setattr(transcript.log, "debug", lambda event, **_kw: None)
    transcript._YtDlpLogger().warning("[youtube] abc: Falling back to generic n function search")
    assert warned == []
