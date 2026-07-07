"""B3: `--max-cost` was accepted but never enforced on `unread tg chats
run --flat` — `_cmd_run_flat` took the `max_cost` param but never called
`estimate_cost`, unlike the per-chat path (`_run_single` in
`analyzer/commands.py`), which has always enforced it.

The fix extracts the per-chat gate (estimate + banner + confirm/Exit)
into a reusable `enforce_max_cost_gate` in `analyzer/commands.py` and
wires it into `_cmd_run_flat` too, right before the (only) `run_analysis`
call, using the final merged message count across every subscription.

This file has two halves:
  1. Gate unit tests — pin the exact exit-code semantics of
     `enforce_max_cost_gate` (verified against the pre-refactor inline
     block in `_run_single` before extraction).
  2. A wiring test — `_cmd_run_flat` actually calls the gate, with the
     merged message count, before `run_analysis`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import typer

from unread.analyzer.commands import enforce_max_cost_gate
from unread.analyzer.prompts import Preset
from unread.config import ChatPricing, PricingCfg, Settings


def _priced_settings() -> Settings:
    """Two fake models, both priced steeply enough that a handful of
    messages already blows past a tiny --max-cost budget."""
    s = Settings()
    s.pricing = PricingCfg(
        chat={
            "flat-filter-b3": ChatPricing(input=1000.0, cached_input=0.0, output=1000.0),
            "flat-final-b3": ChatPricing(input=1000.0, cached_input=0.0, output=1000.0),
        }
    )
    return s


def _preset(**overrides) -> Preset:
    kwargs = {
        "name": "multichat",
        "prompt_version": "v1",
        "system": "You are a careful summarizer of chat logs.",
        "user_template": "{messages}\n{period}\n{title}\n{msg_count}",
        "needs_reduce": True,
        "filter_model": "flat-filter-b3",
        "final_model": "flat-final-b3",
        "output_budget_tokens": 800,
        "map_output_tokens": 200,
    }
    kwargs.update(overrides)
    return Preset(**kwargs)


# ---------------------------------------------------------------------------
# 1. Gate unit tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_over_budget_with_yes_exits_2():
    """Pre-refactor `_run_single` behavior: over-budget + `--yes` prints
    `aborting_yes_set` and raises `typer.Exit(2)` — `--yes` can't drive
    an interactive confirm, so it aborts instead of silently proceeding.
    """
    settings = _priced_settings()
    preset = _preset()
    with pytest.raises(typer.Exit) as excinfo:
        await enforce_max_cost_gate(
            n_messages=5000, preset=preset, settings=settings, max_cost=0.0001, yes=True
        )
    assert excinfo.value.exit_code == 2


@pytest.mark.asyncio
async def test_gate_over_budget_confirm_declines_exits_0():
    """Pre-refactor behavior: over-budget, no `--yes`, user declines the
    `Run anyway?` confirm → prints `aborted` and raises `typer.Exit(0)`
    (a clean cancel, not an error)."""
    settings = _priced_settings()
    preset = _preset()
    with (
        patch("unread.util.prompt.confirm", return_value=False),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await enforce_max_cost_gate(
            n_messages=5000, preset=preset, settings=settings, max_cost=0.0001, yes=False
        )
    assert excinfo.value.exit_code == 0


@pytest.mark.asyncio
async def test_gate_over_budget_confirm_accepts_proceeds():
    """User confirms `Run anyway?` → no exception, run proceeds."""
    settings = _priced_settings()
    preset = _preset()
    with patch("unread.util.prompt.confirm", return_value=True):
        await enforce_max_cost_gate(
            n_messages=5000, preset=preset, settings=settings, max_cost=0.0001, yes=False
        )


@pytest.mark.asyncio
async def test_gate_missing_pricing_not_yes_exits_2():
    """Missing pricing + `--max-cost` set + no `--yes`: fail closed
    (`typer.Exit(2)`) rather than silently skip the guard."""
    settings = Settings()  # no pricing.chat entries at all
    preset = _preset(filter_model="unpriced-filter-b3", final_model="unpriced-final-b3")
    with pytest.raises(typer.Exit) as excinfo:
        await enforce_max_cost_gate(n_messages=10, preset=preset, settings=settings, max_cost=0.01, yes=False)
    assert excinfo.value.exit_code == 2


@pytest.mark.asyncio
async def test_gate_missing_pricing_with_yes_overrides():
    """Missing pricing + `--yes`: logged override, no exception."""
    settings = Settings()
    preset = _preset(filter_model="unpriced-filter-b3-2", final_model="unpriced-final-b3-2")
    await enforce_max_cost_gate(n_messages=10, preset=preset, settings=settings, max_cost=0.01, yes=True)


@pytest.mark.asyncio
async def test_gate_no_max_cost_never_raises():
    """`max_cost=None` → only the banner prints; the gate is a no-op."""
    settings = _priced_settings()
    preset = _preset()
    await enforce_max_cost_gate(n_messages=5000, preset=preset, settings=settings, max_cost=None, yes=False)


@pytest.mark.asyncio
async def test_gate_under_budget_no_max_cost_set_never_raises():
    settings = _priced_settings()
    preset = _preset()
    await enforce_max_cost_gate(n_messages=1, preset=preset, settings=settings, max_cost=100.0, yes=False)


# ---------------------------------------------------------------------------
# 2. `_cmd_run_flat` wiring.
# ---------------------------------------------------------------------------


@contextmanager
def _flat_deps(sub, prepared, *, gate_impl, run_analysis_impl):
    """Patch every heavy dependency `_cmd_run_flat` touches so the test
    exercises only the wiring: does the gate get called (with the
    merged message count) before `run_analysis`?

    `prepare_chat_run` and `run_analysis` are imported *locally* inside
    `_cmd_run_flat`, so they must be patched at their source module, not
    at `unread.runner`.
    """
    fake_repo = AsyncMock()
    fake_repo.list_subscriptions = AsyncMock(return_value=[sub])

    @asynccontextmanager
    async def fake_open_repo(_path):
        yield fake_repo

    @asynccontextmanager
    async def fake_tg_client(_settings):
        yield object()

    async def fake_prepare_chat_run(**_kwargs):
        return prepared

    with (
        patch("unread.runner.open_repo", fake_open_repo),
        patch("unread.runner.tg_client", fake_tg_client),
        patch("unread.core.pipeline.prepare_chat_run", new=fake_prepare_chat_run),
        patch("unread.analyzer.commands.enforce_max_cost_gate", new=gate_impl),
        patch("unread.analyzer.pipeline.run_analysis", new=run_analysis_impl),
    ):
        yield


def _make_sub_and_prepared():
    from unread.core.run import PreparedRun
    from unread.models import Message, Subscription

    sub = Subscription(chat_id=111, source_kind="chat", title="Test Chat")
    msgs = [Message(chat_id=111, msg_id=i, date=datetime.now(UTC)) for i in range(3)]
    prepared = PreparedRun(
        chat_id=111,
        thread_id=None,
        chat_title="Test Chat",
        thread_title=None,
        chat_username=None,
        chat_internal_id=None,
        messages=msgs,
        period=(None, None),
        topic_titles=None,
        topic_markers=None,
        raw_msg_count=len(msgs),
        enrich_stats=None,
        mark_read_fn=None,
        client=None,
        repo=None,
        settings=None,
    )
    return sub, msgs, prepared


@pytest.mark.asyncio
async def test_cmd_run_flat_calls_gate_before_run_analysis():
    """The gate must run on the merged message count, using the
    `multichat` preset (flat mode's default), before `run_analysis`."""
    from unread.runner import _cmd_run_flat

    sub, msgs, prepared = _make_sub_and_prepared()
    calls: list[str] = []

    async def fake_gate(*, n_messages, preset, settings, max_cost, yes, preset_label=None):
        calls.append("gate")
        assert n_messages == len(msgs)
        assert preset.name == "multichat"
        assert max_cost == 0.05
        assert yes is True

    async def fake_run_analysis(**_kwargs):
        calls.append("run_analysis")
        raise RuntimeError("stop-after-run-analysis")

    with (
        _flat_deps(sub, prepared, gate_impl=fake_gate, run_analysis_impl=fake_run_analysis),
        pytest.raises(RuntimeError, match="stop-after-run-analysis"),
    ):
        await _cmd_run_flat(
            only_chat=None,
            preset_override=None,
            period_override=None,
            enrich_override=None,
            enrich_all_override=False,
            no_enrich_override=False,
            mark_read_override=False,
            post_to_override=None,
            max_cost=0.05,
            dry_run=False,
            yes=True,
        )

    assert calls == ["gate", "run_analysis"]


@pytest.mark.asyncio
async def test_cmd_run_flat_gate_exit_blocks_run_analysis():
    """When the gate raises `typer.Exit` (over-budget, e.g.), the flat
    run must stop there — `run_analysis` must never be reached."""
    from unread.runner import _cmd_run_flat

    sub, _msgs, prepared = _make_sub_and_prepared()
    run_analysis_called = False

    async def fake_gate(*, n_messages, preset, settings, max_cost, yes, preset_label=None):
        raise typer.Exit(2)

    async def fake_run_analysis(**_kwargs):
        nonlocal run_analysis_called
        run_analysis_called = True

    with (
        _flat_deps(sub, prepared, gate_impl=fake_gate, run_analysis_impl=fake_run_analysis),
        pytest.raises(typer.Exit) as excinfo,
    ):
        await _cmd_run_flat(
            only_chat=None,
            preset_override=None,
            period_override=None,
            enrich_override=None,
            enrich_all_override=False,
            no_enrich_override=False,
            mark_read_override=False,
            post_to_override=None,
            max_cost=0.0001,
            dry_run=False,
            yes=True,
        )

    assert excinfo.value.exit_code == 2
    assert run_analysis_called is False
