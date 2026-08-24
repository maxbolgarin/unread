"""The saved report header needs real line breaks.

Rows were joined with a single "\\n", which CommonMark treats as a SOFT
break and collapses to a space — so the whole metadata block rendered as
one run-on paragraph in the PDF, and in any other markdown viewer.
"""

from __future__ import annotations

from unread.util.report_render import render_md_header

ROWS = [
    ("**Chat:**", "Биолог МГУ"),
    ("**Period:**", "unread / full history"),
    ("**Messages analyzed:**", "72"),
    ("**Preset:**", "`factcheck` (v=v1)"),
]


def test_each_row_ends_with_a_hard_break() -> None:
    out = render_md_header(ROWS)
    body = [ln for ln in out.split("\n") if ln and ln != "---"]
    # Every row but the last must carry markdown's two-space hard break.
    for line in body[:-1]:
        assert line.endswith("  "), f"soft break collapses this row: {line!r}"


def test_rendered_html_puts_rows_on_separate_lines() -> None:
    """The actual failure, reproduced through the same parser the PDF
    worker uses."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(["table"])
    html = md.render(render_md_header(ROWS))
    assert html.count("<br") >= len(ROWS) - 1, html


def test_values_are_preserved() -> None:
    out = render_md_header(ROWS)
    for label, value in ROWS:
        assert label in out
        assert value in out


def test_empty_rows_render_without_crashing() -> None:
    assert render_md_header([]).strip()


def test_header_is_not_parsed_as_a_heading() -> None:
    """A `---` on the line after text is a SETEXT heading underline, not a
    rule — so the closing delimiter turned the entire metadata block into
    one giant `<h2>`. That's why the header rendered oversized in the PDF,
    which the `<br>` check alone didn't catch."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(["table"])
    html = md.render(render_md_header(ROWS))
    assert "<h1" not in html and "<h2" not in html, html


def test_header_still_has_its_rules() -> None:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(["table"])
    html = md.render(render_md_header(ROWS))
    assert html.count("<hr") >= 2, html
