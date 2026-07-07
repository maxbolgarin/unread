"""Tests for `unread.bot.session_upload` validators and metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unread.bot import session_upload


@dataclass
class _Attr:
    file_name: str | None = None


@dataclass
class _Doc:
    mime_type: str = ""
    size: int | None = None
    attributes: list[_Attr] = field(default_factory=list)


@dataclass
class _Media:
    document: Any = None


@dataclass
class _Msg:
    media: Any = None


@dataclass
class _Event:
    message: _Msg


def test_name_of_attachment_returns_filename():
    ev = _Event(_Msg(media=_Media(document=_Doc(attributes=[_Attr(file_name="session.sqlite")]))))
    assert session_upload._name_of_attachment(ev) == "session.sqlite"


def test_name_of_attachment_empty_when_no_media():
    ev = _Event(_Msg(media=None))
    assert session_upload._name_of_attachment(ev) == ""


def test_name_of_attachment_empty_when_no_filename_attr():
    ev = _Event(_Msg(media=_Media(document=_Doc(attributes=[]))))
    assert session_upload._name_of_attachment(ev) == ""


def test_size_of_attachment_reads_document_size():
    ev = _Event(_Msg(media=_Media(document=_Doc(size=12345))))
    assert session_upload._size_of_attachment(ev) == 12345


def test_size_of_attachment_none_when_no_media():
    ev = _Event(_Msg(media=None))
    assert session_upload._size_of_attachment(ev) is None


@pytest.mark.asyncio
async def test_probe_candidate_owner_id_missing_file(tmp_path):
    """Candidate validator returns None on missing / empty files (no Telethon call)."""
    from unread.bot.session_upload import _probe_candidate_owner_id
    from unread.config import get_settings

    s = get_settings()
    missing = tmp_path / "absent.sqlite"
    assert await _probe_candidate_owner_id(missing, s) is None
    empty = tmp_path / "empty.sqlite"
    empty.write_bytes(b"")
    assert await _probe_candidate_owner_id(empty, s) is None


def test_normalized_session_path_adds_session_suffix():
    """Telethon appends `.session` — installer destination must match."""
    from pathlib import Path

    from unread.bot.session_upload import _normalized_session_path

    assert _normalized_session_path(Path("/x/y/session.sqlite")) == Path("/x/y/session.sqlite.session")
    # Already-suffixed paths are left alone.
    assert _normalized_session_path(Path("/x/y/foo.session")) == Path("/x/y/foo.session")


def test_has_session_blob_finds_dot_session_file(tmp_path, monkeypatch):
    """The legacy `session.sqlite` AND Telethon's `session.sqlite.session`
    both count as a usable session blob."""
    from unread.bot.app import _has_session_blob

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    storage = tmp_path / "storage"
    storage.mkdir()
    # Only the .session-suffixed file exists (this is the real Telethon shape).
    (storage / "session.sqlite.session").write_bytes(b"\x00")

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert _has_session_blob(s) is True
    finally:
        reset_settings()


def test_has_session_blob_false_when_neither_exists(tmp_path, monkeypatch):
    from unread.bot.app import _has_session_blob

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "fresh"))

    from unread.config import load_settings, reset_settings

    reset_settings()
    try:
        s = load_settings()
        assert _has_session_blob(s) is False
    finally:
        reset_settings()


# ----------------------------------------------------------------------
# B1: `handle_uploaded_file` end-to-end — staged filename + install method
# ----------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Per-test install home for `handle_uploaded_file` runs.

    The handler resolves `settings.telegram.session_path` via
    `get_settings()` and `shutil.move`s the validated upload there —
    without this isolation it would write a fake
    `storage/session.sqlite.session` into the session-wide shared
    test home from conftest, contaminating other tests.
    """
    from unread.config import reset_settings

    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    reset_settings()
    yield tmp_path
    reset_settings()


class _FakeBotClient:
    """Captures the `file=` path `download_media` was told to write to,
    and simulates a successful download by writing bytes there."""

    def __init__(self):
        self.staged_path: str | None = None

    async def download_media(self, message, file):
        self.staged_path = file
        Path(file).write_bytes(b"x" * 32)
        return file


class _FakeApp:
    def __init__(self):
        self.bot_client = _FakeBotClient()
        self._chat_state: dict[int, dict] = {}
        self.owner_id = 0
        self.user_session_ready = False


class _FakeUploadDoc:
    def __init__(self, file_name: str = "session.sqlite", size: int = 32):
        self.mime_type = ""
        self.size = size
        self.attributes = [SimpleNamespace(file_name=file_name)]


class _FakeUploadEvent:
    def __init__(self, chat_id: int = 1, file_name: str = "session.sqlite", size: int = 32):
        self.chat_id = chat_id
        doc = _FakeUploadDoc(file_name=file_name, size=size)
        self.message = SimpleNamespace(media=SimpleNamespace(document=doc))
        self.replies: list[str] = []

    async def reply(self, text, *args, **kwargs):
        self.replies.append(text)


def _make_authorized_fake_telegram_client(captured: dict):
    class _FakeTelegramClient:
        def __init__(self, session, api_id=None, api_hash=None):
            captured["session_arg"] = session
            # Snapshot existence NOW — the caller's `finally` block cleans
            # up the tempdir after this constructor returns, so checking
            # from the test body (post-await) would always see it gone.
            captured["session_arg_existed_at_construction"] = Path(session).exists()

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=987654321)

        async def disconnect(self):
            pass

    return _FakeTelegramClient


@pytest.mark.asyncio
async def test_staged_session_filename_ends_with_dot_session(monkeypatch, isolated_home):
    """Regression for B1: Telethon's `SQLiteSession` appends `.session` to any
    session name that doesn't already end in it. If we stage the download as
    `candidate.sqlite`, the probe opens a brand-new empty `candidate.sqlite.session`
    instead of the file we just downloaded, so `is_user_authorized()` is always
    False and every valid upload is rejected. The staged filename must already
    end in `.session` so Telethon opens exactly the file we downloaded."""
    captured: dict = {}
    monkeypatch.setattr("telethon.TelegramClient", _make_authorized_fake_telegram_client(captured))

    app = _FakeApp()
    event = _FakeUploadEvent()

    await session_upload.handle_uploaded_file(event, app=app)

    staged_path = app.bot_client.staged_path
    assert staged_path is not None
    assert staged_path.endswith(".session"), (
        f"staged path {staged_path!r} must end in '.session' or Telethon "
        "mangles it into a different, empty session file"
    )


@pytest.mark.asyncio
async def test_probe_opens_the_exact_staged_file(monkeypatch, isolated_home):
    """The path Telethon's `TelegramClient` is constructed with must be
    byte-identical to the on-disk staged file — not a mangled sibling."""
    captured: dict = {}
    monkeypatch.setattr("telethon.TelegramClient", _make_authorized_fake_telegram_client(captured))

    app = _FakeApp()
    event = _FakeUploadEvent()

    await session_upload.handle_uploaded_file(event, app=app)

    staged_path = app.bot_client.staged_path
    assert captured["session_arg"] == staged_path
    # Also prove it was a real file on disk at that exact path when
    # Telethon opened it (not a missing/mangled sibling).
    assert captured["session_arg_existed_at_construction"] is True


@pytest.mark.asyncio
async def test_successful_upload_reports_install_and_updates_owner(monkeypatch, isolated_home):
    captured: dict = {}
    monkeypatch.setattr("telethon.TelegramClient", _make_authorized_fake_telegram_client(captured))

    app = _FakeApp()
    event = _FakeUploadEvent()

    await session_upload.handle_uploaded_file(event, app=app)

    assert app.user_session_ready is True
    assert app.owner_id == 987654321
    assert any("installed" in r.lower() for r in event.replies)
    # The install must land inside the per-test home, not the shared one.
    installed = isolated_home / "storage" / "session.sqlite.session"
    assert installed.exists()
    assert installed.read_bytes() == b"x" * 32


@pytest.mark.asyncio
async def test_install_uses_shutil_move_not_os_replace(monkeypatch, isolated_home):
    """B1: `os.replace` fails across filesystems (Docker tmpfs → named
    volume mount) with EXDEV. The installer must use `shutil.move`."""
    captured: dict = {}
    monkeypatch.setattr("telethon.TelegramClient", _make_authorized_fake_telegram_client(captured))

    move_calls: list[tuple[str, str]] = []
    real_move = session_upload.shutil.move

    def fake_move(src, dst, *a, **kw):
        move_calls.append((str(src), str(dst)))
        return real_move(src, dst, *a, **kw)

    def fake_replace(*a, **kw):
        raise AssertionError("os.replace must not be used for the final install — use shutil.move")

    monkeypatch.setattr(session_upload.shutil, "move", fake_move)
    monkeypatch.setattr(session_upload.os, "replace", fake_replace)

    app = _FakeApp()
    event = _FakeUploadEvent()

    await session_upload.handle_uploaded_file(event, app=app)

    assert len(move_calls) == 1
