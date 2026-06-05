"""The ``cached.toml`` index — the produced-dataset registry.

``cached.toml`` is to produced datasets what ``datasets.toml`` is to fetched
ones: a registry, sibling to the manifest by default, that lists each produced
dataset by its **portable** identity rather than by an absolute path, and is the
set of *roots* that keeps produced cache artifacts reachable for garbage
collection.

Schema 4 keys each recipe by its **cachetype** — one table ``["<cachetype>"]``
(or ``["<cachetype>@<version>"]`` when versioned) carrying ``ref`` / ``format``,
and a ``["<cachetype>[@version]".instances]`` table mapping each produced
variation's parameter ``hash`` to **its on-disk location** — the full artifact
directory (hash included), recorded per instance:

    ["mypkg.mod.run@v3"]
    ref = "mypkg.mod:run"
    format = "pickle"

    ["mypkg.mod.run@v3".instances]
    "83b2…" = "cached/mypkg.mod.run/v3/83b2…"
    "9c41…" = "/scratch/runs/9c41…"       # e.g. a moved one — recorded per instance

So ``cached.toml`` is a per-object **inventory of where objects are** (read-only;
it never directs a *write* — writes always follow the current ``datacache_dir``
/ ``storage_path`` directive). The **params** that produced each variation are
not stored here — they live in each artifact's own ``config.toml`` sidecar.

The version rides in the key (``@<version>``) so the common unversioned case
stays a bare cachetype and two versions of one cachetype never collide; a
cachetype may **not** contain ``@``. Schema 3 (params-body + a recipe-level
``storage_path``), schema 2 (``[[produced]]``) and schema 1 (flat) are still
**read** and rewritten as schema 4 (params dropped; each instance inherits the
old recipe ``storage_path``, or ``""`` when none was recorded).

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

# Recipe-level fields (one per (cachetype, version)); each instance is a
# ``hash -> storage_path`` entry. These document the schema-4 shape.
CACHED_RECIPE_FIELDS = ("cachetype", "version", "ref", "format")
CACHED_INSTANCE_FIELDS = ("hash", "storage_path")


class CachedIndex:
    """Read/register/write a ``cached.toml`` produced-dataset registry (schema 4).

    In memory the index is ``recipes``: a dict keyed by the recipe identity
    ``(cachetype, version)`` whose value is
    ``{"ref", "format", "instances": {hash: storage_path}}`` — the per-instance
    ``storage_path`` is the recorded full artifact directory (hash included) of that variation. On
    disk the recipe is a table keyed ``["<cachetype>@<version>"]`` (``@``
    separates the version; a bare ``["<cachetype>"]`` is unversioned; a cachetype
    may not contain ``@``). :meth:`write` uses the recursive canonical key
    ordering (:func:`datamanifest.store.sort_recursive`), so a read/write
    round-trip is byte-stable.
    """

    SCHEMA = 4
    # Reserved separator embedding the version in a recipe's table key
    # (``cachetype@version``); a bare cachetype key is the unversioned recipe.
    _VERSION_SEP = "@"

    def __init__(self, recipes: dict = None, path: str = ""):
        # {(cachetype, version): {ref, format, instances: {hash: storage_path}}}
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
        """Read a ``cached.toml`` (schema 4 hash→path, schema 3 params-body,
        schema 2 ``[[produced]]``, or schema 1 flat) from *path* (a file, or a
        directory holding the default-named index)."""
        target = cls._resolve_path(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        schema = data.get("_META", {}).get("schema", 1)
        recipes = {}
        if schema >= 4:
            # ["<cachetype>@<version>"] tables; ``instances`` maps hash → the
            # per-instance recorded storage_path (its full artifact dir).
            for key, rec in data.items():
                if key == "_META" or not isinstance(rec, dict):
                    continue
                cachetype, version = cls._split_key(key)
                instances = {
                    h: p for h, p in rec.get("instances", {}).items()
                    if isinstance(p, str)
                }
                if not instances:
                    # Roots nothing — drop dead/residual entries.
                    continue
                recipes[(cachetype, version)] = {
                    "ref": rec.get("ref", ""),
                    "format": rec.get("format", ""),
                    "instances": instances,
                }
        elif schema == 3:
            # Legacy: instances are a hash→params-body map and the recipe carries
            # one storage_path (the parent dir). Migrate to per-instance full
            # artifact dirs (parent/hash); drop the params (they live in the
            # artifact's config.toml sidecar).
            for key, rec in data.items():
                if key == "_META" or not isinstance(rec, dict):
                    continue
                cachetype, version = cls._split_key(key)
                recipe_sp = rec.get("storage_path", "")
                instances = {
                    h: (f"{recipe_sp}/{h}" if recipe_sp else "")
                    for h, p in rec.get("instances", {}).items()
                    if isinstance(p, dict)
                }
                if not instances:
                    continue
                recipes[(cachetype, version)] = {
                    "ref": rec.get("ref", ""),
                    "format": rec.get("format", ""),
                    "instances": instances,
                }
        elif schema == 2:
            for rec in data.get("produced", []):
                if not isinstance(rec, dict):
                    continue
                key = (rec.get("cachetype", ""), rec.get("version", ""))
                instances = {
                    inst.get("hash", ""): ""
                    for inst in rec.get("instances", [])
                    if inst.get("hash", "")
                }
                if not instances:
                    continue
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
                if not h:
                    continue
                key = (ctype, e.get("version", ""))
                recipes[key] = {
                    "ref": e.get("ref", ""),
                    "format": e.get("format", ""),
                    "instances": {h: ""},
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
        ref: str = "",
        format: str = "",
        storage_path: str = "",
        version: str = "",
    ) -> None:
        """Add (or update) the produced *variation* ``hash`` under its recipe.

        The recipe is identified by ``(cachetype, version)``; the variation by
        its parameter ``hash`` plus the per-instance ``storage_path`` — the
        recorded full artifact directory of *this* variation. Registering
        accumulates: a new ``hash`` adds an instance rather than replacing the
        recipe. Recipe-level ``ref`` / ``format`` are refreshed on each register
        (so ``ref`` tracks the producing function across a refactor); the
        per-instance ``storage_path`` is updated to where the artifact was just
        written / found.

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
            rec = {"ref": ref, "format": format, "instances": {}}
            self.recipes[key] = rec
        else:
            rec["ref"], rec["format"] = ref, format
        rec["instances"][hash] = storage_path

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

    def instance_path_of(self, *, cachetype: str, version: str,
                         hash: str) -> str:
        """The recorded per-instance ``storage_path`` (full artifact dir) of a
        variation, or ``""`` when the instance is absent / unrecorded."""
        rec = self.recipes.get((cachetype, version))
        return rec["instances"].get(hash, "") if rec else ""

    def set_instance_path(self, *, cachetype: str, version: str, hash: str,
                          storage_path: str) -> bool:
        """Update a recorded variation's ``storage_path`` (e.g. after a
        ``--move``). Returns ``True`` if the instance existed and was updated,
        ``False`` if it is not in the index (an unrooted/orphan artifact)."""
        rec = self.recipes.get((cachetype, version))
        if not rec or hash not in rec["instances"]:
            return False
        rec["instances"][hash] = storage_path
        return True

    def remove_instance(self, *, cachetype: str, version: str,
                        hash: str) -> bool:
        """Drop a recorded variation (e.g. after a ``--delete``); the recipe is
        removed too once its last instance is gone. Returns ``True`` if the
        instance existed and was removed."""
        rec = self.recipes.get((cachetype, version))
        if not rec or hash not in rec["instances"]:
            return False
        del rec["instances"][hash]
        if not rec["instances"]:
            del self.recipes[(cachetype, version)]
        return True

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
        ``instances`` mapping ``hash -> storage_path``), for inspection."""
        out = []
        for (cachetype, version), rec in self.recipes.items():
            out.append({
                "cachetype": cachetype, "version": version,
                "ref": rec["ref"], "format": rec["format"],
                "instances": dict(rec["instances"]),
            })
        return out

    def to_dict(self) -> dict:
        """Build the schema-4 TOML structure: a ``["<cachetype>@<version>"]``
        table per recipe (bare cachetype when unversioned) carrying ``ref`` /
        ``format`` and an ``instances`` map of ``hash -> storage_path``.
        Canonical key sorting is applied on top by :meth:`write`."""
        out = {"_META": {"schema": self.SCHEMA}}
        for (cachetype, version), rec in self.recipes.items():
            out[self._join_key(cachetype, version)] = {
                "ref": rec["ref"],
                "format": rec["format"],
                "instances": dict(rec["instances"]),
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
