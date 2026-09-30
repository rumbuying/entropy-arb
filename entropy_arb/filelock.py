"""Advisory per-file lock for collaborative profile writes.

The console (manual edits) and tools/auto_band.py (band write-back) patch
the same profile YAML — both must take the same lock (spec §13.1), not just
the console. fcntl.flock is process-blocking on Linux and macOS; the lock
file sits beside the target (`<name>.yaml.lock`) so different profiles never
contend. Callers keep critical sections tiny: read-modify-write only.
"""
from __future__ import annotations

import contextlib
import fcntl
import os


@contextlib.contextmanager
def file_lock(path: str):
    lock_path = path + ".lock"
    d = os.path.dirname(os.path.abspath(lock_path))
    os.makedirs(d, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
