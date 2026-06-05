"""The ``cached.toml`` index — the produced-dataset registry.

``cached.toml`` is to produced datasets what ``datasets.toml`` is to fetched
ones: a registry, sibling to the manifest by default, that lists each produced
dataset by its **portable** identity rather than by an absolute path, and is the
set of *roots* that keeps produced cache artifacts reachable for garbage
collection.

Schema 3 (spec-v4) keys each recipe by its **cachetype** directly — one table
``["<cachetype>"]`` (or ``["<cachetype>@<version>"]`` when versioned) carrying
``ref`` / ``format``, and one ``["<cachetype>[@version]".instances.<hash>]``
sub-table per produced *variation* whose **body is the params** (the key table)
that produced it. So a recipe accumulates **every** variation — calling it with
different parameters adds an instance, it does not overwrite — and the params sit
directly under the hash with no separate wrapper:

    ["mypkg.mod.run@v3"]
    ref = "mypkg.mod:run"
    format = "pickle"

    ["mypkg.mod.run@v3".instances.83b2…]
    grid = "5x5"

The version rides in the key (``@<version>``) so the common unversioned case
stays a bare cachetype and two versions of one cachetype never collide. Schema 2
(``[[produced]]`` array-of-tables) and schema 1 (a flat table per *name*) are
still **read** and rewritten as schema 3. (spec-v4 dropped the recipe ``scope``
and ``store``; any present in an older file are ignored on read.)

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

__all__ = [
    "CachedIndex",
    "CACHED_INDEX_NAME",
    "CACHED_RECIPE_FIELDS",
    "CACHED_INSTANCE_FIELDS",
]

CACHED_INDEX_NAME = "cached.toml"

# Recipe-level fields (one per (cachetype, version)) and the per-variation
# instance fields. Write ordering is canonical/sorted regardless; these document
# the schema-2 shape.
CACHED_RECIPE_FIELDS = ("cachetype", "version", "ref", "format")
CACHED_INSTANCE_FIELDS = ("hash", "params")


class CachedIndex:
    """Read/register/write a ``cached.toml`` produced-dataset registry (schema 3).

    In memory the index is ``recipes``: a dict keyed by the recipe identity
    ``(cachetype, version)`` whose value is
    ``{"storage_path", "ref", "format", "instances": {hash: params}}``. On disk
    (schema 3) the recipe is a table keyed ``["<cachetype>@<version>"]`` —
    ``@`` separates the version, and a bare ``["<cachetype>"]`` is unversioned. A
    cachetype may **not** contain ``@`` (the separator is reserved), so the split
    is unambiguous; produced cachetypes are ``module.qualname``, which never do.
    :meth:`write` uses the recursive canonical key ordering
    (:func:`datamanifest.store.sort_recursive`) as the manifest writer, so a
    read/write round-trip is byte-stable.
    """

    SCHEMA = 3
    # Reserved separator embedding the version in a recipe's table key
    # (``cachetype@version``); a bare cachetype key is the unversioned recipe.
    _VERSION_SEP = "@"

    def __init__(self, recipes: dict = None, path: str = ""):
        # {(cachetype, version):
        #     {storage_path, ref, format, instances: {hash: params}}}
        self.recipes = dict(recipes) if recipes else {}
        self.path = path

    @classmethod
    def _split_key(cls, key: str):
        """``"cachetype@version"`` / ``"cachetype"`` → ``(cachetype, version)``
        (a cachetype never contains ``@``, so the partition is unambiguous)."""
        cachetype, _, version = key.partition(cls._VERSION_SEP)
        return cachetype, version

    @classmethod
    def _join_key(cls, cachetype: str, version: str) -> str:
        """``(cachetype, version)`` → the recipe table key (``cachetype@version``
        when versioned, else the bare cachetype)."""
        return f"{cachetype}{cls._VERSION_SEP}{version}" if version else cachetype

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
        """Read a ``cached.toml`` (schema 3 cachetype-keyed, schema 2 nested, or
        schema 1 flat) from *path* (a file, or a directory holding the
        default-named index)."""
        target = cls._resolve_path(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        schema = data.get("_META", {}).get("schema", 1)
        recipes = {}
        if schema >= 3:
            # ["<cachetype>@<version>"] tables (bare cachetype = unversioned);
            # each recipe's ``instances`` sub-table maps a hash to its params
            # body, plus ``storage_path`` (the recipe's parent dir).
            for key, rec in data.items():
                if key == "_META" or not isinstance(rec, dict):
                    continue
                cachetype, version = cls._split_key(key)
                instances = {
                    h: dict(p)
                    for h, p in rec.get("instances", {}).items()
                    if isinstance(p, dict)
                }
                if not instances:
                    # A recipe with no produced variations roots nothing — skip it
                    # (drops dead/residual entries rather than round-tripping them).
                    continue
                recipes[(cachetype, version)] = {
                    "storage_path": rec.get("storage_path", ""),
                    "ref": rec.get("ref", ""),
                    "format": rec.get("format", ""),
                    "instances": instances,
                }
        elif schema == 2:
            for rec in data.get("produced", []):
                if not isinstance(rec, dict):
                    continue
                key = (rec.get("cachetype", ""), rec.get("version", ""))
                instances = {}
                for inst in rec.get("instances", []):
                    h = inst.get("hash", "")
                    if h:
                        instances[h] = dict(inst.get("params", {}))
                if not instances:
                    continue
                recipes[key] = {
                    "storage_path": "",
                    "ref": rec.get("ref", ""),
                    "format": rec.get("format", ""),
                    "instances": instances,
                }
        else:
            # Schema 1: a flat table per name with a single hash and no params.
            for name, e in data.items():
                if name == "_META" or not isinstance(e, dict):
                    continue
                ctype, h = e.get("cachetype", ""), e.get("hash", "")
                if not h:
                    continue
                key = (ctype, e.get("version", ""))
                recipes[key] = {
                    "storage_path": "",
                    "ref": e.get("ref", ""),
                    "format": e.get("format", ""),
                    "instances": {h: {}},
                }
        return cls(recipes=recipes, path=target)

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
        *,
        cachetype: str,
        hash: str,
        params: dict = None,
        ref: str = "",
        format: str = "",
        storage_path: str = "",
        version: str = "",
    ) -> None:
        """Add (or update) the produced *variation* ``hash`` under its recipe.

        The recipe is identified by ``(cachetype, version)``; the variation by
        its parameter ``hash`` plus the ``params`` (key table) that produced it.
        Registering accumulates: a new ``hash`` adds an instance rather than
        replacing the recipe, so all variations stay referenced. Recipe-level
        metadata (``ref`` / ``format`` / ``storage_path`` — the recipe's parent
        dir, parallel to the cachetype) is refreshed on each register, so e.g.
        ``ref`` tracks the producing function across a refactor and
        ``storage_path`` records where the recipe's artifacts were last written.

        A *cachetype* may not contain the reserved version separator ``@``.
        """
        if self._VERSION_SEP in cachetype:
            raise ValueError(
                f"cachetype {cachetype!r} may not contain {self._VERSION_SEP!r} "
                "(reserved as the cached.toml version separator)"
            )
        key = (cachetype, version)
        rec = self.recipes.get(key)
        if rec is None:
            rec = {"storage_path": storage_path, "ref": ref, "format": format,
                   "instances": {}}
            self.recipes[key] = rec
        else:
            rec["ref"], rec["format"] = ref, format
            if storage_path:
                rec["storage_path"] = storage_path
        rec["instances"][hash] = dict(params or {})

    def has_instance(self, *, cachetype: str, version: str,
                     hash: str) -> bool:
        """Whether this index already roots the variation
        ``(cachetype, version, hash)``."""
        rec = self.recipes.get((cachetype, version))
        return bool(rec) and hash in rec["instances"]

    def ref_of(self, *, cachetype: str, version: str):
        """The recorded ``ref`` for a recipe, or ``None`` when absent."""
        rec = self.recipes.get((cachetype, version))
        return rec["ref"] if rec else None

    def storage_path_of(self, *, cachetype: str, version: str) -> str:
        """The recipe's recorded ``storage_path`` (its parent dir), or ``""``
        when the recipe is absent / unrecorded."""
        rec = self.recipes.get((cachetype, version))
        return rec["storage_path"] if rec else ""

    def reachable_keys(self) -> set:
        """The set of ``(cachetype, version, hash)`` tuples this index roots —
        **every** instance of every recipe. Reachability (spec-v4) is keyed on
        ``(cachetype, version, hash)`` alone."""
        return {
            (cachetype, version, h)
            for (cachetype, version), rec in self.recipes.items()
            for h in rec["instances"]
        }

    def keys(self) -> set:
        """The set of portable ``"<cachetype>/<hash>"`` keys this index roots
        (across all variations)."""
        return {
            f"{cachetype}/{h}"
            for (cachetype, _version), rec in self.recipes.items()
            for h in rec["instances"]
            if cachetype and h
        }

    def recipe_records(self) -> list:
        """The recipes as a list of plain dicts (identity + metadata +
        ``instances`` mapping ``hash -> params``), for inspection."""
        out = []
        for (cachetype, version), rec in self.recipes.items():
            out.append({
                "cachetype": cachetype, "version": version,
                "storage_path": rec["storage_path"],
                "ref": rec["ref"], "format": rec["format"],
                "instances": dict(rec["instances"]),
            })
        return out

    def to_dict(self) -> dict:
        """Build the schema-3 TOML structure: a ``["<cachetype>@<version>"]``
        table per recipe (bare cachetype when unversioned) carrying
        ``storage_path`` / ``ref`` / ``format`` and an ``instances`` map of
        ``hash -> params``. Canonical key sorting is applied on top by
        :meth:`write`."""
        out = {"_META": {"schema": self.SCHEMA}}
        for (cachetype, version), rec in self.recipes.items():
            out[self._join_key(cachetype, version)] = {
                "storage_path": rec["storage_path"],
                "ref": rec["ref"],
                "format": rec["format"],
                "instances": {
                    h: dict(rec["instances"][h]) for h in rec["instances"]
                },
            }
        return out

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
