"""Durable mirror of the bot's per-chat sticky settings.

`BotApp._chat_state` is the hot path: every handler reads it synchronously
while building a run's options. It also holds ephemeral things — the burst
accumulator, open confirm panels, a pending `/upload_session` — that have
no business surviving a restart.

This module is the narrow bridge between that dict and
`data.sqlite::bot_chat_settings`: `load_all` at startup, write-through on
every slash command that changes a sticky value. Only the allowlisted
keys in :data:`unread.db._keys.BOT_CHAT_SETTING_KEYS` cross over; the
ephemeral keys never touch the DB.

Why it exists: a multi-admin bot means each admin has their own language
(`/lang ru` for one, `/lang en` for another). Without persistence, every
`docker compose up` silently reset all of them back to the config default,
and the reset is invisible until someone notices a report in the wrong
language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from unread.bot.runtime import (
    STICKY_CONFIRM_DISABLED,
    STICKY_ENRICH_EXTRAS,
    STICKY_PRESET,
    STICKY_REPORT_LANGUAGE,
    STICKY_TG_WINDOW,
)
from unread.db.repo import open_repo

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)

# Keys whose in-memory form isn't a plain string. Everything else stores
# and restores verbatim.
_SET_KEYS = frozenset({STICKY_ENRICH_EXTRAS})
_BOOL_KEYS = frozenset({STICKY_CONFIRM_DISABLED})

# Persisted keys, in the order `/settings` shows them. Also the guard
# against persisting an ephemeral key by accident.
PERSISTED_KEYS: tuple[str, ...] = (
    STICKY_PRESET,
    STICKY_REPORT_LANGUAGE,
    STICKY_ENRICH_EXTRAS,
    STICKY_TG_WINDOW,
    STICKY_CONFIRM_DISABLED,
)


def _encode(key: str, value: Any) -> str:
    """In-memory value → the TEXT column."""
    if key in _SET_KEYS:
        return ",".join(sorted(value or ()))
    if key in _BOOL_KEYS:
        return "1" if value else "0"
    return str(value or "")


def _decode(key: str, raw: str) -> Any:
    """TEXT column → the in-memory value.

    Type fidelity matters: `resolve_options` does set-membership on
    `enrich_extras`, and `_handle` branches on the truthiness of
    `confirm_disabled` — where the string "0" would read as True and
    silently disable the confirm panel.
    """
    if key in _SET_KEYS:
        return {part for part in (raw or "").split(",") if part}
    if key in _BOOL_KEYS:
        return raw == "1"
    return raw


async def load_all(app: BotApp) -> None:
    """Restore every chat's sticky settings into `app._chat_state`.

    Merges into the existing per-chat dict rather than replacing it, so a
    chat that already accumulated runtime state keeps it. Failure is
    logged and swallowed: a bot that can't read its preferences should
    still start and serve requests with config defaults.
    """
    try:
        async with open_repo(app.settings.storage.data_path) as repo:
            stored = await repo.get_all_bot_chat_settings()
    except Exception:
        log.exception("bot.prefs.load_failed")
        return

    for chat_id, values in stored.items():
        chat_state = app._chat_state.setdefault(chat_id, {})
        for key, raw in values.items():
            chat_state[key] = _decode(key, raw)
    if stored:
        log.info("bot.prefs.loaded", chats=len(stored))


async def set_sticky(app: BotApp, *, chat_id: int, key: str, value: Any) -> None:
    """Set one sticky value in memory and persist it.

    Memory is updated first and unconditionally — a DB hiccup must not
    make the command look like it did nothing for the rest of the
    session.
    """
    if key not in PERSISTED_KEYS:
        raise ValueError(f"not a persisted sticky key: {key!r}")
    app._chat_state.setdefault(chat_id, {})[key] = value
    try:
        async with open_repo(app.settings.storage.data_path) as repo:
            await repo.put_bot_chat_setting(chat_id=chat_id, key=key, value=_encode(key, value))
    except Exception:
        log.exception("bot.prefs.save_failed", chat_id=chat_id, key=key)


async def clear_sticky(app: BotApp, *, chat_id: int, key: str) -> None:
    """Drop one sticky value from memory and from the DB."""
    if key not in PERSISTED_KEYS:
        raise ValueError(f"not a persisted sticky key: {key!r}")
    app._chat_state.setdefault(chat_id, {}).pop(key, None)
    try:
        async with open_repo(app.settings.storage.data_path) as repo:
            await repo.delete_bot_chat_setting(chat_id=chat_id, key=key)
    except Exception:
        log.exception("bot.prefs.delete_failed", chat_id=chat_id, key=key)
