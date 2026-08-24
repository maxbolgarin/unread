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
import time
from typing import Any


def run_status_line(*, preset: str, settings: Any, source: str, kind: str = "") -> str:
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

    # `_effective_preset` returns "" when the chat has no sticky `/preset`
    # and no `bot.default_preset`; the analyze command then falls back to
    # its kind's default. Mirror that, or the line shows an empty backtick
    # pair and resolves the wrong model alongside it.
    if not preset and kind:
        from unread.bot.confirm import default_preset_for_kind

        preset = default_preset_for_kind(kind)

    try:
        preset_obj = get_presets(_report_lang(settings)).get(preset)
    except Exception:  # preset dir missing / unreadable
        preset_obj = None

    searching = bool(getattr(preset_obj, "needs_web_search", False))
    verb = "🔎 Fact-checking" if searching else "⏳ Analyzing"

    # Mirror `run_analysis`'s precedence exactly:
    #   final_model = model_override or preset.final_model or config default
    # A preset's pin WINS over `ai.chat_model`, and every shipped preset
    # pins one. Showing `resolve_chat_model` advertised the provider-
    # routed id (`openai/gpt-5.6-luna`) while the run actually sends the
    # preset's bare `gpt-5.6-luna` — a progress line naming the wrong
    # model is worse than one naming none.
    # Precedence, mirroring `run_analysis`:
    #   model_override or preset.final_model or config default
    # The bot passes `ai.chat_model` as the override, so it comes FIRST —
    # otherwise the line names the preset's pin while the run uses the
    # override, which is the same wrong-model bug in a new place.
    model = getattr(settings.ai, "chat_model", "") or ""
    if not model:
        model = getattr(preset_obj, "final_model", "") or ""
    if not model:
        try:
            model = resolve_chat_model(settings)
        except Exception:  # unconfigured provider
            model = "?"
    # "openai", not "?": both keys are empty on a fresh install, but the
    # resolver's own fallback is openai. Rendering "?" reads as breakage.
    provider = getattr(settings.ai, "chat_provider", "") or getattr(settings.ai, "provider", "") or "openai"

    bits = [f"{verb} {source}…", f"preset `{preset}`", f"`{provider}`/`{model}`"]
    if searching:
        bits.append("web search on (billed per search)")
    return " · ".join(bits)


def _report_lang(settings: Any) -> str:
    locale = getattr(settings, "locale", None)
    return (getattr(locale, "report_language", "") or getattr(locale, "language", "") or "en").lower()


class LiveProgress:
    """Throttled progress-message editor for one in-flight bot run.

    The pipeline emits an update per finished chunk; Telegram rate-limits
    edits, so a 30-chunk run firing 30 edits in a burst earns the bot a
    flood wait. This lets the first update through immediately (waiting
    out the window before the FIRST edit leaves the user staring at a
    stale line) and then at most one per `min_interval`.

    Every edit is best-effort: progress is decoration and must never kill
    the run it describes.
    """

    def __init__(self, msg: Any, *, min_interval: float = 3.0) -> None:
        self._msg = msg
        self._min_interval = min_interval
        self._last_at = 0.0
        self._last_text = ""

    async def __call__(self, text: str) -> None:
        now = time.monotonic()
        throttled = self._last_at and (now - self._last_at) < self._min_interval
        # A PHASE change always goes through. Per-chunk ticks are noise
        # worth throttling, but "Analyzing 20 chunks… 14/20" → "Merging
        # 20 fragments…" is the difference between a live message and one
        # frozen at 14/20 for the whole reduce phase.
        if throttled and self._same_phase(text):
            return
        await self._write(text, now)

    def _same_phase(self, text: str) -> bool:
        """True when `text` continues the phase already displayed.

        Compared on the last line's leading word, which is what the
        pipeline varies between phases ("Analyzing" / "Merging").
        """

        def _phase(value: str) -> str:
            last = (value or "").strip().split("\n")[-1].strip()
            return last.split(" ")[0] if last else ""

        return _phase(text) == _phase(self._last_text)

    async def flush(self, text: str) -> None:
        """Write unconditionally — for the final line of a run.

        Without this the message can end on a stale "2/3" because the
        last update landed inside the throttle window.
        """
        await self._write(text, time.monotonic())

    async def _write(self, text: str, now: float) -> None:
        if self._msg is None or text == self._last_text:
            # Telegram rejects a no-op edit with MESSAGE_NOT_MODIFIED;
            # don't spend a request to find that out.
            return
        self._last_at = now
        self._last_text = text
        with contextlib.suppress(Exception):
            await self._msg.edit(text, buttons=None)


async def edit_progress(msg: Any, text: str) -> None:
    """Edit `msg` to `text` with buttons cleared. Silently no-op on failure."""
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.edit(text, buttons=None)
