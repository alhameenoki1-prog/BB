"""The login/boot service files and the sender's own clock."""

import asyncio
from pathlib import Path

import pytest

from bb import service


def test_launchd_and_systemd_files(tmp_path):
    root = tmp_path / "BB"; root.mkdir()
    home = tmp_path / "home"
    path, notes = service.install(root, tmp_path / "data", python="/venv/bin/python", system="Darwin", home=home, load=False,
                                  env={"BB_PORT": "8000"})
    assert path == home / "Library" / "LaunchAgents" / "com.bb.outreast.plist".replace("outreast", "outreach")
    text = path.read_text()
    assert "<string>-m</string>" in text and "<string>bb</string>" in text and "/venv/bin/python" in text
    assert "<key>KeepAlive</key>" in text and "<key>RunAtLoad</key>" in text and str(root) in text
    assert str(tmp_path / "data" / "site.log") in text and "<key>PATH</key>" in text and "/opt/homebrew/bin" in text
    assert any("starts at login" in n for n in notes) and any("sleeping" in n for n in notes)
    path, notes = service.install(root, tmp_path / "data", python="/venv/bin/python", system="Linux", home=home, load=False)
    assert path == home / ".config" / "systemd" / "user" / "bb.service"
    unit = path.read_text()
    assert "ExecStart=/venv/bin/python -m bb" in unit and "Restart=always" in unit and f"WorkingDirectory={root}" in unit
    assert "TimeoutStopSec=30" in unit and "Wants=network-online.target" in unit
    assert "WantedBy=default.target" in unit and any("journalctl" in n for n in notes)
    assert service.uninstall(system="Linux", home=home, unload=False) == [f"Removed {path}. The site no longer starts on its own; python -m bb still runs it by hand."]
    assert service.uninstall(system="Linux", home=home, unload=False) == ["No service was installed."]


def test_cli_install_service_writes_the_file(tmp_path, monkeypatch, capsys):
    from bb import __main__ as cli
    monkeypatch.setenv("BB_HOME", str(tmp_path / "home")); monkeypatch.setenv("BB_BASE_URL", "http://x.test")
    monkeypatch.setattr(service, "platform", type("P", (), {"system": staticmethod(lambda: "Linux")}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    cli.main(["install-service", "--no-start"])
    out = capsys.readouterr().out
    assert "Wrote" in out and (tmp_path / "userhome" / ".config" / "systemd" / "user" / "bb.service").exists()
    assert "no bb.env" in out


class _Brain:
    paused_reason = ""
    async def is_within_budget(self, *a, **k):
        return True


class _Handler:
    def __init__(self, ok=True):
        self.ok = ok; self.runs = 0; self.backlog_scans = 0
    async def run(self, backlog=True):
        self.runs += 1
        self.backlog_scans += 1 if backlog else 0
        return self.ok


class _Sender:
    def __init__(self):
        self.runs = 0
    async def run(self):
        self.runs += 1


class _Cfg:
    class usage:
        max_daily_claude_percent = 80; max_daily_cost_usd = 0
        class quiet_hours:
            enabled = False; start = "22:00"; end = "07:00"; timezone = "UTC"
    class channels:
        class email:
            send_every_seconds = 1


@pytest.mark.asyncio
async def test_send_loop_reads_replies_then_sends_on_its_own_clock(monkeypatch):
    import harvey.main as main_mod
    handler, sender, stop = _Handler(), _Sender(), asyncio.Event()
    task = asyncio.create_task(main_mod._send_loop(handler, sender, _Brain(), _Cfg(), stop, 1, 160))
    await asyncio.sleep(0.3)
    assert handler.runs == 1 and sender.runs == 1          # the first pass runs at once, not after the first wait
    assert handler.backlog_scans == 0                      # the frequent passes skip the older-mail sweep
    stop.set(); await task
    # Replies that could not be read: nothing is sent that pass.
    handler, sender = _Handler(ok=False), _Sender()
    assert await main_mod._mail_pass(handler, sender, _Brain(), _Cfg(), 160) == "replies couldn't all be read and handled"
    assert handler.runs == 1 and sender.runs == 0
    # Quiet hours or a paused brain: skipped with the reason.
    class QuietCfg(_Cfg):
        class usage(_Cfg.usage):
            class quiet_hours:
                enabled = True; start = "00:00"; end = "23:59"; timezone = "UTC"
    assert await main_mod._mail_pass(_Handler(), _Sender(), _Brain(), QuietCfg(), 160) == "quiet hours"
    # A paused brain does not stop SMTP sends: the mailbox needs no model.
    paused = _Brain(); paused.paused_reason = "no key"
    sender = _Sender()
    assert await main_mod._mail_pass(_Handler(), sender, paused, _Cfg(), 160) == "" and sender.runs == 1
    # The lock: a heartbeat task holding it makes the pass wait, never overlap.
    handler, sender = _Handler(), _Sender()
    async with main_mod._mail_lock:
        waiter = asyncio.create_task(main_mod._mail_pass(handler, sender, _Brain(), _Cfg(), 160))
        await asyncio.sleep(0.05)
        assert sender.runs == 0
    assert await waiter == "" and sender.runs == 1
