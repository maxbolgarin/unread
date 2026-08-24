"""Bot progress-edit helper.

Every status edit goes through `edit_progress` so:

1. The inline keyboard is *always* cleared (`buttons=None`). Otherwise
   Telegram's MTProto edit can leave stale buttons attached to a
   message whose new text says "⏳ Pulling messages…" — tapping them
   then does nothing useful but confuses the user.
2. `MESSAGE_NOT_MODIFIED` and transient network errors don't tear
   down the request. Status updates are best-effort by definition.

Use everywhere instead of bare `await msg.edit(text)`.
"""

from __future__ import annotations

import contextlib
from typing import Any


def run_status_line(*, preset: str, settings: Any, source: str) -> str:
    """The "what's happening right now" line shown while a run is in flight.

    Carries the three facts worth knowing while you wait: what kind of run
    it is, which model is spending the money, and whether it will also be
    billed per web search. The final caption reports actual tokens and
    cost; this is the part you can act on BEFORE it finishes.

    A fact-check is not an analysis, and saying "Analyzing video…" for one
    is just wrong.
    """
    from unread.ai.providers import resolve_chat_model
    from unread.analyzer.prompts import get_presets

    try:
        preset_obj = get_presets(_report_lang(settings)).get(preset)
    except Exception:  # preset dir missing / unreadable
        preset_obj = None

    searching = bool(getattr(preset_obj, "needs_web_search", False))
    verb = "🔎 Fact-checking" if searching else "⏳ Analyzing"

    try:
        model = resolve_chat_model(settings)
    except Exception:  # unconfigured provider
        model = "?"
    provider = getattr(settings.ai, "chat_provider", "") or getattr(settings.ai, "provider", "") or "?"

    bits = [f"{verb} {source}…", f"preset `{preset}`", f"`{provider}`/`{model}`"]
    if searching:
        bits.append("web search on (billed per search)")
    return " · ".join(bits)


def _report_lang(settings: Any) -> str:
    locale = getattr(settings, "locale", None)
    return (getattr(locale, "report_language", "") or getattr(locale, "language", "") or "en").lower()


async def edit_progress(msg: Any, text: str) -> None:
    """Edit `msg` to `text` with buttons cleared. Silently no-op on failure."""
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.edit(text, buttons=None)
