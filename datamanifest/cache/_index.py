"""The state file (``.datamanifest-state.toml``) — the local object inventory.

The state file is to ``datamanifest.toml`` what a lockfile is to a manifest,
except it is **git-ignored, regenerable local state**: ``datamanifest.toml`` says
*what* to track and *how* to obtain it (committed, the source of intent); the
state file records *where* each object actually landed **on this machine** (+ a
fetched dataset's checksum). One inventory for everything materialized — fetched
**and** produced.

Schema 5 has two top-level namespaces, parallel to the two storage fields:

    [_META]
    schema = 5

    # produced artifacts: cachetype[@version] → instances{hash → artifact dir}
    [datacache."mypkg.mod.run@v3"]
    ref = "mypkg.mod:run"
    format = "pickle"

    [datacache."mypkg.mod.run@v3".instances]
    "83b2…" = "cached/mypkg.mod.run/v3/83b2…"

    # fetched datasets: key → resolved location (+ actual checksum)
    [datasets."example.com/host/a.csv"]
    storage_path = "datasets/example.com/host/a.csv"
    sha256 = "abc123…"

A produced recipe is keyed by its **cachetype** (with ``@<version>`` when
versioned; a cachetype may **not** contain ``@``), carrying ``ref`` / ``format``
and an ``instances`` map of each variation's parameter ``hash`` → **its on-disk
artifact directory** (hash included), recorded per instance. A fetched dataset is
keyed by its storage **key** and records the resolved ``storage_path`` plus the
actual ``sha256`` (omitted when ``skip_checksum``).

The state file is a per-object **inventory of where objects are** (read-only; it
never directs a *write* — writes always follow the current ``datacache_dir`` /
``datasets_dir`` / ``storage_path`` directive). The **params** that produced each
variation are not stored here — they live in each artifact's ``config.toml``
sidecar.

Older shapes are still **read** and rewritten forward: schema 4 (top-level
``["<cachetype>"]`` recipe tables) migrates into the ``datacache`` namespace;
schema 3 (params-body + recipe-level ``storage_path``), schema 2
(``[[produced]]``) and schema 1 (flat) as before. The previous filename
``cached.toml`` is still **read** as a fallback (and rewritten to
``.datamanifest-state.toml`` on the next write).

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
    "STATE_FILE_NAME",
    "CACHED_RECIPE_FIELDS",
    "CACHED_INSTANCE_FIELDS",
    "DATASET_STATE_FIELDS",
]

# The canonical state-file name (git-ignored). ``CACHED_INDEX_NAME`` is kept as
# the historical alias used across the codebase.
STATE_FILE_NAME = ".datamanifest-state.toml"
CACHED_INDEX_NAME = STATE_FILE_NAME
# Filenames read as a fallback when the canonical one is absent (so a project
# carrying the previous ``cached.toml`` keeps resolving; the next write migrates
# it to the canonical name).
_LEGACY_INDEX_NAMES = ("cached.toml",)

# Produced-recipe fields (one per (cachetype, version)); each instance is a
# ``hash -> storage_path`` entry. Fetched-dataset entries carry the fields below.
CACHED_RECIPE_FIELDS = ("cachetype", "version", "ref", "format")
CACHED_INSTANCE_FIELDS = ("hash", "storage_path")
DATASET_STATE_FIELDS = ("key", "storage_path", "sha256")


class CachedIndex:
    """Read/register/write a ``.datamanifest-state.toml`` object inventory (schema 5).

    In memory the inventory is two maps:

    - ``recipes`` (produced): keyed by ``(cachetype, version)`` →
      ``{"ref", "format", "instances": {hash: storage_path}}`` — each per-instance
      ``storage_path`` is the recorded full artifact directory (hash included).
    - ``datasets`` (fetched): keyed by the storage **key** →
      ``{"storage_path", "sha256"}`` — the resolved on-disk location and actual
      checksum.

    On disk these live under the ``datacache`` and ``datasets`` namespaces. A
    produced recipe table is keyed ``["<cachetype>@<version>"]`` (``@`` separates
    the version; a bare key is unversioned; a cachetype may not contain ``@``).
    :meth:`write` uses the recursive canonical key ordering
    (:func:`datamanifest.store.sort_recursive`), so a read/write round-trip is
    byte-stable.
    """

    SCHEMA = 5
    # Reserved separator embedding the version in a recipe's table key
    # (``cachetype@version``); a bare cachetype key is the unversioned recipe.
    _VERSION_SEP = "@"

    def __init__(self, recipes: dict = None, datasets: dict = None, path: str = ""):
        # {(cachetype, version): {ref, format, instances: {hash: storage_path}}}
        self.recipes = dict(recipes) if recipes else {}
        # {key: {storage_path, sha256}}
        self.datasets = dict(datasets) if datasets else {}
        self.path = path

    # ----- key helpers -----
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

    # ----- path helpers (canonical name + legacy fallback) -----
    @classmethod
    def _resolve_path(cls, path: str) -> str:
        """Normalize *path* to a state-file path: a directory yields its canonical
        ``.datamanifest-state.toml``; a file path is returned verbatim (so an
        explicit legacy / custom name is honored)."""
        path = os.fspath(path)
        if os.path.isdir(path):
            return os.path.join(path, STATE_FILE_NAME)
        return path

    @classmethod
    def _canonical_path(cls, path: str) -> str:
        """The path a write should target: a directory or a legacy-named file
        migrates to the canonical ``.datamanifest-state.toml`` sibling; a
        canonical or explicit custom file path is honored verbatim."""
        path = os.fspath(path)
        if os.path.isdir(path):
            return os.path.join(path, STATE_FILE_NAME)
        if os.path.basename(path) in _LEGACY_INDEX_NAMES:
            return os.path.join(os.path.dirname(path) or ".", STATE_FILE_NAME)
        return path

    @classmethod
    def locate(cls, base: str) -> str:
        """The state file to **read** at *base* (a directory or a file path): the
        canonical ``.datamanifest-state.toml`` when present, else a legacy
        ``cached.toml`` sibling, else the canonical path (which may not exist).

        Lets callers find an existing inventory under either name without first
        knowing which is on disk.
        """
        base = os.fspath(base)
        if os.path.isfile(base):
            return base
        d = base if os.path.isdir(base) else (os.path.dirname(base) or ".")
        canonical = os.path.join(d, STATE_FILE_NAME)
        if os.path.isfile(canonical):
            return canonical
        for legacy in _LEGACY_INDEX_NAMES:
            p = os.path.join(d, legacy)
            if os.path.isfile(p):
                return p
        return canonical

    # ----- reading -----
    @classmethod
    def read(cls, path: str) -> "CachedIndex":
        """Read a state file (schema 5 namespaced, or any older shape) from
        *path* — a file, or a directory holding the inventory under its canonical
        or legacy name."""
        target = cls.locate(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        schema = data.get("_META", {}).get("schema", 1)
        recipes: dict = {}
        datasets: dict = {}
        if schema >= 5:
            cls._read_datacache_namespace(data.get("datacache", {}), recipes)
            cls._read_datasets_namespace(data.get("datasets", {}), datasets)
        elif schema == 4:
            # Top-level ["<cachetype>@<version>"] recipe tables (no namespace).
            cls._read_datacache_namespace(
                {k: v for k, v in data.items() if k != "_META"}, recipes,
            )
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
        return cls(recipes=recipes, datasets=datasets, path=target)

    @classmethod
    def _read_datacache_namespace(cls, table, recipes: dict) -> None:
        """Populate *recipes* from a ``datacache`` namespace (schema 5) or the
        top-level recipe tables (schema 4) — both are ``key → recipe`` maps with
        an ``instances`` hash→storage_path map. Instance-less recipes are dropped
        (they root nothing)."""
        if not isinstance(table, dict):
            return
        for key, rec in table.items():
            if not isinstance(rec, dict):
                continue
            cachetype, version = cls._split_key(key)
            instances = {
                h: p for h, p in rec.get("instances", {}).items()
                if isinstance(p, str)
            }
            if not instances:
                continue
            recipes[(cachetype, version)] = {
                "ref": rec.get("ref", ""),
                "format": rec.get("format", ""),
                "instances": instances,
            }

    @classmethod
    def _read_datasets_namespace(cls, table, datasets: dict) -> None:
        """Populate *datasets* from a ``datasets`` namespace (key → {storage_path,
        sha256})."""
        if not isinstance(table, dict):
            return
        for key, rec in table.items():
            if not isinstance(rec, dict):
                continue
            sp = rec.get("storage_path", "")
            sha = rec.get("sha256", "")
            datasets[key] = {
                "storage_path": sp if isinstance(sp, str) else "",
                "sha256": sha if isinstance(sha, str) else "",
            }

    @classmethod
    def read_or_empty(cls, path: str) -> "CachedIndex":
        """Read the state file at *path* (canonical or legacy name), or return an
        empty one bound to the canonical path when none exists. Either way the
        bound ``path`` is the canonical name, so the next :meth:`write` migrates a
        legacy file forward."""
        canonical = cls._canonical_path(path)
        target = cls.locate(path)
        if os.path.isfile(target):
            idx = cls.read(target)
            idx.path = canonical
            return idx
        return cls(path=canonical)

    # ----- produced recipes -----
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

        The recipe is identified by ``(cachetype, version)``; the variation by its
        parameter ``hash`` plus the per-instance ``storage_path`` — the recorded
        full artifact directory of *this* variation. Registering accumulates: a
        new ``hash`` adds an instance rather than replacing the recipe. Recipe-level
        ``ref`` / ``format`` are refreshed on each register; the per-instance
        ``storage_path`` is updated to where the artifact was just written / found.

        A *cachetype* may not contain the reserved version separator ``@``.
        """
        if self._VERSION_SEP in cachetype:
            raise ValueError(
                f"cachetype {cachetype!r} may not contain {self._VERSION_SEP!r} "
                "(reserved as the state-file version separator)"
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
        """Whether this inventory already roots the variation
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
        ``False`` if it is not in the inventory (an unrooted/orphan artifact)."""
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
        """The set of ``(cachetype, version, hash)`` tuples this inventory roots —
        **every** instance of every recipe."""
        return {
            (cachetype, version, h)
            for (cachetype, version), rec in self.recipes.items()
            for h in rec["instances"]
        }

    def keys(self) -> set:
        """The set of portable ``"<cachetype>/<hash>"`` keys this inventory roots
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

    # ----- fetched datasets -----
    def register_dataset(self, *, key: str, storage_path: str = "",
                         sha256: str = "") -> None:
        """Record (or refresh) a fetched dataset's resolved location / checksum.

        Accumulates additively: a non-empty *storage_path* / *sha256* overwrites
        the recorded one (so a relocated dataset is repointed, a freshly hashed
        one gains its checksum), while an empty argument leaves the existing value
        untouched (a ``skip_checksum`` re-record keeps any prior sha)."""
        rec = self.datasets.get(key)
        if rec is None:
            rec = {"storage_path": "", "sha256": ""}
            self.datasets[key] = rec
        if storage_path:
            rec["storage_path"] = storage_path
        if sha256:
            rec["sha256"] = sha256

    def has_dataset(self, key: str) -> bool:
        """Whether a fetched dataset *key* is recorded."""
        return key in self.datasets

    def dataset_path_of(self, key: str) -> str:
        """The recorded resolved ``storage_path`` of a fetched dataset, or ``""``."""
        return self.datasets.get(key, {}).get("storage_path", "")

    def dataset_sha256_of(self, key: str) -> str:
        """The recorded actual ``sha256`` of a fetched dataset, or ``""``."""
        return self.datasets.get(key, {}).get("sha256", "")

    def set_dataset_path(self, key: str, storage_path: str) -> bool:
        """Repoint a recorded dataset's ``storage_path`` (e.g. after a ``--move``).
        Returns ``True`` if the dataset was recorded and updated."""
        rec = self.datasets.get(key)
        if rec is None:
            return False
        rec["storage_path"] = storage_path
        return True

    def remove_dataset(self, key: str) -> bool:
        """Drop a recorded dataset (e.g. after a ``--delete``). Returns ``True`` if
        it was present."""
        return self.datasets.pop(key, None) is not None

    def dataset_records(self) -> list:
        """The fetched datasets as a list of plain dicts (``key`` / ``storage_path``
        / ``sha256``), for inspection."""
        return [
            {"key": k, "storage_path": rec.get("storage_path", ""),
             "sha256": rec.get("sha256", "")}
            for k, rec in self.datasets.items()
        ]

    # ----- serialization -----
    def to_dict(self) -> dict:
        """Build the schema-5 TOML structure: a ``datacache`` namespace of
        ``["<cachetype>@<version>"]`` recipe tables and a ``datasets`` namespace of
        per-key location/checksum tables. An empty namespace is omitted. Canonical
        key sorting is applied on top by :meth:`write`."""
        out: dict = {"_META": {"schema": self.SCHEMA}}
        datacache: dict = {}
        for (cachetype, version), rec in self.recipes.items():
            datacache[self._join_key(cachetype, version)] = {
                "ref": rec["ref"],
                "format": rec["format"],
                "instances": dict(rec["instances"]),
            }
        if datacache:
            out["datacache"] = datacache
        datasets: dict = {}
        for key, rec in self.datasets.items():
            entry = {"storage_path": rec.get("storage_path", "")}
            if rec.get("sha256"):
                entry["sha256"] = rec["sha256"]
            datasets[key] = entry
        if datasets:
            out["datasets"] = datasets
        return out

    def write(self, path: str = "") -> str:
        """Write the state file to *path* (or its bound ``path``, migrating a
        legacy name to the canonical one), canonically ordered. Returns the path
        written.

        The write is **atomic** — a sibling temp file is filled and then
        ``os.replace``-renamed into place — so concurrent writers (parallel
        downloads / ``@cached`` produces, each re-reading and additively merging
        before writing) never observe or leave a half-written inventory.
        """
        target = self._canonical_path(path) if path else self.path
        if not target:
            raise ValueError("no path given and CachedIndex has no loaded path")
        target = self._resolve_path(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        data = self.to_dict()
        # ``_META`` (and any ``_``-table) first, then ``datacache`` / ``datasets``.
        ordered = {
            k: sort_recursive(v)
            for k, v in sorted(
                data.items(), key=lambda kv: (not kv[0].startswith("_"), kv[0])
            )
        }
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            tomli_w.dump(ordered, f)
        os.replace(tmp, target)
        self.path = target
        return target
