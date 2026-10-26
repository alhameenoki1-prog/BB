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
    assert "<key>ProcessType</key>" in text and "<string>Interactive</string>" in text   # not throttled as a background job
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
    monkeypatch.setattr(cli, "ENV_FILES", (tmp_path / "bb.env",))
    cli.main(["install-service", "--no-start"])
    out = capsys.readouterr().out
    assert "Wrote" in out and (tmp_path / "userhome" / ".config" / "systemd" / "user" / "bb.service").exists()
    # The shell's exports were folded into bb.env so the service starts the same way.
    assert "Added to" in out and "BB_BASE_URL" in out
    assert "BB_BASE_URL=http://x.test" in (tmp_path / "bb.env").read_text() and "BB_HOME=" in (tmp_path / "bb.env").read_text()
    cli.main(["install-service", "--no-start"])
    assert "Added to" not in capsys.readouterr().out            # nothing new the second time


class _Brain:
    paused_reason = ""
    async def is_within_budget(self, *a, **k):
        return True


class _Handler:
    def __init__(self, ok=True):
        self.ok = ok; self.runs = 0; self.backlog_scans = 0; self.models = []
    async def run(self, backlog=True, model=True):
        self.runs += 1
        self.backlog_scans += 1 if backlog else 0
        self.models.append(model)
        return self.ok


class _Sender:
    def __init__(self):
        self.runs = 0; self.first_only = []; self.auth_refused = False
    async def run(self, first_only=False):
        self.runs += 1; self.first_only.append(first_only)


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
    # Replies that could not be read: follow-ups wait, first emails still go
    # (nobody has written back to one yet, so there is no reply to miss).
    handler, sender = _Handler(ok=False), _Sender()
    assert await main_mod._mail_pass(handler, sender, _Brain(), _Cfg(), 160) == \
        "replies couldn't all be read and handled; follow-ups wait, first emails still go out"
    assert handler.runs == 1 and sender.runs == 1 and sender.first_only == [True]
    # The handler's own reason travels with it, so the dashboard can say why.
    handler = _Handler(ok=False); handler.last_problem = "the AI can't answer right now: usage limit reached"
    assert await main_mod._mail_pass(handler, _Sender(), _Brain(), _Cfg(), 160) == \
        "replies couldn't all be read and handled (the AI can't answer right now: usage limit reached); " \
        "follow-ups wait, first emails still go out"
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


def test_cli_sees_a_running_site_and_its_version(tmp_path, monkeypatch, capsys):
    from bb import __main__ as cli
    from bb.workspaces import code_version
    monkeypatch.setenv("BB_HOME", str(tmp_path / "home")); monkeypatch.setenv("BB_PORT", "8123")
    monkeypatch.setattr(cli, "ENV_FILES", ())
    assert cli.running_site(8123) is None
    with pytest.raises(SystemExit):
        cli.main(["restart"])
    assert "Nothing is running" in capsys.readouterr().out
    # A site answering /health with an older version: serve refuses and says what to do.
    monkeypatch.setattr(cli, "running_site", lambda port: {"ok": True, "version": "older"})
    with pytest.raises(SystemExit):
        cli.main(["serve"])
    out = capsys.readouterr().out
    assert "already running" in out and "OLDER code" in out
    cli.main(["status"])
    assert "OLDER code than the files on disk" in capsys.readouterr().out
    monkeypatch.setattr(cli, "running_site", lambda port: {"ok": True, "version": code_version()})
    cli.main(["status"])
    assert "on the current code" in capsys.readouterr().out


def test_templates_tolerate_a_half_applied_update(tmp_path, monkeypatch):
    """Templates are read from disk; after git pull they may name variables
    the running program does not pass yet. That renders empty, not a 500."""
    from starlette.testclient import TestClient
    from bb.app import create_app
    from tests.test_bb_web import _settings, _admin
    monkeypatch.setattr("bb.app.check_domain", lambda d: asyncio.sleep(0, result=None))
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _admin(client)
        tpl = client.app.state.settings and __import__("bb.app", fromlist=["TEMPLATES_DIR"]).TEMPLATES_DIR / "admin_users.html"
        original = tpl.read_text()
        tpl.write_text(original.replace("<h2>Create a login</h2>", "<h2>Create a login {{ brand_new.thing.nobody_passes }}</h2>"))
        try:
            r = client.get("/admin/users")
            assert r.status_code == 200 and "Create a login" in r.text
        finally:
            tpl.write_text(original)


@pytest.mark.asyncio
async def test_a_paused_or_over_budget_brain_still_sends_first_emails():
    """Out of allowance (or over the daily cap): replies wait for the model,
    but first emails need none and still go out; follow-ups wait with the
    replies."""
    import harvey.main as main_mod

    class _Over(_Brain):
        paused_reason = "LLM API usage limit reached for this plan"
        async def is_within_budget(self, *a, **k):
            return False

    handler, sender = _Handler(), _Sender()
    out = await main_mod._mail_pass(handler, sender, _Over(), _Cfg(), 160)
    assert out.startswith("LLM API usage limit reached") and out.endswith("follow-ups wait, first emails still go out")
    # Bounces are still read, with no model call; then first emails go.
    assert handler.runs == 1 and handler.models == [False] and handler.backlog_scans == 0
    assert sender.runs == 1 and sender.first_only == [True]
    # A mailbox that can't be read for bounces does not hold the first emails.
    class _Broken(_Handler):
        async def run(self, backlog=True, model=True):
            raise OSError("IMAP down")
    sender = _Sender()
    await main_mod._mail_pass(_Broken(), sender, _Over(), _Cfg(), 160)
    assert sender.first_only == [True]


@pytest.mark.asyncio
async def test_a_refused_login_stays_on_the_dashboard(tmp_path):
    """The sender writes "the mailbox refused the login" (the Reconnect card
    reads it); the send loop's own reason must not write over it."""
    import harvey.main as main_mod
    from harvey.state import StateManager
    sm = StateManager(str(tmp_path / "h.db")); await sm.init_db()

    class _Refusing(_Sender):
        async def run(self, first_only=False):
            await super().run(first_only)
            self.auth_refused = True
            await sm.set_setting("sender_skip_reason", "the mailbox refused the login: 535 bad credentials")

    stop = asyncio.Event()
    task = asyncio.create_task(main_mod._send_loop(_Handler(ok=False), _Refusing(), _Brain(), _Cfg(), stop, 1, 160, sm))
    await asyncio.sleep(0.3)
    stop.set(); await task
    assert (await sm.get_setting("sender_skip_reason")).startswith("the mailbox refused the login")


@pytest.mark.asyncio
async def test_the_over_budget_heartbeat_writes_your_message_and_leaves_sending_to_the_send_loop(monkeypatch):
    """Over the AI budget with the general message on: the heartbeat writes it
    with no model call and checks the mailbox; with a send loop running it
    sends nothing itself (the loop keeps the pace), without one it reads
    bounces and sends first emails. It comes back in one interval, not an hour."""
    import harvey.main as main_mod
    from types import SimpleNamespace

    class _Over(_Brain):
        provider = "openai"; provider_label = "LLM"
        paused_reason = "LLM API usage limit reached for this plan"
        async def preflight(self):
            return ""
        async def recheck(self):
            pass
        async def is_within_budget(self, *a, **k):
            return False

    class _Writer:
        use_template = False; template_fallback = True
        def __init__(self):
            self.runs = []
        async def run(self, use_model=True):
            self.runs.append(use_model)

    class _NativeSender(_Sender):
        is_native = True; provider = object(); ledger = None

    checks, sleeps = [], []
    async def fake_check(state, provider, config, quiet=False, force=False):
        checks.append(provider)
    monkeypatch.setattr("harvey.health.run_check", fake_check)

    async def fake_sleep(seconds, stop_event, *a, **k):
        sleeps.append(seconds); stop_event.set(); return True
    monkeypatch.setattr(main_mod, "_interruptible_sleep", fake_sleep)
    monkeypatch.setattr(main_mod, "_ledger_checkin", lambda *a, **k: asyncio.sleep(0))
    cfg = SimpleNamespace(usage=SimpleNamespace(max_daily_claude_percent=80, max_daily_cost_usd=0,
                                                heartbeat_interval_minutes=5, quiet_hours=_Cfg.usage.quiet_hours),
                          channels=_Cfg.channels)
    for has_loop in (True, False):
        writer, sender, handler, stop = _Writer(), _NativeSender(), _Handler(), asyncio.Event()
        checks.clear(); sleeps.clear()
        await main_mod._heartbeat_cycles(stop, cfg, None, _Over(), None, writer, sender, handler, None,
                                         300, 160, 0, has_loop)
        assert writer.runs == [False] and len(checks) == 1 and sleeps == [300]
        if has_loop:
            assert sender.runs == 0 and handler.runs == 0
        else:
            assert sender.first_only == [True] and handler.models == [False]
