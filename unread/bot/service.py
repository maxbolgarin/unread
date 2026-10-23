"""Run the bot in the background: `unread bot setup / start / stop / status / logs`.

Two ways to keep `unread bot run` alive, picked per machine:

* **systemd** (Linux with a user session manager): a `systemd --user`
  unit at `~/.config/systemd/user/unread-bot.service`. Restarts on a
  crash, starts at boot once linger is on, logs go to the journal. Same
  unit name and shape `scripts/install-bot.sh` writes, so either one
  manages the other's install.
* **background process** (macOS, containers, anything without a user
  systemd): a detached `unread bot run` with a pid file and a log file
  under `storage/`. No restart on crash and none at boot — `status`
  says so.

`setup` fills `~/.unread/.env.bot` (the canonical bot overlay that
`load_settings` already reads): the @BotFather token and the owner id,
found by reading the `/start` the owner sends their new bot.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console

from unread.core.paths import storage_dir, unread_home

console = Console()

UNIT_NAME = "unread-bot.service"
# Override for a self-hosted Bot API server (or a mock in tests), as in
# scripts/install-bot-docker.sh.
TELEGRAM_API = os.environ.get("UNREAD_TELEGRAM_API", "https://api.telegram.org")
READY_MARKER = "bot ready"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def bot_env_path() -> Path:
    return unread_home() / ".env.bot"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def pid_path() -> Path:
    return storage_dir() / "bot.pid"


def log_path() -> Path:
    return storage_dir() / "bot.log"


def lock_path() -> Path:
    return storage_dir() / "bot.lock"


# ---------------------------------------------------------------------------
# .env.bot
# ---------------------------------------------------------------------------


def update_env_file(path: Path, values: dict[str, str]) -> None:
    """Upsert `KEY='value'` lines, keeping every other line as it was.

    Single quotes keep the value literal for both unread's own loader and
    a shell. Written 0600 from the start — `_load_dotenv` refuses a file
    other users can read.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else ""
        if key in pending and not line.lstrip().startswith("#"):
            out.append(f"{key}='{pending.pop(key)}'")
        else:
            out.append(line)
    out.extend(f"{k}='{v}'" for k, v in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# Telegram Bot API — only for setup: check the token, find the owner.
# ---------------------------------------------------------------------------


class TokenRejected(Exception):
    pass


def merge_ids(*lists: str) -> str:
    """`merge_ids("1,2", "2,3")` → `"1,2,3"`: order kept, duplicates dropped."""
    out: list[str] = []
    for raw in lists:
        for part in raw.replace(" ", "").split(","):
            if part and part not in out:
                out.append(part)
    return ",".join(out)


def valid_ids(raw: str) -> bool:
    """Comma-separated numeric Telegram ids, like `123456789,987654321`."""
    parts = raw.replace(" ", "").split(",")
    return bool(raw.strip()) and all(p.isdigit() and 3 <= len(p) <= 15 for p in parts)


def bot_username(token: str, *, client: httpx.Client | None = None) -> str:
    """`@username` of the bot behind `token`; raises TokenRejected on 401/404."""
    c = client or httpx.Client(timeout=20)
    r = c.get(f"{TELEGRAM_API}/bot{token}/getMe")
    if r.status_code in (401, 404):
        raise TokenRejected
    r.raise_for_status()
    return r.json()["result"]["username"]


@dataclass(frozen=True)
class Sender:
    id: int
    name: str
    username: str

    def describe(self) -> str:
        return f"{self.name} (@{self.username})" if self.username else self.name


def private_senders(updates: list[dict]) -> list[Sender]:
    """People who wrote to the bot in a private chat, oldest first."""
    out = []
    for u in updates:
        msg = u.get("message") or {}
        who = msg.get("from") or {}
        if (msg.get("chat") or {}).get("type") != "private" or who.get("is_bot") or not who.get("id"):
            continue
        name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or "someone"
        out.append(Sender(int(who["id"]), name, who.get("username") or ""))
    return out


def wait_for_sender(token: str, *, timeout: float = 300, client: httpx.Client | None = None) -> Sender | None:
    """Long-poll getUpdates until someone messages the bot.

    Only messages sent after the call count: `offset=-1` first drops the
    backlog, so an old message from somebody else can't be mistaken for
    the owner's /start.
    """
    c = client or httpx.Client(timeout=40)
    base = f"{TELEGRAM_API}/bot{token}/getUpdates"
    latest = c.get(base, params={"offset": -1, "timeout": 0}).json().get("result") or []
    offset = (latest[-1]["update_id"] + 1) if latest else 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        updates = c.get(base, params={"offset": offset, "timeout": 20}).json().get("result") or []
        if updates:
            offset = updates[-1]["update_id"] + 1
            senders = private_senders(updates)
            if senders:
                # Acknowledge, so the /start doesn't linger in the queue.
                with contextlib.suppress(httpx.HTTPError):
                    c.get(base, params={"offset": offset, "timeout": 0})
                return senders[-1]
    return None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def unread_command() -> list[str]:
    """Absolute argv that runs this same `unread`, for a service file.

    The `uv tool` shim on PATH is preferred: it survives `uv tool upgrade`.
    """
    found = shutil.which("unread")
    if found:
        return [os.path.abspath(found)]
    argv0 = Path(sys.argv[0])
    if argv0.name == "unread" and argv0.exists():
        return [str(argv0.resolve())]
    return [sys.executable, "-c", "from unread.cli import main; main()"]


def systemd_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show-environment"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def render_unit(command: list[str]) -> str:
    exec_start = " ".join(_systemd_quote(a) for a in [*command, "bot", "run"])
    env = ""
    if os.environ.get("UNREAD_HOME"):
        env = f"Environment={_systemd_quote('UNREAD_HOME=' + os.environ['UNREAD_HOME'])}\n"
    return f"""[Unit]
Description=unread Telegram bot
After=network-online.target
Wants=network-online.target
# Give up after 5 failed starts in 5 minutes instead of looping forever;
# `unread bot status` shows it, `unread bot start` clears it.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart={exec_start}
{env}Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _systemd_quote(arg: str) -> str:
    if arg and all(ch.isalnum() or ch in "/._-=:+@," for ch in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def linger_enabled() -> bool:
    user = os.environ.get("USER") or ""
    if not user or not shutil.which("loginctl"):
        return False
    r = subprocess.run(
        ["loginctl", "show-user", user, "--property=Linger"], capture_output=True, text=True, check=False
    )
    return r.stdout.strip() == "Linger=yes"


def service_installed() -> bool:
    return unit_path().exists()


def start_systemd() -> None:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(unread_command()), encoding="utf-8")
    was_running = systemd_state() in ("active", "activating", "reloading")
    _systemctl("daemon-reload")
    _systemctl("reset-failed", UNIT_NAME)
    r = _systemctl("enable", "--now", UNIT_NAME)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "systemctl enable failed")
    if was_running:
        # `enable --now` leaves a running unit alone; restart it so a
        # changed setting or an upgraded binary takes effect.
        _systemctl("restart", UNIT_NAME)


def systemd_state() -> str:
    return _systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"


def systemd_logs(lines: int, follow: bool) -> int:
    cmd = ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines), "--no-pager", "-o", "cat"]
    if follow:
        cmd.append("-f")
    return subprocess.call(cmd)


def systemd_recent_log(since: float) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    r = subprocess.run(
        ["journalctl", "--user", "-u", UNIT_NAME, "--since", stamp, "--no-pager", "-o", "cat"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout


def background_pid() -> int | None:
    """PID of the background bot, or None (stale pid files are removed)."""
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pid_path().unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return pid


def start_background() -> subprocess.Popen:
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            [*unread_command(), "bot", "run"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # Not the caller's cwd: `bot run` also reads a `./.env.bot`.
            cwd=unread_home() if unread_home().is_dir() else None,
            env={**os.environ, "NO_COLOR": "1"},
        )
    pid_path().write_text(str(proc.pid))
    # The handle, not the pid: until this command exits, a crashed child is
    # a zombie of ours, and `kill(pid, 0)` still calls it alive.
    return proc


def stop_background(pid: int, *, timeout: float = 15) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    pid_path().unlink(missing_ok=True)


def background_recent_log(offset: int) -> str:
    try:
        with open(log_path(), "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def wait_ready(read_log, alive, *, timeout: float = 60) -> bool | None:
    """True once the log says `bot ready`, False if the bot died, None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if READY_MARKER in read_log():
            return True
        if not alive():
            return False
        time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# One instance per install
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def single_instance():
    """Hold an exclusive lock for the bot's lifetime.

    Two `unread bot run` against one install — the service plus a
    terminal, or a double start — would answer every message twice and
    race on the same session files. The second one fails fast instead.
    """
    try:
        import fcntl
    except ImportError:  # Windows: no advisory locks; carry on unguarded.
        yield
        return
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")  # noqa: SIM115 — held open for the lock's lifetime
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise AlreadyRunning from e
        yield
    finally:
        fh.close()


class AlreadyRunning(Exception):
    pass
