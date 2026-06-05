"""Cache-layer inspection — enumerate produced artifacts for maintenance.

This module is the cache-layer mechanics half of the ``datamanifest list``
maintenance surface (spec-v3, replacing the old automatic ``gc`` collector):
given a single ``datacache_dir`` root, it enumerates the **produced** artifacts
under it as field-bearing :class:`CacheObject` records (``kind`` / ``key`` /
``hash`` / ``cachetype`` / ``version`` / ``format`` / ``location`` / ``size`` /
``created`` / ``last_access``) and deletes or moves an explicitly-selected
subset.

It reads **only** the cache folder it is handed — never the datasets folder,
never ``datasets.toml`` / ``cached.toml`` themselves. Reachability
(``referenced``) is **not** decided here: the CLI composition root derives the
referenced-key set from the manifests / indexes and tags each object. This
module never collects on its own — the only deletion is the explicit
:func:`delete_object` on a caller-selected object, so a read-only consumer is
never reclaimed out from under anyone (the hole that retired the old GC).

A **produced** artifact is a directory holding a ``config.toml`` sidecar: that is
exactly what distinguishes a ``@cached`` output from a *fetched* dataset (which
has no ``config.toml``). So a fetched dataset sharing the folder is never
enumerated here and never deleted.

Layering: imports only the Layer 0 substrate (:func:`materialize.remove_path`)
plus the cache-layer sidecar / usage siblings + stdlib — never the fetch layer.
"""

import os
import shutil

from ..store import materialize
from ._sidecars import (
    CONFIG_NAME,
    METADATA_NAME,
    config_key_table,
    read_config,
    read_metadata,
)
from ._usage import iso_from_mtime, last_access

__all__ = [
    "CacheObject",
    "find_produced_artifacts",
    "enumerate_artifacts",
    "delete_object",
    "move_object",
]

# Files inside an artifact directory that are bookkeeping, not the produced
# value — excluded when guessing the serialized value's format.
_SIDECAR_NAMES = {CONFIG_NAME, METADATA_NAME}


class CacheObject:
    """A maintenance view of one store object (produced artifact or fetched
    dataset).

    Attributes
    ----------
    kind:
        ``"cached"`` for a produced artifact, ``"datasets"`` for a fetched one.
    location:
        Absolute on-disk path of the object (an artifact directory, or a fetched
        file/dir).
    key:
        The portable key — ``"<cachetype>/<hash>"`` for produced artifacts, the
        dataset name for fetched ones.
    hash, cachetype, version:
        Produced-artifact identity (empty for fetched objects).
    format:
        Serialization format (file extension of the stored value).
    size:
        Total bytes on disk.
    created, last_access:
        RFC-3339 UTC timestamps (best-effort, advisory).
    referenced:
        ``True`` / ``False`` once the composition root has resolved reachability,
        ``None`` while unknown.
    name:
        Friendly display label — a dataset name, or a produced artifact's
        registry name (falls back to ``key``).
    present:
        Whether the object is materialized on disk. Always true for an
        enumerated cached artifact; false for a manifest dataset not yet fetched.
    """

    __slots__ = (
        "kind", "location", "key", "hash", "cachetype", "version",
        "format", "size", "created", "last_access", "referenced",
        "name", "present", "params", "storage_path",
    )

    def __init__(
        self, *, kind, location, key="", hash="", cachetype="", version="",
        format="", size=0, created="", last_access="",
        referenced=None, name="", present=True, params=None, storage_path="",
    ):
        self.kind = kind
        self.location = location
        self.key = key
        self.hash = hash
        self.cachetype = cachetype
        self.version = version
        self.format = format
        # For a fetched dataset: its ``storage_path`` field. Non-empty ⇒ the
        # dataset deviates from the global ``$datasets_dir/$key`` default (a
        # custom / user-managed location) — surfaced by ``list``.
        self.storage_path = storage_path
        self.size = size
        self.created = created
        self.last_access = last_access
        self.referenced = referenced
        # *name* is the friendly display label (a dataset name, or a produced
        # artifact's cachetype); *present* is whether the object is materialized
        # on disk (always true for enumerated cached artifacts; false for a
        # manifest dataset not yet fetched); *params* is the produced variation's
        # key table (the kwargs it was produced with).
        self.name = name or key
        self.present = present
        self.params = dict(params) if params else {}

    def __repr__(self):
        ref = {True: "referenced", False: "orphan", None: "?"}[self.referenced]
        return f"<CacheObject {self.kind} {self.key} {ref} {self.location}>"


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
            # An artifact dir is a leaf for enumeration — do not descend further.
            dirnames[:] = []


def _dir_size(path: str) -> int:
    """Total size in bytes of all files under *path* (best-effort)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _guess_format(artifact_dir: str) -> str:
    """The serialized value's format — the extension of the first non-sidecar
    file in *artifact_dir* (empty when none is found)."""
    try:
        names = sorted(os.listdir(artifact_dir))
    except OSError:
        return ""
    for name in names:
        if name in _SIDECAR_NAMES or name.startswith("."):
            continue
        full = os.path.join(artifact_dir, name)
        if os.path.isfile(full):
            stem, _, ext = name.rpartition(".")
            if ext and stem:
                return ext
    return ""


def _created(artifact_dir: str) -> str:
    """The artifact's creation stamp — ``metadata.toml``'s ``created`` when
    present, else the directory mtime as RFC-3339 UTC (best-effort)."""
    try:
        created = read_metadata(artifact_dir).get("created", "")
        if created:
            return created
    except Exception:  # noqa: BLE001 - provenance is best-effort
        pass
    return iso_from_mtime(artifact_dir)


def enumerate_artifacts(cache_root: str):
    """Yield a :class:`CacheObject` for every produced artifact under
    *cache_root* (the ``datacache_dir``).

    ``referenced`` is left ``None`` — the CLI composition root resolves it.
    """
    for artifact_dir, key in find_produced_artifacts(cache_root):
        try:
            config = read_config(artifact_dir)
        except Exception:  # noqa: BLE001 - already filtered, belt-and-braces
            continue
        meta = config.get("_META", {})
        cachetype = meta.get("cachetype", "")
        h = meta.get("hash", "")
        version = meta.get("version", "")
        yield CacheObject(
            kind="cached",
            location=os.path.abspath(artifact_dir),
            key=key,
            name=cachetype,                 # the recipe identity is the cachetype
            hash=h,
            cachetype=cachetype,
            version=version,
            format=_guess_format(artifact_dir),
            size=_dir_size(artifact_dir),
            created=_created(artifact_dir),
            last_access=last_access(artifact_dir),
            params=config_key_table(config),  # the kwargs that produced it
        )


def delete_object(obj: "CacheObject") -> None:
    """Delete a produced artifact directory and its sibling completion / lock
    markers.

    Refuses anything that is not ``kind="cached"`` — fetched datasets,
    ``$data`` / ``$repo`` and ``local_path`` data are never removed by the
    maintenance surface.
    """
    if obj.kind != "cached":
        raise ValueError(
            f"refusing to delete a {obj.kind!r} object at {obj.location} — only "
            "produced (cached) artifacts are deletable"
        )
    materialize.remove_path(obj.location)
    for suffix in (".complete", ".lock", ".tmp"):
        materialize.remove_path(obj.location + suffix)


def move_object(obj: "CacheObject", dest_root: str) -> str:
    """Move a produced artifact to *dest_root*, preserving its
    ``<cachetype>/[<version>/]<hash>`` key path. Returns the new location.

    Refuses anything that is not ``kind="cached"`` (same guard as
    :func:`delete_object`).
    """
    if obj.kind != "cached":
        raise ValueError(
            f"refusing to move a {obj.kind!r} object at {obj.location} — only "
            "produced (cached) artifacts are movable"
        )
    parts = [obj.cachetype]
    if obj.version:
        parts.append(obj.version)
    parts.append(obj.hash)
    dest = os.path.join(dest_root, *parts)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    shutil.move(obj.location, dest)
    for suffix in (".complete", ".lock"):
        marker = obj.location + suffix
        if os.path.exists(marker):
            shutil.move(marker, dest + suffix)
    return dest
