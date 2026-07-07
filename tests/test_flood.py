"""Cover the final-attempt failure path in `unread.util.flood.retry_on_flood`.

The decorator retries `FloodWaitError` up to `max_retries` times. The
final attempt (after the loop) used to bubble the raw Telethon
exception, which crashed `unread chats run` with a confusing traceback
that bypassed the per-subscription error handler in runner.py. The fix
converts a final-attempt FloodWait into a friendly RuntimeError so the
runner can attribute the error to the offending chat instead.
"""

from __future__ import annotations

import pytest

from unread.util.flood import retry_on_flood


class _FakeFloodWait(Exception):
    """Stand-in for telethon's `FloodWaitError`.

    The decorator imports `FloodWaitError` lazily at call time so we
    monkeypatch the module attribute below to point at this class.
    """

    def __init__(self, seconds: int) -> None:
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds


@pytest.fixture(autouse=True)
def _patch_flood_error(monkeypatch):
    """Replace telethon's FloodWaitError with our fake.

    The decorator does `from telethon.errors.rpcerrorlist import FloodWaitError`
    inside its inner. We patch the rpcerrorlist module so the fake matches
    the `except` clause shape. If telethon isn't installed in the test env,
    we register a synthetic module so the import succeeds.
    """
    import sys
    import types

    pkg_name = "telethon.errors.rpcerrorlist"
    if pkg_name not in sys.modules:
        # Build the minimum chain of stub modules.
        for parent in ("telethon", "telethon.errors"):
            if parent not in sys.modules:
                sys.modules[parent] = types.ModuleType(parent)
        mod = types.ModuleType(pkg_name)
        sys.modules[pkg_name] = mod
    sys.modules[pkg_name].FloodWaitError = _FakeFloodWait
    yield


async def test_final_attempt_converts_to_runtime_error(monkeypatch):
    """When every retry hits FloodWait, the final attempt's FloodWait
    becomes a RuntimeError with a "rate-limited" message — not a raw
    telethon exception that bubbles to the user as a stacktrace.
    """
    # Stub asyncio.sleep so the decorator's between-retry backoff
    # (seconds=42 → ~43s * max_retries) doesn't slow the test.
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    @retry_on_flood(max_retries=2)
    async def always_floods():
        raise _FakeFloodWait(seconds=42)

    with pytest.raises(RuntimeError, match="rate-limited"):
        await always_floods()


async def test_success_inside_retry_window_returns_value(monkeypatch):
    """Sanity: if the function succeeds before max_retries is exhausted,
    the decorator returns the value and never sleeps further.
    """
    calls = {"n": 0}

    @retry_on_flood(max_retries=3)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeFloodWait(seconds=0)  # 0s sleep keeps test fast
        return "ok"

    # Stub asyncio.sleep so the 0s "+1" delay still doesn't slow the test.
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)
    assert await flaky() == "ok"
    assert calls["n"] == 2


async def test_non_flood_exception_propagates(monkeypatch):
    """The decorator must not swallow exceptions other than FloodWait.

    A `ValueError` from the wrapped function escapes immediately so the
    caller sees the actual bug, not a retry storm.
    """

    @retry_on_flood(max_retries=5)
    async def explodes():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await explodes()


async def test_retry_on_flood_makes_exactly_max_retries_attempts(monkeypatch):
    """On permanent failure, the wrapped fn is called exactly `max_retries`
    times — not `max_retries + 1` (the old off-by-one that made an extra
    unguarded call after the loop)."""
    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    @retry_on_flood(max_retries=3)
    async def always_floods():
        calls["n"] += 1
        raise _FakeFloodWait(seconds=1)

    with pytest.raises(RuntimeError, match="rate-limited"):
        await always_floods()
    assert calls["n"] == 3


# ---------- retry_on_429 --------------------------------------------------


class _FakeRateLimitError(Exception):
    """Stand-in for openai.RateLimitError."""


class _FakeAPITimeoutError(Exception):
    """Stand-in for openai.APITimeoutError."""


class _FakeAPIStatusError(Exception):
    """Stand-in for openai.APIStatusError; carries a status_code like the real one."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


_FakeRateLimitError.__name__ = "RateLimitError"
_FakeAPITimeoutError.__name__ = "APITimeoutError"
_FakeAPIStatusError.__name__ = "APIStatusError"


@pytest.fixture(autouse=True)
def _patch_openai_errors(monkeypatch):
    """Register a fake `openai` module so `retry_on_429`'s lazy
    `from openai import ...` import resolves to our fakes instead of
    requiring the real SDK / real exception hierarchy in tests."""
    import sys
    import types

    mod = sys.modules.get("openai")
    if mod is None:
        mod = types.ModuleType("openai")
        sys.modules["openai"] = mod
    mod.RateLimitError = _FakeRateLimitError
    mod.APITimeoutError = _FakeAPITimeoutError
    mod.APIStatusError = _FakeAPIStatusError
    yield


async def test_retry_on_429_makes_exactly_max_retries_attempts(monkeypatch):
    """On permanent rate-limit failure, exactly `max_retries` calls are made."""
    from unread.util.flood import retry_on_429

    monkeypatch.setattr("unread.util.flood.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    @retry_on_429(max_retries=3)
    async def always_rate_limited():
        calls["n"] += 1
        raise _FakeRateLimitError("rate limited")

    with pytest.raises(_FakeRateLimitError):
        await always_rate_limited()
    assert calls["n"] == 3


async def test_retry_on_429_reraises_original_exception_type_on_exhaustion(monkeypatch):
    """Exhaustion must re-raise the ORIGINAL SDK exception type — not a
    wrapped/friendly error — since provider-error handling and
    `tests/test_provider_auth_error_friendly.py` dispatch on the concrete
    exception class."""
    from unread.util.flood import retry_on_429

    monkeypatch.setattr("unread.util.flood.asyncio.sleep", _no_sleep)

    @retry_on_429(max_retries=2)
    async def always_times_out():
        raise _FakeAPITimeoutError("timed out")

    with pytest.raises(_FakeAPITimeoutError):
        await always_times_out()


async def test_retry_on_429_logs_exhausted_event(monkeypatch):
    """A structured `openai.retry.exhausted` log fires on the final failure."""
    from unread.util.flood import retry_on_429

    monkeypatch.setattr("unread.util.flood.asyncio.sleep", _no_sleep)

    captured: list[tuple[str, dict]] = []

    class _FakeLog:
        def warning(self, event: str, **kw) -> None:
            captured.append((event, kw))

        def error(self, event: str, **kw) -> None:
            captured.append((event, kw))

    monkeypatch.setattr("unread.util.flood.log", _FakeLog())

    @retry_on_429(max_retries=2)
    async def always_rate_limited():
        raise _FakeRateLimitError("rate limited")

    with pytest.raises(_FakeRateLimitError):
        await always_rate_limited()

    exhausted = [c for c in captured if c[0] == "openai.retry.exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0][1]["attempts"] == 2


async def test_retry_on_429_success_inside_retry_window(monkeypatch):
    """Sanity: a transient failure that clears before exhaustion still
    returns the value normally."""
    from unread.util.flood import retry_on_429

    monkeypatch.setattr("unread.util.flood.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    @retry_on_429(max_retries=3)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _FakeRateLimitError("rate limited")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 2


async def _no_sleep(_seconds):
    """Drop-in for asyncio.sleep that returns immediately."""
    return None
