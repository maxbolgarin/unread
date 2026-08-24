"""Regressions from the second pre-release review."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unread.bot.app import BotApp
from unread.config import load_settings, reset_settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "111")
    reset_settings()
    s = load_settings()
    s.storage.data_path = tmp_path / "d.sqlite"
    yield BotApp(s)
    reset_settings()


class _Msg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.raw_text = text
        self.deleted = False
        self.media = None

    async def delete(self):
        self.deleted = True


class _Event:
    def __init__(self, text: str = "", chat_id: int = 7, sender_id: int = 111) -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = _Msg(text)
        self.text = text
        self.raw_text = text
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw) -> Any:
        self.replies.append(text)
        return None


# --- the API-key prompt must not eat an ordinary message ---------------------


async def test_expired_key_prompt_does_not_capture(app) -> None:
    """Tap 🔑, get distracted, paste a link an hour later — that link was
    stored AS THE API KEY, clobbering a working credential."""
    from unread.bot.handlers import cmds

    state = app._chat_state.setdefault(7, {})
    state["pending_api_key"] = "openai"
    state["pending_api_key_at"] = time.time() - 3600

    event = _Event(text="https://youtu.be/abc123")
    assert await cmds.maybe_consume_api_key(event, app=app) is False
    assert event.message.deleted is False


async def test_obvious_non_key_is_rejected(app) -> None:
    """Even inside the window, a URL is not an API key."""
    from unread.bot.handlers import cmds

    state = app._chat_state.setdefault(7, {})
    state["pending_api_key"] = "openai"
    state["pending_api_key_at"] = time.time()

    event = _Event(text="https://youtu.be/abc123")
    handled = await cmds.maybe_consume_api_key(event, app=app)
    assert handled is True  # consumed, but...
    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        secrets = await repo.get_secrets()
    assert "openai.api_key" not in secrets
    assert any("doesn't look like" in r.lower() or "not stored" in r.lower() for r in event.replies)


async def test_a_plausible_key_is_still_accepted(app) -> None:
    from unread.bot.handlers import cmds

    state = app._chat_state.setdefault(7, {})
    state["pending_api_key"] = "openai"
    state["pending_api_key_at"] = time.time()

    await cmds.maybe_consume_api_key(_Event(text="sk-proj-abcdef1234567890abcdef"), app=app)
    from unread.db.repo import open_repo

    async with open_repo(app.settings.storage.data_path) as repo:
        secrets = await repo.get_secrets()
    assert secrets.get("openai.api_key") == "sk-proj-abcdef1234567890abcdef"


# --- the splitter must never exceed the limit --------------------------------


def test_unorphaning_a_long_heading_never_exceeds_the_limit() -> None:
    """`_unorphan_headings` prepended a heading to an already-full part
    with no re-check. Over the limit, BOTH send attempts raise, and
    `suppress(Exception)` drops that section of the report silently."""
    from unread.bot.reply import split_for_telegram

    heading = "## Claim 4: the GDP figure quoted in the interview"
    body = "\n\n".join(
        [("x" * 120), heading, ("y" * 120), "## Claim 5: another long heading here", ("z" * 120)]
    )
    for limit in (150, 200, 260, 400):
        parts = split_for_telegram(body, limit=limit)
        assert all(len(p) <= limit for p in parts), f"limit={limit}: {[len(p) for p in parts]}"


def test_content_survives_unorphaning() -> None:
    from unread.bot.reply import split_for_telegram

    heading = "## A heading long enough to overflow the part it is moved into"
    body = "\n\n".join([("x" * 90), heading, ("y" * 90)])
    parts = split_for_telegram(body, limit=120)
    joined = "".join(parts)
    assert "x" * 90 in joined
    assert "y" * 90 in joined
    assert heading in joined


# --- the model menu must actually change the model ---------------------------


async def test_model_choice_reaches_the_analyze_call(app) -> None:
    """`ai.chat_model` is never read by the analyze pipeline — presets pin
    `final_model`, and only `model_override` beats a pin. Writing
    `ai.chat_model` and reporting success was a no-op."""
    from unread.bot.confirm import RunOptions
    from unread.bot.handlers import youtube as yt
    from unread.config import get_settings

    get_settings().ai.chat_model = "gpt-5.6-sol"
    analyze = AsyncMock()
    with (
        patch("unread.youtube.commands.cmd_analyze_youtube", new=analyze),
        patch("unread.bot.reply.send_youtube_report", new=AsyncMock()),
    ):
        await yt.execute(
            _Event(), {"url": "https://youtu.be/abc12345678"}, RunOptions(), app=app, progress_msg=None
        )
    assert analyze.call_args.kwargs["model"] == "gpt-5.6-sol"


async def test_no_model_override_leaves_the_preset_pin_alone(app) -> None:
    from unread.bot.confirm import RunOptions
    from unread.bot.handlers import youtube as yt
    from unread.config import get_settings

    get_settings().ai.chat_model = ""
    analyze = AsyncMock()
    with (
        patch("unread.youtube.commands.cmd_analyze_youtube", new=analyze),
        patch("unread.bot.reply.send_youtube_report", new=AsyncMock()),
    ):
        await yt.execute(
            _Event(), {"url": "https://youtu.be/abc12345678"}, RunOptions(), app=app, progress_msg=None
        )
    assert analyze.call_args.kwargs["model"] is None


async def test_switching_provider_also_switches_to_a_valid_model(app) -> None:
    """Presets pin OpenAI ids. Switching to Anthropic without changing the
    model sent `gpt-5.6-luna` to Anthropic, 4xx-ing every later run."""
    from unread.bot.settings_menu import encode_settings_callback
    from unread.config import get_settings

    class _Cb:
        data = encode_settings_callback("S_PROV", 5, "anthropic")
        sender_id = 111
        chat_id = 7

        async def answer(self, *_a, **_kw):
            return None

        async def edit(self, *_a, **_kw):
            return None

    await app._handle_callback(_Cb())
    s = get_settings()
    assert s.ai.chat_provider == "anthropic"
    assert s.ai.chat_model, "must pin a model valid for the new provider"
    assert "gpt-" not in s.ai.chat_model.lower()


# --- authz on the settings menu ----------------------------------------------


async def test_extra_admin_cannot_change_provider(app) -> None:
    """Provider is bot-wide and spends the owner's budget — the same
    reasoning that gates the key button."""
    from unread.bot.settings_menu import encode_settings_callback
    from unread.config import get_settings

    app.allowed_ids.add(999)
    before = get_settings().ai.chat_provider

    class _Cb:
        data = encode_settings_callback("S_PROV", 5, "anthropic")
        sender_id = 999
        chat_id = 7

        def __init__(self):
            self.answers = []

        async def answer(self, text: str = "", **_kw):
            self.answers.append(text)

        async def edit(self, *_a, **_kw):
            return None

    cb = _Cb()
    await app._handle_callback(cb)
    assert get_settings().ai.chat_provider == before
    assert cb.answers


# --- /stop must cover every run path -----------------------------------------


def test_every_semaphore_gated_path_registers_for_stop() -> None:
    """`/stop` was wired into ONE of the run paths, so it answered
    "Nothing is running" for a burst, a combined run, a dump, a forward
    action, and `/confirm off` — while the run kept spending."""
    import re
    from pathlib import Path

    src = Path("unread/bot/app.py").read_text()
    # Every `async with self._semaphore:` block must register the task.
    blocks = src.split("async with self._semaphore:")[1:]
    missing = [i for i, b in enumerate(blocks) if "register_running" not in b[:600]]
    assert not missing, f"{len(missing)} semaphore block(s) don't register for /stop"
    assert len(blocks) >= 4, "expected several gated paths"
    assert not re.search(r"register_running\(\s*\)", src)


async def test_done_callback_only_clears_its_own_entry(app) -> None:
    """A cancelled task's late done-callback popped by chat_id and deleted
    the entry of the run started right after it."""
    import asyncio

    async def _sleep():
        await asyncio.sleep(60)

    first = asyncio.create_task(_sleep())
    app.register_running(7, first)
    first.cancel()
    second = asyncio.create_task(_sleep())
    app.register_running(7, second)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert app._running.get(7) is second, "the new run's entry was clobbered"
    second.cancel()


# --- progress must not end stale ---------------------------------------------


def test_progress_path_flushes_the_final_line() -> None:
    """The throttle silently dropped the last `k/N` and the "Merging…"
    line, freezing the message mid-run — the exact case `flush` exists
    for."""
    from pathlib import Path

    src = Path("unread/bot/handlers/youtube.py").read_text()
    assert "live.flush" in src, "the progress callback never flushes"


# --- key rotation must not silently revert on restart ------------------------


async def test_key_rotation_warns_when_env_will_win(app, monkeypatch) -> None:
    """`load_settings` fills a stored secret only into fields the env left
    empty. Rotating a key that's also in `.env.bot` works until the next
    restart, then silently reverts to the stale one and 401s."""
    from unread.bot.handlers import cmds

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-file")
    state = app._chat_state.setdefault(7, {})
    state["pending_api_key"] = "openai"
    state["pending_api_key_at"] = time.time()

    event = _Event(text="sk-proj-newkey1234567890abc")
    await cmds.maybe_consume_api_key(event, app=app)
    joined = " ".join(event.replies).lower()
    assert "env" in joined or "restart" in joined, joined


async def test_no_warning_when_env_is_not_set(app, monkeypatch) -> None:
    from unread.bot.handlers import cmds

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = app._chat_state.setdefault(7, {})
    state["pending_api_key"] = "openai"
    state["pending_api_key_at"] = time.time()

    event = _Event(text="sk-proj-newkey1234567890abc")
    await cmds.maybe_consume_api_key(event, app=app)
    joined = " ".join(event.replies).lower()
    assert "restart" not in joined
