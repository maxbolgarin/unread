"""Subprocess worker for PDF rendering.

Run as ``python -m unread.bot._pdf_worker``. Reads a JSON payload
{"md": ..., "title": ..., "css": ...} from stdin, writes PDF bytes
to stdout, status to stderr.

Isolating the WeasyPrint + Cairo/Pango ctypes call into a subprocess
means a renderer segfault (a known hazard on Apple Silicon when the
WeasyPrint loader can't find the right Pango build) kills only this
worker — the parent bot catches the non-zero exit code and falls
back to a `.md` upload instead of crashing the whole process.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Class applied to every emoji run in the rendered HTML. The stylesheet
# in `unread/bot/pdf.py` gives ONLY this class an emoji font; the body
# stack has none. `tests/test_pdf_fonts.py` pins the two together.
EMOJI_CLASS = "emoji"

# Pictographs, Misc Symbols + Dingbats (✅ ❌ ⚠ ❓ all live here), and
# Misc Symbols & Arrows — followed by any number of the joiners that
# belong to the same character: variation selectors, ZWJ, the combining
# keycap, and skin-tone modifiers. Splitting a base from its selector
# would hand the two halves to different fonts.
_EMOJI_RE = re.compile(
    "(?:"
    "[\\U0001f000-\\U0001faff\\u2600-\\u27bf\\u2b00-\\u2bff]"  # base pictograph
    "[\\ufe0e\\ufe0f\\u200d\\u20e3\\U0001f3fb-\\U0001f3ff]*"  # selectors, ZWJ, keycap, tones
    ")+"
)

# Verdict icons get a colour class on top of the emoji class. Symbola
# draws them as hairline monochrome outlines — correct, but at 11pt a
# ✅ and a ❌ look alike at a glance, which is the one thing a verdict
# column must never do. Colour is decoration only: every preset writes
# the verdict word next to the icon, so a greyscale print still reads.
_EMOJI_TONE = {
    "\u2705": "ok",  # ✅ True
    "\u274c": "bad",  # ❌ False
    "\u26a0": "warn",  # ⚠ Misleading
    "\U0001f3ad": "spin",  # 🎭 Manipulated
    "\u2753": "unk",  # ❓ Unverifiable
}

_TAG_RE = re.compile(r"<[^>]*>")

# Rewriting inside these would corrupt the code the user asked to see.
_VERBATIM_TAGS = frozenset({"pre", "code"})


def wrap_emoji_spans(html: str) -> str:
    """Put every emoji run in its own `<span class="emoji">`.

    Emoji fonts cover far more than emoji. Noto Color Emoji's cmap
    includes the ASCII digits — they are the keycap bases for 0️⃣-9️⃣ —
    so naming it anywhere in the body font stack lets fontconfig hand it
    the digits, and its CBDT colour bitmaps are glyphs WeasyPrint cannot
    embed in a PDF. 1.5.1 shipped exactly that: every number in the
    report came out as blank space.

    Isolating emoji in a span means the text stack never has to name an
    emoji family at all, so no fallback ordering can put one in front of
    a digit. Only text nodes are touched — tags, attributes and the
    contents of `<pre>` / `<code>` are passed through untouched.
    """

    def _classes(run: str) -> str:
        tone = _EMOJI_TONE.get(run[0], "")
        return f"{EMOJI_CLASS} {EMOJI_CLASS}-{tone}" if tone else EMOJI_CLASS

    def _wrap(text: str) -> str:
        return _EMOJI_RE.sub(lambda m: f'<span class="{_classes(m.group(0))}">{m.group(0)}</span>', text)

    out: list[str] = []
    pos = 0
    verbatim = 0
    for tag_match in _TAG_RE.finditer(html):
        chunk = html[pos : tag_match.start()]
        out.append(chunk if verbatim else _wrap(chunk))
        tag = tag_match.group(0)
        out.append(tag)
        name = tag.lstrip("</").split(" ")[0].rstrip(">/").lower()
        if name in _VERBATIM_TAGS:
            if tag.startswith("</"):
                verbatim = max(0, verbatim - 1)
            elif not tag.endswith("/>"):
                verbatim += 1
        pos = tag_match.end()
    tail = html[pos:]
    out.append(tail if verbatim else _wrap(tail))
    return "".join(out)


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        sys.stderr.write(f"pdf_worker: invalid stdin payload: {e}\n")
        return 2

    md_text = payload.get("md", "")
    title = payload.get("title", "Report")
    css = payload.get("css", "")

    try:
        from markdown_it import MarkdownIt
        from weasyprint import CSS, HTML
    except Exception as e:
        sys.stderr.write(f"pdf_worker: dependency import failed: {e}\n")
        return 3

    try:
        md = MarkdownIt("commonmark", {"breaks": False, "html": False}).enable(["table", "strikethrough"])
        html_body = wrap_emoji_spans(md.render(md_text))
        safe_title = title.replace("<", "&lt;").replace(">", "&gt;")
        html_doc = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'>"
            f"<title>{safe_title}</title>"
            "</head><body>"
            f"{html_body}"
            "</body></html>"
        )
        stylesheets = [CSS(string=css)] if css else None
        pdf_bytes = HTML(string=html_doc).write_pdf(stylesheets=stylesheets)
    except Exception as e:
        sys.stderr.write(f"pdf_worker: render failed: {type(e).__name__}: {e}\n")
        return 4

    # Write PDF bytes to stdout (binary). On Windows the buffer would
    # need explicit binary handling, but the bot runs Linux/macOS.
    try:
        sys.stdout.buffer.write(pdf_bytes)
        sys.stdout.buffer.flush()
    except Exception as e:
        sys.stderr.write(f"pdf_worker: stdout write failed: {e}\n")
        return 5
    return 0


if __name__ == "__main__":
    # Force unbuffered stdout for the binary PDF payload.
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(_main())
