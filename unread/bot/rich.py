"""Native Telegram rich messages — `messages.sendMessage(rich_message=…)`.

Bot API 10.1 (June 2026) let bots hand Telegram GFM markdown and have it
rendered natively: real tables, headings, lists, blockquotes, collapsible
`<details>`, up to 32768 characters. Until then the only formatting a bot
could send was Telethon's delimiter markdown — bold, italic, code, links
and nothing else — so a report had to be flattened by
`unread/bot/tg_markdown.py` before sending, which turned a fact-check
verdict table into a column of `·`-joined lines.

This module is the fast path for `/format rich`: send the report exactly
as written. `unread/bot/reply.py` keeps the flattened path as the
fallback, because none of the preconditions are ours to guarantee —
the server, the bot's own capabilities and the reader's client version
all have to cooperate, and a formatting failure must never cost the user
the report itself.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Documented ceiling for one rich message, in UTF-8 characters. Eight
# times the 4096 of a plain message, so a report that used to arrive as
# "(1/4)…(4/4)" now lands in one piece.
RICH_LIMIT = 32768


def rich_supported() -> bool:
    """True when the installed Telethon can build a rich message.

    `InputRichMessageMarkdown` and `SendMessageRequest.rich_message`
    landed in Telethon 1.44. `pyproject.toml` asks for that, but a bot
    installed over an older resolved environment must degrade to the
    flattened path instead of raising ImportError mid-request.
    """
    try:
        from telethon.tl.functions.messages import SendMessageRequest
        from telethon.tl.types import InputRichMessageMarkdown  # noqa: F401
    except ImportError:
        return False
    return "rich_message" in getattr(SendMessageRequest.__init__, "__annotations__", {})


async def send_rich_markdown(event: Any, markdown: str) -> bool:
    """Send `markdown` as one native rich message. False if we couldn't.

    Returns False rather than raising for every reason a caller might
    need to fall back on: Telethon too old, text over the limit, or the
    server rejecting the request. A True return means Telegram accepted
    it — how a given client chooses to draw it is out of our hands.
    """
    if not rich_supported():
        return False
    if not markdown.strip():
        return False
    if len(markdown) > RICH_LIMIT:
        # Splitting here would cut a table off from its header row, and
        # the flattened path already knows how to split. Hand it over.
        log.info("bot.rich_too_long", chars=len(markdown), limit=RICH_LIMIT)
        return False

    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputReplyToMessage, InputRichMessageMarkdown

    client = getattr(event, "client", None)
    if client is None:
        return False

    try:
        peer = await client.get_input_entity(event.chat_id)
        reply_to = None
        msg_id = getattr(event, "id", None)
        if msg_id:
            reply_to = InputReplyToMessage(reply_to_msg_id=msg_id)
        await client(
            SendMessageRequest(
                peer=peer,
                # The body travels in `rich_message`; anything here
                # would arrive as a second copy above it.
                message="",
                reply_to=reply_to,
                rich_message=InputRichMessageMarkdown(markdown=markdown),
            )
        )
    except Exception:
        log.warning("bot.rich_send_failed", exc_info=True)
        return False
    return True
