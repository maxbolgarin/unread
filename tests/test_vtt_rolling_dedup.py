"""YouTube auto-captions repeat themselves; the transcript must not.

Auto-captions scroll: cue N+1 re-emits the last line of cue N and adds a
new one. Joining cues verbatim repeats every line 2-3 times, which
inflates the transcript, the token bill, and the noise the model reads.
Whole-cue dedup can't catch it — the JOINED bodies differ.
"""

from __future__ import annotations

from unread.youtube.transcript import _parse_vtt_timed

ROLLING = """WEBVTT

00:00:01.000 --> 00:00:03.000
Подождите, мы не можем начать смертную

00:00:03.000 --> 00:00:05.000
Подождите, мы не можем начать смертную
[музыка] казнь нидееспособным людям,

00:00:05.000 --> 00:00:07.000
[музыка] казнь нидееспособным людям,
которые не совсем отдают отчёт, что они

00:00:07.000 --> 00:00:09.000
которые не совсем отдают отчёт, что они
делают. Они же подростки.
"""


def _text(vtt: str) -> str:
    return " ".join(t for _, t in _parse_vtt_timed(vtt))


def test_no_line_is_emitted_twice() -> None:
    text = _text(ROLLING)
    for phrase in (
        "Подождите, мы не можем начать смертную",
        "[музыка] казнь нидееспособным людям,",
        "которые не совсем отдают отчёт, что они",
    ):
        assert text.count(phrase) == 1, f"{phrase!r} appears {text.count(phrase)}x"


def test_all_content_survives_in_order() -> None:
    text = _text(ROLLING)
    positions = [
        text.index("Подождите"),
        text.index("казнь"),
        text.index("которые"),
        text.index("подростки"),
    ]
    assert positions == sorted(positions), text


def test_dedup_roughly_halves_a_rolling_transcript() -> None:
    """The point is the size: this is what the token bill is paid on."""
    text = _text(ROLLING)
    raw = sum(len(line) for line in ROLLING.splitlines() if line and "-->" not in line and line != "WEBVTT")
    assert len(text) < raw * 0.65, f"{len(text)} vs raw {raw}"


def test_non_rolling_captions_are_untouched() -> None:
    """Manual subtitles have no overlap — nothing may be dropped."""
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
First distinct line.

00:00:03.000 --> 00:00:05.000
Second distinct line.

00:00:05.000 --> 00:00:07.000
Third distinct line.
"""
    text = _text(vtt)
    for phrase in ("First distinct line.", "Second distinct line.", "Third distinct line."):
        assert phrase in text


def test_a_genuine_repeat_far_apart_is_kept() -> None:
    """A speaker really can say the same sentence twice. Only the rolling
    window is suppressed, not repetition in general."""
    filler = "".join(
        f"\n00:00:{10 + i:02d}.000 --> 00:00:{11 + i:02d}.000\nfiller line {i}\n" for i in range(8)
    )
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nRepeated sentence.\n"
        + filler
        + "\n00:00:30.000 --> 00:00:32.000\nRepeated sentence.\n"
    )
    assert _text(vtt).count("Repeated sentence.") == 2


def test_timestamps_track_the_kept_line() -> None:
    """Citations jump to a moment, so the surviving line must carry the
    cue it was FIRST said in."""
    entries = _parse_vtt_timed(ROLLING)
    starts = [s for s, _ in entries]
    assert starts == sorted(starts)
    assert entries[0][0] == 1
