"""Markdown → Telegram conversion for `/format rich`.

Telethon's `md` parse mode is MarkdownV1-ish: bold, italic, code, links.
It has NO headings, NO horizontal rules and NO tables — so sending a
report body verbatim showed literal `---`, `## TL;DR` and pipe-delimited
table rows to the user.
"""

from __future__ import annotations

import pytest

from unread.bot.tg_markdown import to_telegram_markdown


def test_headings_become_bold_lines() -> None:
    out = to_telegram_markdown("## TL;DR\n\nbody")
    assert "##" not in out
    assert "**TL;DR**" in out


@pytest.mark.parametrize("level", ["#", "##", "###", "####"])
def test_every_heading_level_is_converted(level) -> None:
    out = to_telegram_markdown(f"{level} Title\n\nbody")
    assert "#" not in out


def test_horizontal_rules_are_dropped() -> None:
    out = to_telegram_markdown("---\n\n**Chat:** x\n\n---\n\nbody")
    assert "---" not in out
    assert "**Chat:** x" in out


def test_tables_become_readable_lines() -> None:
    """Telegram can't render a table at all; the pipes leaked through as
    literal text."""
    table = (
        "| # | Claim | Verdict | Confidence |\n"
        "|---|---|---|---|\n"
        "| 1 | Mushrooms kill 4M | ⚠️ Misleading | High |\n"
        "| 2 | WHO listed 19 | ✅ True | High |\n"
    )
    out = to_telegram_markdown(table)
    assert "|---" not in out
    assert "Mushrooms kill 4M" in out
    assert "⚠️ Misleading" in out
    # Each data row on its own line, no leading pipe.
    for line in out.splitlines():
        assert not line.strip().startswith("|")


def test_a_verdict_gets_its_own_line() -> None:
    """A four-column row joined with `·` ran to three wrapped lines of
    prose, and the verdict — the one thing being looked up — landed in
    the middle of it. Claim on top, verdict under it."""
    table = (
        "| # | Claim | Verdict | Confidence |\n"
        "|---|---|---|---|\n"
        "| 1 | Something long enough to wrap | ✅ True | High |\n"
    )
    out = to_telegram_markdown(table)
    claim_line = next(ln for ln in out.splitlines() if "Something long" in ln)
    assert "True" not in claim_line, claim_line
    verdict_line = next(ln for ln in out.splitlines() if "True" in ln)
    assert "High" in verdict_line


def test_a_two_column_table_stays_on_one_line() -> None:
    """Nothing to separate — a second line would just be padding."""
    table = "| Key | Value |\n|---|---|\n| Model | luna |\n"
    out = to_telegram_markdown(table)
    row = next(ln for ln in out.splitlines() if "Model" in ln)
    assert "luna" in row


def test_list_markers_become_bullets() -> None:
    """Telegram renders no lists, so a literal `-` is what the reader
    sees. A bullet at least looks deliberate."""
    out = to_telegram_markdown("- one\n* two\n+ three")
    for line in out.splitlines():
        assert not line.startswith(("- ", "* ", "+ ")), line
    assert out.count("•") == 3


def test_nested_list_indentation_survives() -> None:
    out = to_telegram_markdown("- top\n  - nested")
    nested = next(ln for ln in out.splitlines() if "nested" in ln)
    assert nested.startswith("  ")


def test_numbered_lists_keep_their_numbers() -> None:
    out = to_telegram_markdown("1. first\n2. second")
    assert "1. first" in out and "2. second" in out


def test_bullets_are_preserved() -> None:
    out = to_telegram_markdown("- one\n- two")
    assert "one" in out and "two" in out


def test_inline_formatting_survives() -> None:
    out = to_telegram_markdown("**bold** and `code` and [link](https://x.com)")
    assert "**bold**" in out
    assert "`code`" in out
    assert "[link](https://x.com)" in out


def test_blockquotes_survive_as_text() -> None:
    out = to_telegram_markdown("> quoted line")
    assert "quoted line" in out


def test_empty_input() -> None:
    assert to_telegram_markdown("") == ""


def test_no_runaway_blank_lines() -> None:
    out = to_telegram_markdown("---\n\n## A\n\n---\n\n## B\n\ntext")
    assert "\n\n\n" not in out


async def test_rich_send_flattens_the_report() -> None:
    """The converter has to be WIRED, not merely available — the whole bug
    was that `_send_rich` posted the body verbatim."""
    from unread.bot.reply import _send_rich

    sent: list[str] = []

    class _Event:
        chat_id = 7
        client = None

        async def reply(self, text, **_kw):
            sent.append(text)

    body = (
        "---\n\n**Chat:** X  \n\n---\n\n## TL;DR\n\nSome text\n\n"
        "| # | Claim | Verdict |\n|---|---|---|\n| 1 | A claim | ✅ True |\n"
    )
    await _send_rich(_Event(), md_text=body, caption="✓ 3s")
    joined = "\n".join(sent)
    assert "##" not in joined
    assert "|---" not in joined
    assert "---\n" not in joined
    assert "A claim" in joined
    assert "✅ True" in joined
