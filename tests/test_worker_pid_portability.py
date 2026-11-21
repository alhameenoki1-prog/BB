"""Cross-platform worker PID liveness: no Unix waitpid flags required on Windows."""
import os

import pytest

from bb import workspaces as ws


def test_process_gone_falls_back_when_wnohang_unavailable(monkeypatch):
    # Mimics Windows, where os.WNOHANG does not exist.
    monkeypatch.delattr(os, "WNOHANG", raising=False)
    seen = []

    def alive(pid, signal):
        seen.append((pid, signal))

    monkeypatch.setattr(os, "kill", alive)
    assert ws._process_gone(12345) is False
    assert seen == [(12345, 0)]


def test_process_gone_without_wnohang_detects_missing_pid(monkeypatch):
    monkeypatch.delattr(os, "WNOHANG", raising=False)

    def missing(pid, signal):
        raise ProcessLookupError("No such process")

    monkeypatch.setattr(os, "kill", missing)
    assert ws._process_gone(12345) is True


def test_process_gone_without_wnohang_keeps_permission_denied_pid(monkeypatch):
    monkeypatch.delattr(os, "WNOHANG", raising=False)

    def inaccessible(pid, signal):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(os, "kill", inaccessible)
    assert ws._process_gone(12345) is False


def test_process_gone_windows_invalid_pid_error(monkeypatch):
    monkeypatch.delattr(os, "WNOHANG", raising=False)

    def invalid(pid, signal):
        err = OSError("Invalid parameter")
        err.winerror = 87
        raise err

    monkeypatch.setattr(os, "kill", invalid)
    assert ws._process_gone(12345) is True
