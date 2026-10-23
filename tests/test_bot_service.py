"""`unread bot setup / start / stop / status / logs` — the background bot.

The background-process path runs for real against a stub `unread` on
PATH (it prints the ready line and sleeps); systemd is exercised through
the unit file it writes and a faked `systemctl`.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest
import typer

from unread.bot import service
from unread.config import reset_settings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process handling")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "unread-home"
    (h / "storage").mkdir(parents=True)
    monkeypatch.setenv("UNREAD_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_settings()
    yield h
    reset_settings()


# ---------------------------------------------------------------------------
# .env.bot
# ---------------------------------------------------------------------------


def test_update_env_file_upserts_and_keeps_other_lines(tmp_path):
    path = tmp_path / ".env.bot"
    path.write_text("# mine\nUNREAD_BOT_TOKEN='old'\nOTHER=1\n# UNREAD_BOT_OWNER_ID=commented\n")
    path.chmod(0o644)
    service.update_env_file(path, {"UNREAD_BOT_TOKEN": "new", "UNREAD_BOT_OWNER_ID": "42"})
    assert path.read_text().splitlines() == [
        "# mine",
        "UNREAD_BOT_TOKEN='new'",
        "OTHER=1",
        "# UNREAD_BOT_OWNER_ID=commented",
        "UNREAD_BOT_OWNER_ID='42'",
    ]
    # unread's loader refuses files other users can read.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_values_reach_settings(home):
    from unread.config import load_settings

    service.update_env_file(
        service.bot_env_path(), {"UNREAD_BOT_TOKEN": "1:abc", "UNREAD_BOT_OWNER_ID": "7,8"}
    )
    s = load_settings()
    assert s.bot.token == "1:abc"
    assert s.bot.owner_ids == [7, 8]


# ---------------------------------------------------------------------------
# Bot API helpers
# ---------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_bot_username_and_rejection():
    def handler(req):
        if "/botgood/" in req.url.path:
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_bot"}})
        return httpx.Response(401, json={"ok": False, "error_code": 401})

    c = _client(handler)
    assert service.bot_username("good", client=c) == "my_bot"
    with pytest.raises(service.TokenRejected):
        service.bot_username("bad", client=c)


def _update(uid, name="Max", username="maxb", chat="private", is_bot=False, update_id=10):
    who = {"id": uid, "is_bot": is_bot, "first_name": name}
    if username:
        who["username"] = username
    return {"update_id": update_id, "message": {"chat": {"type": chat}, "from": who}}


def test_private_senders_skips_groups_and_bots():
    got = service.private_senders(
        [_update(1, chat="group"), _update(2, is_bot=True), _update(3, username="")]
    )
    assert got == [service.Sender(3, "Max", "")]
    assert got[0].describe() == "Max"


def test_wait_for_sender_ignores_the_backlog():
    """`offset=-1` drops old messages: someone who wrote last week isn't the owner."""
    calls = []

    def handler(req):
        offset = int(req.url.params["offset"])
        calls.append(offset)
        if offset == -1:
            return httpx.Response(200, json={"ok": True, "result": [_update(999, update_id=5)]})
        if offset == 6 and len(calls) == 2:
            return httpx.Response(200, json={"ok": True, "result": [_update(42, update_id=6)]})
        return httpx.Response(200, json={"ok": True, "result": []})

    sender = service.wait_for_sender("t", timeout=5, client=_client(handler))
    assert sender == service.Sender(42, "Max", "maxb")
    assert calls[:2] == [-1, 6]
    assert calls[-1] == 7  # acknowledged


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------


def test_unit_runs_this_unread_and_keeps_unread_home(monkeypatch):
    monkeypatch.setenv("UNREAD_HOME", "/srv/my bot")
    unit = service.render_unit(["/home/me/.local/bin/unread"])
    assert "ExecStart=/home/me/.local/bin/unread bot run" in unit
    assert 'Environment="UNREAD_HOME=/srv/my bot"' in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_start_systemd_writes_unit_and_enables(home, monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(service, "_systemctl", fake)
    monkeypatch.setattr(service, "unread_command", lambda: ["/opt/unread"])
    service.start_systemd()
    assert "ExecStart=/opt/unread bot run" in service.unit_path().read_text()
    assert ("enable", "--now", service.UNIT_NAME) in calls
    # Not running before → no needless restart right after starting.
    assert ("restart", service.UNIT_NAME) not in calls


def test_systemd_not_used_off_linux(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    assert service.systemd_available() is False


# ---------------------------------------------------------------------------
# Background process, for real
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_unread(tmp_path, monkeypatch, home):
    """A fake `unread` on PATH; STUB_MODE picks ready / crash / hang."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "unread"
    exe.write_text(
        "#!/bin/sh\n"
        'case "$STUB_MODE" in\n'
        '  crash) echo "ApiIdInvalidError: boom"; exit 1 ;;\n'
        "  hang) exec sleep 600 ;;\n"
        "esac\n"
        'echo "→ starting Telegram bot client…"\n'
        'echo "✓ bot ready · owner=42 · session=missing"\n'
        "exec sleep 600\n"
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(service, "systemd_available", lambda: False)
    monkeypatch.setenv("UNREAD_BOT_TOKEN", "1:abc")
    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "42")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0" * 32)
    yield exe
    pid = service.background_pid()
    if pid:
        service.stop_background(pid, timeout=2)


def test_background_start_status_logs_stop(stub_unread, capsys):
    from unread.bot import commands

    commands.cmd_bot_start()
    out = capsys.readouterr().out
    assert "The bot is running as a background process" in out
    pid = service.background_pid()
    assert pid

    commands.cmd_bot_status()
    out = capsys.readouterr().out
    assert "running — background process" in out and "bot ready" in out

    commands.cmd_bot_logs(lines=5, follow=False)
    assert "bot ready" in capsys.readouterr().out

    commands.cmd_bot_start()  # a second start doesn't spawn a second bot
    assert "Already running" in capsys.readouterr().out
    assert service.background_pid() == pid

    commands.cmd_bot_stop()
    assert "Stopped" in capsys.readouterr().out
    assert service.background_pid() is None
    with pytest.raises(typer.Exit):
        commands.cmd_bot_status()


def test_background_crash_is_reported(stub_unread, monkeypatch, capsys):
    from unread.bot import commands

    monkeypatch.setenv("STUB_MODE", "crash")
    with pytest.raises(typer.Exit):
        commands.cmd_bot_start()
    out = capsys.readouterr().out
    assert "stopped right after starting" in out and "ApiIdInvalidError" in out


def test_start_refuses_without_setup(home, monkeypatch, capsys):
    from unread.bot import commands

    for var in ("UNREAD_BOT_TOKEN", "UNREAD_BOT_OWNER_ID", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(commands, "preflight_problems", lambda s: [("@BotFather token missing.", "hint")])
    with pytest.raises(typer.Exit):
        commands.cmd_bot_start()
    out = capsys.readouterr().out
    assert "token missing" in out and "unread bot setup" in out


def test_second_instance_is_refused(home):
    with service.single_instance(), pytest.raises(service.AlreadyRunning), service.single_instance():
        pass
    # Released afterwards.
    with service.single_instance():
        pass


def test_stale_pid_file_is_cleared(home):
    service.pid_path().write_text("999999")
    assert service.background_pid() is None
    assert not service.pid_path().exists()


def test_wait_ready_outcomes():
    assert service.wait_ready(lambda: "✓ bot ready", lambda: True, timeout=1) is True
    assert service.wait_ready(lambda: "", lambda: False, timeout=1) is False
    start = time.monotonic()
    assert service.wait_ready(lambda: "", lambda: True, timeout=1) is None
    assert time.monotonic() - start < 5


def test_log_path_is_under_storage(home):
    assert service.log_path() == Path(home) / "storage" / "bot.log"


# ---------------------------------------------------------------------------
# Allowed people
# ---------------------------------------------------------------------------


def test_merge_and_validate_ids():
    assert service.merge_ids("1,2", "2, 3", "") == "1,2,3"
    assert service.valid_ids("123456789,987654321")
    assert not service.valid_ids("")
    assert not service.valid_ids("12")  # too short to be a Telegram id
    assert not service.valid_ids("123,abc")


def test_users_add_and_remove(home, monkeypatch, capsys):
    from unread.bot import commands
    from unread.config import load_settings

    monkeypatch.delenv("UNREAD_BOT_OWNER_ID", raising=False)
    monkeypatch.setattr(commands, "_service_backend", lambda: None)
    service.update_env_file(service.bot_env_path(), {"UNREAD_BOT_OWNER_ID": "4242"})

    commands.cmd_bot_users("add", ["5555,4242", "6666"])
    assert "Allowed now: 4242,5555,6666" in capsys.readouterr().out
    assert load_settings().bot.owner_ids == [4242, 5555, 6666]

    commands.cmd_bot_users("remove", ["5555"])
    assert load_settings().bot.owner_ids == [4242, 6666]

    commands.cmd_bot_users("list")
    assert "4242" in capsys.readouterr().out

    with pytest.raises(typer.Exit):
        commands.cmd_bot_users("remove", ["4242,6666"])
    assert "nobody" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        commands.cmd_bot_users("add", ["12ab"])


def test_users_refuses_when_env_overrides_the_file(home, monkeypatch, capsys):
    from unread.bot import commands

    monkeypatch.setenv("UNREAD_BOT_OWNER_ID", "4242")
    with pytest.raises(typer.Exit):
        commands.cmd_bot_users("add", ["5555"])
    assert "set in the environment" in capsys.readouterr().out


def test_setup_keeps_earlier_extra_people(monkeypatch):
    """Re-detecting the owner must not silently drop people added before."""
    from unread.bot import commands
    from unread.util import prompt

    monkeypatch.setattr(prompt, "ask_text", lambda msg, default="", **kw: default)
    assert commands._ask_more_people("4242", previous="4242,5555") == "4242,5555"
    monkeypatch.setattr(prompt, "ask_text", lambda msg, default="", **kw: "none")
    assert commands._ask_more_people("4242", previous="4242,5555") == "4242"
