"""Every LLM call must send a model the provider it calls can serve.

The `--self-check` audit, link summaries and `ask`'s rerank pass used
`openai.filter_model_default`, and `ask`'s answer
`openai.chat_model_default`, whatever provider was active — OpenAI ids
that Anthropic and Gemini reject and OpenRouter spells `openai/…`. The
cheap passes also went through the chat slot's provider even when the
filter slot named another one.

Each call now takes its provider and its model from the same slot: the
cheap passes from the filter slot, the answer from the chat slot.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import typer

from unread.ai import ChatResult
from unread.ai.providers import _provider_class_defaults
from unread.analyzer import openai_client
from unread.analyzer.commands import _self_check
from unread.analyzer.pipeline import AnalysisResult
from unread.ask import commands as ask_commands
from unread.ask import rerank as rerank_mod
from unread.config import get_settings
from unread.enrich import link as link_mod
from unread.models import Message

_PROVIDERS = ["anthropic", "google", "openrouter"]

_HTML = "<html><head><title>Page</title></head><body>Postgres 18 ships async I/O.</body></html>"


@pytest.fixture(autouse=True)
def _no_slot_env(monkeypatch):
    """Slot routing exported in the developer's shell (a Docker `.env`)
    must not leak into the settings these tests build."""
    for slot in ("CHAT", "FILTER"):
        monkeypatch.delenv(f"UNREAD_AI_{slot}_PROVIDER", raising=False)
        monkeypatch.delenv(f"UNREAD_AI_{slot}_MODEL", raising=False)


def _on(provider: str, *, filter_provider: str | None = None):
    """Route the chat slot (and the filter slot, unless given) to `provider`."""
    s = get_settings()
    s.ai.chat_provider = provider
    s.ai.filter_provider = filter_provider or provider
    s.anthropic.api_key = "sk-ant-test"
    s.google.api_key = "g-test"
    s.openrouter.api_key = "sk-or-test"
    return s


def _filter_default(provider: str) -> str:
    """The filter slot's model when nothing overrides it: the legacy
    `[openai]` default on OpenAI, the provider's own default elsewhere."""
    if provider == "openai":
        return get_settings().openai.filter_model_default
    return _provider_class_defaults(provider)[1]


def _capture(monkeypatch, module) -> list[tuple[str, str]]:
    """Replace `module.chat_complete`; record `(provider, model)` per call."""
    seen: list[tuple[str, str]] = []

    async def fake_chat_complete(provider, *, model, **_kw):
        seen.append((provider.name, model))
        return ChatResult(
            text='[{"msg_id": 1, "score": 5}]', prompt_tokens=1, cached_tokens=0, completion_tokens=1
        )

    monkeypatch.setattr(module, "chat_complete", fake_chat_complete)
    return seen


def _msg(msg_id: int) -> Message:
    return Message(chat_id=1, msg_id=msg_id, date=datetime(2026, 1, 1, tzinfo=UTC), text=f"postgres {msg_id}")


def _result() -> AnalysisResult:
    return AnalysisResult(
        preset="summary",
        model="m",
        chat_id=1,
        thread_id=0,
        msg_count=1,
        chunk_count=1,
        batch_hashes=[],
        final_result="Postgres 18 ships async I/O.",
        total_cost_usd=0.0,
        cache_hits=0,
        cache_misses=1,
    )


class _LinkRepo:
    async def get_link_enrichment(self, _h):
        return None

    async def put_link_enrichment(self, *_a, **_kw):
        return None


async def _summarize_link(monkeypatch) -> tuple[list[tuple[str, str]], list[str], object]:
    fetched: list[str] = []

    async def fake_fetch(url, _timeout_sec):
        fetched.append(url)
        return _HTML

    monkeypatch.setattr(link_mod, "_fetch", fake_fetch)
    seen = _capture(monkeypatch, link_mod)
    res = await link_mod.enrich_url("https://example.com/post", repo=_LinkRepo())
    return seen, fetched, res


# --- `## Verification` audit (`--self-check`) ---------------------------------


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_self_check_sends_the_provider_its_own_model(monkeypatch, provider):
    _on(provider)
    seen = _capture(monkeypatch, openai_client)
    _text, err = await _self_check(result=_result(), messages=[_msg(1)], repo=None)
    assert err is None
    assert seen == [(provider, _filter_default(provider))]


async def test_self_check_on_openai_is_unchanged(monkeypatch):
    seen = _capture(monkeypatch, openai_client)
    await _self_check(result=_result(), messages=[_msg(1)], repo=None)
    assert seen == [("openai", get_settings().openai.filter_model_default)]


@pytest.mark.parametrize(("chat", "filt"), [("openai", "anthropic"), ("anthropic", "openai")])
async def test_self_check_runs_on_the_filter_slot(monkeypatch, chat, filt):
    """The audit is a cheap pass: provider and model both follow the
    filter slot, wherever the chat slot points."""
    _on(chat, filter_provider=filt)
    seen = _capture(monkeypatch, openai_client)
    await _self_check(result=_result(), messages=[_msg(1)], repo=None)
    assert seen == [(filt, _filter_default(filt))]


# --- link summaries (`--enrich=link`) -----------------------------------------


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_link_summary_sends_the_provider_its_own_model(monkeypatch, provider):
    _on(provider)
    seen, _fetched, res = await _summarize_link(monkeypatch)
    assert seen == [(provider, _filter_default(provider))]
    assert res.model == _filter_default(provider)


async def test_link_summary_runs_on_the_filter_slot(monkeypatch):
    _on("openai", filter_provider="google")
    seen, _fetched, _res = await _summarize_link(monkeypatch)
    assert seen == [("google", _filter_default("google"))]


async def test_link_model_config_still_wins(monkeypatch):
    _on("anthropic").enrich.link_model = "claude-sonnet-4-6"
    seen, _fetched, _res = await _summarize_link(monkeypatch)
    assert seen == [("anthropic", "claude-sonnet-4-6")]


async def test_link_summary_skipped_when_the_filter_slot_has_no_key(monkeypatch):
    """Like an image with no vision key: skip the URL with a warning, and
    don't download a page nothing can summarize."""
    _on("openai", filter_provider="google").google.api_key = ""
    seen, fetched, res = await _summarize_link(monkeypatch)
    assert res is None
    assert fetched == []
    assert seen == []


# --- `ask`: rerank pass and answer ----------------------------------------------


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_ask_reranks_with_the_filter_slot_model(monkeypatch, provider):
    s = _on(provider)
    seen: dict[str, str] = {}

    async def fake_retrieve(**_kw):
        return [(_msg(i), 10) for i in range(1, 4)]

    async def fake_rerank(*, model, **_kw):
        seen["model"] = model
        return []  # nothing kept → the turn stops before the answer call

    monkeypatch.setattr(ask_commands, "retrieve_messages", fake_retrieve)
    monkeypatch.setattr(rerank_mod, "rerank", fake_rerank)
    with pytest.raises(typer.Exit):
        await ask_commands._run_single_turn(
            question="what about postgres",
            history=[],
            client=None,
            repo=None,
            settings=s,
            ask_cfg=s.ask,
            oai=None,
            used_model="unused",
            chat_ids=[1],
            chat_titles={},
            thread=None,
            folder=None,
            since_dt=None,
            until_dt=None,
            limit=2,
            rerank_on=True,
            semantic=False,
            show_retrieved=False,
            output=None,
            console_out=True,
            max_cost=None,
            yes=True,
        )
    assert seen["model"] == _filter_default(provider)


@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_rerank_calls_the_filter_slot_provider(monkeypatch, provider):
    _on("openai", filter_provider=provider)
    seen = _capture(monkeypatch, rerank_mod)
    pool = [(_msg(i), 10) for i in range(1, 4)]
    await rerank_mod.rerank(repo=None, pool=pool, question="q", model=_filter_default(provider), keep=1)
    assert seen == [(provider, _filter_default(provider))]


async def test_rerank_keeps_keyword_order_when_the_filter_slot_has_no_key(monkeypatch):
    """Rerank is best-effort: a filter slot without a key must not fail the
    whole `ask` — the keyword pool goes through, best scores first."""
    _on("openai", filter_provider="google").google.api_key = ""
    seen = _capture(monkeypatch, rerank_mod)
    pool = [(_msg(1), 10), (_msg(2), 30), (_msg(3), 20)]
    kept = await rerank_mod.rerank(repo=None, pool=pool, question="q", model="m", keep=2)
    assert [m.msg_id for m, _ in kept] == [2, 3]
    assert seen == []


@asynccontextmanager
async def _repo(*_a, **_kw):
    repo = AsyncMock()
    repo.get_chat = AsyncMock(return_value=None)
    yield repo


@pytest.mark.parametrize("provider", [*_PROVIDERS, "openai"])
async def test_ask_answers_with_the_chat_slot_model(monkeypatch, provider):
    _on(provider)
    seen: dict[str, str] = {}

    async def fake_run_single_turn(*, oai, used_model, **_kw):
        seen.update(provider=oai.name, model=used_model)
        return "answer", [(_msg(1), 50)]

    monkeypatch.setattr(ask_commands, "_run_single_turn", fake_run_single_turn)
    monkeypatch.setattr(ask_commands, "open_repo", _repo)
    await ask_commands.cmd_ask(
        question="hello", ref=None, chat=None, folder=None, global_scope=True, no_followup=True
    )
    expected = (
        get_settings().openai.chat_model_default
        if provider == "openai"
        else _provider_class_defaults(provider)[0]
    )
    assert seen == {"provider": provider, "model": expected}


async def test_ask_answer_model_from_config(monkeypatch):
    """`ai.chat_model` (the `/settings` model pick) reaches the
    Telegram-archive answer, as it already did for files and URLs."""
    _on("anthropic").ai.chat_model = "claude-opus-4-7"
    seen: dict[str, str] = {}

    async def fake_run_single_turn(*, used_model, **_kw):
        seen["model"] = used_model
        return "answer", [(_msg(1), 50)]

    monkeypatch.setattr(ask_commands, "_run_single_turn", fake_run_single_turn)
    monkeypatch.setattr(ask_commands, "open_repo", _repo)
    await ask_commands.cmd_ask(
        question="hello", ref=None, chat=None, folder=None, global_scope=True, no_followup=True
    )
    assert seen["model"] == "claude-opus-4-7"
