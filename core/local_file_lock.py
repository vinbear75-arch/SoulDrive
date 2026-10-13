"""Small standard-library file lock for local multi-worker coordination."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def local_file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Lock one byte in a sidecar file for threads and local processes."""
    lock_path = os.fspath(path)
    lock_key = os.path.abspath(lock_path)
    with _LOCKS_GUARD:
        local_lock = _LOCKS.setdefault(lock_key, threading.RLock())

    with local_lock:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
