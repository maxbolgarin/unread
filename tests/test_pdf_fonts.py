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


def test_h3_is_visually_distinct_from_body_bold() -> None:
    """A fact-check makes every claim an `h3`, directly above bold
    `**Said:**` labels. At 1.05em it read as just another bold line —
    "no headers", as reported."""
    import re

    from unread.bot.pdf import _PDF_CSS

    block = re.search(r"h3\s*\{(.*?)\}", _PDF_CSS, re.S)
    assert block, _PDF_CSS
    rules = block.group(1)
    size = re.search(r"font-size:\s*([\d.]+)em", rules)
    assert size and float(size.group(1)) > 1.05, rules
    assert "border-left" in rules or "text-transform" in rules


def test_headings_are_not_stranded_at_a_page_break() -> None:
    from unread.bot.pdf import _PDF_CSS

    assert "break-after: avoid" in _PDF_CSS or "page-break-after: avoid" in _PDF_CSS


# --- pagination ---------------------------------------------------------------


def test_paragraphs_and_list_items_set_orphans_and_widows() -> None:
    """A page break left one dangling word ("…быстрее света. В") at the
    foot of a page. Orphans/widows force at least a few lines to travel
    together, so a break lands somewhere readable."""
    import re

    from unread.bot.pdf import _PDF_CSS

    for prop in ("orphans", "widows"):
        m = re.search(rf"{prop}:\s*(\d+)", _PDF_CSS)
        assert m, f"{prop} not set"
        assert int(m.group(1)) >= 2, f"{prop} too low to prevent a fragment"


def test_table_rows_are_not_split_across_pages() -> None:
    """A verdict row cut in half is unreadable — and a table row is short
    enough that moving it whole always fits."""
    from unread.bot.pdf import _PDF_CSS

    assert "tr" in _PDF_CSS
    assert "break-inside: avoid" in _PDF_CSS


def test_long_list_items_are_still_allowed_to_break() -> None:
    """`break-inside: avoid` on `li` would be worse than the problem: a
    multi-page claim can't fit anywhere, so the renderer leaves most of a
    page blank and breaks it anyway."""
    import re

    from unread.bot.pdf import _PDF_CSS

    li_block = re.search(r"(?<!\w)li\s*\{(.*?)\}", _PDF_CSS, re.S)
    assert li_block
    assert "break-inside: avoid" not in li_block.group(1)


def test_page_margins_are_not_wasteful() -> None:
    """The blank band at a page change is the bottom+top margin. Every
    extra centimetre is both a bigger gap and an extra page break to hit."""
    import re

    from unread.bot.pdf import _PDF_CSS

    m = re.search(r"margin:\s*([\d.]+)cm\s+([\d.]+)cm", _PDF_CSS)
    assert m, _PDF_CSS
    vertical = float(m.group(1))
    assert vertical <= 1.3, f"vertical margin {vertical}cm"
