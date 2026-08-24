"""PDF font coverage for the verdict emoji.

A fact-check PDF rendered ✅ and ❌ as tofu boxes while ⚠️ came through:
the container installs `libpango` (the shaping engine) but no fonts, and
the CSS stack named only macOS/Windows families. Meaning survived —
the preset pairs every emoji with a word — but the table looked broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_css_falls_back_to_an_emoji_family() -> None:
    from unread.bot.pdf import _PDF_CSS

    lowered = _PDF_CSS.lower()
    assert "emoji" in lowered, "no emoji family in the font stack"


def test_css_keeps_a_linux_text_family() -> None:
    """`-apple-system` and `Segoe UI` don't exist on the Linux box that
    actually renders these."""
    from unread.bot.pdf import _PDF_CSS

    lowered = _PDF_CSS.lower()
    assert "dejavu" in lowered or "liberation" in lowered or "noto sans" in lowered


@pytest.mark.parametrize("package", ["fonts-dejavu-core", "fonts-noto-color-emoji"])
def test_image_installs_the_fonts_it_renders_with(package) -> None:
    """Naming a font in CSS does nothing if the image has no font files."""
    dockerfile = Path("Dockerfile").read_text()
    assert package in dockerfile, f"{package} missing from the image"


def test_verdict_emoji_are_paired_with_words_in_both_presets() -> None:
    """Defence in depth: even with no emoji font anywhere, a reader must
    still be able to tell True from False."""
    from unread.analyzer.prompts import get_presets

    for language, words in (("en", ("True", "False")), ("ru", ("Правда", "Ложь"))):
        system = get_presets(language)["factcheck"].system
        for word in words:
            assert word in system
