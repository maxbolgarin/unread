# Telegram bot

← Back to [README](../README.md)

`unread bot run` is the same analysis pipeline as the CLI, exposed as
a self-hosted Telegram bot. You message your bot with something; the
bot replies with a Markdown report. The bot is **single-user** — only
one Telegram ID (yours) gets answered, everyone else is silently
dropped.

This page covers the user-facing surface. For end-to-end VM
deployment (GHCR image, docker-compose, session bootstrapping) see
[`bot-vm-deploy.md`](bot-vm-deploy.md).

## What you can send

| Input | Result |
|---|---|
| **File** — PDF, DOCX, audio (`.mp3` / `.m4a` / `.ogg` / `.opus` / `.flac`), video (`.mp4` / `.mov` / `.mkv`), images (`.png` / `.jpg`), Markdown / text / source code | Summarized via the same `unread <path>` pipeline. Audio + video go through Whisper, images through vision. |
| **Web URL** (any HTTP/HTTPS link) | Page is fetched + extracted (trafilatura), summarized with the `website` preset. |
| **YouTube URL** | Captions if the video has them, audio + Whisper if it doesn't. Citations become `t=SECONDS` deep links. |
| **Forwarded Telegram message** | Analyzes the forwarded content; if forwarded from a channel, offers a picker to also pull a day/week/month of that channel. |
| **`t.me/<chat>/<msg>` link** | Pulls the chat and analyzes. **Requires a Telegram user session installed via `/upload_session`** — the bot token alone can't read user chats. |
| **`@channel` ref** | Same as the `t.me/` form. Needs `/upload_session`. |

## Common use cases

A few of the moments where forwarding to the bot beats anything else:

- **Long voice message** — a friend sends a 12-minute voice. Forward it; the bot replies with a TL;DR in roughly the time it takes to put your phone down.
- **Podcast / lecture video** — drop a `.mp4` or YouTube URL, get the talk's main points without watching.
- **Recorded meeting** — `.mp4` from Zoom / Meet / Teams. The audio track is extracted, transcribed, summarized.
- **Suspicious link** — that "you have to see this" URL from a stranger. Forward it; the bot fetches, summarizes, tells you what it actually says without you clicking.
- **PDF you'd rather not read** — contract, paper, manual. Same drop-in.
- **Channel preview** — paste a `t.me/<channel>/<msg>` link and the bot summarizes the channel's last day / week / month before you decide whether to subscribe.

## Reply format

For each input, the bot:

1. Sends an inline **TL;DR** as a text message (the report's first section, lifted out for at-a-glance reading).
2. Attaches the **full Markdown report** as a `.md` document with a one-line caption:
   ```
   ✓ 23.4s | 1842↓ + 612↑ tok (1280 cached) | $0.0041
   ```
   Format: `✓ <elapsed>s | <prompt>↓ + <completion>↑ tok [(<cached>)] | $<cost>`. Cached-token count is shown only if the provider returned one; cost line is dropped when it's zero (e.g. a fully cache-hit re-run).

Citations in the report follow the same shape as the CLI: `[#42](t.me/…)` for Telegram, `[#754](youtube.com/watch?v=…&t=754s)` for YouTube, `[#7](file://…)` for local files, paragraph indices for web pages. See [`sources.md`](sources.md) for the full citation matrix.

## The confirm panel

When the bot receives something to analyze, it doesn't run immediately — it sends a small inline panel with a **▶ Run** button:

```
🌐 Web page: https://example.com/article
Preset: `website`
[▶ Run]
```

Tap **Run** and the analysis starts. The panel is there so an accidentally-tapped Telegram link doesn't silently spend money. Per-run tuning happens through slash commands (`/preset`, `/lang`, `/enrich`, `/window`) and is **sticky** — set once, applies to every subsequent run in the same chat.

**YouTube links get three buttons**, because "summarize this", "give me the words" and "is any of this true?" are different jobs with very different costs:

```
🎬 YouTube: https://youtu.be/dQw4w9WgXcQ
What do you want?
[▶ Analyze]  [📝 Transcript]
[🔎 Fact-check]
```

- **▶ Analyze** — the normal pipeline: the `video` preset over the timestamped transcript, report as PDF/`.md`.
- **📝 Transcript** — no analysis at all. The bot writes `transcript.md` (metadata header + plain transcript text) and uploads it as a Markdown file. No LLM call, so the only possible cost is Whisper, and only when the video has no usable captions.
- **🔎 Fact-check** — pulls the checkable claims out of the video and verifies them against the web, then replies with a verdict table plus per-claim detail and source links. The most expensive of the three: flagship model *and* a per-search fee. See [Fact-checking](sources.md#fact-checking) for how it behaves on providers without web search, and why the cost caption understates this one.

Fact-check isn't YouTube-only — `/preset factcheck` makes it the default for everything you send in that chat, articles and forwarded posts included.

**Forwarded messages get a richer picker.** If you forward a message from a channel, the panel asks what to analyze:

- *This message / image / caption* — just the forwarded content
- *From this msg in channel* — open the source channel from the forwarded message as the start anchor
- *Channel · day* / *week* / *month* — pull a time-window of the source channel and summarize

**To skip the panel**, send `/confirm off` once. The bot will then run analyses immediately on receipt (the default before this gate was added). `/confirm on` puts it back.

## Slash commands

| Command | Effect |
|---|---|
| `/help` | Show the input list + this command list. |
| `/ping` | Health check — reply `pong`. |
| `/settings` | Show current sticky settings (preset, language, enrich, window) + their defaults. |
| `/preset <name>` | Sticky preset for this chat (e.g. `/preset digest`). Bare `/preset` clears the override. Names match the CLI: `summary`, `tldr`, `digest`, `highlights`, `quotes`, `links`, `action_items`, `decisions`, `questions`, `reactions`, `factcheck`, `video`, `website`. |
| `/lang <code>` | Sticky report language (e.g. `/lang en`, `/lang ru`). Bare clears. |
| `/enrich <list\|all\|none>` | Sticky extra enrichments for Telegram chat analyses. `/enrich image,link` turns those two on; `/enrich all` enables every kind; `/enrich none` strips even the defaults. |
| `/window <day\|week\|month\|msg\|from_msg\|none>` | Sticky default time window for TG-chat analyses. |
| `/format <pdf\|md\|rich>` | How reports come back. `pdf` — rendered document, best on phones (default). `md` — the raw Markdown file. `rich` — the report as Telegram messages, nothing to download; long reports are split at the server's message limit, never mid-heading. Bare `/format` restores the default. |
| `/settings` | Show this chat's settings, and change the AI provider, model and API key from an inline menu. Model rows show prices so you don't land on a flagship by accident. |
| `/stop` | Cancel the run in progress in this chat. Only yours — another admin's run is untouched. |
| `/confirm on\|off` | Toggle the pre-run confirm panel (default: on). |
| `/upload_session` | Install your Telegram user session (one-time). The bot waits for you to send `~/.unread/storage/session.sqlite` as a Telegram document. |
| `/cancel` | Drop any pending `/upload_session` state. |

Sticky settings are **per chat and persistent** — stored in `data.sqlite::bot_chat_settings` and restored at startup, so a `docker compose up` doesn't reset them. Each admin has their own 1:1 chat with the bot, so each admin has their own settings.

### Language

`/lang` is the one to know about in a multi-admin bot:

```
you:      /lang ru      → your analyses, report headings and transcripts in Russian
admin #2: /lang en      → theirs in English
```

It sets everything language-related for that admin: the language the LLM writes in, the report's own `## Sources` / `## Verification` headings and metadata table, and which caption track a YouTube transcript prefers. `/lang none` clears it back to the bot's config default.

Transcripts fall back rather than fail: ask for English on a video with no English captions and you get the captions that exist, with a `⚠️` line at the top of the transcript saying which language it actually is. The *analysis* is still written in your language — only the source text falls back.

Transcripts are cached per requested language, so you asking for Russian and admin #2 asking for English don't evict each other, and neither pays to re-transcribe what the other already fetched.

## Who can use the bot

The bot answers an explicit allowlist of Telegram IDs — by default just you.

```
UNREAD_BOT_OWNER_ID=111222333              # solo
UNREAD_BOT_OWNER_ID=111222333,444555666    # you + one more admin
```

- **At startup**, the bot probes `~/.unread/storage/session.sqlite` (the user session). If an authorized session is there, its account becomes the **primary owner**. Any ids from `UNREAD_BOT_OWNER_ID` stay on as extra admins.
- If there's no session AND no `UNREAD_BOT_OWNER_ID`, the bot refuses to start handling events — there's no safe allowlist.
- After a successful `/upload_session`, the primary owner is re-derived from the just-installed session; the previous primary stays on as an admin.
- Every event is filtered by Telethon's `from_users=` AND a defense-in-depth `sender_id` check inside the handler. Anything else is silently dropped — no acknowledgement, no log noise to the sender.

### Primary owner vs extra admins

Every admin shares **one** Telegram user session — the primary owner's. So a `t.me/...` link sent by an extra admin would read the *primary owner's* chats. Three surfaces are therefore primary-only:

| Surface | Primary owner | Extra admins |
|---|---|---|
| Files, voice, web links, YouTube | ✅ | ✅ |
| `t.me/...` links (chats & channels) | ✅ | ❌ refused |
| Forwarded msg → *analyze the source channel* | ✅ | ❌ refused (analyzing the forward itself is fine) |
| `/upload_session` | ✅ | ❌ refused |

Extra admins get their own sticky settings (`/preset`, `/lang`, …) — `_chat_state` is keyed by chat.

Add someone only if you're fine with them spending your API budget. They can't read your Telegram, but every analysis they run bills your key.

`/upload_session` is gated by the bootstrap allowlist so a fresh deploy with `UNREAD_BOT_OWNER_ID` set (but no session yet) lets only you upload the session; nobody else can install themselves as the bot's owner.

### Changing provider, model and keys from the bot

A container has its own `~/.unread`, so `unread settings` on your laptop
does **not** reach a deployed bot. `/settings` gives you an inline menu:

- **🔀 Provider** — openai / openrouter / anthropic / google / local. Applies
  immediately, no restart, and is persisted so it survives one.
- **🧠 Model** — the chat-capable models for that provider, with prices per
  1M tokens. **preset default** hands control back to each preset's pin,
  which is the right choice unless you have a reason.
- **🔑 API key** — the bot asks for the key as your next message, stores it
  in the secrets DB, and **deletes your message**. The confirmation shows
  only a masked tail.

On the key: deleting the message removes it from the chat history, but it
still travelled through Telegram's servers to get there. If that's not
acceptable for your threat model, set the key in `.env.bot` on the host
instead — the bot never asks for one you've already provided that way.
Only the **primary owner** can rotate keys; credentials are bot-wide, so a
second admin changing them would silently change everyone's runs.

## Privacy & data flow

The bot machine holds the same `~/.unread/` directory the CLI would:
SQLite cache, generated reports, your API keys, your Telegram user
session (after `/upload_session`). API calls go to your chosen
provider (OpenAI, Anthropic, Gemini, OpenRouter, or a local
OpenAI-compatible endpoint). The only network endpoints are Telegram
servers, your AI provider, and any URLs you point the bot at.

If you self-host on a VM, treat the disk like the CLI's disk: snapshot
`~/.unread/storage/`, encrypt at rest, restrict access. See
[`security.md`](security.md) for the credential-storage options
(`keystore`, passphrase-derived `pass`) the CLI also supports — they
all work for the bot.

## Running it

**Locally** (foreground, useful for first-time setup and testing):

```bash
cp .env.bot.example .env.bot
# Edit .env.bot — at minimum:
#   UNREAD_BOT_TOKEN=...            (from @BotFather)
#   UNREAD_BOT_OWNER_ID=...         (your Telegram numeric ID, or a comma-separated list; optional if a session is already mounted)
#   OPENAI_API_KEY=...              (or whichever provider you configured)
unread bot run
```

**On a server with docker-compose:**

```bash
docker compose -f docker-compose.bot.yml --env-file .env.bot up -d --build
```

**On a Linux VM, pulling a prebuilt image from GHCR** — the
zero-source-checkout flow with `scripts/deploy-bot.sh` and
`docker-compose.bot.prod.yml` — see
[`bot-vm-deploy.md`](bot-vm-deploy.md) for the full recipe.
