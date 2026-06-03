"""Safe materialization substrate (atomic publish + completion marker + lock).

Lifted out of ``pipelines.py`` so both the fetch layer (``pipelines``) and the
cache layer (``cache/``) can consume the same publish primitive without either
importing the other. The block is recipe-agnostic: it knows nothing about
``uri`` fetches or ``@cached`` functions — only how to atomically publish a
caller-populated staging path under a pidfile lock and a completion marker.

- ``materialize(target, write_fn)`` — populate ``<target>.tmp`` via *write_fn*,
  then atomically rename into place and drop a ``.complete`` marker, holding a
  ``<target>.lock`` pidfile for the duration.
- ``is_complete(target)`` — whether *target* exists and carries its marker.

Path helpers (``tmp_path`` / ``lock_path`` / ``marker_path``) live in
:mod:`datamanifest.store.locations`.
"""

import contextlib
import os
import shutil

from . import locations

__all__ = [
    "materialize",
    "is_complete",
    "remove_path",
]


def _pid_alive(pid: int) -> bool:
    """True when *pid* names a live process (best effort)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(lock: str) -> int:
    """The PID recorded in *lock*, or ``0`` when unreadable/malformed."""
    try:
        with open(lock) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _acquire_lock(lock: str) -> bool:
    """Create *lock* as an exclusive pidfile, reclaiming it when its recorded
    PID is dead.

    Returns ``True`` when this process now owns the lock (and is therefore
    responsible for removing it), ``False`` when a live process already holds it
    — in which case the caller proceeds without exclusivity rather than
    deadlocking, since the completion marker still guards against acting on a
    partial publish.
    """
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_lock_pid(lock)
            if pid and not _pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.remove(lock)
                continue
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True


def remove_path(path: str) -> None:
    """Remove *path* whether a file, symlink, or directory (no error if absent)."""
    if os.path.islink(path) or os.path.isfile(path):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def materialize(target: str, write_fn) -> None:
    """Atomically publish *target*, holding a pidfile lock for the duration.

    ``write_fn(tmp)`` populates the staging path ``<target>.tmp`` (a file or a
    directory). On success the staging path is atomically moved into place via
    :func:`os.replace` and a completion marker is created
    (``<target>/.complete`` for a directory, ``<target>.complete`` for a file).
    A ``<target>.lock`` pidfile is held while writing and removed afterwards. A
    killed or failed write leaves no completion marker and no partial final
    entry — only a leftover ``.tmp``.
    """
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = locations.tmp_path(target)
    lock = locations.lock_path(target)
    owned = _acquire_lock(lock)
    try:
        remove_path(tmp)
        write_fn(tmp)
        remove_path(target)
        os.replace(tmp, target)
        with open(locations.marker_path(target), "w"):
            pass
    finally:
        if owned:
            with contextlib.suppress(OSError):
                os.remove(lock)


def is_complete(target: str) -> bool:
    """True when *target* exists and carries its completion marker.

    Readers treat a missing marker as absent (an interrupted or partial publish
    that must be re-fetched).
    """
    return os.path.exists(target) and os.path.exists(locations.marker_path(target))
