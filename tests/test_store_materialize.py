"""Tests for the Layer 0 safe-materialization primitive.

``datamanifest.store.materialize`` — atomic publish (`.tmp` → rename), a
``.complete`` marker, and a ``.lock`` pidfile — is the shared substrate both the
fetch layer and the cache layer build on, so it is worth covering directly.
Offline, stdlib only.
"""

import os
import socket
import threading
import time

import pytest

from datamanifest.store import locations
from datamanifest.store.materialize import (
    LockedError,
    _acquire_lock,
    _read_lock_pid,
    _stale_lock,
    is_complete,
    materialize,
    remove_path,
)


def test_materialize_file_atomic_publish_and_marker(tmp_path):
    target = str(tmp_path / "obj.bin")

    def write_fn(tmp):
        with open(tmp, "w") as f:
            f.write("payload")

    materialize(target, write_fn)

    assert os.path.isfile(target)
    assert open(target).read() == "payload"
    assert os.path.exists(locations.marker_path(target))  # <target>.complete
    assert is_complete(target)
    # No leftover staging path and the lock is released.
    assert not os.path.exists(locations.tmp_path(target))
    assert not os.path.exists(locations.lock_path(target))


def test_materialize_directory_marker_lives_inside(tmp_path):
    target = str(tmp_path / "dir_obj")

    def write_fn(tmp):
        os.makedirs(tmp)
        with open(os.path.join(tmp, "a.txt"), "w") as f:
            f.write("x")

    materialize(target, write_fn)

    assert os.path.isdir(target)
    assert os.path.isfile(os.path.join(target, "a.txt"))
    assert os.path.exists(os.path.join(target, ".complete"))  # marker travels inside
    assert is_complete(target)


def test_materialize_failed_write_leaves_no_complete(tmp_path):
    target = str(tmp_path / "obj.bin")

    def boom(tmp):
        with open(tmp, "w") as f:
            f.write("partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        materialize(target, boom)

    # Nothing published, no marker, lock released — a reader sees it as absent.
    assert not os.path.exists(target)
    assert not os.path.exists(locations.marker_path(target))
    assert not is_complete(target)
    assert not os.path.exists(locations.lock_path(target))


def test_is_complete_false_without_marker(tmp_path):
    target = str(tmp_path / "obj.bin")
    with open(target, "w") as f:
        f.write("placed by hand, no marker")
    assert os.path.exists(target)
    assert not is_complete(target)  # present but unmarked == not complete


def test_acquire_lock_reclaims_stale_dead_holder(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(f"999999 {socket.gethostname()}")
    past = time.time() - 60                     # past stale_age, no heartbeat
    os.utime(lock, (past, past))
    monkeypatch.setattr(
        "datamanifest.store.materialize._pid_alive", lambda pid: False
    )
    assert _acquire_lock(lock, "fail", 30.0) is True   # stale dead holder reclaimed
    assert _read_lock_pid(lock) == os.getpid()         # now owned by us
    os.remove(lock)


def test_acquire_lock_proceed_yields_to_live_holder(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("4242")                         # legacy bare-PID form parses too
    monkeypatch.setattr(
        "datamanifest.store.materialize._pid_alive", lambda pid: True
    )
    assert _acquire_lock(lock, "proceed", 30.0) is False  # live holder keeps it
    assert _read_lock_pid(lock) == 4242                   # left untouched


def test_acquire_lock_fail_raises_on_live_holder(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(f"4242 {socket.gethostname()}")
    monkeypatch.setattr(
        "datamanifest.store.materialize._pid_alive", lambda pid: True
    )
    with pytest.raises(LockedError, match="locked by another process"):
        _acquire_lock(lock, "fail", 30.0)


def test_stale_lock_rules(tmp_path):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("12345 some-other-host")
    assert _stale_lock(lock, 30.0) is False     # fresh: never stale
    past = time.time() - 60
    os.utime(lock, (past, past))
    assert _stale_lock(lock, 30.0) is False     # remote host, within the 5x grace
    past = time.time() - 200
    os.utime(lock, (past, past))
    assert _stale_lock(lock, 30.0) is True      # missed-heartbeat grace exceeded


def test_wait_then_adopt_peer_publish(tmp_path):
    """A contender blocks on the holder's lock (on_locked="wait", the default),
    then the skip_if recheck adopts what the holder published — the contender's
    write never runs (spec-v5.2 compute-once)."""
    target = str(tmp_path / "obj.bin")
    ran = []

    def slow_write(tmp):
        ran.append("holder")
        time.sleep(1.0)
        with open(tmp, "w") as f:
            f.write("holder")

    holder = threading.Thread(target=materialize, args=(target, slow_write))
    holder.start()
    deadline = time.time() + 5
    while not os.path.exists(locations.lock_path(target)):
        assert time.time() < deadline, "holder never took the lock"
        time.sleep(0.01)

    def contender_write(tmp):
        ran.append("contender")
        with open(tmp, "w") as f:
            f.write("contender")

    start = time.time()
    materialize(target, contender_write, skip_if=is_complete)
    waited = time.time() - start
    holder.join()

    assert waited > 0.5                     # actually blocked on the lock
    assert ran == ["holder"]                # contender skipped after the recheck
    assert open(target).read() == "holder"
    assert not os.path.exists(locations.lock_path(target))


def test_heartbeat_keeps_long_write_fresh(tmp_path):
    """A write running far past 5 * stale_age never goes stale: the heartbeat
    refreshes the lock's mtime, so a contender still sees a live holder."""
    target = str(tmp_path / "obj.bin")
    lock = locations.lock_path(target)

    def slow_write(tmp):
        time.sleep(1.5)                     # >> 5 * stale_age = 1.0
        with open(tmp, "w") as f:
            f.write("x")

    holder = threading.Thread(
        target=materialize, args=(target, slow_write), kwargs={"stale_age": 0.2}
    )
    holder.start()
    deadline = time.time() + 5
    while not os.path.exists(lock):
        assert time.time() < deadline, "holder never took the lock"
        time.sleep(0.01)
    time.sleep(1.2)                         # well past the no-heartbeat grace
    with pytest.raises(LockedError):
        materialize(target, lambda tmp: None, on_locked="fail", stale_age=0.2)
    holder.join()
    assert is_complete(target)


def test_materialize_proceed_under_live_holder(tmp_path):
    """on_locked="proceed" publishes via process-private staging and leaves the
    foreign holder's lock untouched."""
    target = str(tmp_path / "obj.bin")
    lock = locations.lock_path(target)
    with open(lock, "w") as f:
        f.write("1 " + socket.gethostname())  # pid 1: alive, foreign, fresh

    materialize(target, lambda tmp: open(tmp, "w").write("z"), on_locked="proceed")

    assert open(target).read() == "z"
    assert is_complete(target)
    assert os.path.exists(lock)             # not ours to remove
    os.remove(lock)


def test_materialize_rejects_unknown_on_locked(tmp_path):
    with pytest.raises(ValueError, match="on_locked"):
        materialize(str(tmp_path / "x"), lambda tmp: None, on_locked="bogus")


def test_remove_path_handles_file_dir_and_absent(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "g").write_text("y")
    remove_path(str(f))
    remove_path(str(d))
    remove_path(str(tmp_path / "never-existed"))  # no error
    assert not f.exists() and not d.exists()
