"""Caption text must be decoded and read as prose, not one cue per line.

WebVTT payloads are HTML-escaped, so YouTube tracks carry `&nbsp;` /
`&amp;` that leaked verbatim into transcripts; and joining cues with
newlines broke sentences at arbitrary points.
"""

from __future__ import annotations

from unread.youtube.cache import _row_to_result
from unread.youtube.transcript import _parse_vtt, _parse_vtt_timed, cues_to_text

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hitler's attempt to capture the Lebensraum he&nbsp; so desperately craved.

00:00:03.000 --> 00:00:05.000
The German High Command

00:00:05.000 --> 00:00:07.000
expected the Soviets to field around&nbsp;300 divisions &amp; more.
"""


def test_entities_decoded_and_spaces_collapsed() -> None:
    cues = _parse_vtt_timed(VTT)
    joined = " ".join(t for _, t in cues)
    assert "&nbsp;" not in joined and "&amp;" not in joined and "\xa0" not in joined
    assert "Lebensraum he so desperately" in joined
    assert "around 300 divisions & more." in joined


def test_plain_text_has_no_mid_sentence_breaks() -> None:
    text = _parse_vtt(VTT)
    assert "\n" not in text
    assert "The German High Command expected the Soviets" in text


def test_long_text_splits_into_paragraphs_at_sentence_ends() -> None:
    cues = [(i, f"Sentence number {i} goes on for a while here.") for i in range(60)]
    paragraphs = cues_to_text(cues).split("\n\n")
    assert len(paragraphs) > 1
    assert all(p.endswith(".") for p in paragraphs)


def test_unpunctuated_text_still_gets_hard_breaks() -> None:
    cues = [(i, "words without any punctuation at all") for i in range(200)]
    assert "\n\n" in cues_to_text(cues)


def test_cached_rows_are_cleaned_on_load() -> None:
    row = {
        "transcript": "he&nbsp; so\nThe German",
        "transcript_source": "captions",
        "transcript_timed_json": '[[1, "he&nbsp; so"], [3, "The German"]]',
    }
    res = _row_to_result(row, duration_sec=None)
    assert res.text == "he so The German"
    assert res.timed_cues == [(1, "he so"), (3, "The German")]
