"""Trivial slash commands (`/start`, `/help`, `/ping`, `/preset`, `/cancel`).

These never call the analyze pipeline, so they bypass the worker
semaphore and reply immediately.
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import TYPE_CHECKING

import structlog
from telethon import events

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from unread.bot.app import BotApp


_SLASH_COMMANDS = """\
Slash commands:
`/help` — this message
`/ping` — health check
`/settings` — show current sticky + default settings for this chat
`/preset <name>` — sticky preset (e.g. `/preset digest`); bare `/preset` clears
`/lang <code>` — your language for analyses, report headings and transcripts (e.g. `/lang en`); bare clears
`/enrich <list|all|none>` — sticky extra enrichments for TG chats (e.g. `image,link`)
`/window <day|week|month|msg|from_msg|none>` — sticky default TG window
`/format <pdf|md|rich>` — how reports come back:
   • `pdf` — rendered document, best on phones (default)
   • `md` — the raw Markdown file
   • `rich` — the report in the chat itself, tables and all, nothing to download
   bare `/format` restores the default
`/confirm on|off` — toggle the pre-run confirm panel (default: on)
`/upload_session` — install your Telegram user session (one-time)
`/stop` — cancel the run currently in progress in this chat
`/cancel` — drop any pending `/upload_session`
"""

# Telethon's default markdown parser is MarkdownV1-ish: **double**
# asterisks for bold, `backticks` for inline code. Single asterisks
# render literally — don't use them.

_HELP_TEXT_FULL = """\
**unread bot** — send me one of:
• a file (PDF, audio, video, text, code, …)
• a web URL → I'll summarize the page
• a YouTube URL → I'll summarize the transcript
• a forwarded Telegram message → I'll analyze its contents
• a `t.me/<chat>/<msg>` link → I'll pull the chat and analyze

"""

_HELP_TEXT_NO_SESSION = """\
**unread bot** — send me one of:
• a file (PDF, audio, video, text, code, …)
• a web URL → I'll summarize the page
• a YouTube URL → I'll summarize the transcript

⚠️ **No Telegram user session installed**, so I can't read your private chats. \
Forwarded messages, `t.me/<chat>/<msg>` links, and `@channel` refs won't work \
until you run `/upload_session` and send me your `session.sqlite` file.

"""


def _build_help_text(app: BotApp) -> str:
    base = _HELP_TEXT_FULL if app.user_session_ready else _HELP_TEXT_NO_SESSION
    return base + _SLASH_COMMANDS


# How long a 🔑 prompt stays armed. Long enough to fetch a key from a
# password manager, short enough that a forgotten prompt doesn't swallow
# tomorrow's first message.
_API_KEY_PROMPT_TTL_SECONDS = 300.0

# Environment variable that outranks each stored secret at load time.
# Mirrors the overlay in `config.load_settings`.
_ENV_NAME_FOR_SECRET: dict[str, str] = {
    "openai.api_key": "OPENAI_API_KEY",
    "openrouter.api_key": "OPENROUTER_API_KEY",
    "anthropic.api_key": "ANTHROPIC_API_KEY",
    "google.api_key": "GOOGLE_API_KEY",
}


def _looks_like_api_key(raw: str) -> bool:
    """Cheap sanity check on a pasted secret.

    Deliberately permissive about FORMAT (providers differ, and new ones
    appear) but strict about the shapes that are obviously not keys: a
    URL, whitespace in the middle, or something far too short. The cost
    of a false accept is a destroyed credential.
    """
    if len(raw) < 16 or len(raw) > 400:
        return False
    if any(ch.isspace() for ch in raw):
        return False
    lowered = raw.lower()
    return not lowered.startswith(("http://", "https://", "t.me/", "www."))


async def maybe_consume_api_key(event: events.NewMessage.Event, *, app: BotApp) -> bool:
    """Consume this message as an API key when the key flow is armed.

    Returns True when it swallowed the message. The message is deleted as
    soon as the key is stored: it still crossed Telegram's servers on the
    way here, which is unavoidable, but it must not sit in the chat's
    history afterwards. The confirmation shows only a masked tail —
    echoing the key would defeat the deletion.
    """
    chat_state = app._chat_state.get(event.chat_id) or {}
    provider = chat_state.get("pending_api_key")
    if not provider:
        return False

    # The prompt expires. Without a TTL, tapping 🔑 and then getting
    # distracted meant the next ordinary message — a pasted link, say —
    # was stored AS THE API KEY, silently clobbering a working
    # credential and 401-ing every later run.
    armed_at = float(chat_state.get("pending_api_key_at") or 0.0)
    if armed_at and (time.time() - armed_at) > _API_KEY_PROMPT_TTL_SECONDS:
        chat_state.pop("pending_api_key", None)
        chat_state.pop("pending_api_key_at", None)
        await event.reply(
            "The API-key prompt expired (it lasts "
            f"{int(_API_KEY_PROMPT_TTL_SECONDS // 60)} min). Nothing was stored — "
            "tap 🔑 in /settings again if you still want to set one."
        )
        return False

    from unread.bot.settings_menu import mask_secret, secret_key_for_provider
    from unread.db.repo import open_repo

    chat_state.pop("pending_api_key", None)
    chat_state.pop("pending_api_key_at", None)
    raw = (getattr(event, "raw_text", None) or getattr(event, "text", "") or "").strip()

    # Delete FIRST: if storing fails we still don't want the key sitting
    # in the history while somebody debugs.
    with contextlib.suppress(Exception):
        await event.message.delete()

    if not raw or raw.startswith("/"):
        await event.reply("Cancelled — no key stored.")
        return True

    if not _looks_like_api_key(raw):
        # Deleted above, so the stray message is gone either way — but a
        # URL or a sentence is not a key, and storing it would destroy a
        # working credential.
        await event.reply(
            "That doesn't look like an API key, so nothing was stored. Tap 🔑 in /settings to try again."
        )
        return True

    key_name = secret_key_for_provider(provider)
    if not key_name:
        await event.reply(f"`{provider}` takes no API key.", parse_mode="md")
        return True

    try:
        async with open_repo(app.settings.storage.data_path) as repo:
            await repo.put_secrets({key_name: raw})
    except Exception:
        log.exception("bot.settings.key_store_failed", provider=provider)
        await event.reply("⚠️ Couldn't store the key; see the bot logs.")
        return True

    # Apply to the live settings so the next run uses it without a restart.
    from unread.config import get_settings

    section, _, field = key_name.partition(".")
    live = get_settings()
    targets = [app.settings] if app.settings is live else [app.settings, live]
    for target in targets:
        with contextlib.suppress(Exception):
            setattr(getattr(target, section), field, raw)

    # A stored secret only fills fields the higher layers left EMPTY, so
    # if the same key is set in the environment the rotation works now and
    # silently reverts on the next restart. Say so rather than let it
    # 401 later.
    env_name = _ENV_NAME_FOR_SECRET.get(key_name, "")
    warning = ""
    if env_name and os.environ.get(env_name):
        warning = (
            f"\n\n⚠️ `{env_name}` is also set in the environment and outranks "
            "this on startup — the bot will revert to it after a restart. "
            "Update `.env.bot` too, or remove it there."
        )

    await event.reply(
        f"🔑 Stored the **{provider}** key (`{mask_secret(raw)}`) and deleted your message." + warning,
        parse_mode="md",
    )
    return True


async def handle(
    event: events.NewMessage.Event,
    payload: dict,
    *,
    app: BotApp,
) -> None:
    cmd = payload.get("name", "")
    args = payload.get("args", [])

    if cmd in ("start", "help"):
        await event.reply(_build_help_text(app), parse_mode="md")
        return

    if cmd == "stop":
        if app.stop_running(event.chat_id):
            await event.reply("🛑 Stopped the run in progress.")
        else:
            await event.reply("Nothing is running in this chat right now.")
        return

    if cmd == "ping":
        await event.reply("pong")
        return

    if cmd == "preset":
        from unread.bot import prefs
        from unread.bot.runtime import STICKY_PRESET

        if not args:
            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_PRESET)
            await event.reply("Sticky preset cleared. Falling back to the default.")
        else:
            preset = args[0].strip()
            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_PRESET, value=preset)
            await event.reply(f"Sticky preset → `{preset}` (kept across restarts until you clear it).")
        return

    if cmd == "confirm":
        chat_state = app._chat_state.setdefault(event.chat_id, {})
        if not args:
            state = "off" if chat_state.get("confirm_disabled") else "on"
            await event.reply(
                f"Pre-run confirm panel is currently `{state}`. Use `/confirm on|off` to change.",
                parse_mode="md",
            )
            return
        choice = args[0].strip().lower()
        if choice == "off":
            from unread.bot import prefs
            from unread.bot.runtime import STICKY_CONFIRM_DISABLED

            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_CONFIRM_DISABLED, value=True)
            await event.reply(
                "Pre-run confirm panel disabled. Messages will run immediately with sticky defaults."
            )
        elif choice == "on":
            from unread.bot import prefs
            from unread.bot.runtime import STICKY_CONFIRM_DISABLED

            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_CONFIRM_DISABLED)
            await event.reply(
                "Pre-run confirm panel re-enabled. Each message will get a ▶ Run / ⚙ Change / ✖ Cancel panel."
            )
        else:
            await event.reply("Usage: `/confirm on` or `/confirm off`.", parse_mode="md")
        return

    if cmd == "format":
        from unread.bot import prefs
        from unread.bot.runtime import STICKY_REPORT_FORMAT, parse_format_value

        value, msg = parse_format_value(args[0] if args else "")
        if value is None:
            await event.reply(msg, parse_mode="md")
            return
        if value:
            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_REPORT_FORMAT, value=value)
        else:
            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_REPORT_FORMAT)
        await event.reply(msg)
        return

    if cmd == "lang":
        from unread.bot import prefs
        from unread.bot.runtime import STICKY_REPORT_LANGUAGE, parse_lang_value

        arg = args[0] if args else ""
        value, msg = parse_lang_value(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_REPORT_LANGUAGE, value=value)
        else:
            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_REPORT_LANGUAGE)
        await event.reply(msg)
        return

    if cmd == "enrich":
        from unread.bot import prefs
        from unread.bot.runtime import STICKY_ENRICH_EXTRAS, parse_enrich_list

        arg = " ".join(args) if args else ""
        value, msg = parse_enrich_list(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_ENRICH_EXTRAS, value=value)
        else:
            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_ENRICH_EXTRAS)
        await event.reply(msg)
        return

    if cmd == "window":
        from unread.bot import prefs
        from unread.bot.runtime import STICKY_TG_WINDOW, parse_window_value

        arg = args[0] if args else ""
        value, msg = parse_window_value(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            await prefs.set_sticky(app, chat_id=event.chat_id, key=STICKY_TG_WINDOW, value=value)
        else:
            await prefs.clear_sticky(app, chat_id=event.chat_id, key=STICKY_TG_WINDOW)
        await event.reply(msg)
        return

    if cmd == "settings":
        from unread.bot.settings_menu import build_settings_menu
        from unread.config import get_settings

        chat_state = app._chat_state.get(event.chat_id) or {}
        # Sent with a placeholder id, then edited with the real one — the
        # buttons must exist before Telethon hands back the message id.
        # Same two-step as the confirm panel.
        text, buttons = build_settings_menu(chat_state=chat_state, settings=get_settings(), panel_msg_id=0)
        sent = await event.reply(text, buttons=buttons, parse_mode="md")
        panel_id = getattr(sent, "id", 0) or 0
        if panel_id:
            text, buttons = build_settings_menu(
                chat_state=chat_state, settings=get_settings(), panel_msg_id=panel_id
            )
            with contextlib.suppress(Exception):
                await sent.edit(text, buttons=buttons, parse_mode="md")
        return

    if cmd == "cancel":
        chat_state = app._chat_state.setdefault(event.chat_id, {})
        had_pending = chat_state.pop("pending_session_upload", False)
        if had_pending:
            await event.reply("Session-upload cancelled.")
        else:
            await event.reply("Nothing to cancel.")
        return

    if cmd == "upload_session":
        # Replacing the session swaps the account every t.me analysis
        # reads from — primary owner only, never an extra admin.
        if not app.is_primary_owner(event.sender_id):
            await event.reply("🔒 Only the session owner can replace the Telegram session.")
            return

        from unread.bot import session_upload

        await session_upload.start_upload(event, app=app)
        return

    await event.reply(f"Unknown command: /{cmd}. Try /help.")
