"""OpenAI-compatible adapters.

Three providers in this file all speak the OpenAI Chat Completions
API and share an `AsyncOpenAI` client (different `base_url` + key).
Splitting them lets each carry its own provider-specific defaults
(model names, base URL) without runtime conditionals.

  - :class:`OpenAIProvider`     — vanilla OpenAI.
  - :class:`OpenRouterProvider` — `https://openrouter.ai/api/v1` proxy
                                  to many models. Key from `settings.openrouter`.
  - :class:`LocalProvider`      — self-hosted server (Ollama / LM Studio /
                                  vLLM). Key from `settings.local` (defaults
                                  to a placeholder; most local servers ignore
                                  the header).
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from unread.ai.providers import ChatResult, ProviderUnavailableError
from unread.ai.trust import enforce_base_url_trust
from unread.util.flood import retry_on_429

# OpenRouter uses `HTTP-Referer` + `X-Title` to attribute requests to a
# specific app on its public leaderboard / per-app analytics. Sending
# them is optional but recommended; without them the app shows up as
# "Unknown" in the OpenRouter dashboard.
OPENROUTER_APP_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/maxbolgarin/unread",
    "X-Title": "unread",
}


def _is_reasoning_model(model: str) -> bool:
    """True when `model` is an OpenAI reasoning-class model that rejects
    custom `temperature`.

    Looks up :class:`unread.ai.models.ModelInfo.reasoning` first — that's
    the curated source of truth (covers gpt-5.x including mini/nano,
    o-series, and OpenRouter aliases like `openai/gpt-5.4-mini`). When
    the model isn't in the catalog, falls back to a name-shape heuristic:
    `o1`/`o3`/`o4`/`gpt-5` prefixes (matched against the bare suffix so
    `vendor/model` routing still works). The heuristic is permissive —
    accidentally dropping temperature for a non-reasoning model is
    harmless (defaults to 1.0 server-side), while incorrectly
    *forwarding* temperature to a reasoning model 400s the request.
    """
    from unread.ai.models import find_model

    info = find_model(model)
    if info is not None and info.reasoning:
        return True
    name = model.rsplit("/", 1)[-1].lower()
    return name.startswith("o1") or name.startswith("o3") or name.startswith("o4") or name.startswith("gpt-5")


class _OpenAICompatBase:
    """Shared `AsyncOpenAI` plumbing.

    Subclasses set `name`, supply `_make_client(settings)`, and pin
    their `default_chat_model` / `default_filter_model`. The actual
    HTTP call lives here so per-provider classes stay tiny.
    """

    name: str = "openai-compat"
    default_chat_model: str = ""
    default_filter_model: str = ""
    # Off on the base class on purpose: OpenRouter and local servers
    # inherit this plumbing but have no `web_search` tool. Only
    # `OpenAIProvider` flips it on.
    supports_web_search: bool = False

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        self._settings = settings
        self._client = self._make_client(settings)

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def _search_extra_body(self) -> dict[str, Any] | None:
        """Non-standard body fields that turn on web search, if any.

        None on the base class: a plain OpenAI-compatible server has no
        such concept, and sending an unknown field would 400 an ordinary
        local model. Only OpenRouter overrides it.
        """
        return None

    @retry_on_429()
    async def _completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        # `max_completion_tokens` is the modern name (gpt-5+, reasoning
        # models). Older OpenAI-compat servers still accept it; the few
        # that don't are local-model bridges that vary by version, and
        # the user can fall back by editing the local server's config.
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        # OpenAI's reasoning model family (gpt-5, gpt-5.4, o1, o3, ...)
        # rejects any `temperature` other than the default 1.0 with a
        # 400. Drop the parameter for those models so the wired-in
        # default of 0.2 (config.py) doesn't silently 4xx every chat
        # call when the user picks the catalog default `gpt-5.4-mini`.
        if not _is_reasoning_model(model):
            kwargs["temperature"] = temperature
        return await self._client.chat.completions.create(**kwargs)

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        web_search: bool = False,
    ) -> ChatResult:
        # Adapters without the capability return None here, so `web_search`
        # is a no-op for them rather than a 400 from an unexpected body
        # field. `OpenAIProvider` never reaches this — it overrides `chat`
        # because its search path is a different API entirely.
        extra_body = self._search_extra_body() if web_search else None
        resp = await self._completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        finish = getattr(choice, "finish_reason", None)
        truncated = finish == "length"
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        return ChatResult(
            text=text,
            prompt_tokens=prompt,
            cached_tokens=cached,
            completion_tokens=completion,
            truncated=truncated,
        )


class OpenAIProvider(_OpenAICompatBase):
    name = "openai"
    default_chat_model = "gpt-5.6-luna"
    default_filter_model = "gpt-5.6-luna"
    supports_web_search = True

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        web_search: bool = False,
    ) -> ChatResult:
        """Chat Completions, or the Responses API when searching.

        The `web_search` tool exists only on the Responses API, so a
        search-grounded call is a genuinely different request shape —
        not a parameter on the usual one. Everything else keeps going
        through Chat Completions unchanged; this is the only place in
        the codebase that touches `responses.create`.
        """
        if not web_search:
            return await super().chat(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        return await self._searched_completion(model=model, messages=messages, max_tokens=max_tokens)

    @retry_on_429()
    async def _searched_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> ChatResult:
        # The Responses API takes a single `input` plus a separate
        # `instructions`, not a messages array. Our callers always build
        # `[system, user]` (see `build_messages`), so fold accordingly.
        instructions = "\n\n".join(
            m.get("content", "") for m in messages if m.get("role") == "system"
        ).strip()
        user_input = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system").strip()

        kwargs: dict[str, Any] = {
            "model": model,
            "input": user_input,
            "tools": [{"type": "web_search"}],
            "max_output_tokens": max_tokens,
            # The Responses API defaults to store=True, which would retain
            # the prompt (a whole transcript or chat) on OpenAI's servers.
            # Every other call in this project goes through Chat
            # Completions, which doesn't. Opt out explicitly so the search
            # path doesn't quietly weaken the local-only posture.
            "store": False,
        }
        if instructions:
            kwargs["instructions"] = instructions
        resp = await self._client.responses.create(**kwargs)

        text = getattr(resp, "output_text", "") or ""
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "input_tokens", 0) or 0)
        completion = int(getattr(usage, "output_tokens", 0) or 0)
        cached = 0
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)

        # No count in usage — infer it from the tool-call items the model
        # emitted. Defensive `getattr` throughout: the exact item shape
        # is the part of this integration most likely to drift.
        searches = 0
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", "") == "web_search_call":
                searches += 1

        return ChatResult(
            text=text,
            prompt_tokens=prompt,
            cached_tokens=cached,
            completion_tokens=completion,
            truncated=getattr(resp, "status", "") == "incomplete",
            web_searches=searches,
        )

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        if not settings.openai.api_key:
            raise ProviderUnavailableError(
                "OpenAI provider selected but `openai.api_key` is empty. Run `unread init` to add one."
            )
        enforce_base_url_trust("openai", settings)
        kwargs: dict[str, Any] = {
            "api_key": settings.openai.api_key,
            "timeout": settings.openai.request_timeout_sec,
        }
        # `settings.ai.base_url` lets the user point the OpenAI SDK at
        # a private gateway (e.g. internal Azure OpenAI proxy) without
        # switching to the OpenRouter / Local adapters.
        if settings.ai.base_url:
            kwargs["base_url"] = settings.ai.base_url
        return AsyncOpenAI(**kwargs)


class OpenRouterProvider(_OpenAICompatBase):
    name = "openrouter"
    # OpenRouter has no provider-native tool; it exposes search as its own
    # `web` plugin, which rides along on the ordinary Chat Completions
    # body. Billed per result rather than per search.
    supports_web_search = True
    # OpenRouter prefixes models by upstream vendor. These are widely
    # available, cheap defaults; users can override via `ai.chat_model`.
    default_chat_model = "openai/gpt-5.6-luna"
    default_filter_model = "openai/gpt-5.6-luna"

    def _search_extra_body(self) -> dict[str, Any] | None:
        # `plugins` is an OpenRouter extension to the OpenAI body, so it
        # goes through the SDK's `extra_body` passthrough. The `:online`
        # model-suffix form does the same thing, but mangling the model
        # name would corrupt cost lookups and the report's model row.
        return {"plugins": [{"id": "web"}]}

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        if not settings.openrouter.api_key:
            raise ProviderUnavailableError(
                "OpenRouter provider selected but `openrouter.api_key` is empty. "
                "Run `unread init` to add one."
            )
        enforce_base_url_trust("openrouter", settings)
        return AsyncOpenAI(
            api_key=settings.openrouter.api_key,
            base_url=settings.ai.base_url or settings.openrouter.base_url,
            timeout=settings.openai.request_timeout_sec,
            default_headers=OPENROUTER_APP_HEADERS,
        )


class LocalProvider(_OpenAICompatBase):
    name = "local"
    # Most local servers ship Llama 3.1 / Qwen 2.5 by default. The user
    # almost always needs to override; the default is a "model that
    # exists somewhere" placeholder so a CLI smoke run reaches the
    # endpoint instead of erroring at config validation time.
    default_chat_model = "llama3.1"
    default_filter_model = "llama3.1"

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        return AsyncOpenAI(
            api_key=settings.local.api_key or "local-no-key",
            base_url=settings.ai.base_url or settings.local.base_url,
            timeout=settings.openai.request_timeout_sec,
        )
