"""The `factcheck` preset and its `needs_web_search` frontmatter flag.

Fact-checking is the first preset whose verify pass wants live web access,
so the preset has to be able to say so — the pipeline routes the final
call through a search-enabled provider path only when it does.
"""

from __future__ import annotations

import pytest

from unread.analyzer.prompts import get_presets


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_preset_exists_in_both_languages(language) -> None:
    """Preset language dirs are autonomous — one without the other means
    half the users silently can't fact-check."""
    assert "factcheck" in get_presets(language)


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_preset_requests_web_search(language) -> None:
    assert get_presets(language)["factcheck"].needs_web_search is True


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_preset_maps_then_reduces(language) -> None:
    """Claims are spread across the whole source, so the map phase must
    run over every chunk before the single verify pass."""
    assert get_presets(language)["factcheck"].needs_reduce is True


def test_other_presets_do_not_request_web_search() -> None:
    """Default is off — a summary must never silently start billing for
    web searches."""
    presets = get_presets("en")
    assert [name for name, p in presets.items() if p.needs_web_search] == ["factcheck"]


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_preset_is_visible_in_the_picker(language) -> None:
    assert get_presets(language)["factcheck"].hidden is False


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_preset_has_a_description(language) -> None:
    assert (get_presets(language)["factcheck"].description or "").strip()


@pytest.mark.parametrize("language", ["en", "ru"])
def test_factcheck_prompt_forbids_a_verdict_without_a_source(language) -> None:
    """The whole output makes claims about truth. A verdict the model
    can't source must land in the unverifiable bucket, never a guess."""
    system = get_presets(language)["factcheck"].system.lower()
    assert "unverifi" in system or "не пров" in system
