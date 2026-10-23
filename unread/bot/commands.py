"""Typer command surface for `unread bot`.

The `bot_app` Typer subgroup is constructed in :mod:`unread.cli` so it
can share the `_UnreadTyper` / `_UnreadGroup` machinery the rest of the
CLI uses. This module only owns the async command bodies — the same
split as `unread/tg/commands.py`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer
from rich.console import Console

from unread.config import get_settings, reset_settings
from unread.i18n import t as _t

console = Console()


def _maybe_opt_into_cwd_env_bot() -> str | None:
    """When the operator hasn't set `UNREAD_BOT_ENV_FILE` and a
    `./.env.bot` exists in CWD, point at it FOR THIS CALL ONLY.

    Returns the previous value of `UNREAD_BOT_ENV_FILE` (or None when
    unset) so the caller can restore it. We do NOT permanently mutate
    `os.environ` because the bot process spawns subprocesses (ffmpeg
    at minimum), and a leaked file path would pollute their env.

    Only `unread bot run` opts into CWD discovery — every other
    command stays strict (canonical `~/.unread/.env.bot` + explicit
    override only). This matches the user expectation that
    ``cp .env.bot.example .env.bot && unread bot run`` Just Works
    in a project checkout, while preserving the rule that a stray
    `.env.bot` in some unrelated directory never silently shadows
    real settings for non-bot commands.

    Returns the sentinel string `"__unread_sentinel_unset__"` when
    the var was unset before, so the caller can distinguish "was
    empty string" from "was missing".
    """
    if os.environ.get("UNREAD_BOT_ENV_FILE"):
        return None  # Caller already opted in; nothing to do, nothing to restore.
    cwd_candidate = Path.cwd() / ".env.bot"
    if not cwd_candidate.is_file():
        return None
    os.environ["UNREAD_BOT_ENV_FILE"] = str(cwd_candidate)
    return _UNSET_SENTINEL


_UNSET_SENTINEL = "__unread_sentinel_unset__"


def _restore_bot_env_var(previous: str | None) -> None:
    """Counterpart to `_maybe_opt_into_cwd_env_bot` — restore os.environ."""
    if previous is None:
        return
    if previous == _UNSET_SENTINEL:
        os.environ.pop("UNREAD_BOT_ENV_FILE", None)
    else:
        os.environ["UNREAD_BOT_ENV_FILE"] = previous


def preflight_problems(s) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """What stops the bot from starting, as `(problem, hint)` pairs.

    Shared by `bot run` (refuse to start) and `bot start` (refuse to
    install a service that would only crash-loop).

    1. Telegram api_id / api_hash — needed even in bot mode: Telethon's
       MTProto layer authenticates the *application* separately from the
       account.
    2. The @BotFather token.
    3. Some path to a non-empty allowlist: `UNREAD_BOT_OWNER_ID`, or an
       authorized user session the owner is derived from. With neither,
       the first person to message the bot would become its owner (TOFU)
       — refused.
    """
    from unread.bot.app import _has_session_blob
    from unread.cli import _telegram_credentials_present

    problems: list[tuple[str, str]] = []
    if not _telegram_credentials_present():
        problems.append((_t("bot_missing_tg_creds"), _t("bot_missing_tg_creds_hint")))
    if not s.bot.token:
        problems.append((_t("bot_missing_token"), _t("bot_missing_token_hint")))
    if not s.bot.owner_ids and not _has_session_blob(s):
        problems.append((_t("bot_missing_owner_id"), _t("bot_missing_owner_id_hint")))
    return problems


async def cmd_bot_run() -> None:
    """Start the self-hosted Telegram bot in long-polling mode.

    Blocks forever (until SIGINT). `owner_id` is auto-derived from
    `settings.telegram.session_path` when that session is present and
    authorized, so a deploy that mounts the session ahead of time doesn't
    need `UNREAD_BOT_OWNER_ID` at all. One bot per install: a second
    `run` (say, a terminal next to the background service) exits instead
    of answering every message twice.
    """
    from unread.bot.app import BotApp
    from unread.bot.service import AlreadyRunning, single_instance

    # Opt into the CWD `.env.bot` convention BEFORE the settings
    # singleton crystallizes, so the values land in the dotenv
    # overlay rather than as too-late shell mutations. The env-var
    # mutation is restored on exit so test code (and any wrapping
    # process) doesn't see a leaked path.
    _restore = _maybe_opt_into_cwd_env_bot()
    try:
        reset_settings()
        s = get_settings()
        problems = preflight_problems(s)
        if problems:
            problem, hint = problems[0]
            console.print(f"[red]{problem}[/]\n[grey70]{hint}[/]")
            raise typer.Exit(1)
        try:
            with single_instance():
                await BotApp(s).run_forever()
        except AlreadyRunning:
            console.print(
                "[red]The bot is already running for this install.[/] "
                "[grey70]Stop it first (`unread bot stop`), or check `unread bot status`.[/]"
            )
            raise typer.Exit(1) from None
    finally:
        _restore_bot_env_var(_restore)
        reset_settings()


# ---------------------------------------------------------------------------
# setup / start / stop / restart / status / logs
# ---------------------------------------------------------------------------


def _print_problems(problems: list[tuple[str, str]]) -> None:
    for problem, hint in problems:
        console.print(f"[red]✖ {problem}[/]\n  [grey70]{hint}[/]")


def _bot_ai_key_present(s) -> bool:  # type: ignore[no-untyped-def]
    from unread.ai.providers import _resolve_provider_name

    provider = _resolve_provider_name(s, "chat")
    if provider == "local":
        return True
    return bool(getattr(getattr(s, provider, None), "api_key", ""))


def cmd_bot_setup() -> None:
    """Interactive: Telegram API id/hash if missing, bot token, owner id."""
    import asyncio

    from unread.bot import service
    from unread.bot.app import _has_session_blob
    from unread.cli import _telegram_credentials_present
    from unread.util.prompt import ask_text, confirm

    reset_settings()
    s = get_settings()
    console.print(f"[bold]Telegram bot setup[/] — answers go to [cyan]{service.bot_env_path()}[/]\n")

    # 1. Telegram API id + hash — every MTProto app needs them, bots too.
    if not _telegram_credentials_present():
        console.print(
            "The bot connects to Telegram the way apps do (MTProto), not via the plain Bot API,\n"
            "which can't download files over 20 MB. That connection needs an app id and hash,\n"
            "even though you never log in to your own account. They're free:\n"
            "  1. Open https://my.telegram.org and log in with your phone number.\n"
            '  2. Open "API development tools" and create an app (any name).\n'
        )
        api_id = ""
        while not api_id.isdigit():
            api_id = ask_text("App api_id").strip()
        api_hash = ""
        while len(api_hash) != 32:
            api_hash = ask_text("App api_hash", password=True).strip()
        from unread.secrets import write_secrets

        asyncio.run(write_secrets(s, {"telegram.api_id": api_id, "telegram.api_hash": api_hash}))
        console.print("[green]✓[/] Telegram API id and hash saved.\n")

    # 2. Bot token, checked with Telegram.
    token = s.bot.token
    username = ""
    if token:
        try:
            username = service.bot_username(token)
        except Exception:
            token = ""
        if token and not confirm(f"Keep the current bot, @{username}?", default=True):
            token = ""
    while not token:
        console.print("In Telegram, open @BotFather, send /newbot and follow the prompts.")
        candidate = ask_text("Bot token", password=True).strip()
        try:
            username = service.bot_username(candidate)
        except service.TokenRejected:
            console.print("[yellow]Telegram rejected that token — copy it again from @BotFather.[/]")
            continue
        except Exception as e:  # offline, proxy…
            console.print(f"[yellow]Couldn't reach Telegram to check it ({type(e).__name__}); keeping it.[/]")
        token = candidate
    console.print(f"[green]✓[/] Bot: @{username or '?'}\n")

    # 3. Who may use it. A logged-in Telegram session already names the
    #    owner; otherwise read the id off the /start they send the bot.
    owner = ",".join(str(i) for i in s.bot.owner_ids)
    if owner and not confirm(f"Keep the allowed users ({owner})?", default=True):
        owner = ""
    if not owner and _has_session_blob(s):
        console.print("[green]✓[/] Owner: the account you logged in with in `unread init`.\n")
    elif not owner:
        console.print(
            f"Open [bold]https://t.me/{username}[/] and press [bold]Start[/]. "
            "[grey70]Waiting… (Ctrl-C to type your id instead)[/]"
        )
        try:
            sender = service.wait_for_sender(token)
        except KeyboardInterrupt:
            sender = None
        except Exception as e:
            console.print(f"[yellow]Couldn't read the bot's messages ({type(e).__name__}).[/]")
            sender = None
        if sender and confirm(
            f"Got a message from {sender.describe()}, id {sender.id}. Is that you?", default=True
        ):
            owner = str(sender.id)
        while not owner:
            raw = ask_text("Your numeric Telegram id (@userinfobot tells you)").replace(" ", "")
            if raw and all(part.isdigit() for part in raw.split(",")):
                owner = raw
        console.print(f"[green]✓[/] Owner: {owner}\n")
        owner = _ask_more_people(owner, previous=",".join(str(i) for i in s.bot.owner_ids))

    values = {"UNREAD_BOT_TOKEN": token}
    if owner:
        values["UNREAD_BOT_OWNER_ID"] = owner
    service.update_env_file(service.bot_env_path(), values)

    reset_settings()
    if not _bot_ai_key_present(get_settings()):
        console.print(
            "[yellow]! No AI key for the active provider yet.[/] Run `unread init`, or set one "
            "in the bot later: /settings → 🔑 API key.\n"
        )
    if confirm("Start the bot now?", default=True):
        cmd_bot_start()
    else:
        console.print("Start it any time with [bold]unread bot start[/].")


def _ask_more_people(owner: str, *, previous: str = "") -> str:
    """Offer to allow more people besides `owner`; returns the merged list.

    `previous` is the list before this setup: its other people are the
    default, so re-detecting the owner doesn't drop them.
    """
    from unread.bot import service
    from unread.util.prompt import ask_text

    first = owner.split(",", maxsplit=1)[0]
    before = ",".join(i for i in previous.split(",") if i and i != first)
    console.print(
        "Anyone else? Their numeric ids (they can message @userinfobot to get theirs). "
        "[grey70]Change it later with `unread bot users add ID`.[/]"
    )
    while True:
        hint = '"none" removes them' if before else "Enter to skip"
        raw = ask_text(f"Other people's ids, comma-separated ({hint})", default=before).replace(" ", "")
        if not raw or raw == "none":
            return owner
        if service.valid_ids(raw):
            merged = service.merge_ids(owner, raw)
            console.print(f"[green]✓[/] Allowed: {merged} (the first one manages AI settings)\n")
            return merged
        console.print("[yellow]Type digits only, like 123456789 (several: 111,222).[/]")


def cmd_bot_users(action: str = "list", ids: list[str] | None = None) -> None:
    """List, add or remove the people allowed to use the bot."""
    from unread.bot import service

    reset_settings()
    current = ",".join(str(i) for i in get_settings().bot.owner_ids)
    raw = ",".join(ids or []).replace(" ", "")
    if action == "list":
        if not current:
            console.print("Nobody is listed yet. Run [bold]unread bot setup[/].")
            return
        console.print("People who may use the bot (the first one manages AI settings):")
        for i in current.split(","):
            console.print(f"  {i}")
        console.print("[grey70]Add or remove with: unread bot users add ID / unread bot users remove ID[/]")
        return
    if action not in ("add", "remove"):
        console.print("[red]Use: unread bot users \\[list | add ID… | remove ID…][/]")
        raise typer.Exit(2)
    if not raw or not service.valid_ids(raw):
        console.print(
            f"[red]Which id?[/] e.g. unread bot users {action} 123456789 — Telegram ids are digits "
            "only (people get theirs from @userinfobot)."
        )
        raise typer.Exit(1)
    if os.environ.get("UNREAD_BOT_OWNER_ID") is not None:
        console.print(
            "[red]UNREAD_BOT_OWNER_ID is set in the environment, which overrides the file.[/] "
            "Change it there."
        )
        raise typer.Exit(1)
    if action == "add":
        new = service.merge_ids(current, raw)
    else:
        drop = set(raw.split(","))
        new = ",".join(i for i in current.split(",") if i and i not in drop)
        if not new:
            console.print("[red]That would leave nobody who can use the bot.[/]")
            raise typer.Exit(1)
    if new == current:
        console.print(f"Nothing to change: {current}")
        return
    service.update_env_file(service.bot_env_path(), {"UNREAD_BOT_OWNER_ID": new})
    console.print(f"[green]✓[/] Allowed now: {new} (the first one, {new.split(',')[0]}, manages AI settings)")
    if _service_backend():
        cmd_bot_restart()
    else:
        console.print("It applies the next time the bot starts.")


def _service_backend() -> str | None:
    """Where the bot runs now: "systemd", "background" or None."""
    from unread.bot import service

    if (
        service.service_installed()
        and service.systemd_available()
        and service.systemd_state() in ("active", "activating", "reloading")
    ):
        return "systemd"
    if service.background_pid():
        return "background"
    return None


def _systemd_alive() -> bool:
    from unread.bot import service

    state = service.systemd_state()
    restarts = service._systemctl("show", service.UNIT_NAME, "-p", "NRestarts", "--value").stdout.strip()
    return state in ("active", "activating") and restarts in ("", "0")


def cmd_bot_start(*, no_service: bool = False) -> None:
    """Start the bot in the background: a systemd user service when the
    machine has one, a detached process otherwise."""
    import time

    from unread.bot import service

    reset_settings()
    problems = preflight_problems(get_settings())
    if problems:
        _print_problems(problems)
        console.print("\nRun [bold]unread bot setup[/] to fill these in.")
        raise typer.Exit(1)

    running = _service_backend()
    if running == "background" and not no_service and service.systemd_available():
        # Moving from a plain process to the service: stop the old one first.
        pid = service.background_pid()
        if pid:
            service.stop_background(pid)
        running = None
    if running == "background":
        console.print("[green]●[/] Already running in the background — `unread bot restart` to restart it.")
        return

    if service.systemd_available() and not no_service:
        since = time.time()
        try:
            service.start_systemd()
        except RuntimeError as e:
            console.print(f"[red]systemd refused to start it:[/] {e}")
            raise typer.Exit(1) from None
        console.print("[grey70]Waiting for the bot to connect to Telegram…[/]")
        ready = service.wait_ready(lambda: service.systemd_recent_log(since), _systemd_alive)
        log = service.systemd_recent_log(since)
        if ready is False:
            service._systemctl("disable", "--now", service.UNIT_NAME)
        _report_start(ready, log, where=f"as the systemd user service {service.UNIT_NAME}")
        if ready is not False and not service.linger_enabled():
            user = os.environ.get("USER", "$USER")
            console.print(
                "[yellow]![/] To keep it running after you log out and start it at boot, run once:\n"
                f"    [bold]sudo loginctl enable-linger {user}[/]"
            )
        return

    offset = service.log_path().stat().st_size if service.log_path().exists() else 0
    proc = service.start_background()
    console.print("[grey70]Waiting for the bot to connect to Telegram…[/]")
    ready = service.wait_ready(lambda: service.background_recent_log(offset), lambda: proc.poll() is None)
    if ready is False:
        service.pid_path().unlink(missing_ok=True)
    _report_start(ready, service.background_recent_log(offset), where="as a background process")
    if ready is not False:
        console.print(
            "[grey70]It stops when this computer sleeps or restarts, and isn't restarted after a crash. "
            "For always-on, use a server: the Docker installer or a machine with systemd.[/]"
        )


def _report_start(ready: bool | None, log: str, *, where: str) -> None:
    tail = "\n".join("    " + line for line in log.strip().splitlines()[-15:])
    if ready:
        console.print(
            f"[green]✓ The bot is running[/] {where}. Manage it with `unread bot status | logs | stop`."
        )
        return
    if ready is False:
        console.print(f"[red]✖ The bot stopped right after starting.[/] Its last log lines:\n{tail}")
        console.print("Fix the setting (`unread bot setup`, `unread init`) and run `unread bot start` again.")
        raise typer.Exit(1)
    console.print(
        f"[yellow]! Started {where}, but it hasn't reported ready yet.[/] Last log lines:\n{tail}\n"
        "Follow along with `unread bot logs -f`."
    )


def cmd_bot_stop() -> None:
    from unread.bot import service

    stopped = False
    if service.service_installed() and service.systemd_available():
        if service.systemd_state() != "inactive":
            stopped = True
        # disable, not just stop: a stopped-but-enabled unit comes back at boot.
        service._systemctl("disable", "--now", service.UNIT_NAME)
    pid = service.background_pid()
    if pid:
        service.stop_background(pid)
        stopped = True
    console.print("[green]✓[/] Stopped." if stopped else "The bot isn't running.")


def cmd_bot_restart() -> None:
    from unread.bot import service

    pid = service.background_pid()
    if pid:
        service.stop_background(pid)
    cmd_bot_start()


def cmd_bot_status() -> None:
    from unread.bot import service

    backend = _service_backend()
    if backend == "systemd":
        out = subprocess.run(
            ["journalctl", "--user", "-u", service.UNIT_NAME, "-n", "300", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        console.print(f"[green]●[/] running — systemd user service ({service.UNIT_NAME})")
    elif backend == "background":
        out = service.background_recent_log(0)
        console.print(f"[green]●[/] running — background process, pid {service.background_pid()}")
    else:
        console.print("[yellow]●[/] not running — start it with `unread bot start`")
        if service.service_installed() and service.systemd_available():
            state = service.systemd_state()
            if state == "failed":
                console.print("  [red]The service failed.[/] See `unread bot logs`.")
        raise typer.Exit(1)
    ready = [line for line in out.splitlines() if service.READY_MARKER in line]
    if ready:
        console.print(f"  {ready[-1].strip()}")
    else:
        console.print("  [yellow]not connected yet[/] — see `unread bot logs`")
    if backend == "systemd" and not service.linger_enabled():
        console.print("  [grey70]stops when you log out — `sudo loginctl enable-linger $USER` keeps it on[/]")


def cmd_bot_logs(*, lines: int, follow: bool) -> None:
    import time

    from unread.bot import service

    if service.service_installed() and service.systemd_available():
        raise typer.Exit(service.systemd_logs(lines, follow))
    path = service.log_path()
    if not path.exists():
        console.print("No bot log yet — start the bot with `unread bot start`.")
        raise typer.Exit(1)
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(text[-lines:]))
    if not follow:
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
