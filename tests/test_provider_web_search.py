"""Web-search capability across the provider adapters.

Only the fact-check preset asks for this, and only for its final verify
call. Three providers support it natively; OpenRouter and local servers
don't, and must say so rather than silently returning an unchecked answer.

These tests stub the SDK clients — they pin the request shape WE send and
how we read the response back, not the live APIs.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from unread.config import load_settings, reset_settings


@pytest.fixture(autouse=True)
def _settings():
    reset_settings()
    yield
    reset_settings()


def _s(**overrides):
    s = load_settings()
    s.openai.api_key = "sk-test"
    s.anthropic.api_key = "sk-ant-test"
    s.google.api_key = "g-test"
    s.openrouter.api_key = "or-test"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# --- capability flags --------------------------------------------------------


def test_openai_advertises_web_search():
    from unread.ai.openai_provider import OpenAIProvider

    assert OpenAIProvider(_s()).supports_web_search is True


def test_openrouter_does_not_advertise_web_search():
    """Shares `_OpenAICompatBase` with OpenAI — the capability must not be
    inherited, or a fact-check on OpenRouter would claim it searched."""
    from unread.ai.openai_provider import OpenRouterProvider

    assert OpenRouterProvider(_s()).supports_web_search is False


def test_local_does_not_advertise_web_search():
    from unread.ai.openai_provider import LocalProvider

    assert LocalProvider(_s()).supports_web_search is False


# --- OpenAI: Responses API ---------------------------------------------------


class _FakeResponses:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs

        class _Usage:
            input_tokens = 100
            output_tokens = 50
            input_tokens_details = None

        class _Resp:
            output_text = "verified answer"
            usage = _Usage()
            status = "completed"
            output: ClassVar[list] = [type("I", (), {"type": "web_search_call"})()]

        return _Resp()


async def test_openai_web_search_uses_the_responses_api():
    """Chat Completions has no `web_search` tool — the search path has to
    go through `responses.create`."""
    from unread.ai.openai_provider import OpenAIProvider

    p = OpenAIProvider(_s())
    fake = _FakeResponses()
    p._client = type("C", (), {"responses": fake})()

    res = await p.chat(
        model="gpt-5.4",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "check this"}],
        max_tokens=500,
        temperature=0.2,
        web_search=True,
    )
    assert fake.kwargs is not None
    assert fake.kwargs["tools"] == [{"type": "web_search"}]
    assert fake.kwargs["model"] == "gpt-5.4"
    assert res.text == "verified answer"
    assert res.prompt_tokens == 100
    assert res.completion_tokens == 50
    assert res.web_searches == 1


async def test_openai_without_web_search_still_uses_chat_completions():
    """The normal path must not change — every non-factcheck call still
    goes through Chat Completions."""
    from unread.ai.openai_provider import OpenAIProvider

    p = OpenAIProvider(_s())
    calls: dict[str, Any] = {}

    class _Completions:
        async def create(self, **kwargs):
            calls.update(kwargs)

            class _R:
                choices: ClassVar[list] = [
                    type(
                        "C",
                        (),
                        {
                            "message": type("M", (), {"content": "plain"})(),
                            "finish_reason": "stop",
                        },
                    )()
                ]
                usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 2})()

            return _R()

    p._client = type("C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})()
    res = await p.chat(
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        temperature=0.2,
    )
    assert res.text == "plain"
    assert "tools" not in calls
    assert res.web_searches == 0


# --- Anthropic ---------------------------------------------------------------


async def test_anthropic_web_search_sends_the_server_tool():
    from unread.ai.anthropic_provider import AnthropicProvider

    p = AnthropicProvider(_s())
    seen: dict[str, Any] = {}

    class _Messages:
        async def create(self, **kwargs):
            seen.update(kwargs)

            class _R:
                content: ClassVar[list] = [type("B", (), {"type": "text", "text": "checked"})()]
                usage = type(
                    "U",
                    (),
                    {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "server_tool_use": type("S", (), {"web_search_requests": 3})(),
                    },
                )()
                stop_reason = "end_turn"

            return _R()

    p._client = type("C", (), {"messages": _Messages()})()
    res = await p.chat(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "check"}],
        max_tokens=100,
        temperature=0.2,
        web_search=True,
    )
    tools = seen["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "web_search"
    assert tools[0]["type"].startswith("web_search_")
    assert res.text == "checked"
    # Search count comes back in usage — the only provider that reports it.
    assert res.web_searches == 3


async def test_anthropic_without_web_search_sends_no_tools():
    from unread.ai.anthropic_provider import AnthropicProvider

    p = AnthropicProvider(_s())
    seen: dict[str, Any] = {}

    class _Messages:
        async def create(self, **kwargs):
            seen.update(kwargs)

            class _R:
                content: ClassVar[list] = [type("B", (), {"type": "text", "text": "plain"})()]
                usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
                stop_reason = "end_turn"

            return _R()

    p._client = type("C", (), {"messages": _Messages()})()
    await p.chat(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        temperature=0.2,
    )
    assert "tools" not in seen


def test_anthropic_web_search_tool_version_is_configurable():
    """The dated tool type 400s when it's newer than the account's API
    version, so it must be fixable without shipping a release."""
    from unread.ai.anthropic_provider import AnthropicProvider

    s = _s()
    s.ai.anthropic_web_search_tool = "web_search_20250305"
    p = AnthropicProvider(s)
    assert p._web_search_tool()["type"] == "web_search_20250305"


# --- Google ------------------------------------------------------------------


async def test_google_web_search_enables_the_google_search_tool():
    from unread.ai.google_provider import GoogleProvider

    p = GoogleProvider(_s())
    seen: dict[str, Any] = {}

    class _Models:
        async def generate_content(self, **kwargs):
            seen.update(kwargs)

            class _R:
                text = "grounded"
                candidates: ClassVar[list] = [
                    type("C", (), {"finish_reason": "STOP", "safety_ratings": ()})()
                ]
                usage_metadata = type(
                    "U",
                    (),
                    {"prompt_token_count": 7, "candidates_token_count": 3, "cached_content_token_count": 0},
                )()

            return _R()

    p._client = type("C", (), {"aio": type("A", (), {"models": _Models()})()})()
    res = await p.chat(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "check"}],
        max_tokens=100,
        temperature=0.2,
        web_search=True,
    )
    assert res.text == "grounded"
    tools = getattr(seen["config"], "tools", None)
    assert tools, "google_search tool must be attached to the config"
    assert getattr(tools[0], "google_search", None) is not None
