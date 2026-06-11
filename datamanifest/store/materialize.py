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

Lock semantics (spec-v5.2). The lock records ``<pid> <hostname>`` (the same
format as Julia's stdlib ``Pidfile``, so the two tools read each other's locks)
and the holder refreshes its mtime every ``stale_age / 2`` (a heartbeat), so a
live holder's lock age stays near zero however long the write takes. A
contender **waits** by default (``on_locked="wait"``), and a ``skip_if(target)``
predicate — evaluated only once the lock is acquired — lets it adopt the entry
its peer just published instead of rewriting it. A lock is reclaimed as stale
once its age exceeds ``stale_age`` AND (its PID is dead on this host, or the
age exceeds ``5 * stale_age`` — a holder that missed many consecutive
heartbeats on another node). Reclaiming wrongly is safe by construction
(staging + atomic rename + completion marker): worst case is duplicate work,
never a partial entry.

Path helpers (``tmp_path`` / ``lock_path`` / ``marker_path``) live in
:mod:`datamanifest.store.locations`.
"""

import contextlib
import os
import shutil
import socket
import threading
import time

from . import locations

__all__ = [
    "materialize",
    "is_complete",
    "remove_path",
    "LockedError",
    "LOCK_STALE_AGE",
]

# Default lock staleness age (seconds); ``$DATAMANIFEST_LOCK_STALE_AGE``
# overrides (cluster tuning). With 30s: a crashed same-host holder is picked up
# within ~30s, a cross-host frozen holder after ~2.5min (5 * stale_age).
LOCK_STALE_AGE = 30.0
_STALE_GRACE_FACTOR = 5


class LockedError(RuntimeError):
    """Raised under ``on_locked="fail"`` when a live peer holds the lock."""


def _lock_stale_age(env=os.environ) -> float:
    try:
        v = float(env.get("DATAMANIFEST_LOCK_STALE_AGE", ""))
    except (TypeError, ValueError):
        return LOCK_STALE_AGE
    return v if v > 0 else LOCK_STALE_AGE


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


def _parse_lock(lock: str):
    """The ``(pid, hostname, age)`` recorded in *lock* — ``(0, "", 0.0)`` for
    any field that cannot be read. A legacy bare-PID lock parses with an empty
    hostname (treated as local)."""
    try:
        with open(lock) as f:
            content = f.read()
        mtime = os.stat(lock).st_mtime
    except OSError:
        return 0, "", 0.0
    fields = content.strip().split(" ", 1)
    try:
        pid = int(fields[0])
    except ValueError:
        pid = 0
    hostname = fields[1] if len(fields) == 2 else ""
    return pid, hostname, time.time() - mtime


def _read_lock_pid(lock: str) -> int:
    """The PID recorded in *lock*, or ``0`` when unreadable/malformed."""
    return _parse_lock(lock)[0]


def _stale_lock(lock: str, stale_age: float) -> bool:
    """Whether *lock* may be reclaimed (spec-v5.2): age beyond *stale_age* AND
    (PID dead on this host, or age beyond the missed-heartbeat grace multiple)."""
    pid, hostname, age = _parse_lock(lock)
    if age <= stale_age:
        return False
    if age > stale_age * _STALE_GRACE_FACTOR:
        return True
    if hostname and hostname != socket.gethostname():
        return False  # can't inspect remote hosts within the grace window
    return not (pid and _pid_alive(pid))


class _LockHeartbeat:
    """Daemon thread refreshing the lock's mtime every *interval* seconds, so a
    live holder's lock never looks stale however long the write takes."""

    def __init__(self, lock: str, interval: float):
        self._lock = lock
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(max(interval, 0.1),), daemon=True
        )
        self._thread.start()

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                os.utime(self._lock, None)
            except OSError:
                return  # lock gone (released or reclaimed): nothing to refresh

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def _acquire_lock(lock: str, on_locked: str = "wait", stale_age=None) -> bool:
    """Create *lock* as an exclusive ``<pid> <hostname>`` pidfile.

    Returns ``True`` when this process now owns the lock (and is therefore
    responsible for removing it). When a live peer holds it: ``"wait"`` polls
    until the lock is released or goes stale (see :func:`_stale_lock`),
    ``"fail"`` raises :class:`LockedError`, ``"proceed"`` returns ``False`` —
    the caller goes ahead without exclusivity, the completion marker still
    guarding against acting on a partial publish.
    """
    if stale_age is None:
        stale_age = _lock_stale_age()
    poll = max(0.1, min(1.0, stale_age / 10))
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _stale_lock(lock, stale_age):
                with contextlib.suppress(OSError):
                    os.remove(lock)
                continue
            if on_locked == "wait":
                time.sleep(poll)
                continue
            if on_locked == "fail":
                pid, hostname, _ = _parse_lock(lock)
                where = f" on {hostname}" if hostname else ""
                raise LockedError(
                    f"target is locked by another process (pid {pid}{where}): {lock}"
                )
            return False  # "proceed"
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {socket.gethostname()}")
        return True


def remove_path(path: str) -> None:
    """Remove *path* whether a file, symlink, or directory (no error if absent)."""
    if os.path.islink(path) or os.path.isfile(path):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def materialize(target: str, write_fn, *, on_locked: str = "wait",
                stale_age=None, skip_if=None) -> None:
    """Atomically publish *target*, holding a pidfile lock for the duration.

    ``write_fn(tmp)`` populates the staging path ``<target>.tmp`` (a file or a
    directory). On success the staging path is atomically moved into place via
    :func:`os.replace` and a completion marker is created
    (``<target>/.complete`` for a directory, ``<target>.complete`` for a file).
    A ``<target>.lock`` pidfile (mtime-refreshed every ``stale_age / 2`` while
    held) is held while writing and removed afterwards. A killed or failed
    write leaves no completion marker and no partial final entry — only a
    leftover ``.tmp``.

    When the lock is already held, *on_locked* decides (spec-v5.2): ``"wait"``
    (default) blocks until the holder releases it or its lock goes stale;
    ``"fail"`` raises :class:`LockedError`; ``"proceed"`` goes ahead without
    exclusivity, staging under a process-private ``<target>.tmp.<pid>`` (the
    last atomic rename wins).

    *skip_if(target)*, when given, is evaluated **after** the lock is acquired:
    if it returns true the write is skipped entirely — the recheck that lets a
    waiter adopt the entry its peer just published instead of recomputing it.
    """
    if on_locked not in ("wait", "fail", "proceed"):
        raise ValueError(
            f'materialize: on_locked must be "wait", "fail", or "proceed"; '
            f"got {on_locked!r}"
        )
    if stale_age is None:
        stale_age = _lock_stale_age()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = locations.tmp_path(target)
    lock = locations.lock_path(target)
    owned = _acquire_lock(lock, on_locked, stale_age)
    heartbeat = _LockHeartbeat(lock, stale_age / 2) if owned else None
    if not owned:
        tmp = f"{tmp}.{os.getpid()}"  # private staging: never clobber a peer's
    try:
        # Recheck under the lock: a peer may have just published this target.
        if skip_if is not None and skip_if(target):
            return
        remove_path(tmp)
        write_fn(tmp)
        remove_path(target)
        os.replace(tmp, target)
        with open(locations.marker_path(target), "w"):
            pass
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if owned:
            with contextlib.suppress(OSError):
                os.remove(lock)


def is_complete(target: str) -> bool:
    """True when *target* exists and carries its completion marker.

    Readers treat a missing marker as absent (an interrupted or partial publish
    that must be re-fetched).
    """
    return os.path.exists(target) and os.path.exists(locations.marker_path(target))
