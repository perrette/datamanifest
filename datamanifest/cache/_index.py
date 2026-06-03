"""The ``cached.toml`` index — the produced-dataset registry.

``cached.toml`` is to produced datasets what ``datasets.toml`` is to fetched
ones: a registry, sibling to the manifest by default, that lists each produced
dataset by its **portable** key (``cachetype`` + parameter ``hash``) rather than
by an absolute path. It carries its own ``_META.schema = 1`` and is the set of
*roots* that keeps produced cache artifacts reachable for garbage collection.
One table per produced dataset records ``cachetype``, ``hash``, ``ref``,
``format``, and ``store``.

Layering: this module imports only the Layer 0 substrate
(:func:`datamanifest.store.sort_recursive` for canonical key ordering) plus
stdlib — never the fetch layer.
"""

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
import tomli_w

from ..store import sort_recursive

__all__ = ["CachedIndex", "CACHED_INDEX_NAME", "CACHED_ENTRY_FIELDS"]

CACHED_INDEX_NAME = "cached.toml"

# Fields recorded for each produced dataset in ``cached.toml`` (write ordering is
# canonical/sorted regardless; this documents the schema and drives
# :meth:`CachedIndex.register`). ``scope``/``version`` are spec-v3 additions
# recorded only when non-empty (an artifact's project-id scope and the optional
# recipe ``version``), so a plain entry stays the original five fields.
CACHED_ENTRY_FIELDS = ("cachetype", "hash", "ref", "format", "store", "scope", "version")

# A produced artifact defaults to the OS-reclaimable ``$cache`` folder.
DEFAULT_STORE = "$cache"


class CachedIndex:
    """Read/register/write a ``cached.toml`` produced-dataset registry.

    The on-disk shape mirrors ``datasets.toml``: a ``[_META]`` block with
    ``schema = 1`` plus one table per produced dataset, keyed by the dataset's
    portable name. Each table holds ``cachetype``, ``hash``, ``ref``,
    ``format``, ``store``. :meth:`write` uses the same recursive canonical key
    ordering (:func:`datamanifest.store.sort_recursive`) as the manifest writer,
    so a read/write round-trip is byte-stable.
    """

    SCHEMA = 1

    def __init__(self, entries: dict = None, path: str = ""):
        # name -> {cachetype, hash, ref, format, store}
        self.entries = dict(entries) if entries else {}
        self.path = path

    @classmethod
    def _resolve_path(cls, path: str) -> str:
        """Normalize *path* to a ``cached.toml`` file path (accepts a directory
        holding the default-named index)."""
        path = os.fspath(path)
        if path.endswith(CACHED_INDEX_NAME):
            return path
        if os.path.isdir(path):
            return os.path.join(path, CACHED_INDEX_NAME)
        return path

    @classmethod
    def read(cls, path: str) -> "CachedIndex":
        """Read a ``cached.toml`` from *path* (a file, or a directory holding the
        default-named index)."""
        target = cls._resolve_path(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        data.pop("_META", None)
        return cls(entries=data, path=target)

    @classmethod
    def read_or_empty(cls, path: str) -> "CachedIndex":
        """Read the index at *path*, or return an empty one bound to that path
        when it does not yet exist."""
        target = cls._resolve_path(path)
        if os.path.isfile(target):
            return cls.read(target)
        return cls(path=target)

    def register(
        self,
        name: str,
        *,
        cachetype: str,
        hash: str,
        ref: str = "",
        format: str = "",
        store: str = DEFAULT_STORE,
        scope: str = "",
        version: str = "",
    ) -> None:
        """Add or update the produced dataset *name* (keyed by portable name).

        Identity is the portable ``(cachetype, hash)`` pair — never an absolute
        path — so the index stays relocatable. Re-registering an existing name
        overwrites it. *scope* (the artifact's project-id scope) and *version*
        (the optional recipe version) are spec-v3 fields recorded only when
        non-empty, so a plain entry keeps the original five fields.
        """
        entry = {
            "cachetype": cachetype,
            "hash": hash,
            "ref": ref,
            "format": format,
            "store": store,
        }
        if scope:
            entry["scope"] = scope
        if version:
            entry["version"] = version
        self.entries[name] = entry

    def keys(self) -> set:
        """The set of portable cache keys ``"<cachetype>/<hash>"`` this index
        roots — the produced live-key contribution to GC reachability."""
        out = set()
        for entry in self.entries.values():
            ctype = entry.get("cachetype", "")
            h = entry.get("hash", "")
            if ctype and h:
                out.add(f"{ctype}/{h}")
        return out

    def to_dict(self) -> dict:
        data = {"_META": {"schema": self.SCHEMA}}
        for name, entry in self.entries.items():
            data[name] = dict(entry)
        return data

    def write(self, path: str = "") -> str:
        """Write the index to *path* (or its loaded ``path``), canonically
        ordered. Returns the path written."""
        target = path or self.path
        if not target:
            raise ValueError("no path given and CachedIndex has no loaded path")
        target = self._resolve_path(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as f:
            tomli_w.dump(sort_recursive(self.to_dict()), f)
        self.path = target
        return target
