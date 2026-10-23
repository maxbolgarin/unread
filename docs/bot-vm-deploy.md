# Deploying the bot

← Back to [README](../README.md) · What the bot does: [bot.md](bot.md)

The bot runs on any always-on Linux machine; a small VM is plenty. Pick
one way to run it:

| Way | Best for |
|---|---|
| **[1. One-command Docker install](#1-one-command-docker-install)** (recommended) | A fresh server. It asks a few questions and does the rest. |
| **[2. Docker Compose by hand](#2-docker-compose-by-hand)** | You already run Compose stacks, or want every knob. |
| **[3. From the installed `unread`](#3-from-the-installed-unread)** | You already use the CLI; no Docker; ARM servers; your own computer. |

## What you need

- **A bot token.** In Telegram, message [@BotFather](https://t.me/botfather) → `/newbot`.
- **Your Telegram user id.** The bot answers only the ids you list and
  silently ignores everyone else. The installer reads it off the first
  message you send your bot; otherwise [@userinfobot](https://t.me/userinfobot) tells you.
- **An AI key.** OpenAI (the default, and the one that transcribes voice
  and video), Anthropic, Google Gemini or OpenRouter.
- **A Telegram API id + hash.** Free at <https://my.telegram.org> →
  *API development tools* → create an app (any name). The bot talks to
  Telegram over MTProto, the protocol Telegram's own apps use, instead
  of the plain Bot API, which can't download files over 20 MB. MTProto
  needs an app id and hash even for a bot; your own account isn't
  involved.

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
   type the id. Then it asks for anyone else who should be able to use
   the bot (their ids, comma-separated; Enter skips).
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
| `unread-bot users` | Who may use the bot; `users add ID` / `users remove ID` change it and restart |
| `unread-bot stop` / `start` | Stop or start the bot |
| `unread-bot doctor` | Check the setup from inside the container |
| `unread-bot uninstall` | Remove the bot; asks before deleting any data |

It's a thin wrapper around Docker Compose, so `cd ~/unread-bot && docker compose logs -f`
works just as well.

Day-to-day settings live in the bot itself: send `/settings` to change
the language, report format, preset, AI provider and key. **🧠 Model**
there picks the AI model: tap one of the listed models (prices shown) or
send any model id. Until you pick one, runs use the default it names
(`gpt-5.6-luna` on OpenAI).

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
[installed `unread`](#3-from-the-installed-unread).

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

## 3. From the installed `unread`

No Docker: the bot runs from the same `unread` you use on the command
line, on x86_64 or ARM, Linux or macOS.

```bash
unread bot setup     # bot token (checked), your Telegram id (press Start in the bot), API id if missing
unread bot start     # runs it in the background
```

`setup` saves the token and your id to `~/.unread/.env.bot`. The AI key
and Telegram API id/hash come from `unread init`; `setup` asks for the
API id/hash only if `init` didn't save them. If you logged in to
Telegram in `init`, the bot already knows who its owner is, and it can
read `t.me/…` links right away.

`start` picks the best way this machine has to keep it running:

- **systemd** (most Linux servers): a user service,
  `~/.config/systemd/user/unread-bot.service`. It restarts after a
  crash and, once you run `sudo loginctl enable-linger $USER`, keeps
  running after you log out and starts at boot. `start` tells you if
  that step is missing.
- **Background process** (macOS, or no systemd): it runs until the
  computer sleeps or restarts and isn't restarted after a crash. Fine
  on your own computer; for always-on, use a server.
  `--no-service` forces this mode on a systemd machine too.

| Command | What it does |
|---|---|
| `unread bot status` | Running or not, whether it's connected |
| `unread bot logs` | Recent log lines; `-f` to follow, `-n 200` for more |
| `unread bot restart` | Restart; picks up changed settings and upgrades |
| `unread bot stop` | Stop it (the service won't come back at boot) |
| `unread bot users` | Who may use the bot; `users add ID` / `users remove ID` change it |
| `unread bot run` | Run in the foreground instead, for debugging |

Only one copy runs per install: `unread bot run` refuses to start next
to the background one. To update: `unread update`, then `unread bot restart`.

Voice and video need ffmpeg, and YouTube videos without captions need
[Deno](https://deno.com) — see [Install](install.md). The Docker image
ships both.

**A fresh server without `unread` yet?** One command installs it, its
system packages (uv, ffmpeg, PDF libraries) and a systemd service:

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot.sh | bash
```

It runs `unread init`, asks for the bot token and sets up the same
`unread-bot` service, so `unread bot status | logs | restart | stop`
manage it afterwards. Flags: `--reset` wipes `~/.unread/` first;
`--skip-init` skips the wizard when `~/.unread/.env` is already filled in.
Without a Telegram login in `init`, run `unread bot setup` afterwards
to add your id.

Everything the bot keeps (reports, cache, secrets, session) is in
`~/.unread/`; back that directory up and you've backed up the bot.

**Troubleshooting:**

- *Voice / video fails*: ffmpeg is missing. Install it and `unread bot restart`.
- *Reports arrive as `.md` instead of PDF*: libpango is missing
  (`sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0`). The bot
  switches back to PDF by itself once it's there.

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

Without Docker, copy it to `~/.unread/storage/` instead and run
`unread bot restart`.

The session's account becomes the bot's primary owner; ids in
`UNREAD_BOT_OWNER_ID` stay on as extra admins. Only the primary owner
can analyze Telegram chats, because every admin would be reading
through that one account. See [Who can use the bot](bot.md#who-can-use-the-bot).

## Troubleshooting

- **The bot doesn't answer.** Check `unread-bot status` (or the log) for
  `✓ bot ready`, and that your id is in `UNREAD_BOT_OWNER_ID`. Messages
  from anyone else are dropped without a reply.
- **Every message gets two answers.** Two copies of the bot are running
  with one token, e.g. `unread bot start` and the Docker one. Stop one
  (`unread bot stop`, or `unread-bot stop` for Docker).
- **"Telegram is rate-limiting this bot token"** right after start.
  Leave it running; it waits the time out by itself. Restarting only
  starts the wait over.
- **The AI says the key is wrong.** Set a new one with `/settings` → 🔑,
  or in `.env` and `unread-bot restart`. A key in `.env` wins over one
  set in the bot.
