"""`BotApp` — the long-running bot process.

Two-phase setup: start the bot-mode Telethon client (authed via
`bot_token`), then verify that the owner's user-mode session at
`settings.telegram.session_path` exists and is authorized. The user
client is NOT held open across the bot's lifetime — each TG-handler
invocation opens its own short-lived client through the existing
`unread.tg.client.tg_client` context manager, the same path the CLI
takes. This keeps the bot a thin layer over `cmd_analyze_*` and avoids
two-clients-one-SQLite-file lifetime headaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import structlog
from rich.console import Console
from telethon import TelegramClient, events

from unread.config import Settings
from unread.core.paths import default_bot_session_path

log = structlog.get_logger(__name__)
console = Console()


# Shown when a non-primary admin tries a session-backed surface. Kept as
# module constants so the message stays identical across the three call
# sites (message gate, callback toast, /upload_session).
_NOT_PRIMARY_OWNER_MSG = (
    "🔒 Only the session owner can analyze Telegram chats. "
    "This bot reads t.me links through one Telegram account, so the "
    "restriction keeps that account's private chats private. "
    "Send me a file, a web link, or a YouTube link instead."
)
_NOT_PRIMARY_OWNER_TOAST = "Only the session owner can read Telegram chats."


def _default_model_for(provider: str) -> str:
    """The adapter's own default chat model, from its class attributes.

    Read off the class rather than by constructing the adapter, so this
    works for a provider whose credentials aren't configured yet — which
    is the normal case when you're switching TO it.
    """
    from unread.ai.providers import _provider_class_defaults

    try:
        chat, _filter = _provider_class_defaults((provider or "").lower())
    except Exception:
        return ""
    return chat or ""


def _default_filter_model_for(provider: str) -> str:
    from unread.ai.providers import _provider_class_defaults

    try:
        _chat, filt = _provider_class_defaults((provider or "").lower())
    except Exception:
        return ""
    return filt or ""


def _extra_admins_suffix(owner_id: int, allowed_ids: set[int]) -> str:
    """` · admins=[…]` fragment for the startup line, empty when solo."""
    extras = sorted(i for i in allowed_ids if i != owner_id)
    if not extras:
        return ""
    return " · admins=[cyan]" + ",".join(str(i) for i in extras) + "[/]"


# Sign-in attempts before giving up on a Telegram flood wait. Each failure
# sleeps the full wait it was given, so this is "tolerate three separate
# limits", not "retry three times quickly".
_BOT_START_FLOOD_RETRIES = 3


class BotApp:
    """Bot-mode Telegram client + per-message dispatcher.

    The single long-running Telethon connection here is the bot client
    (authed via `bot_token`). Every TG-link analyze request opens a
    transient user-mode client via `tg_client()` for the duration of
    that one request. The bot only checks that the user session is
    *present* and authorized at startup so it can surface a focused
    error before the first request fails.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.bot.concurrency)
        self.bot_client: TelegramClient | None = None
        # True iff the owner's user session at
        # settings.telegram.session_path was authorized at startup or
        # after a successful /upload_session.
        self.user_session_ready: bool = False
        # Effective allowlist. Seeded from `settings.bot.owner_id` so
        # the env var works as a bootstrap allowlist when no session
        # is mounted yet; `_verify_user_session` overrides this with
        # the session-derived ID when available (and logs a warning
        # if the env var and the session disagree). One of:
        #   - 0 → no allowlist resolved yet (bot will refuse to wire
        #     handlers and exit).
        #   - >0 → the single Telegram user ID we serve.
        # Full allowlist. The primary owner (`self.owner_id`) is always a
        # member; extra admins can drive everything EXCEPT the surfaces
        # that read the primary owner's Telegram session (t.me links,
        # forward→source-channel windows, /upload_session).
        # Must be assigned BEFORE `owner_id` — its setter writes into it.
        self.allowed_ids: set[int] = set(settings.bot.owner_ids)
        self.owner_id = settings.bot.owner_id
        # Per-chat ephemeral state (sticky `/preset`, pending
        # `/upload_session`, etc.). Keyed by chat_id. Reset on restart.
        self._chat_state: dict[int, dict] = {}
        # In-flight task set so a graceful shutdown can await them.
        self._tasks: set[asyncio.Task] = set()
        # The analyze task currently running for each chat, so `/stop` can
        # cancel one admin's run without touching anybody else's. Keyed by
        # chat_id and self-cleaning: the done-callback drops the entry, so
        # a finished run is never reported as cancellable.
        self._running: dict[int, asyncio.Task] = {}

    @property
    def owner_id(self) -> int:
        """Primary owner — the account whose Telegram session gets read."""
        return self._owner_id

    @owner_id.setter
    def owner_id(self, value: int) -> None:
        """Set the primary owner, keeping the allowlist consistent.

        A property rather than a plain attribute because three places
        reassign the primary owner (startup probe, `/upload_session`
        re-derive, tests) and every one of them must also land in
        `allowed_ids` — otherwise the bot adopts a session whose owner it
        then refuses to answer. The previous primary stays in the
        allowlist as an ordinary admin.
        """
        self._owner_id = value
        if value:
            self.allowed_ids.add(value)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the bot client, resolve the allowlist, run until SIGINT.

        User-visible status uses `console.print` (always shown, even
        at the default "normal" log mode which filters INFO). The
        parallel `log.info` calls survive for `-v / --verbose` debug
        traces.
        """
        console.print("[grey70]→ starting Telegram bot client…[/]")
        log.info("bot.startup.begin", owner_id_from_env=self.settings.bot.owner_id)
        await self._start_bot_client()
        log.info("bot.startup.bot_client_ready")

        console.print("[grey70]→ checking your Telegram user session…[/]")
        log.info("bot.startup.verifying_user_session")
        await self._verify_user_session()
        log.info(
            "bot.startup.user_session_done",
            user_session_ready=self.user_session_ready,
            owner_id=self.owner_id,
        )

        if not self.allowed_ids:
            console.print(
                "[red]Bot has no owner allowlist.[/] Set UNREAD_BOT_OWNER_ID "
                "(one id, or several separated by commas) or mount/upload an "
                "authorized user session before starting."
            )
            log.error("bot.no_owner_allowlist")
            raise RuntimeError("no owner allowlist")

        # Restore each admin's sticky settings (/lang, /preset, …) before
        # wiring handlers, so the first message after a restart already
        # runs with the right language instead of the config default.
        from unread.bot import prefs

        await prefs.load_all(self)

        self._wire_handlers()
        session_state = (
            "[green]ready[/]"
            if self.user_session_ready
            else "[yellow]missing[/] — TG-chat analysis disabled until /upload_session"
        )
        console.print(
            f"[green]✓ bot ready[/] · owner=[cyan]{self.owner_id}[/]"
            f"{_extra_admins_suffix(self.owner_id, self.allowed_ids)}"
            f" · session={session_state}"
            f" · concurrency={self.settings.bot.concurrency}"
        )
        console.print("[grey70]Listening for messages. Ctrl-C to stop.[/]")
        log.info(
            "bot.ready",
            owner_id=self.owner_id,
            allowed_ids=sorted(self.allowed_ids),
            user_session_ready=self.user_session_ready,
            concurrency=self.settings.bot.concurrency,
        )
        # PDF availability probe deferred to first request — it spawns
        # a subprocess that on a misconfigured macOS Pango can take
        # ~10s. Keeping it off the startup path means the bot is
        # accepting messages as soon as the `bot ready` line appears.
        try:
            assert self.bot_client is not None
            await self.bot_client.run_until_disconnected()
        finally:
            await self._shutdown()

    async def _start_bot_client(self) -> None:
        """Authenticate the bot-mode Telethon client.

        The session is PERSISTED to `storage/bot_session.sqlite`. It used
        to be an in-memory `StringSession()` on the reasoning that a bot
        session is regenerable from the token — true, but regenerating it
        costs an `ImportBotAuthorization` call, and Telegram rate-limits
        that. Combined with `restart: unless-stopped`, one crash became a
        loop: restart, re-authorize, earn a longer flood wait, crash,
        repeat. Reusing the stored session means a restart re-authorizes
        nothing.
        """
        from telethon.errors import FloodWaitError

        s = self.settings
        session_path = default_bot_session_path()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session_path),
            api_id=s.telegram.api_id,
            api_hash=s.telegram.api_hash,
        )
        # A flood wait is a WAIT, not a fatal error — so sleep it out in
        # process. Exiting looks tidier but is actively harmful under
        # `restart: unless-stopped`: the container comes straight back,
        # re-enters the same limit, and hammers it (observed once per
        # second). Sleeping here needs no operator action and cannot
        # re-trigger anything.
        for attempt in range(_BOT_START_FLOOD_RETRIES):
            try:
                await client.start(bot_token=s.bot.token)
                break
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 0) or 0)
                mins = max(1, round(wait / 60))
                if attempt == _BOT_START_FLOOD_RETRIES - 1:
                    console.print(
                        f"[red]Telegram is still rate-limiting this bot token[/] "
                        f"after {_BOT_START_FLOOD_RETRIES} attempts ({wait}s left). "
                        "Giving up so the container doesn't sit here forever — "
                        "start it again once the wait has passed."
                    )
                    log.error("bot.client.flood_wait_giving_up", seconds=wait)
                    raise
                console.print(
                    f"[yellow]Telegram is rate-limiting this bot token:[/] waiting "
                    f"{wait}s (~{mins} min) before retrying, in place. "
                    "Leave the container running — restarting it would only "
                    "re-enter the same limit. Once it signs in, the session is "
                    "saved and future restarts re-authorize nothing."
                )
                log.warning("bot.client.flood_wait", seconds=wait, attempt=attempt + 1)
                # Small cushion so we don't wake a beat early and burn a retry.
                await asyncio.sleep(wait + 5)
        self.bot_client = client
        log.info("bot.client.started", session=str(session_path))

    async def _verify_user_session(self) -> None:
        """One-shot probe of the owner's user-mode session.

        Two outputs:

        * `self.user_session_ready` — gates the TG-link handler so it
          can reply "send /upload_session" instead of letting the
          first analyze attempt blow up with a confusing Telethon
          error.
        * `self.owner_id` — when the session is authorized, the
          allowlist is overridden by the session's own user ID
          (via `get_me()`). A `UNREAD_BOT_OWNER_ID` env var that
          disagrees with the session is logged as a warning; the
          session wins.

        Uses `unread.tg.client.build_client` so the bot picks up the
        same session the CLI uses regardless of backend (on-disk
        SQLite, system keychain, or passphrase-encrypted StringSession
        in the secrets DB). Does NOT keep the client connected.
        """
        if not _has_session_blob(self.settings):
            log.warning(
                "bot.user_session.missing",
                session_path=str(self.settings.telegram.session_path),
                hint=(
                    "no session file or DB blob found — send /upload_session via "
                    "Telegram, or SCP your existing session into "
                    f"{self.settings.telegram.session_path}.session "
                    "(Telethon appends `.session` to the path)."
                ),
            )
            return
        derived = await _probe_session_owner_id(self.settings)
        if derived is None:
            log.warning(
                "bot.user_session.unauthorized",
                session_path=str(self.settings.telegram.session_path),
                hint="session blob exists but isn't authorized — re-export from a logged-in host.",
            )
            return
        self.user_session_ready = True
        if self.owner_id and self.owner_id != derived:
            log.warning(
                "bot.owner_id.env_conflict",
                env_owner_id=self.owner_id,
                session_owner_id=derived,
                action="using session owner_id; ignoring env override",
            )
        # The session's own account becomes the primary owner — it IS
        # the account whose chats get read. Configured extra admins are
        # kept: the override only decides who's primary, not who's
        # allowed in.
        self.owner_id = derived
        log.info(
            "bot.user_session.ready",
            owner_id=self.owner_id,
            allowed_ids=sorted(self.allowed_ids),
        )

    async def _shutdown(self) -> None:
        """Cancel in-flight tasks and disconnect the bot client cleanly."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.bot_client is not None:
            with contextlib.suppress(Exception):
                await self.bot_client.disconnect()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _wire_handlers(self) -> None:
        """Attach the allowlisted dispatch handlers.

        Uses the resolved `self.allowed_ids` (env var list, plus the
        session-derived id when a session is present). `run_forever`
        already refuses to call this when the allowlist is empty.
        """
        assert self.bot_client is not None
        assert self.allowed_ids, "wire_handlers called without an allowlist"
        allowed = sorted(self.allowed_ids)

        @self.bot_client.on(events.NewMessage(from_users=allowed))
        async def _on_owner_message(event: events.NewMessage.Event) -> None:
            # Defense in depth — never serve a non-admin under any
            # circumstance, even if a future filter change lets one
            # past the `from_users` gate above.
            if event.sender_id not in self.allowed_ids:
                return
            task = asyncio.create_task(self._handle(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # `events.CallbackQuery` has no `from_users=` (unlike NewMessage)
        # — it accepts `chats=` only. Each admin's user_id IS the chat_id
        # of their 1:1 conversation with the bot, so `chats=allowed`
        # filters out callbacks from any other chat (groups, other DMs).
        # The `sender_id not in allowed_ids` check inside
        # `_handle_callback` is the defense-in-depth fallback.
        @self.bot_client.on(events.CallbackQuery(chats=allowed))
        async def _on_owner_callback(event: events.CallbackQuery.Event) -> None:
            if event.sender_id not in self.allowed_ids:
                return
            task = asyncio.create_task(self._handle_callback(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _handle(self, event: events.NewMessage.Event) -> None:
        """Per-message worker. Classify → show confirm OR execute → reply.

        Wrapped in a top-level try/except so a handler raising never
        kills the event loop or leaves the bot silent.

        Two paths for analysis-shaped messages:
        * Default: show the confirm panel (cheap, no semaphore) and
          stash a `PendingRun`. The actual analyze runs later when the
          user taps ▶ Run on the panel (semaphore-gated in
          `_handle_callback`).
        * `/confirm off` chat state: skip the panel and run analyze
          immediately (today's pre-panel behavior, semaphore-gated
          here in `_handle`).
        """
        # Pending `/upload_session`: the very next document from the
        # owner is consumed by the upload state machine, never routed
        # to the file handler. Check before classification so the
        # session sqlite blob doesn't get classified as a generic file
        # and accidentally analyzed.
        chat_state = self._chat_state.get(event.chat_id) or {}
        if chat_state.get("pending_session_upload") and event.message.media is not None:
            from unread.bot import session_upload

            try:
                await session_upload.handle_uploaded_file(event, app=self)
            except Exception:
                log.exception("bot.session_upload_failed")
                await _safe_reply(event, "⚠️ Session install failed; see bot logs.")
            return

        # An armed `/settings` → API key prompt consumes the next message
        # whole. Checked before classification so a key is never routed to
        # the analyze path (and so never reaches a log line or a report).
        if chat_state.get("pending_api_key"):
            from unread.bot.handlers import cmds as _cmds

            try:
                if await _cmds.maybe_consume_api_key(event, app=self):
                    return
            except Exception:
                log.exception("bot.api_key_capture_failed")
                await _safe_reply(event, "⚠️ Couldn't store that key; see the bot logs.")
                return

        from unread.bot.dispatcher import classify

        try:
            kind, payload = classify(event)
        except Exception:
            log.exception("bot.classify_failed")
            await _safe_reply(event, "⚠️ Couldn't read that message.")
            return

        # Quick paths — never block on the semaphore.
        if kind == "cmd":
            await self._handle_cmd(event, payload)
            return

        # Telegram-chat analysis reads the PRIMARY owner's account through
        # the shared user session at `settings.telegram.session_path`.
        # Extra admins keep the file / URL / YouTube surface but must not
        # be able to pull the primary owner's private chats.
        if kind == "tg" and not self.is_primary_owner(event.sender_id):
            await _safe_reply(event, _NOT_PRIMARY_OWNER_MSG)
            log.info("bot.tg_link.refused_non_primary", sender_id=event.sender_id)
            return

        if chat_state.get("confirm_disabled"):
            # No panel — straight to execute. Same path the original
            # pre-confirm bot took. Semaphore gates the analyze work.
            from unread.bot.confirm import default_options

            options = default_options(kind, self.settings)
            async with self._semaphore:
                # Registered so `/stop` can reach this run; see `register_running`.
                self.register_running(event.chat_id, asyncio.current_task())
                try:
                    await self._run_execute(event, kind, payload, options, progress_msg=None)
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.handler_failed", kind=kind)
                    await _safe_reply(event, f"⚠️ {type(e).__name__}: {e}")
            return

        # Default: append to the chat's burst and let the debounce
        # timer flush it into one consolidated `▶ Run separately /
        # ▶ Run combined` panel. Multiple links pasted in quick
        # succession produce ONE panel, not N.
        from unread.bot.burst import add_to_burst

        try:
            await add_to_burst(self, event, kind, payload)
        except Exception:
            log.exception("bot.add_to_burst_failed", kind=kind)
            await _safe_reply(event, "⚠️ Couldn't queue the message.")

    def register_running(self, chat_id: int, task: asyncio.Task) -> None:
        """Mark `task` as this chat's in-flight run, for `/stop`."""
        self._running[chat_id] = task

        # Identity-checked: a cancelled task's done-callback fires a tick
        # later, and popping by chat_id alone deleted the entry of the run
        # started in between — leaving `/stop` blind to a live run.
        def _clear(done: asyncio.Task, _chat_id: int = chat_id) -> None:
            if self._running.get(_chat_id) is done:
                self._running.pop(_chat_id, None)

        task.add_done_callback(_clear)

    def stop_running(self, chat_id: int) -> bool:
        """Cancel this chat's in-flight run. True if there was one."""
        task = self._running.get(chat_id)
        if task is None or task.done():
            self._running.pop(chat_id, None)
            return False
        task.cancel()
        self._running.pop(chat_id, None)
        return True

    def is_primary_owner(self, sender_id: int) -> bool:
        """True when `sender_id` owns the Telegram session the bot reads.

        Every session-backed surface gates on this rather than on plain
        allowlist membership — extra admins share the primary owner's
        session, so letting them drive it would hand them the primary
        owner's private chats.
        """
        return bool(self.owner_id) and sender_id == self.owner_id

    async def _handle_cmd(self, event: events.NewMessage.Event, payload: dict) -> None:
        """Trivial-reply slash commands. Imported lazily."""
        from unread.bot.handlers import cmds

        await cmds.handle(event, payload, app=self)

    async def _run_execute(
        self,
        event: events.NewMessage.Event,
        kind: str,
        payload: dict,
        options,
        *,
        progress_msg=None,
    ) -> None:
        """Dispatch to the kind-specific `execute`. Lazy-imports the module."""
        # Stash the app on the event so the reply layer can read this
        # chat's sticky `/format` without threading `app` through every
        # send_* signature. Set here rather than in `_handle` so every
        # execution path (panel tap, /confirm off, burst) is covered.
        with contextlib.suppress(Exception):
            event._unread_app = self
        if kind == "file":
            from unread.bot.handlers import file as file_handler

            await file_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "youtube":
            from unread.bot.handlers import youtube as yt_handler

            await yt_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "url":
            from unread.bot.handlers import url as url_handler

            await url_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "tg":
            from unread.bot.handlers import tg as tg_handler

            await tg_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        else:
            await _safe_reply(event, f"⚠️ Unknown message kind: {kind!r}")

    # ------------------------------------------------------------------
    # Callback handling (inline-keyboard taps)
    # ------------------------------------------------------------------

    async def _handle_callback(self, event: events.CallbackQuery.Event) -> None:
        """Route a confirm-panel button tap.

        The only action is `R` (Run) — the panel exists solely to gate
        analyze on an explicit tap. Per-run tuning is via slash
        commands (`/preset <name>`), not buttons.

        Stale panels (TTL-expired or post-restart) reply with a
        "session expired" toast — user sends the link again.
        """
        from unread.bot.confirm import parse_callback, prune_pending_runs

        if event.sender_id not in self.allowed_ids:
            return

        # Settings taps use their own action namespace and carry no
        # PendingRun, so they're handled before the run-panel bookkeeping
        # below (which would reject them as an expired session).
        if await self._maybe_handle_settings_callback(event):
            return
        chat_state = self._chat_state.setdefault(event.chat_id, {})
        prune_pending_runs(chat_state)
        try:
            action, panel_msg_id, _arg = parse_callback(event.data)
        except ValueError:
            log.warning("bot.callback.bad_data", data=event.data)
            with contextlib.suppress(Exception):
                await event.answer("Invalid request.", alert=True)
            return

        pending_runs = chat_state.get("pending_runs") or {}
        pending = pending_runs.get(panel_msg_id)
        if pending is None:
            with contextlib.suppress(Exception):
                await event.answer("Session expired — send again.", alert=True)
            with contextlib.suppress(Exception):
                await event.edit("✖ Session expired.", buttons=None)
            return

        # Drop the pending before kicking off — prevents a double-tap
        # from running twice while the first is in flight.
        from unread.bot.confirm import tg_window_for_action

        is_tg_window = tg_window_for_action(action) is not None
        is_forward = action in ("F_FULL", "F_TXT", "F_FROM", "F_DAY", "F_WK", "F_MO")

        # Same restriction as a bare t.me link, enforced at the tap: these
        # actions open the primary owner's user session to read a chat or
        # channel. Checked BEFORE the pop below so a refused tap leaves
        # the panel usable — an extra admin can still hit F_FULL / F_TXT.
        if (is_tg_window or action in ("F_FROM", "F_DAY", "F_WK", "F_MO")) and not self.is_primary_owner(
            event.sender_id
        ):
            with contextlib.suppress(Exception):
                await event.answer(_NOT_PRIMARY_OWNER_TOAST, alert=True)
            log.info("bot.callback.refused_non_primary", action=action, sender_id=event.sender_id)
            return

        if action in ("R", "A", "M", "Y_DUMP", "Y_FACT") or is_tg_window or is_forward:
            pending_runs.pop(panel_msg_id, None)
            with contextlib.suppress(Exception):
                await event.answer("Running…")
            try:
                panel_msg = await event.get_message()
            except Exception:
                panel_msg = None

        # Every panel coming out of the burst flow is `kind="batch"`,
        # whether the burst held 1 item or N. R and A both mean "run
        # each item under its own handler"; the only difference is the
        # button label in build_batch_panel. Route both through the
        # same loop so a single-item batch doesn't trip the kind
        # dispatch in _run_execute.
        if action in ("R", "A"):
            await self._run_batch_separately(pending, panel_msg)
            return

        if action == "M":
            await self._run_batch_combined(pending, panel_msg)
            return

        if action == "Y_DUMP":
            await self._run_youtube_dump(pending, panel_msg)
            return

        if action == "Y_FACT":
            # Stamp the preset and reuse the ordinary single-item run
            # path — `_run_batch_separately` merges panel options over
            # the kind defaults, so the override survives.
            pending.options.preset_override = "factcheck"
            await self._run_batch_separately(pending, panel_msg)
            return

        if is_tg_window:
            # Stamp the chosen window onto pending.options, then go
            # through the normal single-item run path —
            # `_run_batch_separately` merges it over the kind defaults
            # (see `runtime.merge_panel_options`) so the TG handler's
            # execute() reads it and overrides its default from_msg /
            # last_days computation.
            pending.options.tg_window = tg_window_for_action(action)
            await self._run_batch_separately(pending, panel_msg)
            return

        if is_forward:
            await self._run_forward_action(action, pending, panel_msg)
            return

        # Unknown action — log and ignore so a single bad button doesn't
        # leave the user staring at a frozen panel.
        log.warning("bot.callback.unknown_action", action=action)
        with contextlib.suppress(Exception):
            await event.answer()

    # ------------------------------------------------------------------
    # Batch (burst) execution
    # ------------------------------------------------------------------

    async def _run_batch_separately(self, pending, panel_msg) -> None:
        """Loop items, run each through its kind-specific `execute`.

        Sequential — the analyze pipeline is heavy enough that fanning
        out N parallel runs would just thrash the semaphore + the AI
        provider's rate limit.

        For a single-item batch, the panel itself becomes the progress
        message — avoids spawning a second "⏳ Working…" reply right
        next to the panel. For N≥2, the panel is edited to a
        "⏳ Running k/N …" status line between items and each item's
        execute() spawns its own progress reply.
        """
        from unread.bot.confirm import default_options
        from unread.bot.runtime import merge_panel_options

        items = pending.payload.get("items") or []
        total = len(items)
        if total == 0:
            return

        if total == 1:
            item = items[0]
            options = merge_panel_options(
                defaults=default_options(item.kind, self.settings),
                panel=pending.options,
            )
            async with self._semaphore:
                # Registered so `/stop` can cancel THIS chat's run without
                # touching another admin's. Registration happens inside the
                # semaphore so a queued run isn't advertised as running.
                self.register_running(item.event.chat_id, asyncio.current_task())
                try:
                    await self._run_execute(
                        item.event,
                        item.kind,
                        item.payload,
                        options,
                        progress_msg=panel_msg,
                    )
                except asyncio.CancelledError:
                    # `/stop`. Acknowledge in the chat rather than letting
                    # the task die silently, and re-raise so the loop sees
                    # the cancellation.
                    await _safe_reply(item.event, "🛑 Stopped.")
                    raise
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.batch.item_failed", kind=item.kind)
                    await _safe_reply(item.event, f"⚠️ {type(e).__name__}: {e}")
            return

        from unread.bot.progress import edit_progress

        for idx, item in enumerate(items, start=1):
            await edit_progress(panel_msg, f"⏳ Running {idx}/{total}: {_burst_item_label(item)}")
            options = merge_panel_options(
                defaults=default_options(item.kind, self.settings),
                panel=pending.options,
            )
            async with self._semaphore:
                # Registered so `/stop` can reach this run; see `register_running`.
                self.register_running(item.event.chat_id, asyncio.current_task())
                try:
                    await self._run_execute(
                        item.event,
                        item.kind,
                        item.payload,
                        options,
                        progress_msg=None,
                    )
                except Exception as e:
                    if _is_clean_exit(e):
                        continue
                    log.exception("bot.batch.item_failed", kind=item.kind, idx=idx)
                    await _safe_reply(item.event, f"⚠️ Item {idx}/{total} failed: {type(e).__name__}: {e}")
        await edit_progress(panel_msg, f"✓ Finished {total} items.")

    async def _maybe_handle_settings_callback(self, event) -> bool:
        """Apply a `/settings` menu tap. False when it isn't one."""
        from unread.bot.settings_menu import (
            build_model_menu,
            build_provider_menu,
            build_settings_menu,
            key_prompt_text,
            parse_settings_callback,
        )

        try:
            action, panel_id, value = parse_settings_callback(event.data)
        except ValueError:
            return False

        chat_state = self._chat_state.setdefault(event.chat_id, {})
        settings = self.settings

        # Provider and model are BOT-WIDE and persisted: a second admin
        # changing them silently changes the primary owner's runs and
        # spends their budget. Same reasoning as the key button, which was
        # the only one gated before.
        if action in ("S_PROVS", "S_MODELS", "S_PROV", "S_MODEL") and not self.is_primary_owner(
            event.sender_id
        ):
            with contextlib.suppress(Exception):
                await event.answer(_NOT_PRIMARY_OWNER_TOAST, alert=True)
            return True

        if action == "S_PROVS":
            text, buttons = build_provider_menu(settings=settings, panel_msg_id=panel_id)
        elif action == "S_MODELS":
            text, buttons = build_model_menu(settings=settings, panel_msg_id=panel_id)
        elif action == "S_PROV":
            await self._apply_ai_setting("ai.chat_provider", value or "")
            await self._apply_ai_setting("ai.filter_provider", value or "")
            # Every preset pins an OpenAI model id, and a pin beats config
            # — so switching provider without also switching the model
            # sent `gpt-5.6-luna` to Anthropic and 4xx'd every later run.
            # Pin the new provider's own default; the model menu can
            # change it from there.
            await self._apply_ai_setting("ai.chat_model", _default_model_for(value or ""))
            await self._apply_ai_setting("ai.filter_model", _default_filter_model_for(value or ""))
            text, buttons = build_settings_menu(
                chat_state=chat_state, settings=settings, panel_msg_id=panel_id
            )
        elif action == "S_MODEL":
            await self._apply_ai_setting("ai.chat_model", value or "")
            text, buttons = build_settings_menu(
                chat_state=chat_state, settings=settings, panel_msg_id=panel_id
            )
        elif action == "S_KEY":
            # Credentials are bot-wide, not per-chat: a second admin
            # rotating the key would silently change every admin's runs.
            if not self.is_primary_owner(event.sender_id):
                with contextlib.suppress(Exception):
                    await event.answer(_NOT_PRIMARY_OWNER_TOAST, alert=True)
                return True
            provider = (
                getattr(settings.ai, "chat_provider", "") or getattr(settings.ai, "provider", "") or "openai"
            )
            chat_state["pending_api_key"] = provider
            chat_state["pending_api_key_at"] = time.time()
            with contextlib.suppress(Exception):
                await event.answer()
            with contextlib.suppress(Exception):
                await event.edit(key_prompt_text(provider), buttons=None)
            return True
        else:  # S_ROOT
            text, buttons = build_settings_menu(
                chat_state=chat_state, settings=settings, panel_msg_id=panel_id
            )

        with contextlib.suppress(Exception):
            await event.answer()
        with contextlib.suppress(Exception):
            await event.edit(text, buttons=buttons, parse_mode="md")
        return True

    async def _apply_ai_setting(self, key: str, value: str) -> None:
        """Persist an `app_settings` override AND apply it to the live
        settings object.

        Persisting alone would leave the bot on the old provider until
        somebody restarted the container — which is the exact friction
        this menu exists to remove.
        """
        from unread.config import get_settings
        from unread.db.repo import open_repo

        try:
            async with open_repo(self.settings.storage.data_path) as repo:
                if value:
                    await repo.set_app_setting(key, value)
                else:
                    await repo.delete_app_setting(key)
        except Exception:
            log.exception("bot.settings.persist_failed", key=key)

        section, _, field = key.partition(".")
        # Dedupe by identity: `Settings` is a pydantic model and isn't
        # hashable, so a set literal raises.
        live = get_settings()
        targets = [self.settings] if self.settings is live else [self.settings, live]
        for target in targets:
            with contextlib.suppress(Exception):
                setattr(getattr(target, section), field, value)

    async def _run_youtube_dump(self, pending, panel_msg) -> None:
        """Transcript-dump path for the burst's single YouTube item.

        Deliberately NOT routed through `_run_batch_separately`: that
        helper rebuilds `RunOptions` from settings per item, which is
        right for the analyze path but would drop the fact that the user
        asked for a dump. The panel that offers this button is only ever
        built for a single-item burst (see `burst.render_burst_panel`),
        so there is no N-item loop to mirror here.
        """
        from unread.bot.handlers import youtube as yt_handler

        items = pending.payload.get("items") or []
        if not items:
            return
        item = items[0]
        async with self._semaphore:
            # Registered so `/stop` can reach this run; see `register_running`.
            self.register_running(item.event.chat_id, asyncio.current_task())
            try:
                with contextlib.suppress(Exception):
                    item.event._unread_app = self
                await yt_handler.execute_dump(
                    item.event,
                    item.payload,
                    pending.options,
                    app=self,
                    progress_msg=panel_msg,
                )
            except Exception as e:
                if _is_clean_exit(e):
                    return
                log.exception("bot.youtube_dump_failed")
                await _safe_reply(item.event, f"⚠️ {type(e).__name__}: {e}")

    async def _run_forward_action(self, action: str, pending, panel_msg) -> None:
        """Execute a forward-picker button tap.

        F_FULL → analyze the forwarded message in place. File handler
        already reads `payload["caption"]` to combine image extract +
        caption text when both are present.
        F_TXT  → analyze just the caption / inner text (skip vision).
        F_DAY/F_WK/F_MO → synthesize a `t.me/c/<channel_id>` ref and
        dispatch to the TG handler with the matching window override.
        """
        from unread.bot.burst import BurstItem
        from unread.bot.confirm import RunOptions

        items = pending.payload.get("items") or []
        if not items:
            return
        item = items[0]
        payload = item.payload

        if action == "F_FULL":
            # Existing burst-separately path handles this perfectly —
            # the payload already carries `caption` for file.execute to
            # combine with the image extraction.
            await self._run_batch_separately(pending, panel_msg)
            return

        from unread.bot.progress import edit_progress

        if action == "F_TXT":
            # Synthesize a text-only file payload from the caption (for
            # media+caption forwards) or from the inner text (for
            # text-only forwards), then run as a fresh file item.
            text_content = (payload.get("caption") or payload.get("text") or "").strip()
            if not text_content:
                await edit_progress(panel_msg, "✖ Nothing to analyze (no caption).")
                return
            text_payload = {
                "source": "text",
                "text": text_content,
                "name": "forwarded",
            }
            text_item = BurstItem(kind="file", payload=text_payload, event=item.event)
            options = RunOptions()
            async with self._semaphore:
                # Registered so `/stop` can reach this run; see `register_running`.
                self.register_running(text_item.event.chat_id, asyncio.current_task())
                try:
                    await self._run_execute(
                        text_item.event,
                        "file",
                        text_payload,
                        options,
                        progress_msg=panel_msg,
                    )
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.forward.text_failed")
                    await _safe_reply(text_item.event, f"⚠️ {type(e).__name__}: {e}")
            return

        # F_FROM / F_DAY / F_WK / F_MO → open the source channel.
        channel_id = payload.get("fwd_channel_id")
        if not channel_id:
            await edit_progress(panel_msg, "✖ No source channel ID on this forward.")
            return
        if not self.user_session_ready:
            await edit_progress(
                panel_msg,
                "I don't have your Telegram user session — needed to read "
                "private channels. Send `/upload_session` first.",
            )
            return

        # F_FROM additionally anchors on the forwarded msg's id in the
        # source channel — analyze "what was posted from here forward"
        # without a time window. The other window actions ignore msg id
        # and apply last_days only.
        if action == "F_FROM":
            fwd_msg_id = payload.get("fwd_msg_id")
            if not fwd_msg_id:
                await edit_progress(
                    panel_msg,
                    "✖ No msg id on this forward — can't anchor 'from this message'.",
                )
                return
            tg_payload = {"url": f"https://t.me/c/{int(channel_id)}/{int(fwd_msg_id)}"}
            options = RunOptions(tg_window="from_msg")
        else:
            window_by_action = {"F_DAY": "1d", "F_WK": "7d", "F_MO": "30d"}
            tg_payload = {"url": f"https://t.me/c/{int(channel_id)}"}
            options = RunOptions(tg_window=window_by_action[action])

        async with self._semaphore:
            # Registered so `/stop` can reach this run; see `register_running`.
            self.register_running(item.event.chat_id, asyncio.current_task())
            try:
                await self._run_execute(
                    item.event,
                    "tg",
                    tg_payload,
                    options,
                    progress_msg=panel_msg,
                )
            except Exception as e:
                if _is_clean_exit(e):
                    return
                log.exception("bot.forward.channel_failed", action=action)
                await _safe_reply(item.event, f"⚠️ {type(e).__name__}: {e}")

    async def _run_batch_combined(self, pending, panel_msg) -> None:
        """Concat extracted text from every combinable item → one analyze."""
        from unread.bot.combined import run_combined
        from unread.bot.progress import edit_progress

        items = pending.payload.get("items") or []
        if not items:
            return
        async with self._semaphore:
            # Registered so `/stop` can reach this run; see `register_running`.
            self.register_running(items[0].event.chat_id, asyncio.current_task())
            try:
                # Stamp the app for the reply layer's sticky `/format`
                # lookup. `_run_execute` does this for every other path;
                # the combined and dump paths bypass it.
                with contextlib.suppress(Exception):
                    pending.event._unread_app = self
                await run_combined(self, items=items, panel_msg=panel_msg, original_event=pending.event)
            except Exception as e:
                if _is_clean_exit(e):
                    return
                log.exception("bot.batch.combined_failed")
                await edit_progress(panel_msg, f"⚠️ Combined run failed: {type(e).__name__}: {e}")


def _burst_item_label(item) -> str:
    """One-line description for the in-progress edit. Mirrors burst.summary_line
    but available here without importing the burst module up-top (avoids a
    circular import — burst imports confirm which is fine, but app already
    imports burst lazily inside `_handle`)."""
    from unread.bot.burst import summary_line

    return summary_line(item)


# ----------------------------------------------------------------------
# Module helpers (free functions; no `self` needed)
# ----------------------------------------------------------------------


def _has_session_blob(settings: Settings) -> bool:
    """Cheap, synchronous check: is there any plausible session source?

    Used by the startup gate AND `_verify_user_session` so the bot
    can decide before paying a network round-trip whether there's
    something for `build_client()` to load. Three branches mirror
    `unread.tg.client.build_client`:

    1. `db` / `keychain` backend → Telethon's SQLiteSession lives on
       disk. Telethon appends `.session` to the path you give it, so
       the actual file is `<session_path>.session`; older saves and
       the `default_session_path()` constant both write the bare
       name, so we accept either form.
    2. `passphrase` backend → session string lives in
       `data.sqlite::secrets` as `telegram.session_string`.
    """
    p = Path(settings.telegram.session_path)
    if p.exists() or Path(str(p) + ".session").exists():
        return True
    try:
        from unread.secrets_backend import (
            BACKEND_PASSPHRASE,
            read_active_backend_sync,
        )

        backend = read_active_backend_sync(settings.storage.data_path)
        if backend == BACKEND_PASSPHRASE:
            from unread.db.repo import read_data_db_secrets_sync

            secrets = read_data_db_secrets_sync(settings.storage.data_path)
            return bool(secrets.get("telegram.session_string"))
    except Exception:
        log.exception("bot.session_blob_check_failed")
    return False


async def _probe_session_owner_id(settings: Settings) -> int | None:
    """Open the user-mode client via build_client(), return `me.id` if authorized.

    Going through `build_client` is what reconciles the bot with the
    CLI: same backend resolution (db/keychain/passphrase), same path
    semantics (Telethon's `.session` suffix handling), same secrets
    DB lookup for the encrypted-session case. If the CLI can log in
    on this machine, the bot picks up the same session.

    Hard-capped at 15s total — a flaky network or wedged Telegram
    datacenter shouldn't hang bot startup forever. On timeout the
    probe behaves like "unauthorized": bot keeps running with
    `user_session_ready=False`, TG-link handlers reply "send
    /upload_session", and the operator sees a clear timeout warning.

    Returns the user's Telegram ID on success, None on missing /
    unauthorized / timeout / any error. Always disconnects.
    """
    from unread.tg.client import build_client

    try:
        client = build_client(settings)
    except SystemExit:
        # build_client calls `_exit_missing_telegram_credentials()`
        # via typer.Exit when api_id/api_hash are missing. The bot's
        # `cmd_bot_run` gate catches that earlier — but defensively
        # treat it as "no session" rather than letting it tear down
        # the bot startup.
        return None
    except Exception:
        log.exception("bot.session.build_client_failed")
        return None
    try:
        return await asyncio.wait_for(_probe_inner(client), timeout=15.0)
    except TimeoutError:
        log.warning(
            "bot.session.probe_timeout",
            hint="user-session probe took >15s; continuing without TG-chat support",
        )
        return None
    except Exception:
        log.exception("bot.session.probe_failed")
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _probe_inner(client) -> int | None:
    """Inner body of `_probe_session_owner_id` — wrapped in `wait_for` above."""
    await client.connect()
    if not await client.is_user_authorized():
        return None
    me = await client.get_me()
    if me is None or not getattr(me, "id", 0):
        return None
    return int(me.id)


def _is_clean_exit(exc: BaseException) -> bool:
    """True iff `exc` is a `typer.Exit(0)` / `SystemExit(0)` graceful bail.

    Used to suppress the "⚠️ Exit: 0" reply that would otherwise fire
    when `cmd_analyze*` legitimately exits with no work to do (empty
    window, already-read chat, etc.). Non-zero exit codes still fall
    through to the warning path so a real failure stays visible.
    """
    import typer as _typer

    if isinstance(exc, _typer.Exit | SystemExit):
        return getattr(exc, "exit_code", getattr(exc, "code", None)) in (0, None)
    return False


async def _safe_reply(event: events.NewMessage.Event, text: str) -> None:
    """Reply, swallowing transport errors so the bot loop keeps running."""
    try:
        await event.reply(text)
    except Exception:
        log.exception("bot.reply_failed")
