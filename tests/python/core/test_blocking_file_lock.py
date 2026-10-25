"""Tests for locks.blocking_file_lock — blocking per-path exclusive lock."""

from __future__ import annotations

import threading
import time

from astinuslab.core.locks import blocking_file_lock


def test_creates_lock_file_and_releases(tmp_path):
    lock_path = tmp_path / "locks" / "job-1.lock"
    with blocking_file_lock(lock_path):
        assert lock_path.exists()
    # Re-acquirable immediately after release.
    with blocking_file_lock(lock_path):
        pass


def test_second_acquirer_blocks_until_release(tmp_path):
    lock_path = tmp_path / "job-1.lock"
    order: list[str] = []
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def holder():
        with blocking_file_lock(lock_path):
            order.append("holder-in")
            holder_entered.set()
            release_holder.wait(timeout=10)
            order.append("holder-out")

    def waiter():
        holder_entered.wait(timeout=10)
        with blocking_file_lock(lock_path):
            order.append("waiter-in")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t2.start()
    holder_entered.wait(timeout=10)
    time.sleep(0.2)  # give the waiter time to block on the flock
    assert order == ["holder-in"]  # waiter must not have entered yet
    release_holder.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert order == ["holder-in", "holder-out", "waiter-in"]


def test_different_paths_do_not_contend(tmp_path):
    with blocking_file_lock(tmp_path / "a.lock"):
        with blocking_file_lock(tmp_path / "b.lock"):
            pass
