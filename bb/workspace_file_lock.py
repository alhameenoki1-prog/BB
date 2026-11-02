"""Cross-process advisory locks for workspace JSON storage."""

import sys

if sys.platform == 'win32':
    import msvcrt
else:
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
            if sys.platform == 'win32':
                # Windows byte-range locks require an existing byte and a stable offset.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'\\0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == 'win32':
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
