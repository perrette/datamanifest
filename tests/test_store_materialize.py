"""Tests for the Layer 0 safe-materialization primitive.

``datamanifest.store.materialize`` — atomic publish (`.tmp` → rename), a
``.complete`` marker, and a ``.lock`` pidfile — is the shared substrate both the
fetch layer and the cache layer build on, so it is worth covering directly.
Offline, stdlib only.
"""

import os

import pytest

from datamanifest.store import locations
from datamanifest.store.materialize import (
    _acquire_lock,
    _read_lock_pid,
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


def test_acquire_lock_reclaims_dead_holder(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("999999")
    monkeypatch.setattr(
        "datamanifest.store.materialize._pid_alive", lambda pid: False
    )
    assert _acquire_lock(lock) is True          # dead holder reclaimed
    assert _read_lock_pid(lock) == os.getpid()  # now owned by us
    os.remove(lock)


def test_acquire_lock_yields_to_live_holder(tmp_path, monkeypatch):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("4242")
    monkeypatch.setattr(
        "datamanifest.store.materialize._pid_alive", lambda pid: True
    )
    assert _acquire_lock(lock) is False         # a live holder keeps the lock
    assert _read_lock_pid(lock) == 4242         # left untouched


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
