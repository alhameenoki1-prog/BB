"""Waking a sleeping worker, and showing that it is alive.

The site and a worker are separate processes. Two small files in the
worker's ``data/`` folder connect them:

* ``worker.wake`` is written by the site when someone presses *Wake up
  now*. The worker looks for it every second or two; when it finds it, it
  removes it (that is the site's sign the worker heard) and every sleep in
  the worker ends: the next cycle, and the next send pass, run straight
  away. A worker in the middle of a cycle keeps the wake for its next
  sleep, which is then skipped.
* ``worker.beat`` is rewritten by the worker every few seconds, from a task
  of its own, with what it is doing: working, sleeping until a time, quiet
  hours until a time. A beat that stops changing while the process is
  alive means something froze the worker, and the site restarts it.

Every sleep also ends by the wall clock, not only by the event loop's
monotonic timer: a machine that slept (a Mac with its lid closed) would
otherwise leave a sleep waiting out the rest of its timer after it wakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("harvey.wake")

WAKE_FILE = "worker.wake"
BEAT_FILE = "worker.beat"
POLL_SECONDS = 1.0        # how often a sleep looks at the clock and for a wake
BEAT_SECONDS = 10.0       # how often the beat is rewritten while nothing changes
STALE_SECONDS = 300.0     # a beat this old, with the process alive, is a stuck worker

# The sleepers of this process (the heartbeat, the send loop) each get the
# wake once: set when the wake file is found, taken by that sleeper.
_pending: dict[str, bool] = {}
_SLEEPERS = ("heartbeat", "send")
_state: dict = {"state": "starting", "until": 0.0, "detail": "", "since": time.time(), "since_steady": time.monotonic()}
_woken_at = {"t": 0.0, "by": "", "from": "", "until": 0.0, "detail": ""}
_folder: dict = {"path": None}   # set by beat_loop: until then (a test, a one-off command) no beat is written


def data_dir() -> Path:
    from harvey.paths import DATA_DIR
    return DATA_DIR


# ── the site's side ──

def request_wake(folder: Path, by: str = "the dashboard") -> None:
    """Ask the worker whose data folder this is to wake up now."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (WAKE_FILE + ".tmp")
    tmp.write_text(json.dumps({"at": time.time(), "by": by}))
    tmp.replace(folder / WAKE_FILE)


TEST_SEND_FILE = "send_test.json"


def request_test_send(folder: Path, count: int, by: str = "the dashboard") -> None:
    """While sending is paused: ask the worker to send the next ``count``
    first emails once, then hold again (a test that the mailbox delivers)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (TEST_SEND_FILE + ".tmp")
    tmp.write_text(json.dumps({"count": int(count), "at": time.time(), "by": by}))
    tmp.replace(folder / TEST_SEND_FILE)


def test_send_pending(folder: Path) -> int:
    """How many test emails a request still waiting for the worker asks for (0: none)."""
    try:
        return max(0, int(json.loads((Path(folder) / TEST_SEND_FILE).read_text()).get("count") or 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def take_test_send(folder: Path) -> int:
    """The worker's side: the pending request's count, and the request is gone."""
    path = Path(folder) / TEST_SEND_FILE
    count = test_send_pending(folder)
    try:
        path.unlink()
    except OSError:
        pass
    return count


def wake_pending(folder: Path) -> bool:
    """Is a wake still waiting for the worker to pick it up?"""
    return (Path(folder) / WAKE_FILE).exists()


def read_beat(folder: Path) -> dict | None:
    """The worker's last beat: {at, pid, state, until, detail, since, woken_at,
    age}, or None when it has written none (not started, or older code)."""
    path = Path(folder) / BEAT_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["age"] = max(0.0, time.time() - float(data.get("at") or 0))
    return data


# ── the worker's side ──

def set_state(state: str, until: float = 0.0, detail: str = ""):
    """What the worker is doing now, for the beat (written at once)."""
    if state != _state.get("state") or until != _state.get("until") or detail != _state.get("detail"):
        _state.update(state=state, until=float(until or 0), detail=detail, since=time.time(), since_steady=time.monotonic())
        _write_beat()


def _write_beat(folder: Path | None = None):
    folder = folder or _folder["path"]
    if folder is None:
        return
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (BEAT_FILE + ".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "pid": os.getpid(), "state": _state["state"],
                                   "until": _state["until"], "detail": _state["detail"], "since": _state["since"],
                                   # how long in this state, counting only time the machine was awake
                                   "busy_for": max(0.0, time.monotonic() - float(_state.get("since_steady") or time.monotonic())),
                                   "woken_at": _woken_at["t"], "woken_by": _woken_at["by"],
                                   # what it was doing when the wake came, for the site's answer
                                   "woken_from": _woken_at["from"], "woken_until": _woken_at["until"],
                                   "woken_detail": _woken_at["detail"]}))
        tmp.replace(folder / BEAT_FILE)
    except OSError as e:
        logger.debug(f"could not write the beat: {e}")


def _take_wake(folder: Path | None = None) -> str:
    """The wake file, if there is one: removed, and who asked. Else ""."""
    folder = folder or _folder["path"]
    if folder is None:
        return ""
    path = Path(folder) / WAKE_FILE
    try:
        raw = path.read_text()
    except OSError:
        return ""
    try:
        path.unlink()
    except OSError:
        pass
    try:
        by = str((json.loads(raw) or {}).get("by") or "")
    except (ValueError, AttributeError):
        by = ""
    return by or "the dashboard"


def check_for_wake(folder: Path | None = None) -> bool:
    """Look for a wake now (the beat task does this every second). True
    when one was found; every sleeper of this process then gets it."""
    by = _take_wake(folder)
    if not by:
        return False
    for name in _SLEEPERS:
        _pending[name] = True
    _woken_at.update(t=time.time(), by=by, **{"from": _state["state"]}, until=_state["until"], detail=_state["detail"])
    logger.info(f"Woken up by {by}: carrying on now instead of waiting.")
    _write_beat(folder)
    return True


def stop_beat():
    """The worker is ending: the last beat says so, and nothing more is written."""
    set_state("stopped")
    _folder["path"] = None


def last_woken() -> float:
    """When this process was last woken (0 when never)."""
    return float(_woken_at["t"] or 0)


async def beat_loop(stop_event: asyncio.Event, folder: Path | None = None):
    """Runs beside the worker's loops for as long as the process lives:
    rewrites the beat and picks up a wake. If the event loop is frozen by
    something, this stops too, which is how the site can tell."""
    _folder["path"] = Path(folder or data_dir())
    last = 0.0
    while not stop_event.is_set():
        check_for_wake(folder)
        if time.time() - last >= BEAT_SECONDS:
            _write_beat(folder)
            last = time.time()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    set_state("stopped")


async def sleep(seconds: float, stop_event: asyncio.Event, who: str = "heartbeat", state: str = "sleeping",
                detail: str = "", show: bool = True) -> str:
    """Sleep up to ``seconds``. Returns "stop" (shutdown asked), "wake"
    (someone pressed Wake up now) or "time" (the time is up). Ends by the
    wall clock as well, so a machine that slept does not stretch it."""
    if _pending.pop(who, False):
        return "wake"                    # a wake that came during the last cycle
    deadline = time.time() + max(0.0, float(seconds))
    # The event loop's own clock as well: the wall clock covers a machine
    # that slept (its monotonic clock stops), this one a wall clock set back.
    steady_deadline = time.monotonic() + max(0.0, float(seconds))
    if show:
        set_state(state, deadline, detail)
    try:
        while True:
            if stop_event.is_set():
                return "stop"
            if _pending.pop(who, False):
                return "wake"
            left = min(deadline - time.time(), steady_deadline - time.monotonic())
            if left <= 0:
                return "time"
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=min(POLL_SECONDS, left))
                return "stop"
            except asyncio.TimeoutError:
                pass
    finally:
        if show and not stop_event.is_set():
            set_state("working")
