"""Native Telegram rich messages for `/format rich`.

Bot API 10.1 (June 2026) added rich messages: hand Telegram GFM markdown
and it renders tables, headings and lists natively, up to 32768 chars.
Before that the only option was Telethon's delimiter markdown — no
headings, no tables — so a fact-check verdict table had to be flattened
into `·`-joined lines, which is what the user saw as "rich table broken".
"""

from __future__ import annotations

from typing import Any

import pytest

REPORT = """## Проверка фактов

| # | Утверждение | Вердикт | Уверенность |
|---|---|---|---|
| 1 | 97% людей бедны | ⚠️ Вводит в заблуждение | Высокая |
| 2 | Основатель amoCRM | ✅ Правда | Высокая |

### 1. «97% людей бедны» — ⚠️ Вводит в заблуждение

- **Сказано:** цифра звучит в начале.
- **Источники:** [Росстат](https://rosstat.gov.ru).
"""


class _FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[Any] = []
        self.fail = fail

    async def get_input_entity(self, chat_id):
        return f"peer:{chat_id}"

    async def __call__(self, request):
        if self.fail:
            raise RuntimeError("RPC error: RICH_MESSAGE_UNSUPPORTED")
        self.requests.append(request)


class _FakeEvent:
    def __init__(self, *, fail: bool = False) -> None:
        self.client = _FakeClient(fail=fail)
        self.chat_id = 4242
        self.id = 77
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))


async def test_report_markdown_reaches_telegram_verbatim() -> None:
    """The whole point: no flattening. Telegram renders the table."""
    from unread.bot.rich import send_rich_markdown

    event = _FakeEvent()
    assert await send_rich_markdown(event, REPORT) is True
    (request,) = event.client.requests
    sent = request.rich_message.markdown
    assert "| # | Утверждение | Вердикт | Уверенность |" in sent
    assert "## Проверка фактов" in sent
    assert "- **Сказано:**" in sent


async def test_the_plain_message_field_is_empty() -> None:
    """Rich content travels in `rich_message`; a non-empty `message`
    alongside it would be a second, duplicate body."""
    from unread.bot.rich import send_rich_markdown

    event = _FakeEvent()
    await send_rich_markdown(event, REPORT)
    assert event.client.requests[0].message == ""


async def test_it_replies_to_the_triggering_message() -> None:
    from unread.bot.rich import send_rich_markdown

    event = _FakeEvent()
    await send_rich_markdown(event, REPORT)
    assert event.client.requests[0].reply_to.reply_to_msg_id == 77


async def test_an_rpc_failure_reports_false_instead_of_raising() -> None:
    """Every deployment target is out of our hands: an older server, a
    client that can't render it, a bot without the capability. The
    caller falls back to the flattened path — losing the report to a
    formatting error would be far worse than losing the formatting."""
    from unread.bot.rich import send_rich_markdown

    event = _FakeEvent(fail=True)
    assert await send_rich_markdown(event, REPORT) is False


async def test_oversized_reports_decline_rather_than_truncate() -> None:
    """32768 is generous but finite. Declining hands the report to the
    splitter that already exists rather than cutting it off."""
    from unread.bot.rich import RICH_LIMIT, send_rich_markdown

    event = _FakeEvent()
    assert await send_rich_markdown(event, "x" * (RICH_LIMIT + 1)) is False
    assert not event.client.requests


async def test_limit_matches_the_documented_one() -> None:
    from unread.bot.rich import RICH_LIMIT

    assert RICH_LIMIT == 32768


def test_unsupported_telethon_is_detected_not_crashed_into() -> None:
    """`InputRichMessageMarkdown` arrived in Telethon 1.44. An older one
    must degrade, not raise ImportError mid-request."""
    from unread.bot import rich

    assert isinstance(rich.rich_supported(), bool)


async def test_declines_when_telethon_is_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    from unread.bot import rich

    monkeypatch.setattr(rich, "rich_supported", lambda: False)
    event = _FakeEvent()
    assert await rich.send_rich_markdown(event, REPORT) is False
    assert not event.client.requests


# --- wiring into the report reply -------------------------------------------


async def test_send_rich_prefers_the_native_path() -> None:
    """When Telegram will render it, nothing gets flattened."""
    from unread.bot.reply import _send_rich

    event = _FakeEvent()
    await _send_rich(event, md_text=REPORT, caption="✓ 12s | 1k | $0.01")
    (request,) = event.client.requests
    assert "| # | Утверждение |" in request.rich_message.markdown
    # No flattened body went out as a normal reply — only the caption.
    assert all("Вердикт" not in text for text, _ in event.replies)


async def test_send_rich_falls_back_when_the_native_path_declines() -> None:
    from unread.bot.reply import _send_rich

    event = _FakeEvent(fail=True)
    await _send_rich(event, md_text=REPORT, caption="✓ 12s")
    body = "\n".join(text for text, _ in event.replies)
    assert "97% людей бедны" in body, "report lost when rich formatting failed"
    assert "⚠️ Вводит в заблуждение" in body
    assert "|---" not in body, "unflattened table reached the md path"


async def test_the_caption_uses_a_delimiter_telethon_understands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telethon's markdown italic is `__text__`. Single underscores are
    not a delimiter at all, so `_(1/4)_` reached the chat verbatim."""
    from telethon.extensions import markdown

    from unread.bot import reply

    monkeypatch.setattr(reply, "_send_rich_native", _decline)
    event = _FakeEvent()
    await reply._send_rich(event, md_text="body", caption="✓ 12s | $0.01")
    caption_text = event.replies[-1][0]
    _, entities = markdown.parse(caption_text)
    assert entities, f"caption has no formatting entity: {caption_text!r}"
    assert "_" not in markdown.parse(caption_text)[0]


async def test_the_part_counter_is_not_shown_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from telethon.extensions import markdown

    from unread.bot import reply

    monkeypatch.setattr(reply, "_send_rich_native", _decline)
    monkeypatch.setattr(reply, "telegram_message_limit", lambda _client: 300)
    event = _FakeEvent()
    await reply._send_rich(event, md_text="para\n\n" * 120, caption="c")
    counters = [t for t, _ in event.replies if "/" in t and "(" in t]
    assert counters, "no part counter emitted"
    for text in counters:
        clean, _ = markdown.parse(text)
        assert "_(" not in clean, f"literal underscores shown: {clean!r}"


async def _decline(event, markdown_text):
    return False
