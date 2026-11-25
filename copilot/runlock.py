"""One pipeline run (or Generate) at a time, across the terminal and the app.

Uses an OS file lock rather than a marker file: the operating system releases
it when the process exits, even on a crash or a closed terminal, so a dead run
can never leave the lock stuck.
"""
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RunInProgress(Exception):
    """Another run holds the lock; its details are in .info."""

    def __init__(self, info):
        self.info = info
        who = info.get("source", "another process")
        since = info.get("started", "?")
        super().__init__(f"A run is already in progress ({who}, started {since}). "
                         f"Wait for it to finish, then try again.")


def _paths(data_dir, name="run"):
    return data_dir / f"{name}.lock", data_dir / f"{name}.lock.json"


def _try_lock(fh):
    try:
        if sys.platform == "win32":
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh):
    try:
        if sys.platform == "win32":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def current(data_dir, name="run"):
    """Details of the run holding the lock, or None if nothing is running."""
    lock_path, info_path = _paths(data_dir, name)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+b") as fh:
        if _try_lock(fh):
            _unlock(fh)
            return None
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@contextmanager
def hold(data_dir, source, name="run"):
    """Hold a lock for the duration of the block, or raise RunInProgress. "run" covers
    the pipeline and Generate; "apply" covers the browser the auto-apply runner opens."""
    lock_path, info_path = _paths(data_dir, name)
    lock_path.touch(exist_ok=True)
    fh = open(lock_path, "r+b")
    if not _try_lock(fh):
        fh.close()
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            info = {}
        raise RunInProgress(info)
    info_path.write_text(json.dumps({"source": source, "pid": os.getpid(),
                                     "started": datetime.now().strftime("%H:%M:%S")}), encoding="utf-8")
    try:
        yield
    finally:
        _unlock(fh)
        fh.close()
