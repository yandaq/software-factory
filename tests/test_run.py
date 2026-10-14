"""The run lock: who holds it, and who may take it over."""

import os
from pathlib import Path

import pytest

from sf.run import RunLock


def test_run_lock_reports_its_holder_and_takes_over_a_stale_one(tmp_path: Path):
    lock = RunLock(tmp_path)
    with lock:
        assert lock.holder() == os.getpid()
        with pytest.raises(RuntimeError, match="another sf run is active"):
            RunLock(tmp_path).__enter__()
    assert lock.holder() is None

    lock.path.write_text("2147483647")  # a lock left behind by a dead process
    assert lock.holder() is None
    with RunLock(tmp_path):
        assert lock.holder() == os.getpid()
