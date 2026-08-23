"""Multi-admin allowlist for `unread bot`.

`settings.bot.owner_ids` is the allowlist; its first entry is the
*primary* owner — the account whose Telegram user session the bot reads
chats through. Extra admins get the file / URL / YouTube surface but
must never reach anything that opens that shared session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from unread.bot.app import BotApp
from unread.bot.burst import BurstItem
from unread.bot.confirm import PendingRun, RunOptions, encode_callback
from unread.config import load_settings, reset_settings

PRIMARY = 111
ADMIN = 222
STRANGER = 999


def _settings_with_owners(monkeypatch, value: str):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", value)
    reset_settings()
    return load_settings()


class _FakeMessage:
    id = 11

    def __init__(self) -> None:
        self.media = None


class _FakeEvent:
    def __init__(self, *, sender_id: int = PRIMARY, chat_id: int = 7, text: str = "") -> None:
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.message = _FakeMessage()
        self.text = text
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs) -> Any:
        self.replies.append(text)
        return _FakeMessage()


class _FakeCallbackEvent:
    def __init__(self, *, data: bytes, sender_id: int = PRIMARY, chat_id: int = 7) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text: str = "", **_kwargs) -> None:
        self.answers.append(text)

    async def edit(self, text: str, **_kwargs) -> None:
        self.edits.append(text)

    async def get_message(self):
        return _FakeMessage()


# --- allowlist seeding -------------------------------------------------------


def test_app_seeds_allowed_ids_from_every_configured_owner(monkeypatch):
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        assert app.allowed_ids == {PRIMARY, ADMIN}
        assert app.owner_id == PRIMARY
    finally:
        reset_settings()


def test_app_with_no_configured_owner_has_an_empty_allowlist(monkeypatch):
    monkeypatch.delenv("UNREAD_BOT_OWNER_ID", raising=False)
    reset_settings()
    try:
        app = BotApp(load_settings())
        assert app.allowed_ids == set()
        assert app.owner_id == 0
    finally:
        reset_settings()


async def test_session_probe_makes_the_session_user_primary_and_keeps_extra_admins(monkeypatch):
    """The session's own ID wins as primary (it's the account being read),
    but the configured extra admins must survive the override."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        with (
            patch("unread.bot.app._has_session_blob", return_value=True),
            patch(
                "unread.bot.app._probe_session_owner_id",
                new=AsyncMock(return_value=555),
            ),
        ):
            await app._verify_user_session()
        assert app.owner_id == 555
        assert app.allowed_ids == {555, PRIMARY, ADMIN}
        assert app.user_session_ready is True
    finally:
        reset_settings()


# --- callback allowlist ------------------------------------------------------


async def test_callback_from_a_stranger_is_ignored(monkeypatch):
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeCallbackEvent(data=encode_callback("R", 5), sender_id=STRANGER)
        await app._handle_callback(event)
        assert event.answers == []
        assert event.edits == []
    finally:
        reset_settings()


async def test_callback_from_an_extra_admin_is_accepted(monkeypatch):
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        ran: list[Any] = []

        async def _fake(pending, panel_msg):
            ran.append(pending)

        app._run_batch_separately = _fake  # type: ignore[method-assign]
        item = BurstItem(kind="url", payload={"url": "https://example.com"}, event=_FakeEvent())
        app._chat_state[7] = {
            "pending_runs": {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
        }
        event = _FakeCallbackEvent(data=encode_callback("R", 5), sender_id=ADMIN)
        await app._handle_callback(event)
        assert len(ran) == 1
    finally:
        reset_settings()


# --- t.me links are primary-only --------------------------------------------


async def test_tg_link_from_an_extra_admin_is_refused(monkeypatch):
    """A `t.me/...` analyze reads the PRIMARY owner's chats through the
    shared user session — an extra admin must not be able to trigger it."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeEvent(sender_id=ADMIN, text="https://t.me/somechan/42")
        add_mock = AsyncMock()
        with (
            patch(
                "unread.bot.dispatcher.classify",
                return_value=("tg", {"url": "https://t.me/somechan/42"}),
            ),
            patch("unread.bot.burst.add_to_burst", new=add_mock),
        ):
            await app._handle(event)
        add_mock.assert_not_called()
        assert event.replies and "🔒" in event.replies[0]
    finally:
        reset_settings()


async def test_tg_link_from_the_primary_owner_is_queued(monkeypatch):
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeEvent(sender_id=PRIMARY, text="https://t.me/somechan/42")
        add_mock = AsyncMock()
        with (
            patch(
                "unread.bot.dispatcher.classify",
                return_value=("tg", {"url": "https://t.me/somechan/42"}),
            ),
            patch("unread.bot.burst.add_to_burst", new=add_mock),
        ):
            await app._handle(event)
        add_mock.assert_called_once()
        assert event.replies == []
    finally:
        reset_settings()


async def test_youtube_link_from_an_extra_admin_is_queued(monkeypatch):
    """Extra admins keep the file / URL / YouTube surface."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeEvent(sender_id=ADMIN, text="https://youtu.be/abc")
        add_mock = AsyncMock()
        with (
            patch(
                "unread.bot.dispatcher.classify",
                return_value=("youtube", {"url": "https://youtu.be/abc"}),
            ),
            patch("unread.bot.burst.add_to_burst", new=add_mock),
        ):
            await app._handle(event)
        add_mock.assert_called_once()
    finally:
        reset_settings()


async def test_forward_channel_callback_from_an_extra_admin_is_refused(monkeypatch):
    """F_DAY & friends open the SOURCE channel through the user session —
    same restriction as a bare t.me link, enforced at the tap."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        ran: list[Any] = []

        async def _fake(action, pending, panel_msg):
            ran.append(action)

        app._run_forward_action = _fake  # type: ignore[method-assign]
        item = BurstItem(
            kind="file",
            payload={"fwd_channel_id": 123, "text": "hi"},
            event=_FakeEvent(sender_id=ADMIN),
        )
        pending_runs = {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
        app._chat_state[7] = {"pending_runs": pending_runs}
        event = _FakeCallbackEvent(data=encode_callback("F_DAY", 5), sender_id=ADMIN)
        await app._handle_callback(event)
        assert ran == []
        # The panel stays usable — F_TXT / F_FULL are still fine for them.
        assert 5 in pending_runs
        assert any("owner" in a.lower() for a in event.answers)
    finally:
        reset_settings()


async def test_forward_in_place_callback_from_an_extra_admin_is_allowed(monkeypatch):
    """F_TXT analyzes the forwarded text itself — no session involved."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        ran: list[Any] = []

        async def _fake(action, pending, panel_msg):
            ran.append(action)

        app._run_forward_action = _fake  # type: ignore[method-assign]
        item = BurstItem(
            kind="file",
            payload={"fwd_channel_id": 123, "text": "hi"},
            event=_FakeEvent(sender_id=ADMIN),
        )
        app._chat_state[7] = {
            "pending_runs": {5: PendingRun(kind="batch", payload={"items": [item]}, options=RunOptions())}
        }
        event = _FakeCallbackEvent(data=encode_callback("F_TXT", 5), sender_id=ADMIN)
        await app._handle_callback(event)
        assert ran == ["F_TXT"]
    finally:
        reset_settings()


# --- /upload_session is primary-only ----------------------------------------


async def test_upload_session_from_an_extra_admin_is_refused(monkeypatch):
    from unread.bot.handlers import cmds

    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeEvent(sender_id=ADMIN)
        start_mock = AsyncMock()
        with patch("unread.bot.session_upload.start_upload", new=start_mock):
            await cmds.handle(event, {"name": "upload_session", "args": []}, app=app)
        start_mock.assert_not_called()
        assert event.replies and "🔒" in event.replies[0]
    finally:
        reset_settings()


async def test_upload_session_from_the_primary_owner_is_allowed(monkeypatch):
    from unread.bot.handlers import cmds

    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        event = _FakeEvent(sender_id=PRIMARY)
        start_mock = AsyncMock()
        with patch("unread.bot.session_upload.start_upload", new=start_mock):
            await cmds.handle(event, {"name": "upload_session", "args": []}, app=app)
        start_mock.assert_called_once()
    finally:
        reset_settings()


def test_setting_owner_id_adds_it_to_the_allowlist(monkeypatch):
    """`/upload_session` re-derives the primary owner after installing a
    new session file. If that assignment didn't reach the allowlist, the
    bot would stop answering the very account it just adopted."""
    app = BotApp(_settings_with_owners(monkeypatch, f"{PRIMARY},{ADMIN}"))
    try:
        app.owner_id = 555
        assert app.owner_id == 555
        assert 555 in app.allowed_ids
        # The previous primary stays on as an ordinary admin.
        assert PRIMARY in app.allowed_ids
    finally:
        reset_settings()
