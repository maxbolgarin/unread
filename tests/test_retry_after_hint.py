"""429 backoff must honour the wait the provider asks for.

Observed against OpenAI: "Please try again in 12.953s" answered with a
1.23s then a 2.04s backoff. Both retries were guaranteed to fail — the
TPM window hadn't moved — so the run burned attempts and wall-clock for
nothing.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from unread.util.flood import retry_after_hint


class _Resp:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _Err(Exception):
    def __init__(self, message: str = "", headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if headers is not None:
            self.response = _Resp(headers)


def test_reads_the_retry_after_header() -> None:
    assert retry_after_hint(_Err(headers={"retry-after": "13"})) == pytest.approx(13.0)


def test_header_lookup_is_case_insensitive() -> None:
    assert retry_after_hint(_Err(headers={"Retry-After": "7"})) == pytest.approx(7.0)


def test_reads_the_millisecond_header_openai_sends() -> None:
    hint = retry_after_hint(_Err(headers={"x-ratelimit-reset-tokens": "12.953s"}))
    assert hint == pytest.approx(12.953)


def test_falls_back_to_parsing_the_message() -> None:
    """The header isn't always present; the text always is."""
    err = _Err(
        "Error code: 429 - Rate limit reached for gpt-5.6-luna ... "
        "Please try again in 12.953s. Visit https://platform.openai.com/..."
    )
    assert retry_after_hint(err) == pytest.approx(12.953)


def test_parses_a_millisecond_phrasing() -> None:
    assert retry_after_hint(_Err("Please try again in 550ms.")) == pytest.approx(0.55)


def test_no_hint_returns_none() -> None:
    assert retry_after_hint(_Err("Something else went wrong")) is None
    assert retry_after_hint(Exception("plain")) is None


def test_absurd_hints_are_capped() -> None:
    """A provider asking for an hour shouldn't wedge a request that long."""
    hint = retry_after_hint(_Err(headers={"retry-after": "100000"}))
    assert hint is not None
    assert hint <= 300


def test_garbage_header_is_ignored() -> None:
    assert retry_after_hint(_Err(headers={"retry-after": "soon"})) is None


# --- the decorator actually waits that long ----------------------------------


async def test_backoff_waits_at_least_what_the_provider_asked(monkeypatch) -> None:
    """The bug in the field: told 12.953s, slept 1.23s, retried into the
    same closed window."""
    from unread.util import flood

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(flood.asyncio, "sleep", _fake_sleep)

    from openai import RateLimitError

    class _R:
        status_code = 429
        headers: ClassVar[dict] = {"retry-after": "13"}
        request = None

        def json(self):
            return {}

    calls: list[int] = []

    @flood.retry_on_429(max_retries=3)
    async def _flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RateLimitError("rate limited", response=_R(), body=None)
        return "ok"

    assert await _flaky() == "ok"
    assert slept, "should have backed off"
    assert slept[0] >= 13, f"slept {slept[0]}s after being told 13s"


async def test_backoff_still_exponential_without_a_hint(monkeypatch) -> None:
    from unread.util import flood

    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(flood.asyncio, "sleep", _fake_sleep)

    from openai import APITimeoutError

    calls: list[int] = []

    @flood.retry_on_429(max_retries=3)
    async def _flaky():
        calls.append(1)
        if len(calls) < 2:
            raise APITimeoutError(request=None)
        return "ok"

    assert await _flaky() == "ok"
    assert slept and slept[0] < 5, "no hint means the usual short backoff"
