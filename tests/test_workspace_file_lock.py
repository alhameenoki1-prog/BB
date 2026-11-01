"""Regression tests for cross-process workspace file locking."""

import multiprocessing
import os
import stat
import threading
import time

import pytest

from bb.workspace_file_lock import workspace_file_lock


def _hold_lock(root, ready, release):
    with workspace_file_lock(root, "records.json"):
        ready.set()
        release.wait(timeout=10)


def _wait_for_lock(root, acquired):
    with workspace_file_lock(root, "records.json"):
        acquired.set()


def test_lock_creates_private_companion_file(tmp_path):
    with workspace_file_lock(tmp_path, "records.json"):
        path = tmp_path / "records.json.lock"
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("filename", [
    "",
    "../records.json",
    "folder/records.json",
])
def test_invalid_lock_filename_rejected(tmp_path, filename):
    with pytest.raises(ValueError):
        with workspace_file_lock(tmp_path, filename):
            pass


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with workspace_file_lock(tmp_path, "records.json"):
            raise RuntimeError("Expected failure")

    with workspace_file_lock(tmp_path, "records.json"):
        assert True


def test_separate_process_waits_for_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    acquired = ctx.Event()

    holder = ctx.Process(
        target=_hold_lock,
        args=(str(tmp_path), ready, release),
    )
    waiter = ctx.Process(
        target=_wait_for_lock,
        args=(str(tmp_path), acquired),
    )

    holder.start()

    try:
        assert ready.wait(timeout=10), "Holder did not acquire lock"

        waiter.start()
        time.sleep(0.2)

        assert not acquired.is_set(), (
            "Second process acquired lock before release"
        )

        release.set()

        assert acquired.wait(timeout=10), (
            "Second process failed to acquire released lock"
        )
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join()

        if waiter.pid is not None:
            waiter.join(timeout=10)
            if waiter.is_alive():
                waiter.terminate()
                waiter.join()

    assert holder.exitcode == 0
    assert waiter.exitcode == 0


def test_separate_thread_waits_for_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def holder():
        with workspace_file_lock(tmp_path, "records.json"):
            entered.set()
            release.wait(timeout=10)

    def waiter():
        with workspace_file_lock(tmp_path, "records.json"):
            acquired.set()

    first = threading.Thread(target=holder)
    second = threading.Thread(target=waiter)

    first.start()

    try:
        assert entered.wait(timeout=10), "First thread did not acquire lock"

        second.start()
        time.sleep(0.2)

        assert not acquired.is_set(), (
            "Second thread entered before the first released the lock"
        )

        release.set()

        assert acquired.wait(timeout=10), (
            "Second thread failed to acquire the released lock"
        )
    finally:
        release.set()
        first.join(timeout=10)

        if second.ident is not None:
            second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
