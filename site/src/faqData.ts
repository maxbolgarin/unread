export interface FaqItem {
  q: string;
  /** Plain-text answer used for the JSON-LD FAQPage schema */
  aText: string;
  /** Rendered HTML used in the page (may include <code>, <a>) */
  aHtml: string;
}

export const faqItems: FaqItem[] = [
  {
    q: 'Does this ship my Telegram history to OpenAI?',
    aText:
      'Only the messages within the time window you pick, and only the fields the LLM needs. Everything is stored locally in SQLite under ~/.unread/. You can also enable --redact to scrub PII from the API payload while keeping the originals on your disk.',
    aHtml:
      'Only the messages within the time window you pick, and only the fields the LLM needs. Everything is stored locally in <code>SQLite</code> under <code>~/.unread/</code>. You can also enable <code>--redact</code> to scrub PII (phones, emails, IBANs, Luhn-valid card numbers) from the API payload while keeping the originals on your disk.',
  },
  {
    q: "What if I don't use Telegram?",
    aText:
      'Skip Telegram setup during unread init. The same <ref> syntax works for YouTube videos, web pages, and local files (PDF, DOCX, audio, video, images, stdin).',
    aHtml:
      'Skip Telegram setup during <code>unread init</code>. The same <code>&lt;ref&gt;</code> syntax works for YouTube videos, web pages, and local files (PDF, DOCX, audio, video, images, stdin).',
  },
  {
    q: 'What languages does it actually support?',
    aText:
      'Source language and report language are independent. Hand-tuned preset structures exist for English and Russian; other languages use the English preset as a template and still produce coherent reports. Example: --report-language es turns Russian chats into Spanish digests.',
    aHtml:
      'Source language and report language are <strong>independent</strong>. Hand-tuned preset structures exist for English and Russian; other languages use the English preset as a template and still produce coherent reports. Example: <code>--report-language es</code> turns Russian chats into Spanish digests.',
  },
  {
    q: 'Will it cost me money?',
    aText:
      'Yes — every LLM call costs something. unread keeps it small: --max-cost aborts before overspend, --dry-run estimates without calling, and unread stats shows lifetime spend by chat, preset, and day. A week of group chat is typically pennies on gpt-5.6-luna.',
    aHtml:
      'Yes — every LLM call costs something. <code>unread</code> keeps it small: <code>--max-cost</code> aborts before overspend, <code>--dry-run</code> estimates without calling, and <code>unread stats</code> shows lifetime spend by chat, preset, and day. A week of group chat is typically pennies on <code>gpt-5.6-luna</code>.',
  },
  {
    q: 'Is it actually fast?',
    aText:
      'Yes. Big histories are chunked and analyzed in parallel via map-reduce. There are two caches: a local analysis cache (re-running unchanged input is free) and the provider prompt cache (server-side discount on repeated prefixes). Run unread cache stats to see hit rate.',
    aHtml:
      'Yes. Big histories are chunked and analyzed in parallel via map-reduce. There are two caches: a local analysis cache (re-running unchanged input is free) and the provider prompt cache (server-side discount on repeated prefixes). Run <code>unread cache stats</code> to see hit rate.',
  },
  {
    q: 'Can I run it on a server or in cron?',
    aText:
      'Yes. Non-TTY mode skips interactive prompts. unread watch --interval 1h loops in foreground. API keys can come from environment variables or ~/.unread/.env.',
    aHtml:
      'Yes. Non-TTY mode skips interactive prompts. <code>unread watch --interval 1h</code> loops in foreground. API keys can come from environment variables or <code>~/.unread/.env</code>.',
  },
  {
    q: 'Is it really free and open source?',
    aText:
      'Yes. unread is Apache 2.0 licensed and developed in the open on GitHub. You only pay your LLM provider for the tokens you use. No service, no telemetry, no account.',
    aHtml:
      'Yes. <code>unread</code> is Apache 2.0 licensed and developed in the open on <a href="https://github.com/maxbolgarin/unread" rel="noopener" target="_blank">GitHub</a>. You only pay your LLM provider for the tokens you use. No service, no telemetry, no account.',
  },
  {
    q: 'Can I run this as a Telegram bot?',
    aText:
      'Yes. Send your own bot a file, a voice message, a web link, a YouTube link or a forwarded post and it replies with the gist plus a full report (PDF by default, Markdown, or right in the chat), with time and cost in the caption. It answers only the Telegram accounts you allow; everyone else is silently ignored. On a Linux server one command installs it with Docker: curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash. The bot guide covers the rest.',
    aHtml:
      'Yes. Send your own bot a file, a voice message, a web link, a YouTube link or a forwarded post and it replies with the gist plus a full report (PDF by default, Markdown, or right in the chat), with time and cost in the caption. It answers only the Telegram accounts you allow; everyone else is silently ignored. On a Linux server one command installs it with Docker — see <a href="/unread/docs/bot-vm-deploy/">Deploy the bot</a> and the <a href="/unread/docs/bot/">bot guide</a>.',
  },
  {
    q: 'Does the bot need my Telegram account?',
    aText:
      'No. It needs a bot token from @BotFather, an AI key, and a Telegram API id and hash (free at my.telegram.org — the bot connects the way the Telegram apps do, which lets it download files over the 20 MB limit of the plain Bot API; that connection needs an app id and hash, not your account). Files, voice and video, web links and YouTube all work without your account. Only reading t.me chat links and channels needs your Telegram session, which you can hand to the bot later with /upload_session.',
    aHtml:
      'No. It needs a bot token from <code>@BotFather</code>, an AI key, and a Telegram API id and hash (free at <a href="https://my.telegram.org" rel="noopener" target="_blank">my.telegram.org</a> — the bot connects the way the Telegram apps do, which lets it download files over the 20 MB limit of the plain Bot API; that connection needs an app id and hash, not your account). Files, voice and video, web links and YouTube all work without your account. Only reading <code>t.me/</code> chat links and channels needs your Telegram session, which you can hand to the bot later with <code>/upload_session</code>.',
  },
  {
    q: 'Can it transcribe voice messages and videos?',
    aText:
      'Yes — both standalone files (unread ./voice.ogg, unread ./meeting.mp4) and voice notes / video circles inside Telegram chats. Speech-to-text runs through OpenAI (gpt-4o-mini-transcribe by default) at roughly $0.003 per minute. Video files have their audio track extracted by ffmpeg first. Forwarded voice messages dedupe across chats — each is transcribed once and cached by Telegram document_id.',
    aHtml:
      'Yes — both standalone files (<code>unread ./voice.ogg</code>, <code>unread ./meeting.mp4</code>) and voice notes / video circles inside Telegram chats. Speech-to-text runs through OpenAI (<code>gpt-4o-mini-transcribe</code> by default) at roughly <strong>$0.003 per minute</strong>. Video files have their audio track extracted by <code>ffmpeg</code> first. Forwarded voice messages dedupe across chats — each is transcribed once and cached by Telegram <code>document_id</code>.',
  },
];
