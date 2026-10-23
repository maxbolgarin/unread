<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/maxbolgarin/unread/main/docs/assets/wordmark/wordmark-ondark.png">
    <img alt="unread" src="https://raw.githubusercontent.com/maxbolgarin/unread/main/docs/assets/wordmark/wordmark-onlight.png" width="320">
  </picture>
</p>
<p align="center"><em>Read your Telegram unread. Without reading it.</em></p>

<p align="center">
  <a href="https://pypi.org/project/unread/"><img src="https://img.shields.io/pypi/v/unread.svg" alt="PyPI"></a>
  <a href="https://github.com/maxbolgarin/unread/actions/workflows/ci.yml"><img src="https://github.com/maxbolgarin/unread/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <strong><a href="https://maxbolgarin.github.io/unread/">Website</a></strong> ·
  <strong><a href="https://maxbolgarin.github.io/unread/docs/">Docs</a></strong> ·
  <strong><a href="https://pypi.org/project/unread/">PyPI</a></strong>
</p>

**unread** turns Telegram chats, YouTube videos, web pages, voice
messages and files into short reports, with every claim linked back to
the message, paragraph or timestamp it came from. It runs on your own
machine or server, with whichever AI you have a key for: OpenAI,
Anthropic, Google Gemini, OpenRouter or a local model.

Use it two ways:

- **Command line**: `unread @somegroup --last-days 7` gives you a report on the week.
- **Telegram bot**: forward it a voice message, a PDF, a link or a YouTube video, and it replies with the summary.

![unread analyzing a Telegram channel into a Markdown report](https://raw.githubusercontent.com/maxbolgarin/unread/main/.github/examples/analyze-example.gif)

## Command line

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install.sh | bash
unread init          # pick an AI provider and paste its key; Telegram is optional
```

```bash
unread @somegroup --last-days 7                  # a Telegram chat, channel or forum
unread "https://youtu.be/jmzoJCn8evU"            # a YouTube video
unread "https://paulgraham.com/greatwork.html"   # a web page
unread ./meeting.mp3                             # a file: audio, video, PDF, DOCX, image…
unread ask <any of the above> "your question"    # ask instead of summarize
unread dump <any of the above>                   # save the original text, no AI
```

Here is [a real report](.github/examples/summary.md): two weeks of
`@thehackernews`, 99 messages, $0.016.

More: [install options](docs/install.md) · [sources](docs/sources.md) · [every command](docs/reference.md)

## Telegram bot

One command on a server (a small VM is plenty). It installs Docker if
needed, asks a few questions and starts the bot:

```bash
curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash
```

You need a bot token from [@BotFather](https://t.me/botfather), an AI
key, and a Telegram API id + hash (free at
[my.telegram.org](https://my.telegram.org)). You don't need to log in
to your own Telegram account; do that later only if you want the bot to
read `t.me/…` chat links too. Afterwards, `unread-bot status | logs | update | config`
manages it.

Already use the CLI? Skip Docker and run the bot from it; it becomes a
systemd service where the machine has one:

```bash
unread bot setup && unread bot start
```

<p align="center">
  <img src="https://raw.githubusercontent.com/maxbolgarin/unread/main/.github/examples/bot.jpg" alt="The bot replying to a voice message with a summary and a PDF report" width="280">
</p>

More: [what the bot does](docs/bot.md) · [other ways to deploy it](docs/bot-vm-deploy.md) (Compose by hand, systemd, your own computer)

## Highlights

- **Citations on every claim.** Each point links to its Telegram message, web paragraph or video timestamp.
- **Any language in, any language out.** A Russian chat can become an English digest.
- **Voice and video.** Voice messages, video notes, podcasts and recorded meetings are transcribed, then summarized.
- **Your data stays yours.** Everything is kept in SQLite under `~/.unread/`, and keys can be encrypted at rest. Only your AI provider sees the text you analyze, and `--redact` strips phone numbers, emails and card numbers first.
- **Cheap to run.** Results are cached, long inputs are split and summarized in parallel, and `--max-cost` / `--dry-run` keep the bill in check.

## AI providers

| Provider | Notes |
|---|---|
| **OpenAI** | The default. Also transcribes voice and video. |
| **Anthropic**, **Google Gemini** | Analysis and images. Add an OpenAI key for voice and video. |
| **OpenRouter** | One key, many models. |
| **Local** (Ollama, LM Studio, vLLM) | Any OpenAI-compatible server; the AI runs on your own hardware. |

Switch any time with `unread settings`, or `/settings` in the bot.

## Documentation

| Guide | What's in it |
|---|---|
| [Install](docs/install.md) | Install options, first-run setup, where files live |
| [Sources](docs/sources.md) | Telegram, YouTube, web pages, files, presets, fact-checking |
| [CLI reference](docs/reference.md) | Every command and flag |
| [Configuration](docs/configuration.md) | Languages, models, cost and caching, troubleshooting |
| [Telegram bot](docs/bot.md) | What it does, its commands and settings |
| [Deploying the bot](docs/bot-vm-deploy.md) | Docker installer, Compose, systemd, Telegram session |
| [Security](docs/security.md) | Threat model, encrypted secrets, PII redaction |

The same guides are on the [website](https://maxbolgarin.github.io/unread/docs/).

## Contributing

Pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers the
dev setup, lint and tests; report vulnerabilities as described in
[SECURITY.md](SECURITY.md). Issues and ideas:
<https://github.com/maxbolgarin/unread/issues>.

Built on [Telethon](https://github.com/LonamiWebs/Telethon),
[yt-dlp](https://github.com/yt-dlp/yt-dlp),
[trafilatura](https://github.com/adbar/trafilatura),
[Typer](https://github.com/tiangolo/typer),
[Rich](https://github.com/Textualize/rich) and
[uv](https://github.com/astral-sh/uv).

## License

Apache 2.0, see [LICENSE](LICENSE).
