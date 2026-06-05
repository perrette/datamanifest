"""The ``cached.toml`` index — the produced-dataset registry.

``cached.toml`` is to produced datasets what ``datasets.toml`` is to fetched
ones: a registry, sibling to the manifest by default, that lists each produced
dataset by its **portable** identity rather than by an absolute path, and is the
set of *roots* that keeps produced cache artifacts reachable for garbage
collection.

Schema 2 is **nested**: one ``[[produced]]`` recipe table per
``(cachetype, version)`` carrying recipe-level metadata (``ref`` / ``format``),
with one ``[[produced.instances]]`` table per produced *variation* recording its
parameter ``hash`` and the ``params`` (the key table) that produced it. A recipe
therefore accumulates **every** variation it has produced — calling a recipe
with different parameters adds instances, it does not overwrite. Schema 1 (a flat
table per registry *name* with a single ``hash`` and no params) is still
**read** (each becomes a one-instance recipe). (spec-v4 dropped the recipe
``scope`` and ``store``; any present in an older file are ignored on read.)

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
    """Read/register/write a ``cached.toml`` produced-dataset registry (schema 2).

    In memory the index is ``recipes``: a dict keyed by the recipe identity
    ``(cachetype, version)`` whose value is
    ``{"ref", "format", "instances": {hash: params}}``. :meth:`write` uses the
    same recursive canonical key ordering
    (:func:`datamanifest.store.sort_recursive`) as the manifest writer — with the
    ``produced`` recipe list pre-sorted by identity and each recipe's instances
    pre-sorted by hash — so a read/write round-trip is byte-stable.
    """

    SCHEMA = 2

    def __init__(self, recipes: dict = None, path: str = ""):
        # {(cachetype, version): {ref, format, instances: {hash: params}}}
        self.recipes = dict(recipes) if recipes else {}
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
        """Read a ``cached.toml`` (schema 2 nested, or schema 1 flat) from *path*
        (a file, or a directory holding the default-named index)."""
        target = cls._resolve_path(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        schema = data.get("_META", {}).get("schema", 1)
        recipes = {}
        if schema >= 2:
            for rec in data.get("produced", []):
                if not isinstance(rec, dict):
                    continue
                key = (rec.get("cachetype", ""), rec.get("version", ""))
                instances = {}
                for inst in rec.get("instances", []):
                    h = inst.get("hash", "")
                    if h:
                        instances[h] = dict(inst.get("params", {}))
                recipes[key] = {
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
                key = (ctype, e.get("version", ""))
                recipes[key] = {
                    "ref": e.get("ref", ""),
                    "format": e.get("format", ""),
                    "instances": {h: {}} if h else {},
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
        version: str = "",
    ) -> None:
        """Add (or update) the produced *variation* ``hash`` under its recipe.

        The recipe is identified by ``(cachetype, version)``; the variation by
        its parameter ``hash`` plus the ``params`` (key table) that produced it.
        Registering accumulates: a new ``hash`` adds an instance rather than
        replacing the recipe, so all variations stay referenced. Recipe-level
        metadata (``ref`` / ``format``) is refreshed on each register, so e.g.
        ``ref`` tracks the producing function across a refactor without
        invalidating anything.
        """
        key = (cachetype, version)
        rec = self.recipes.get(key)
        if rec is None:
            rec = {"ref": ref, "format": format, "instances": {}}
            self.recipes[key] = rec
        else:
            rec["ref"], rec["format"] = ref, format
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
                "ref": rec["ref"], "format": rec["format"],
                "instances": dict(rec["instances"]),
            })
        return out

    def to_dict(self) -> dict:
        """Build the schema-2 TOML structure, with ``produced`` pre-sorted by
        ``(cachetype, version)`` and each recipe's ``instances`` by hash
        (canonical key sorting is applied on top by :meth:`write`)."""
        produced = []
        for key in sorted(self.recipes):
            cachetype, version = key
            rec = self.recipes[key]
            entry = {
                "cachetype": cachetype,
                "ref": rec["ref"],
                "format": rec["format"],
                "instances": [
                    ({"hash": h, "params": dict(rec["instances"][h])}
                     if rec["instances"][h] else {"hash": h})
                    for h in sorted(rec["instances"])
                ],
            }
            if version:
                entry["version"] = version
            produced.append(entry)
        return {"_META": {"schema": self.SCHEMA}, "produced": produced}

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
