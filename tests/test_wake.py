"""Wake up now, and a worker that stopped answering: every sleep in the
worker ends on a wake or by the wall clock, the beat shows what it is
doing, the dashboard and Campaigns page wake it, one that does not answer
is restarted, and the site restarts one that froze."""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

import bb.workspaces as ws_mod
from bb.accounts import Accounts
from bb.workspaces import Workspace
from harvey import wake
from tests.test_bb_web import USER, _as_user, client, settings  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_wake_state():
    wake._pending.clear()
    wake._folder["path"] = None
    wake._state.update(state="starting", until=0.0, detail="")
    yield
    wake._pending.clear()
    wake._folder["path"] = None


# ── the worker's side ──

@pytest.mark.asyncio
async def test_a_sleep_ends_on_time_on_stop_and_on_a_wake(tmp_path):
    stop = asyncio.Event()
    assert await wake.sleep(0.05, stop) == "time"
    stop.set()
    assert await wake.sleep(60, stop) == "stop"
    stop.clear()
    wake._folder["path"] = tmp_path

    async def press():
        await asyncio.sleep(0.2)
        wake.request_wake(tmp_path, "a test")
        assert wake.check_for_wake()
    t0 = time.monotonic()
    out, _ = await asyncio.gather(wake.sleep(600, stop), press())
    assert out == "wake" and time.monotonic() - t0 < 5 and not wake.wake_pending(tmp_path)
    beat = wake.read_beat(tmp_path)
    assert beat["woken_by"] == "a test" and beat["woken_from"] == "sleeping" and beat["pid"] == os.getpid()


@pytest.mark.asyncio
async def test_a_wake_during_a_cycle_skips_the_next_sleep_for_each_loop(tmp_path):
    stop = asyncio.Event()
    wake._folder["path"] = tmp_path
    wake.request_wake(tmp_path)
    assert wake.check_for_wake()                       # it came while the worker was busy
    assert await wake.sleep(600, stop, "heartbeat") == "wake"
    assert await wake.sleep(600, stop, "send", show=False) == "wake"
    assert await wake.sleep(0.05, stop, "heartbeat") == "time"    # taken once, not forever


@pytest.mark.asyncio
async def test_a_sleep_ends_by_the_wall_clock_after_the_machine_slept(monkeypatch):
    stop = asyncio.Event()
    real = time.time
    jump = {"by": 0.0}
    monkeypatch.setattr(wake.time, "time", lambda: real() + jump["by"])

    async def lid_closed_for_an_hour():
        await asyncio.sleep(0.2)
        jump["by"] = 3600.0
    t0 = time.monotonic()
    out, _ = await asyncio.gather(wake.sleep(60, stop), lid_closed_for_an_hour())
    assert out == "time" and time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_the_beat_says_what_the_worker_does(tmp_path):
    stop = asyncio.Event()
    task = asyncio.create_task(wake.beat_loop(stop, tmp_path))
    await asyncio.sleep(0.1)
    beat = wake.read_beat(tmp_path)
    assert beat["pid"] == os.getpid() and beat["age"] < 5
    wake.set_state("quiet", time.time() + 3600)
    assert wake.read_beat(tmp_path)["state"] == "quiet"
    stop.set()
    await task
    assert wake.read_beat(tmp_path)["state"] == "stopped"
    assert wake.read_beat(tmp_path / "nowhere") is None


@pytest.mark.asyncio
async def test_the_heartbeat_sleep_wakes_and_still_stops(tmp_path):
    from harvey.main import _interruptible_sleep
    stop = asyncio.Event()
    wake._folder["path"] = tmp_path
    wake.request_wake(tmp_path)
    wake.check_for_wake()
    assert await _interruptible_sleep(600, stop) is False      # woken: the loop carries on
    stop.set()
    assert await _interruptible_sleep(600, stop) is True       # shutdown


def test_a_real_worker_process_wakes_from_another_process(tmp_path):
    """The site and the worker are two processes: the file is the bridge."""
    script = tmp_path / "w.py"
    script.write_text(
        "import asyncio, sys\n"
        "from harvey import wake\n"
        "async def main():\n"
        "    stop = asyncio.Event()\n"
        "    beat = asyncio.create_task(wake.beat_loop(stop))\n"
        "    out = await wake.sleep(600, stop)\n"
        "    print(out, flush=True)\n"
        "    stop.set(); await beat\n"
        "asyncio.run(main())\n")
    home = tmp_path / "home"
    env = {**os.environ, "HARVEY_HOME": str(home), "PYTHONPATH": os.getcwd()}
    proc = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE, text=True, cwd=os.getcwd())
    try:
        for _ in range(100):
            if wake.read_beat(home / "data"):
                break
            time.sleep(0.1)
        beat = wake.read_beat(home / "data")
        assert beat and beat["pid"] == proc.pid and beat["state"] == "sleeping"
        wake.request_wake(home / "data", "a test")
        out, _ = proc.communicate(timeout=20)
        assert out.strip() == "wake" and not wake.wake_pending(home / "data")
    finally:
        if proc.poll() is None:
            proc.kill()


# ── the site's side ──

def _ws(client):
    _as_user(client)
    user = Accounts(client.settings.accounts_db).get_by_email(USER["email"])
    ws = Workspace(client.settings, user)
    ws.ensure()
    return ws


def _beat(ws, pid, age=0.0, state="sleeping", **extra):
    ws.data_dir.mkdir(parents=True, exist_ok=True)
    (ws.data_dir / wake.BEAT_FILE).write_text(json.dumps({"at": time.time() - age, "pid": pid, "state": state,
                                                          "until": time.time() + 50, **extra}))


@pytest.mark.asyncio
async def test_wake_up_now_reaches_a_running_worker(client, monkeypatch):
    ws = _ws(client)
    me = os.getpid()
    monkeypatch.setattr(Workspace, "worker_running", lambda self: me)
    _beat(ws, me)

    async def worker_answers():
        await asyncio.sleep(0.3)
        wake._folder["path"] = ws.data_dir
        wake._state.update(state="sleeping")
        assert wake.check_for_wake()
    (ok, msg), _ = await asyncio.gather(ws.wake_worker(), worker_answers())
    assert ok and msg == "Woken up: it is running a cycle now."
    # In quiet hours it says so, and how to send around the clock.
    wake._state.update(state="quiet", until=time.time() + 7200)

    async def answers_in_quiet_hours():
        await asyncio.sleep(0.3)
        assert wake.check_for_wake()
    (ok, msg), _ = await asyncio.gather(ws.wake_worker(), answers_in_quiet_hours())
    assert ok and "quiet hours for another 1h 59m" in msg and "Only send during these hours" in msg


@pytest.mark.asyncio
async def test_a_worker_that_does_not_answer_is_restarted_and_a_stopped_one_started(client, monkeypatch):
    ws = _ws(client)
    calls = []
    monkeypatch.setattr(ws_mod, "WAKE_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)

    async def fake_stop(self, reason="", grace=30):
        calls.append(("stop", reason, grace))
        return True, "Stopped."
    monkeypatch.setattr(Workspace, "stop_worker", fake_stop)
    monkeypatch.setattr(Workspace, "start_worker", lambda self: (calls.append(("start",)), (True, "Started (pid 4343)."))[1])
    ok, msg = await ws.wake_worker()
    assert ok and "did not answer Wake up now" in msg and "restarted" in msg
    assert calls[0][0] == "stop" and calls[0][2] == ws_mod.STUCK_GRACE_SECONDS and calls[1] == ("start",)
    assert "stopped answering" in ws.log_file.read_text()
    # Not running: started. Paused for edits: left paused.
    calls.clear()
    monkeypatch.setattr(Workspace, "worker_running", lambda self: None)
    ok, msg = await ws.wake_worker()
    assert ok and msg.startswith("It was not running, so it was started") and calls == [("start",)]
    ws.paused_file.write_text("1")
    ok, msg = await ws.wake_worker()
    assert not ok and "paused for edits" in msg


@pytest.mark.asyncio
async def test_a_frozen_worker_is_found_and_restarted_on_the_second_look(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)
    ws.pid_file.write_text("4242")
    old = time.time() - 3600
    os.utime(ws.pid_file, (old, old))
    _beat(ws, 4242, age=30)
    assert ws.worker_stuck() == "" and ws.worker_doing()["text"].startswith("Sleeping: the next cycle starts in")
    _beat(ws, 4242, age=900)
    assert "nothing from it for 15 minutes" in ws.worker_stuck() and ws.worker_doing()["state"] == "stuck"
    _beat(ws, 9999, age=900)                       # an earlier process's beat says nothing about this one
    assert ws.worker_stuck() == "" and ws.worker_beat() is None
    _beat(ws, 4242, age=900)
    restarted = []

    async def fake_restart(self, why):
        restarted.append(why)
        return True, "restarted"
    monkeypatch.setattr(Workspace, "restart_stuck_worker", fake_restart)
    seen = {}
    assert await ws.restart_if_stuck(seen) == "" and restarted == []      # first look: noted
    assert await ws.restart_if_stuck(seen) == "restarted" and len(restarted) == 1
    _beat(ws, 4242, age=10)
    assert await ws.restart_if_stuck(seen) == "" and not seen


def test_the_buttons_on_the_dashboard_and_the_campaigns_page(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)
    _beat(ws, 4242)
    page = client.get("/").text
    assert 'action="/worker/wake"' in page and "Wake up now" in page and "Sleeping: the next cycle starts in" in page

    async def fake_wake(self, by=""):
        return True, f"Woken up by {by}."
    monkeypatch.setattr(Workspace, "wake_worker", fake_wake)
    r = client.post("/worker/wake")
    assert r.headers["location"].startswith("/?msg=Woken%20up%20by%20Wake%20up%20now%20on%20the%20dashboard")
    page = client.get("/campaigns").text
    assert 'action="/campaigns/outreach/main/wake"' in page
    r = client.post("/campaigns/outreach/main/wake")
    assert "Woken%20up%20by%20Wake%20up%20now%20for" in r.headers["location"]


# ── the second round: busy is not frozen, the mail worker, the Mac ──

@pytest.mark.asyncio
async def test_a_busy_worker_keeps_the_wake_instead_of_being_restarted(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(ws_mod, "WAKE_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)
    _beat(ws, 4242, age=5, state="working")                          # it beat five seconds ago
    restarted = []

    async def fake_restart(self, why):
        restarted.append(why)
        return True, "restarted"
    monkeypatch.setattr(Workspace, "restart_stuck_worker", fake_restart)
    ok, msg = await ws.wake_worker()
    assert ok and "busy with a slow step" in msg and restarted == [] and wake.wake_pending(ws.data_dir)


def test_one_cycle_that_never_ends_is_stuck_too(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)
    _beat(ws, 4242, age=5, state="working", since=time.time() - 3 * 3600)
    assert "one cycle has been running for 180 minutes" in ws.worker_stuck()
    _beat(ws, 4242, age=5, state="working", since=time.time() - 600)
    assert ws.worker_stuck() == ""


def test_a_new_worker_starts_without_the_last_ones_files(client, monkeypatch):
    ws = _ws(client)
    wake.request_wake(ws.data_dir)
    _beat(ws, 1, age=900)

    class Proc:
        pid = 4343
    monkeypatch.setattr(ws_mod.subprocess, "Popen", lambda *a, **k: Proc())
    for name in ("llm_configured", "is_configured", "mailbox_connected"):
        monkeypatch.setattr(Workspace, name, lambda self: True)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: None)
    ok, _ = ws.start_worker()
    assert ok and not wake.wake_pending(ws.data_dir) and not (ws.data_dir / wake.BEAT_FILE).exists()


@pytest.mark.asyncio
async def test_a_frozen_mail_worker_is_restarted_and_a_reused_pid_is_not_it(client, monkeypatch):
    ws = _ws(client)
    ws.user_data_dir.mkdir(parents=True, exist_ok=True)
    ws.mail_pid_file.write_text(str(os.getpid()))                   # this test process: not a mail worker
    assert ws.mail_worker_running() is None and not ws.mail_pid_file.exists()
    monkeypatch.setattr(ws_mod, "_is_harvey_process", lambda pid: True)
    ws.mail_pid_file.write_text(str(os.getpid()))
    old = time.time() - 3600
    os.utime(ws.mail_pid_file, (old, old))
    (ws.user_data_dir / "mailworker.heartbeat").write_text(str(int(time.time() - 900)))
    assert "nothing from it for 15 minutes" in ws.mail_worker_stuck()
    calls = []

    async def fake_restart(self, why=""):
        calls.append(why)
        return True, "restarted"
    monkeypatch.setattr(Workspace, "restart_mail_worker", fake_restart)
    seen = {}
    assert await ws.restart_mail_if_stuck(seen) == "" and await ws.restart_mail_if_stuck(seen) == "restarted"
    assert len(calls) == 1
    # The Mail page offers Restart for one that is not answering.
    page = client.get("/mail").text
    assert "Worker not answering" in page and 'action="/mail/worker/restart"' in page


@pytest.mark.asyncio
async def test_a_wake_lets_a_topped_up_account_try_again_now(tmp_path):
    from harvey.brain import Brain
    from harvey.state import StateManager
    sm = StateManager(str(tmp_path / "h.db")); await sm.init_db()
    brain = Brain(sm)
    await brain._start_usage_pause()                                 # out of credit: an hour's pause
    assert brain.paused_reason and not await brain.is_within_budget(10 ** 9)
    await brain.recheck()
    assert brain.paused_reason == "" and await brain.is_within_budget(10 ** 9)


def test_the_site_keeps_a_mac_awake_and_the_service_is_not_throttled(monkeypatch):
    from bb import __main__ as cli
    seen = []
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **k: seen.append(cmd))
    monkeypatch.setattr(cli.Path, "exists", lambda self: True)
    assert "Keeping this Mac awake" in cli.keep_mac_awake(pid=123, platform="darwin")
    assert seen == [["/usr/bin/caffeinate", "-i", "-w", "123"]]
    assert cli.keep_mac_awake(pid=123, platform="linux") == ""
    monkeypatch.setenv("BB_ALLOW_SLEEP", "1")
    assert cli.keep_mac_awake(pid=123, platform="darwin") == ""


def test_the_pill_says_what_the_worker_is_doing(client, monkeypatch):
    ws = _ws(client)
    monkeypatch.setattr(Workspace, "worker_running", lambda self: 4242)
    _beat(ws, 4242, state="quiet")
    page = client.get("/").text
    assert 'data-k="worker">Quiet hours' in page and "Quiet hours: it starts again in" in page
    assert client.get("/api/summary").json()["worker_doing"]["state"] == "quiet"
