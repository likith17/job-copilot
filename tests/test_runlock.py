"""The run lock: one run at a time, and never stuck after a crash."""
import subprocess
import sys
import time

import pytest

from copilot import runlock

HOLDER = """
import sys, time
from pathlib import Path
from copilot import runlock
with runlock.hold(Path(sys.argv[1]), "test-holder"):
    print("locked", flush=True)
    time.sleep(60)
"""


def _start_holder(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_second_run_is_refused_while_first_holds_the_lock(tmp_path):
    holder = _start_holder(tmp_path)
    try:
        info = runlock.current(tmp_path)
        assert info["source"] == "test-holder"
        with pytest.raises(runlock.RunInProgress, match="test-holder"):
            with runlock.hold(tmp_path, "second"):
                pass
    finally:
        holder.kill()
        holder.wait()


def test_lock_is_released_when_the_holder_dies(tmp_path):
    holder = _start_holder(tmp_path)
    holder.kill()          # simulate a crash / closed terminal: no cleanup code runs
    holder.wait()
    for _ in range(50):    # the OS releases the lock as the process exits
        if runlock.current(tmp_path) is None:
            break
        time.sleep(0.1)
    assert runlock.current(tmp_path) is None
    with runlock.hold(tmp_path, "after-crash"):   # no RunInProgress: nothing is stuck
        pass


def test_lock_is_free_after_a_normal_run(tmp_path):
    with runlock.hold(tmp_path, "first"):
        pass
    assert runlock.current(tmp_path) is None
    with runlock.hold(tmp_path, "second"):
        pass
