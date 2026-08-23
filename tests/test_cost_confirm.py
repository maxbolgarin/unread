"""Soft cost confirmation before an expensive analyze run.

`--max-cost` is a HARD ceiling you have to remember to pass. This is the
default guard: above a configurable estimate, ask first. A one-hour
podcast under the fact-check preset is the case that motivated it.
"""

from __future__ import annotations

import pytest
import typer

from unread.analyzer.commands import enforce_cost_gates
from unread.config import load_settings, reset_settings


@pytest.fixture
def settings():
    reset_settings()
    s = load_settings()
    s.analyze.confirm_cost_above_usd = 0.25
    yield s
    reset_settings()


def _gate(settings, **kw):
    base = {
        "lo": 0.01,
        "hi": 0.02,
        "extra_cost": 0.0,
        "max_cost": None,
        "yes": False,
        "n_messages": 10,
        "preset_name": "factcheck",
        "settings": settings,
        "interactive": True,
    }
    base.update(kw)
    return enforce_cost_gates(**base)


def test_cheap_run_is_not_gated(settings, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings)
    assert asked == []


def test_expensive_run_asks_for_confirmation(settings, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=0.40, hi=0.90)
    assert len(asked) == 1


def test_declining_aborts(settings, monkeypatch) -> None:
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: False)
    with pytest.raises(typer.Exit):
        _gate(settings, lo=0.40, hi=0.90)


def test_yes_proceeds_without_asking(settings, monkeypatch) -> None:
    """`--yes` means "don't ask me", not "abort" — the soft threshold must
    not turn a scripted run into a failure the way `--max-cost` does."""
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=0.40, hi=0.90, yes=True)
    assert asked == []


def test_non_interactive_proceeds_without_asking(settings, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=0.40, hi=0.90, interactive=False)
    assert asked == []


def test_threshold_of_zero_disables_the_confirm(settings, monkeypatch) -> None:
    settings.analyze.confirm_cost_above_usd = 0.0
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=4.0, hi=9.0)
    assert asked == []


def test_transcript_cost_counts_towards_the_threshold(settings, monkeypatch) -> None:
    """Whisper on a long podcast can dominate the bill — it must not be
    excluded from the number the user is asked about."""
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=0.01, hi=0.02, extra_cost=0.50)
    assert len(asked) == 1


def test_hard_max_cost_still_aborts_under_yes(settings) -> None:
    """`--max-cost` keeps its existing semantics: a ceiling, not a prompt."""
    with pytest.raises(typer.Exit):
        _gate(settings, lo=0.40, hi=0.90, max_cost=0.10, yes=True)


def test_missing_pricing_never_blocks(settings, monkeypatch) -> None:
    asked = []
    monkeypatch.setattr("unread.util.prompt.confirm", lambda *a, **k: asked.append(a) or True)
    _gate(settings, lo=None, hi=None)
    assert asked == []
