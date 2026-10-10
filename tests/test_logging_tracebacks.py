"""`log.exception(...)` renders a short exception chain, never frame locals.

structlog's default renderer is a Rich traceback with `show_locals=True`.
In the bot one failed yt-dlp download dumped thousands of lines — every
format dict yt-dlp held — and every `Settings` in scope, so the OpenAI and
OpenRouter keys and the bot token landed in `docker logs` in clear text.
"""

from __future__ import annotations

import io

import pytest

from unread.util.logging import _compact_exception, setup_logging

SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"


class DownloadError(Exception):
    pass


class YoutubeFetchError(Exception):
    pass


def _raise_chain() -> None:
    api_key = SECRET  # noqa: F841 — a local the old renderer printed
    try:
        try:
            raise DownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        except DownloadError as e:
            raise YoutubeFetchError(str(e)) from e
    except YoutubeFetchError as e:
        raise RuntimeError("") from e


def _render() -> str:
    try:
        _raise_chain()
    except RuntimeError as e:
        sio = io.StringIO()
        _compact_exception(sio, (type(e), e, e.__traceback__))
        return sio.getvalue()
    raise AssertionError("unreachable")


def test_root_cause_comes_first_and_wrappers_follow() -> None:
    lines = [line for line in _render().splitlines() if line.strip()]
    assert len(lines) == 3
    assert "DownloadError: ERROR: unable to download video data: HTTP Error 403" in lines[0]
    assert "raised as YoutubeFetchError" in lines[1]
    assert "raised as RuntimeError" in lines[2]


def test_points_at_our_own_innermost_frame() -> None:
    """The frame worth reading is ours, not the library's it called into."""
    from unread.util.logging import set_log_mode

    try:
        set_log_mode("bogus")
    except ValueError as e:
        sio = io.StringIO()
        _compact_exception(sio, (type(e), e, e.__traceback__))
    assert "[unread/util/logging.py:" in sio.getvalue()
    assert "in set_log_mode]" in sio.getvalue()


def test_frames_outside_the_package_are_not_cited() -> None:
    """In the image yt-dlp sits next to us in site-packages."""
    assert "[" not in _render()


def test_never_prints_locals() -> None:
    out = _render()
    assert SECRET not in out
    assert "api_key" not in out


def test_secret_in_the_message_is_masked() -> None:
    try:
        raise ValueError(f"bad key {SECRET}")
    except ValueError as e:
        sio = io.StringIO()
        _compact_exception(sio, (type(e), e, e.__traceback__))
    assert SECRET not in sio.getvalue()


@pytest.mark.parametrize("mode", ["normal", "verbose", "debug"])
def test_log_exception_output_has_no_locals(mode, capsys) -> None:
    import structlog

    setup_logging(mode)
    log = structlog.get_logger("t")
    try:
        _raise_chain()
    except RuntimeError:
        log.error("bot.batch.item_failed", exc_info=True, kind="youtube")
    out = capsys.readouterr().out
    assert "bot.batch.item_failed" in out
    assert "HTTP Error 403" in out
    assert SECRET not in out
    assert "api_key = " not in out
    if mode != "debug":
        assert len(out.splitlines()) < 10


def test_cli_crash_screen_hides_locals() -> None:
    from unread.cli import app

    assert app.pretty_exceptions_show_locals is False
