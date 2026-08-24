"""Convert report Markdown into what Telegram can actually render.

Telethon's `md` parse mode is MarkdownV1-ish. It supports bold, italic,
code, pre and links — and nothing else. Reports are written in full
CommonMark, so sending one verbatim (which `/format rich` did) showed the
user literal `---`, `## TL;DR`, and pipe-delimited table rows.

The conversion is deliberately lossy in one direction only: structure is
flattened into text that reads correctly, never dropped. A verdict table
becomes one line per claim, because a four-column table has no
representation in Telegram at all and the pipes are worse than useless.

Code fences are passed through untouched — they're the one block
construct Telegram does understand, and rewriting their contents would
corrupt the code inside.
"""

from __future__ import annotations

import re

# `## Heading` → bold line. Telegram has no headings.
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")

# `---`, `***`, `___` on their own line. In a report these are the
# metadata-block delimiters; as literal text they're just noise.
_HR_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")

# A table row: leading and trailing pipes optional.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")

# The `|---|:--:|` separator under a table header.
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$")

_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# An unordered list marker at the start of a line, indentation kept.
# Telegram renders no lists at all, so the marker reaches the reader as
# a literal `-`; a bullet at least looks deliberate.
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")


def _split_row(line: str) -> list[str]:
    inner = _TABLE_ROW_RE.match(line)
    raw = inner.group(1) if inner else line
    return [cell.strip() for cell in raw.split("|")]


def _render_table(rows: list[list[str]]) -> list[str]:
    """Render each data row as a small block rather than a grid.

    A chat client can't draw a table, so the columns have to become
    lines. Joining all of them with `·` — the first attempt — produced
    three wrapped lines of prose per row with the verdict buried in the
    middle, which is precisely the column a reader is scanning for.

    So: the leading cell and the subject share the first line, and every
    remaining column goes underneath. Two-column rows have nothing to
    separate and stay on one line. Empty cells are dropped rather than
    leaving stray separators behind.
    """
    if not rows:
        return []
    body = rows[1:] if len(rows) > 1 else rows
    out: list[str] = []
    for cells in body:
        parts = [c for c in cells if c]
        if not parts:
            continue
        if len(parts) == 1:
            out.append(parts[0])
            continue
        if len(parts) == 2:
            out.append(f"**{parts[0]}** — {parts[1]}")
            continue
        out.append(f"**{parts[0]}.** {parts[1]}")
        out.append(" · ".join(parts[2:]))
        out.append("")
    while out and not out[-1]:
        out.pop()
    return out


def to_telegram_markdown(text: str) -> str:
    """Flatten report Markdown into Telegram-safe Markdown.

    Inline formatting (`**bold**`, `` `code` ``, `[link](url)`) is left
    alone — Telegram understands all of it.
    """
    if not text:
        return ""

    out: list[str] = []
    table: list[list[str]] = []
    in_fence = False

    def _flush_table() -> None:
        if table:
            out.extend(_render_table(table))
            table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()

        if _FENCE_RE.match(line):
            _flush_table()
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(raw)
            continue

        if _TABLE_SEP_RE.match(line) and table:
            # Header/body separator — carries no content.
            continue
        if _TABLE_ROW_RE.match(line):
            table.append(_split_row(line))
            continue
        _flush_table()

        if _HR_RE.match(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(2).strip()
            out.append(f"**{title}**" if title else "")
            continue
        out.append(_BULLET_RE.sub(r"\1• ", line))

    _flush_table()

    # Collapse the blank runs left behind by dropped rules and headings.
    collapsed: list[str] = []
    for line in out:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()
