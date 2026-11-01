"""Cross-process advisory locks for workspace JSON storage."""

import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path


_guard = threading.Lock()
_thread_locks = {}


def _thread_lock(path):
    """Return the shared thread lock for a workspace lock path."""
    with _guard:
        return _thread_locks.setdefault(str(path), threading.Lock())


@contextmanager
def workspace_file_lock(workspace_root, filename):
    """Exclusively lock a workspace storage file's companion lock file."""
    root = Path(workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    if not filename or Path(filename).name != filename:
        raise ValueError("Lock filename must be a simple filename.")

    lock_path = root / (filename + ".lock")

    with _thread_lock(lock_path):
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )

        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
