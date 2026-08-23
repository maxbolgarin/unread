"""Anthropic Claude adapter.

Wraps `anthropic.AsyncAnthropic.messages.create` to the canonical
:class:`unread.ai.providers.ChatResult` shape. Two notable
translations relative to the OpenAI shape:

  - **System message**: Anthropic doesn't accept `{"role": "system"}`
    inside `messages`. We pull every system-role message out of the
    list and concatenate them into the top-level `system=` parameter.
  - **Truncation signal**: Anthropic uses `stop_reason == "max_tokens"`
    where OpenAI uses `finish_reason == "length"`. We map that to
    `ChatResult.truncated=True` so the orchestrator's truncation-retry
    fires identically.
  - **Usage/cache-token accounting**: Anthropic's `usage.input_tokens`
    excludes cache reads/writes entirely (unlike OpenAI's `prompt_tokens`,
    which includes cached tokens in the total). We normalize to OpenAI
    semantics: `prompt_tokens = input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens`, `cached_tokens = cache_read_input_tokens`,
    so `unread.util.pricing.chat_cost`'s `fresh = prompt_tokens -
    cached_tokens` math stays correct for both providers.
  - **Retries**: SDK retries are disabled (`max_retries=0`) and we run
    our own backoff loop so the user sees the same yellow "retrying
    in Ns" status they get for OpenAI 429s. Without this, an Anthropic
    rate limit looks like a 30-60s freeze and gives no user feedback.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from unread.ai.providers import ChatResult, ProviderUnavailableError
from unread.util.flood import _user_visible_retry_status
from unread.util.logging import get_logger

log = get_logger(__name__)


def _split_system_and_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Pull system-role entries into a single string; return everything else.

    `unread`'s formatter always emits a single system message followed
    by a single user message (see `analyzer.openai_client.build_messages`),
    but be defensive about multiple / interleaved system entries — some
    callers (e.g. the rerank prompt) don't go through `build_messages`.
    """
    system_chunks: list[str] = []
    rest: list[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            system_chunks.append(m.get("content", ""))
        else:
            rest.append(m)
    system_prompt = "\n\n".join(c for c in system_chunks if c)
    return system_prompt, rest


class AnthropicProvider:
    supports_web_search = True

    name = "anthropic"
    # Defaults track the current generally-available lineup (refreshed
    # 2026-05-01). The user can switch via `ai.chat_model` /
    # `ai.filter_model`; the full per-provider catalog lives in
    # `unread.ai.models`.
    default_chat_model = "claude-sonnet-4-6"
    default_filter_model = "claude-haiku-4-5"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # pragma: no cover — pulled in via pyproject
            raise ProviderUnavailableError(
                "Anthropic provider selected but the `anthropic` package isn't installed. "
                "Run `uv sync --extra dev` (or pip install anthropic)."
            ) from e
        if not settings.anthropic.api_key:
            raise ProviderUnavailableError(
                "Anthropic provider selected but `anthropic.api_key` is empty. Run `unread init` to add one."
            )
        # `max_retries=0` disables the SDK's silent transparent retries.
        # We run our own backoff loop in `chat()` so the user sees the
        # same yellow "Rate limited — retrying in Ns" status they get
        # on the OpenAI path.
        self._client = AsyncAnthropic(
            api_key=settings.anthropic.api_key,
            timeout=settings.openai.request_timeout_sec,
            max_retries=0,
        )
        self._settings = settings

    def _web_search_tool(self) -> dict[str, Any]:
        """Server-tool block for Anthropic's native web search.

        The `type` is date-versioned and must not exceed the account's
        API version, so it comes from config — see
        `AICfg.anthropic_web_search_tool`.
        """
        return {
            "type": getattr(self._settings.ai, "anthropic_web_search_tool", "") or "web_search_20250305",
            "name": "web_search",
        }

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        web_search: bool = False,
    ) -> ChatResult:
        system_prompt, rest = _split_system_and_messages(messages)
        # Anthropic requires `messages` non-empty AND each `content`
        # block non-empty — an empty string raises 400
        # ("messages.0.content: at least one message content block is
        # required"). The formatter always produces a populated user
        # message in practice; this is the defensive fallback for
        # unusual call sites (e.g. rerank prompt). Use a single space
        # so the API accepts it; the placeholder costs ~1 token.
        if not rest:
            rest = [{"role": "user", "content": " "}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": rest,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if web_search:
            kwargs["tools"] = [self._web_search_tool()]

        # Own retry loop (SDK retries are off — see __init__). Catches
        # the typed `RateLimitError`, `APIStatusError` 5xx, and
        # `APIConnectionError`. 4xx other than 429 propagates so a
        # programmer / config bug surfaces immediately.
        from anthropic import (  # type: ignore[import-not-found]
            APIConnectionError,
            APIStatusError,
            RateLimitError,
        )

        max_retries = self._settings.openai.max_retries
        resp = None
        for attempt in range(max(1, max_retries)):
            try:
                resp = await self._client.messages.create(**kwargs)
                break
            except (RateLimitError, APIConnectionError) as e:
                if attempt == max_retries - 1:
                    raise
                delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                log.warning(
                    "anthropic.retry",
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    err=type(e).__name__,
                )
                _user_visible_retry_status(
                    f"Anthropic {type(e).__name__} — retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries})…"
                )
                await asyncio.sleep(delay)
            except APIStatusError as e:
                # Retry only 5xx. 4xx (auth, validation, content
                # policy) is user-actionable — propagate immediately.
                if 500 <= int(getattr(e, "status_code", 0) or 0) < 600 and attempt < max_retries - 1:
                    delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                    log.warning(
                        "anthropic.retry_5xx",
                        attempt=attempt + 1,
                        delay=round(delay, 2),
                        status=e.status_code,
                    )
                    _user_visible_retry_status(
                        f"Anthropic {e.status_code} — retrying in {delay:.0f}s "
                        f"(attempt {attempt + 1}/{max_retries})…"
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        if resp is None:
            raise RuntimeError("Anthropic call exhausted retries without a response")

        # Concatenate any text blocks. Anthropic returns a list of content
        # blocks; when `web_search=True` the list ALSO carries
        # `server_tool_use` / `web_search_tool_result` blocks, which we
        # skip — the model's prose already contains the source URLs.
        text_parts: list[str] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts)

        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        # Anthropic's `input_tokens` EXCLUDES cache reads/writes, unlike
        # OpenAI's `prompt_tokens` which reports the full prompt total
        # (fresh + cached) with cached called out separately. `util.pricing
        # .chat_cost` assumes OpenAI semantics (`fresh = prompt_tokens -
        # cached_tokens`), so normalize here rather than at the pricing
        # layer. Cache-creation (write) tokens are folded into the "fresh"
        # bucket, which bills them at the 1.0x input rate — a slight
        # underbill vs Anthropic's actual 1.25x cache-write multiplier,
        # since there's no separate price column for it (out of scope).
        prompt_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached_tokens = cache_read_tokens

        # `pause_turn` means the server-tool turn was suspended mid-flight
        # and is meant to be continued by re-sending the response. We don't
        # implement that continuation loop yet, so flag it as truncated:
        # that stops `_call_cached` from caching a half-finished verdict
        # table (which would poison every later run of the same input) and
        # buys one retry. A proper continuation is the real fix.
        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "pause_turn":
            log.warning("anthropic.pause_turn", model=model, hint="partial answer; retrying")
        truncated = stop_reason in ("max_tokens", "pause_turn")

        # Anthropic is the only provider that reports a search count.
        server_tool_use = getattr(usage, "server_tool_use", None)
        searches = int(getattr(server_tool_use, "web_search_requests", 0) or 0)

        return ChatResult(
            web_searches=searches,
            text=text,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            truncated=truncated,
        )
