# Deploying the bot

← Back to [README](../README.md) · What the bot does: [bot.md](bot.md)

The bot runs on any always-on Linux machine; a small VM is plenty. Pick
one way to run it:

| Way | Best for |
|---|---|
| **[1. One-command Docker install](#1-one-command-docker-install)** (recommended) | A fresh server. It asks a few questions and does the rest. |
| **[2. Docker Compose by hand](#2-docker-compose-by-hand)** | You already run Compose stacks, or want every knob. |
| **[3. Native install (systemd)](#3-native-install-systemd)** | No Docker, or an ARM server. |
| **[4. On your own computer](#4-on-your-own-computer)** | Trying it out. |

## What you need

- **A bot token.** In Telegram, message [@BotFather](https://t.me/botfather) → `/newbot`.
- **Your Telegram user id.** The bot answers only the ids you list and
  silently ignores everyone else. The installer reads it off the first
  message you send your bot; otherwise [@userinfobot](https://t.me/userinfobot) tells you.
- **An AI key**: OpenAI (the default, and the one that transcribes voice
  and video), Anthropic, Google Gemini or OpenRouter.
- **A Telegram API id + hash.** Free at <https://my.telegram.org> →
  *API development tools* → create an app (any name). Telegram asks
  every app that connects to it for these, bots included.

**Not needed: a Telegram user session.** Without one the bot handles
files, voice and video messages, web links, YouTube and forwarded
messages. Add a session later only if you also want it to read
`t.me/…` chat links and channels; see
[Reading your Telegram chats](#reading-your-telegram-chats-optional).

## 1. One-command Docker install

On the server:

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash
```

It walks you through:

1. **Docker**: installed with Docker's official script if it's missing (it asks first).
2. **Bot token**: checked with Telegram right away.
3. **Owner**: open your new bot and press *Start*; the installer picks
   up your id from that message and asks you to confirm. You can also
   type the id.
4. **AI**: pick a provider and paste its key. With anything but OpenAI
   it offers to add an OpenAI key for voice transcription; Enter skips.
5. **Telegram API id + hash.**

Then it writes `~/unread-bot/docker-compose.yml` and `~/unread-bot/.env`
(readable only by you), starts the bot, waits until it's connected, and
the bot says hello in Telegram.

**Re-running is safe.** It keeps your answers and updates the image. To
answer everything again (Enter keeps a value):

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash -s -- --reconfigure
```

Other options: `--dir PATH` (default `~/unread-bot`) and `--image REF`
to pin a version, e.g. `--image ghcr.io/maxbolgarin/unread:1.10.0`.

**Unattended** (cloud-init, CI): export `UNREAD_BOT_TOKEN`,
`UNREAD_BOT_OWNER_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and an AI
key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` or
`OPENROUTER_API_KEY`) before running it, and nothing is asked.

### Managing it

The installer adds an `unread-bot` command:

| Command | What it does |
|---|---|
| `unread-bot status` | Running or not, version, whether it's connected |
| `unread-bot logs` | Follow the log (Ctrl-C stops watching, not the bot) |
| `unread-bot update` | Download the latest release and restart |
| `unread-bot restart` | Restart; also applies edits to `.env` |
| `unread-bot config` | Open `.env` in an editor, then apply it |
| `unread-bot stop` / `start` | Stop or start the bot |
| `unread-bot doctor` | Check the setup from inside the container |
| `unread-bot uninstall` | Remove the bot; asks before deleting any data |

It's a thin wrapper around Docker Compose, so `cd ~/unread-bot && docker compose logs -f`
works just as well.

Day-to-day settings live in the bot itself: send `/settings` to change
the language, report format, preset, AI provider, model and key.

## 2. Docker Compose by hand

```bash
mkdir ~/unread-bot && cd ~/unread-bot
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/maxbolgarin/unread/main/docker-compose.bot.yml
curl -fsSLo .env https://raw.githubusercontent.com/maxbolgarin/unread/main/.env.bot.example
chmod 600 .env
nano .env                  # fill in the required values
docker compose up -d
docker compose logs -f     # wait for "✓ bot ready"
```

Compose reads `.env` from the same directory, so plain `docker compose …`
works with no `-f` or `--env-file`. [`.env.bot.example`](../.env.bot.example)
explains every variable.

**Update:** `docker compose pull && docker compose up -d`.

**A started container is not a working bot.** With
`restart: unless-stopped`, a misconfigured bot restarts in a loop while
`up -d` still prints "Started". Look for this line in the log:

```
✓ bot ready · owner=123456789 · session=missing …
```

`No owner allowlist` means `UNREAD_BOT_OWNER_ID` is missing.

**Another AI provider:** add its key and name it as the provider:

```
ANTHROPIC_API_KEY=sk-ant-…
UNREAD_AI_CHAT_PROVIDER=anthropic
UNREAD_AI_FILTER_PROVIDER=anthropic
UNREAD_AI_VISION_PROVIDER=anthropic
```

Models default to that provider's own; change them from `/settings` in
the bot. A provider or model set in `.env` outranks `/settings`. Voice
and video transcription needs an OpenAI key.

**Customizing the compose file:** put your changes in
`docker-compose.override.yml` next to it. Compose merges that file in,
and the installer never touches it.

**From a repo checkout**, with the repo's file names:

```bash
cp .env.bot.example .env.bot && $EDITOR .env.bot
docker compose -f docker-compose.bot.yml --env-file .env.bot up -d
```

Here every compose command needs `-f docker-compose.bot.yml --env-file .env.bot`,
including `logs` and `ps`, because the required variables are checked on
each one.

**From your laptop:** `scripts/deploy-bot.sh user@host` copies the
compose file and your `.env.bot` to the server over SSH and restarts the
container there. The server needs only Docker and SSH.

**Without Compose:**

```bash
docker run -d --name unread-bot --restart unless-stopped --init \
  -e UNREAD_BOT_TOKEN=… -e UNREAD_BOT_OWNER_ID=… \
  -e TELEGRAM_API_ID=… -e TELEGRAM_API_HASH=… -e OPENAI_API_KEY=… \
  -v unread_state:/root/.unread \
  ghcr.io/maxbolgarin/unread:latest unread bot run
```

### The image

`ghcr.io/maxbolgarin/unread` is a general `unread` runtime with no
entrypoint; the compose file runs `unread bot run` in it. It ships
ffmpeg, PDF fonts and Deno (which yt-dlp needs for YouTube audio). It's
built for x86_64 (amd64); on an ARM server use the
[native install](#3-native-install-systemd).

| Tag | Moves when |
|---|---|
| `:latest` | a release is published |
| `:1.10.0`, `:1.10` | never / on patch releases |
| `:main` | anything lands on `main` (ahead of releases) |

Reports, settings, keys and the Telegram session live in the
`unread_state` volume and survive updates.

The same image runs one-off CLI commands too:

```bash
docker run --rm -v unread_state:/root/.unread ghcr.io/maxbolgarin/unread:latest unread doctor
```

**`docker compose pull` says "denied"?** The package is public, but if
this machine is logged in to `ghcr.io` with an account that can't read
it, GHCR refuses the login rather than falling back to anonymous. Pull
with an empty Docker config instead (the installer and
`unread-bot update` do this automatically):

```bash
mkdir -p ~/.docker-anon && echo '{}' > ~/.docker-anon/config.json
DOCKER_CONFIG=~/.docker-anon docker compose pull
```

## 3. Native install (systemd)

No Docker; works on x86_64 and ARM. On the server:

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh | bash
```

It installs uv, ffmpeg and the PDF libraries (apt, dnf, pacman or
Homebrew), installs `unread`, runs `unread init` (AI key, Telegram API
id/hash and, optionally, your Telegram login), asks for the bot token,
and installs a `systemd --user` service that restarts on crashes and
keeps running after you log out.

Re-running is safe: it skips what's done. Flags: `--reset` wipes
`~/.unread/` first (deletes the session, reports and cache);
`--skip-init` skips the wizard when `~/.unread/.env` is already filled in.

The service reads `~/.unread/.env`. If you skipped the Telegram login
in `unread init`, make sure that file also has `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH` and your `UNREAD_BOT_OWNER_ID`, then restart the
service.

```bash
systemctl --user status unread-bot
journalctl --user -u unread-bot -f        # follow the log
systemctl --user restart unread-bot       # after a config change
uv tool upgrade unread && systemctl --user restart unread-bot   # update
```

Everything the bot keeps (reports, cache, secrets, session) is in
`~/.unread/`; back that directory up and you've backed up the bot.

**Troubleshooting:**

- *"Couldn't enable linger"*: run `sudo loginctl enable-linger $USER`,
  or the service stops when you log out.
- *Voice / video fails*: ffmpeg is missing. Install it and restart.
- *Reports arrive as `.md` instead of PDF*: libpango is missing
  (`sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`). The bot
  switches back to PDF by itself once it's there.

## 4. On your own computer

```bash
uv tool install unread
unread init                                   # AI key; the Telegram step is optional
export UNREAD_BOT_TOKEN=123456:ABC…           # from @BotFather
export UNREAD_BOT_OWNER_ID=123456789          # skip if you logged in to Telegram in init
export TELEGRAM_API_ID=… TELEGRAM_API_HASH=…  # skip if init saved them
unread bot run                                # Ctrl-C to stop
```

In a repo checkout, `unread bot run` also reads `./.env.bot` (it must be
`chmod 600`).

## Reading your Telegram chats (optional)

To analyze `t.me/…` links, `@channel` refs and a forwarded post's source
channel, the bot needs a Telegram *user* session: your own account, not
the bot's. Make one on your computer with `unread init` (it logs in with
your phone number), then hand it to the bot:

**In Telegram (easiest):** send the bot `/upload_session`, then send
`~/.unread/storage/session.sqlite.session` as a file. The bot checks it,
installs it and from then on reads chats through that account.

**Or copy it onto the server:**

```bash
scp ~/.unread/storage/session.sqlite.session my-server:/tmp/
ssh my-server
cd ~/unread-bot
docker compose cp /tmp/session.sqlite.session unread-bot:/root/.unread/storage/session.sqlite.session
docker compose restart && rm /tmp/session.sqlite.session
```

For the native install, copy it to `~/.unread/storage/` on the server
and run `systemctl --user restart unread-bot`.

The session's account becomes the bot's primary owner; ids in
`UNREAD_BOT_OWNER_ID` stay on as extra admins. Only the primary owner
can analyze Telegram chats, because every admin would be reading
through that one account. See [Who can use the bot](bot.md#who-can-use-the-bot).

## Troubleshooting

- **The bot doesn't answer.** Check `unread-bot status` (or the log) for
  `✓ bot ready`, and that your id is in `UNREAD_BOT_OWNER_ID`. Messages
  from anyone else are dropped without a reply.
- **Every message gets two answers.** Two copies of the bot are running
  with one token, e.g. the native service and the Docker one. Stop one:
  `systemctl --user disable --now unread-bot`.
- **"Telegram is rate-limiting this bot token"** right after start.
  Leave it running; it waits the time out by itself. Restarting only
  starts the wait over.
- **The AI says the key is wrong.** Set a new one with `/settings` → 🔑,
  or in `.env` and `unread-bot restart`. A key in `.env` wins over one
  set in the bot.
