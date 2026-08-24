"""Ordering of the YouTube caption-language picker.

Tracks used to be ordered by ISO code while the rows show display names,
so "Chinese" (zh) sorted last and "German" (de) near the top — the list
looked unsorted to anyone reading it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from unread.youtube.commands import _ordered_display_tracks
from unread.youtube.metadata import YoutubeMetadata


def _meta(langs: dict[str, bool]) -> YoutubeMetadata:
    """`{lang: is_auto}` → metadata with those caption tracks."""
    subs = {k: [{}] for k, auto in langs.items() if not auto}
    autos = {k: [{}] for k, auto in langs.items() if auto}
    return YoutubeMetadata(
        video_id="v",
        url="https://youtu.be/v",
        subtitles=subs or None,
        automatic_captions=autos or None,
    )


def _bases(meta) -> list[str]:
    return [t.base for t in _ordered_display_tracks(meta)]


def test_english_and_russian_come_first_in_that_order() -> None:
    meta = _meta({"de": False, "ru": False, "en": False, "fr": False})
    assert _bases(meta)[:2] == ["en", "ru"]


def test_remaining_languages_are_sorted_by_display_name() -> None:
    """By code this would be de, fr, zh; by NAME it's Chinese, French,
    German — which is what the rows actually read."""
    meta = _meta({"de": False, "fr": False, "zh": False})
    assert _bases(meta) == ["zh", "fr", "de"]


def test_pinned_languages_are_skipped_when_absent() -> None:
    # German, then Spanish — by name, which here matches code order too.
    meta = _meta({"de": False, "es": False})
    assert _bases(meta) == ["de", "es"]


def test_a_single_pinned_language_still_leads() -> None:
    meta = _meta({"de": False, "ru": False})
    assert _bases(meta) == ["ru", "de"]


def test_english_leads_even_when_it_is_auto_generated() -> None:
    meta = _meta({"de": False, "en": True})
    assert _bases(meta)[0] == "en"


def test_no_tracks_is_empty() -> None:
    assert _ordered_display_tracks(_meta({})) == []


# --- the picker's default row -------------------------------------------------


async def test_picker_highlights_the_first_row() -> None:
    """The cursor starts on the first row rather than on whatever the
    configured locale preference happens to be."""
    from unread.youtube.commands import _interactive_pick_caption_lang

    captured: dict[str, Any] = {}

    def _fake_select(_prompt, *, choices, default_value=None):
        captured["default"] = default_value
        captured["first"] = choices[0].value
        return choices[0].value

    meta = _meta({"de": False, "ru": False, "en": False})
    with patch("unread.util.prompt.select", _fake_select):
        await _interactive_pick_caption_lang(meta, preselect=["de"])

    assert captured["default"] == captured["first"]
    assert captured["first"].startswith("en")


@pytest.mark.parametrize("langs", [{"en": False, "ru": False}, {"fr": False, "zh": False}])
async def test_picker_rows_follow_the_ordering_helper(langs) -> None:
    from unread.youtube.commands import _interactive_pick_caption_lang

    captured: dict[str, Any] = {}

    def _fake_select(_prompt, *, choices, default_value=None):
        captured["values"] = [c.value for c in choices if getattr(c, "value", None)]
        return choices[0].value

    meta = _meta(langs)
    with patch("unread.util.prompt.select", _fake_select):
        await _interactive_pick_caption_lang(meta, preselect=[])

    expected = [t.lang for t in _ordered_display_tracks(meta)]
    assert captured["values"][: len(expected)] == expected
