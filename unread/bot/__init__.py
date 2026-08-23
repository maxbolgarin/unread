"""Self-hosted Telegram bot frontend for `unread`.

The bot is a thin Telegram-side adapter: it forwards files/URLs/YouTube/
forwarded-TG-messages into the existing `cmd_analyze_*` async pipelines
and uploads the resulting Markdown report back as a TG document.

Allowlisted by design: every event whose sender is not in
`settings.bot.owner_ids` is silently dropped. The FIRST id is the primary
owner. To read private Telegram chats (which a bot_token cannot do), the
bot loads the primary owner's already-bootstrapped Telethon user session
— so `t.me/...` links and `/upload_session` are restricted to that one
account, while extra admins get the file / URL / YouTube surface.

See the architecture map in CLAUDE.md ("Bot") for the full layout.
"""

from __future__ import annotations
