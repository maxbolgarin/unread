"""Retry helpers for Telegram FloodWaitError and OpenAI 429s."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import random
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from unread.util.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


# Installed by a non-terminal consumer (the bot) that wants these
# notices too. A ContextVar rather than a module global because bot
# requests run concurrently under one process — a plain global would
# deliver one chat's "rate limited" line into another chat's message.
_status_sink: ContextVar[Callable[[str], None] | None] = ContextVar("unread_status_sink", default=None)


@contextmanager
def status_sink(fn: Callable[[str], None]) -> Iterator[None]:
    """Route retry/status notices to `fn` for the duration of the block."""
    token = _status_sink.set(fn)
    try:
        yield
    finally:
        _status_sink.reset(token)


def _user_visible_retry_status(message: str) -> None:
    """Surface a one-line retry status, to a sink or to the terminal.

    A long run that hits a 429 / FloodWait used to look frozen — the
    log line went to disk but never reached stdout. This emits a single
    yellow line via Rich when stderr is a TTY; in a non-interactive run
    (CI, scripted) we stay silent and rely on the structured log.

    When a `status_sink` is installed it takes precedence — that's how
    the bot turns "retrying in 13s" into a message edit instead of
    writing to a terminal nobody is watching.
    """
    sink = _status_sink.get()
    if sink is not None:
        # Display is best-effort; never let a UI hiccup change retry
        # semantics. Same contract as the terminal path below.
        with contextlib.suppress(Exception):
            sink(message)
        return
    try:
        import sys as _sys

        from rich.console import Console as _Console

        if not _sys.stderr.isatty():
            return
        _Console(stderr=True).print(f"[yellow]{message}[/]")
    except Exception:
        # Display is best-effort; never let a UI hiccup change retry semantics.
        pass


# Cap for any single FloodWait sleep. Telegram occasionally returns
# 24h+ FloodWait values (banned account, channel-level limit). Without
# a cap, the runner blocks silently for hours. Surface a RuntimeError
# instead so the per-subscription handler in runner.py can move on to
# the next chat.
_MAX_FLOOD_WAIT_SEC = 600


def retry_on_flood(
    max_retries: int = 10,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator that catches Telethon FloodWaitError and sleeps the requested time + 1s.

    Other exceptions propagate immediately. Users see a one-line
    "FloodWait — sleeping {n}s" status on each retry so a 30-second
    pause doesn't look like a frozen process. Sleeps over
    `_MAX_FLOOD_WAIT_SEC` are converted to a RuntimeError so the runner
    can move to the next chat instead of blocking for hours.
    """

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            from telethon.errors.rpcerrorlist import FloodWaitError  # type: ignore[attr-defined]

            for attempt in range(max_retries):
                try:
                    return await fn(*args, **kwargs)
                except FloodWaitError as e:
                    seconds = int(getattr(e, "seconds", 1))
                    if seconds > _MAX_FLOOD_WAIT_SEC:
                        log.error(
                            "tg.flood_wait.too_long",
                            seconds=seconds,
                            cap=_MAX_FLOOD_WAIT_SEC,
                        )
                        raise RuntimeError(
                            f"Telegram FloodWait of {seconds}s exceeds the {_MAX_FLOOD_WAIT_SEC}s "
                            "cap — try again later"
                        ) from e
                    if attempt == max_retries - 1:
                        # Final attempt exhausted — convert FloodWaitError to a
                        # friendly RuntimeError so per-subscription handlers in
                        # runner.py can report the chat cleanly instead of
                        # letting a raw Telethon exception crash the whole
                        # `unread tg chats run`.
                        log.error("tg.flood_wait.exhausted", seconds=seconds, retries=max_retries)
                        raise RuntimeError(
                            f"Telegram rate-limited for {seconds}s after {max_retries} retries — "
                            "try again later"
                        ) from e
                    delay = seconds + 1
                    log.warning("tg.flood_wait", delay=delay, attempt=attempt + 1)
                    _user_visible_retry_status(
                        f"Telegram FloodWait — sleeping {delay}s (attempt {attempt + 1}/{max_retries})…"
                    )
                    await asyncio.sleep(delay)
            # Unreachable when max_retries >= 1 (the only way callers use this
            # decorator) — the last loop iteration always returns or raises.
            raise RuntimeError("retry_on_flood called with max_retries <= 0")  # pragma: no cover

        return inner

    return wrap


# Upper bound on a provider-supplied wait. A 429 asking for an hour
# should not wedge one request for an hour — better to fail and let the
# caller decide.
_MAX_RETRY_AFTER_SECONDS = 300.0

# "Please try again in 12.953s" / "in 550ms" — the phrasing OpenAI uses in
# the 429 body when the header is absent.
_RETRY_AFTER_TEXT_RE = re.compile(r"try again in\s+([0-9.]+)\s*(ms|s)\b", re.IGNORECASE)

# Header carrying a duration string ("12.953s", "550ms") rather than a
# plain seconds count.
_DURATION_RE = re.compile(r"^\s*([0-9.]+)\s*(ms|s)?\s*$", re.IGNORECASE)


def _coerce_duration(raw: str) -> float | None:
    m = _DURATION_RE.match(raw)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value / 1000.0 if (m.group(2) or "").lower() == "ms" else value


def retry_after_hint(err: Any) -> float | None:
    """Seconds the provider asked us to wait, or None if it didn't say.

    Checked in order: the standard `retry-after` header, OpenAI's
    `x-ratelimit-reset-*` duration headers, then the message body. Without
    this the backoff is blind exponential — answering a "try again in
    12.953s" with 1.2s just burns an attempt, since the limit window
    hasn't moved.
    """
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    try:
        lowered = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:  # header container that isn't dict-like
        lowered = {}

    for key in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = lowered.get(key)
        if raw is None:
            continue
        value = _coerce_duration(raw)
        if value is not None and value > 0:
            return min(value, _MAX_RETRY_AFTER_SECONDS)

    text = getattr(err, "message", None) or str(err)
    m = _RETRY_AFTER_TEXT_RE.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    if m.group(2).lower() == "ms":
        value /= 1000.0
    return min(value, _MAX_RETRY_AFTER_SECONDS) if value > 0 else None


def retry_on_429(
    max_retries: int = 5, base: float = 1.5, cap: float = 30.0
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Exponential-backoff decorator for OpenAI rate limit / transient 5xx.

    On a retry-eligible failure, sleep with jitter and emit a one-line
    "Rate limited — retrying in Ns" status to stderr (TTY only) so the
    user knows the CLI is alive during long sleeps.
    """

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            from openai import (  # type: ignore[import-not-found]
                APIStatusError,
                APITimeoutError,
                RateLimitError,
            )

            retriable = (RateLimitError, APITimeoutError, APIStatusError)
            for attempt in range(max_retries):
                try:
                    return await fn(*args, **kwargs)
                except retriable as e:
                    # Retry 429 (rate limit) and 5xx; re-raise other 4xx.
                    is_rate_limit = isinstance(e, RateLimitError)
                    is_4xx_other = isinstance(e, APIStatusError) and not is_rate_limit and e.status_code < 500
                    if is_4xx_other:
                        raise
                    if attempt == max_retries - 1:
                        # Final attempt exhausted — log then re-raise the
                        # ORIGINAL SDK exception type (not a wrapped error).
                        # Provider-friendly-error handling (see
                        # `analyzer/openai_client._is_auth_error` and
                        # `tests/test_provider_auth_error_friendly.py`)
                        # dispatches on the concrete SDK exception class.
                        log.error(
                            "openai.retry.exhausted",
                            attempts=max_retries,
                            err=type(e).__name__,
                        )
                        raise
                    delay = min(base**attempt, cap) + random.uniform(0, 1)
                    # A provider that tells us how long to wait knows
                    # better than our exponential guess. Answering "try
                    # again in 12.953s" with 1.2s just burns an attempt:
                    # the limit window hasn't moved, so the retry fails
                    # exactly the same way.
                    # Only for an actual rate limit. `x-ratelimit-reset-*`
                    # describes bucket refill and rides along on ordinary
                    # and 5xx responses too, so honouring it for a
                    # transient 500 turned a 1.2s backoff into up to 60s
                    # of dead time.
                    hinted = retry_after_hint(e) if is_rate_limit else None
                    if hinted is not None and hinted > delay:
                        delay = hinted + random.uniform(0, 1)
                    log.warning(
                        "openai.retry",
                        attempt=attempt + 1,
                        delay=round(delay, 2),
                        hinted=round(hinted, 2) if hinted is not None else None,
                        err=type(e).__name__,
                    )
                    label = "Rate limited" if is_rate_limit else type(e).__name__
                    _user_visible_retry_status(
                        f"{label} — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
                    )
                    await asyncio.sleep(delay)
            # Unreachable when max_retries >= 1 (the only way callers use this
            # decorator) — the last loop iteration always returns or raises.
            raise RuntimeError("retry_on_429 called with max_retries <= 0")  # pragma: no cover

        return inner

    return wrap


class RateLimiter:
    """Simple rolling-minute token bucket for Telegram read throttle.

    `acquire` is invoked from multiple coroutines concurrently
    (`asyncio.gather` over chats in `_refresh_chats`, parallel workers
    in `save_raw_media`). Without the lock, two coroutines can each
    rebuild `_hits` and `append` simultaneously — the rebuild loses
    one hit and the bucket lets through more requests than `max`,
    which is exactly the over-acquire that triggers a Telegram flood.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max = max(1, int(max_per_minute))
        self._hits: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            self._hits = [t for t in self._hits if now - t < 60]
            if len(self._hits) >= self._max:
                sleep = 60 - (now - self._hits[0]) + 0.05
                if sleep > 0:
                    await asyncio.sleep(sleep)
            self._hits.append(loop.time())
