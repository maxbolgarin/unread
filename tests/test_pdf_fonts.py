"""PDF font coverage for the verdict emoji.

A fact-check PDF rendered ✅ and ❌ as tofu boxes while ⚠️ came through:
the container installs `libpango` (the shaping engine) but no fonts, and
the CSS stack named only macOS/Windows families. Meaning survived —
the preset pairs every emoji with a word — but the table looked broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def test_body_font_stack_has_no_colour_emoji_family() -> None:
    """A colour-emoji font in the BODY stack blanks every ASCII digit.

    Noto Color Emoji's cmap covers U+0030-U+0039 — they're the keycap
    bases for 0️⃣-9️⃣ — so fontconfig can hand it the digits, and its
    glyphs are CBDT colour bitmaps that WeasyPrint cannot embed in a
    PDF at all. Shipped as 1.5.1: every number in the report rendered
    as blank space, verdict icons included. Emoji belong in their own
    span (see `wrap_emoji_spans`), never in the stack that draws text.
    """
    import re

    from unread.bot.pdf import _PDF_CSS

    css = re.sub(r"/\*.*?\*/", "", _PDF_CSS, flags=re.S)  # comments name it on purpose
    body = re.search(r"(?<!\w)body\s*\{(.*?)\}", css, re.S)
    assert body, css
    stack = body.group(1).lower()
    for colour_font in ("color emoji", "colour emoji", "segoe ui emoji"):
        assert colour_font not in stack, f"{colour_font!r} would steal the digits"


def test_emoji_class_names_a_font_that_renders_in_a_pdf() -> None:
    """Symbola draws ✅ ❌ ⚠ 🎭 ❓ as ordinary outlines, so they survive
    PDF embedding. Noto's monochrome symbol fonts have no ✅/❌ at all,
    and every colour emoji font is unembeddable — verified against the
    font files in the image."""
    import re

    from unread.bot.pdf import _PDF_CSS

    rule = re.search(r"\.emoji\s*\{(.*?)\}", _PDF_CSS, re.S)
    assert rule, "no .emoji rule — emoji have nowhere to go"
    assert "symbola" in rule.group(1).lower()


def test_emoji_span_class_matches_the_stylesheet() -> None:
    """The worker writes the class, the stylesheet targets it. Split
    across two modules, so pin them together."""
    from unread.bot._pdf_worker import EMOJI_CLASS
    from unread.bot.pdf import _PDF_CSS

    assert f".{EMOJI_CLASS}" in _PDF_CSS


def test_css_keeps_a_linux_text_family() -> None:
    """`-apple-system` and `Segoe UI` don't exist on the Linux box that
    actually renders these."""
    from unread.bot.pdf import _PDF_CSS

    lowered = _PDF_CSS.lower()
    assert "dejavu" in lowered or "liberation" in lowered or "noto sans" in lowered


@pytest.mark.parametrize("package", ["fonts-dejavu-core", "fonts-symbola"])
def test_image_installs_the_fonts_it_renders_with(package) -> None:
    """Naming a font in CSS does nothing if the image has no font files."""
    dockerfile = Path("Dockerfile").read_text()
    assert package in dockerfile, f"{package} missing from the image"


def test_image_does_not_install_an_unembeddable_colour_emoji_font() -> None:
    """Installing it is what let fontconfig pick it for the digits.
    Nothing in the PDF path can use a CBDT font, so it has no business
    being on the box."""
    dockerfile = Path("Dockerfile").read_text()
    assert "fonts-noto-color-emoji" not in dockerfile


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


# --- emoji isolation ----------------------------------------------------------


def test_digits_stay_out_of_the_emoji_span() -> None:
    """The 1.5.1 bug in one assertion: a digit that lands in the emoji
    font is a digit the reader never sees."""
    from unread.bot._pdf_worker import EMOJI_CLASS, wrap_emoji_spans

    html = wrap_emoji_spans("<p>✅ 60% людей, $0.42 в 2024 году</p>")
    for span in re.findall(rf'<span class="{EMOJI_CLASS}">(.*?)</span>', html):
        assert not any(ch.isdigit() for ch in span), span
    assert "60" in html and "0.42" in html and "2024" in html


def test_emoji_are_wrapped_in_a_span() -> None:
    from unread.bot._pdf_worker import EMOJI_CLASS, wrap_emoji_spans

    for ch in ("✅", "❌", "⚠", "🎭", "❓"):
        html = wrap_emoji_spans(f"<p>{ch} x</p>")
        assert re.search(rf'<span class="{EMOJI_CLASS}[^"]*">{ch}</span>', html), html


def test_a_variation_selector_travels_with_its_base() -> None:
    """⚠️ is U+26A0 U+FE0F. Leaving the selector outside the span splits
    one character across two fonts."""
    from unread.bot._pdf_worker import EMOJI_CLASS, wrap_emoji_spans

    html = wrap_emoji_spans("<p>⚠️</p>")
    assert re.search(rf'<span class="{EMOJI_CLASS}[^"]*">⚠️</span>', html), html


def test_tags_and_attributes_are_never_rewritten() -> None:
    """Wrapping a span into an href would produce a broken link."""
    from unread.bot._pdf_worker import wrap_emoji_spans

    html = '<p><a href="https://example.com/a?b=1">✅ ok</a></p>'
    assert '<a href="https://example.com/a?b=1">' in wrap_emoji_spans(html)


def test_code_blocks_are_left_verbatim() -> None:
    from unread.bot._pdf_worker import wrap_emoji_spans

    assert wrap_emoji_spans("<pre><code>✅ 1</code></pre>") == "<pre><code>✅ 1</code></pre>"


def test_plain_text_is_untouched() -> None:
    from unread.bot._pdf_worker import wrap_emoji_spans

    html = "<p>Обычный текст без иконок — 42 штуки.</p>"
    assert wrap_emoji_spans(html) == html


def test_verdict_icons_get_a_colour_class() -> None:
    """Symbola draws ✅ and ❌ as similar hairline outlines. A verdict
    column where True and False look alike is the one thing it must not
    be — so the icons carry a colour class. Decoration only: the verdict
    word sits next to every icon."""
    from unread.bot._pdf_worker import EMOJI_CLASS, wrap_emoji_spans
    from unread.bot.pdf import _PDF_CSS

    seen = set()
    for icon in ("✅", "❌", "⚠️", "🎭", "❓"):
        html = wrap_emoji_spans(f"<td>{icon} x</td>")
        m = re.search(rf'class="{EMOJI_CLASS} ({EMOJI_CLASS}-\w+)"', html)
        assert m, f"{icon} got no colour class: {html}"
        seen.add(m.group(1))
        assert f".{m.group(1)}" in _PDF_CSS, f"{m.group(1)} has no CSS rule"
    assert len(seen) == 5, f"verdicts share a colour: {seen}"


def test_a_plain_emoji_gets_no_colour_class() -> None:
    from unread.bot._pdf_worker import EMOJI_CLASS, wrap_emoji_spans

    assert f'<span class="{EMOJI_CLASS}">🚀</span>' in wrap_emoji_spans("<p>🚀</p>")
