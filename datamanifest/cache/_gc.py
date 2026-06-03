"""The garbage collector — root-reachability over produced artifacts.

GC is the cache layer's mechanics half of ``datamanifest gc``: given the live
root set as *data* (a set of portable keys ``"<cachetype>/<hash>"``), it
enumerates the **produced** artifacts under a single ``$cache`` root and collects
those no root references and that are older than a grace age.

This module reads **only** the ``$cache`` folder it is handed — never
``$data``/``$repo``, never ``datasets.toml``/``cached.toml`` themselves (the CLI
composition root derives ``live_keys`` from those and passes them in). It imports
only the Layer 0 substrate (for ``remove_path``) plus stdlib.

A **produced** artifact is a directory that contains a ``config.toml`` sidecar:
that is exactly what distinguishes a ``@cached`` output from a *fetched*
``store="$cache"`` dataset (which has no ``config.toml``). So a fetched ``$cache``
dataset is never enumerated here and never collected, independent of whether its
manifest entry happens to root it.
"""

import os
import time

from ..store import materialize
from ._sidecars import CONFIG_NAME, config_is_valid, read_config

__all__ = ["collect", "Candidate", "find_produced_artifacts"]


class Candidate:
    """A produced artifact considered for collection.

    Attributes: ``path`` (artifact directory), ``key`` (``"<cachetype>/<hash>"``),
    ``age`` (seconds since last modification), ``collected`` (whether it was
    actually deleted — always ``False`` in dry-run).
    """

    __slots__ = ("path", "key", "age", "collected")

    def __init__(self, path, key, age, collected=False):
        self.path = path
        self.key = key
        self.age = age
        self.collected = collected

    def __repr__(self):
        state = "collected" if self.collected else "candidate"
        return f"<Candidate {self.key} {state} age={self.age:.0f}s {self.path}>"


def find_produced_artifacts(cache_root: str):
    """Yield ``(artifact_dir, key)`` for every produced artifact under
    *cache_root*.

    A produced artifact is a directory holding a ``config.toml`` sidecar; its key
    is ``"<config._META.cachetype>/<config._META.hash>"``. Directories without a
    ``config.toml`` (e.g. fetched ``$cache`` datasets) are skipped. Walking stops
    descending into an artifact directory once found.
    """
    if not os.path.isdir(cache_root):
        return
    for dirpath, dirnames, filenames in os.walk(cache_root):
        if CONFIG_NAME in filenames:
            try:
                meta = read_config(dirpath).get("_META", {})
            except Exception:  # noqa: BLE001 - skip unreadable/malformed sidecar
                continue
            ctype = meta.get("cachetype", "")
            h = meta.get("hash", "")
            if ctype and h:
                yield dirpath, f"{ctype}/{h}"
            # An artifact dir is a leaf for GC purposes — do not descend further.
            dirnames[:] = []


def _artifact_age(path: str, now: float) -> float:
    """Seconds since *path* (and its config sidecar) were last touched.

    Uses the newest mtime among the directory and its ``config.toml`` so a
    just-produced artifact reads as young even if the directory mtime lags.
    """
    mtimes = []
    for p in (path, os.path.join(path, CONFIG_NAME)):
        try:
            mtimes.append(os.path.getmtime(p))
        except OSError:
            pass
    if not mtimes:
        return 0.0
    return max(0.0, now - max(mtimes))


def collect(
    cache_root: str,
    live_keys,
    *,
    grace_seconds: float = 0.0,
    now: float = None,
    dry_run: bool = False,
):
    """Enumerate produced artifacts under *cache_root* and collect the orphans.

    An artifact is **collectable iff** its key ``"<cachetype>/<hash>"`` is not in
    *live_keys* **and** it is older than *grace_seconds*. In ``dry_run`` mode the
    collectable artifacts are reported but not deleted; otherwise each is removed
    (its directory plus the sibling ``.complete`` / ``.lock`` markers) and marked
    ``collected``.

    *cache_root* MUST be the resolved ``$cache`` folder only — this function
    never touches ``$data`` / ``$repo``. *live_keys* is the union of produced
    roots (from every ``cached.toml``) and fetched ``$cache`` roots (from every
    ``datasets.toml``), computed by the caller.

    Returns the list of :class:`Candidate` objects that were collected (or, in
    dry-run, that *would* be collected).
    """
    live = set(live_keys)
    if now is None:
        now = time.time()

    out = []
    for artifact_dir, key in find_produced_artifacts(cache_root):
        if key in live:
            continue
        age = _artifact_age(artifact_dir, now)
        if age <= grace_seconds:
            continue
        cand = Candidate(path=artifact_dir, key=key, age=age, collected=False)
        if not dry_run:
            _delete_artifact(artifact_dir)
            cand.collected = True
        out.append(cand)

    if not dry_run:
        _sweep_stale_siblings(cache_root, now, grace_seconds)
    return out


def _delete_artifact(artifact_dir: str) -> None:
    """Remove an artifact directory and its sibling completion/lock markers."""
    materialize.remove_path(artifact_dir)
    for suffix in (".complete", ".lock", ".tmp"):
        materialize.remove_path(artifact_dir + suffix)


def _sweep_stale_siblings(cache_root: str, now: float, grace_seconds: float) -> None:
    """Best-effort cleanup of obviously-stale ``.tmp`` / ``.lock`` leftovers.

    Only trivial cases: a ``.tmp`` or ``.lock`` whose corresponding artifact
    directory does not exist (or is not a valid produced artifact) and that is
    itself older than the grace age. Anything ambiguous is left in place.
    """
    if not os.path.isdir(cache_root):
        return
    for dirpath, dirnames, filenames in os.walk(cache_root):
        for entry in list(dirnames) + filenames:
            if not (entry.endswith(".tmp") or entry.endswith(".lock")):
                continue
            full = os.path.join(dirpath, entry)
            base = full.rsplit(".", 1)[0]
            # Keep if the real artifact still exists and is a valid produced dir.
            if os.path.isdir(base) and config_is_valid(base):
                continue
            try:
                age = now - os.path.getmtime(full)
            except OSError:
                continue
            if age > grace_seconds:
                materialize.remove_path(full)
