"""Migrate an older manifest to the current format.

:func:`migrate_manifest` does three things, none of which moves bytes:

1. **Language upgrade (v0 → v1)** — promote each dataset's inline ``python=`` to
   ``[<ds>._LANG.python].fetcher`` and a flat ``julia=`` to
   ``[<ds>._LANG.julia].fetcher`` (see :func:`datamanifest.database.migrate_v0_to_v1`).
2. **Storage reshape (spec-v3 → v4)** — write the two folder fields
   ``[_STORAGE].datasets_dir`` / ``datacache_dir`` at their **defaults** (repo-local
   ``./datasets`` / ``./cached``), drop the retired scope/prefix/store rungs, and
   carry an explicit ``local_path`` over to ``storage_path``. The committed manifest
   stays clean and portable.
3. **Discovery → state file** — probe known locations (the v4 repo-local default
   **and** legacy roots such as ``$user_data_dir/datamanifest/datasets`` and
   ``~/.cache/Datasets``) for data that already exists on disk, and record each
   find's actual location in the git-ignored ``.datamanifest-state.toml`` so
   read-first resolution keeps finding it where it already lives — while *new*
   data follows the clean default. When one location dominates, it proposes
   setting ``datasets_dir`` for the current host instead of recording every
   object. Ambiguity (the same object found in two places) is resolved by an
   interactive menu on a TTY, or an auto-pick (preferring the repo-local copy)
   with ``--no-input`` / no TTY.
"""

import os
import socket
import sys
from collections import Counter

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import platformdirs

from .store import locations

# Retired spec-v3 [_STORAGE] keys, dropped on migration.
_RETIRED_STORAGE_KEYS = (
    "default", "scope", "store", "_SCOPE", "_PREFIX", "_PROFILE", "data", "cache",
)


def migrate_manifest(toml_path, *, env=None, dry_run=False, no_input=False,
                     datasets_pools=None, datacache_pools=None):
    """Upgrade *toml_path* in place (language + storage), then discover existing
    data and record it in the sibling state file. Returns a human-readable summary
    (with *dry_run*, of what *would* change; nothing is written).

    *datasets_pools* / *datacache_pools*, when given (a list of path expressions),
    **override** the built-in discovery locations for this run (an empty list
    disables that side of discovery)."""
    from .cache import CACHED_INDEX_NAME, CachedIndex
    from .database import Database, migrate_v0_to_v1

    env = os.environ if env is None else env
    toml_path = os.path.abspath(toml_path)
    project_root = os.path.dirname(toml_path)

    with open(toml_path, "rb") as f:
        old_cfg = dict(tomllib.load(f).get("_STORAGE", {}))

    db = Database(datasets_toml=toml_path, persist=False)

    datasets_dir = locations.FIELD_DEFAULTS["datasets_dir"]
    datacache_dir = locations.FIELD_DEFAULTS["datacache_dir"]

    changes = [
        f"[_STORAGE].datasets_dir  = {datasets_dir!r}  (default; edit to relocate)",
        f"[_STORAGE].datacache_dir = {datacache_dir!r}  (default; edit to relocate)",
    ]
    needs_attention = []

    # 1. Language upgrade — scan for the summary, then apply in-place.
    for name, entry in db.datasets.items():
        if entry.python and not entry.lang_python_fetcher:
            changes.append(f"{name}.python → [{name}._LANG.python].fetcher")
        julia_inline = entry.extra.get("julia")
        if isinstance(julia_inline, str) and julia_inline:
            changes.append(f"{name}.julia → [{name}._LANG.julia].fetcher")
    migrate_v0_to_v1(db)

    # 2. Storage reshape.
    for name, entry in db.datasets.items():
        old_store = entry.extra.pop("store", "")
        old_local = entry.extra.pop("local_path", "")
        if old_local:
            entry.storage_path = old_local
            changes.append(f"{name}.storage_path = {old_local!r}  (was local_path)")
        elif old_store:
            needs_attention.append(f"{name}  (had store = {old_store!r})")

    new_cfg = {"datasets_dir": datasets_dir, "datacache_dir": datacache_dir}
    if isinstance(old_cfg.get("_HOST"), dict):
        new_cfg["_HOST"] = dict(old_cfg["_HOST"])
    for k, v in old_cfg.items():
        if k in ("datasets_dir", "datacache_dir", "_HOST") or k in _RETIRED_STORAGE_KEYS:
            continue
        new_cfg.setdefault(k, v)
    db.extra["_STORAGE"] = new_cfg

    # 3. Discover existing data on disk and record it in the state file (may also
    #    add a host-scoped datasets_dir override to db.extra["_STORAGE"]).
    ds_roots = (locations.resolve_pool_exprs(
        datasets_pools, project_root=project_root, storage_config=new_cfg)
        if datasets_pools is not None else None)
    dc_roots = (locations.resolve_pool_exprs(
        datacache_pools, project_root=project_root, storage_config=new_cfg)
        if datacache_pools is not None else None)
    discovery = _discover_and_record(
        db, project_root, env, dry_run=dry_run, no_input=no_input,
        dataset_roots=ds_roots, datacache_roots=dc_roots,
    )

    cached_toml = os.path.join(project_root, CACHED_INDEX_NAME)
    legacy_cached = os.path.join(project_root, "cached.toml")
    if os.path.isfile(legacy_cached) and not os.path.isfile(cached_toml):
        changes.append("migrated cached.toml → .datamanifest-state.toml")

    if not dry_run:
        db.write(toml_path)

    verb = "Would update" if dry_run else "Updated"
    summary = [f"{verb} {toml_path}:", "  " + "\n  ".join(changes)]
    if discovery:
        summary.append("\nDiscovered existing data (recorded in the state file):\n  "
                       + "\n  ".join(discovery))
    if needs_attention:
        summary.append(
            "\nNeeds manual attention — these datasets used a retired `store` "
            "selector that was dropped:\n  " + "\n  ".join(needs_attention)
        )
    summary.append(
        "\nThe spec-v4 defaults are repo-local (./datasets/, ./cached/). New data "
        "follows them; data found elsewhere was recorded in "
        ".datamanifest-state.toml so it still resolves. Edit "
        "[_STORAGE].datasets_dir / datacache_dir (or use `datamanifest storage`) "
        "to relocate. No bytes were moved."
    )
    return "\n".join(summary)


# ----- discovery -------------------------------------------------------------

def _dedupe_abspaths(paths):
    seen, out = set(), []
    for p in paths:
        a = os.path.abspath(os.path.expanduser(os.path.expandvars(p)))
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _candidate_dataset_roots(project_root, env):
    """Roots under which a populated ``<root>/<key>`` might already exist — the v4
    repo-local default plus known legacy locations — for migrate discovery."""
    roots = [
        os.path.join(project_root, "datasets"),
        os.path.join(project_root, "Datasets"),
        os.path.join(platformdirs.user_data_dir("datamanifest"), "datasets"),
        os.path.join(platformdirs.user_data_dir(), "datasets"),
        os.path.join(os.path.expanduser("~"), ".cache", "Datasets"),
    ]
    val = env.get("DATAMANIFEST_DATASETS_DIR")
    if val:
        roots.append(val)
    return _dedupe_abspaths(roots)


def _legacy_project_id(project_root):
    """The spec-v3 cache scope segment for this project: ``[project].name`` from a
    sibling ``pyproject.toml``, else a stable hash of the project-root path. Used
    only to scope the legacy global cache to *this* project (it is shared across
    projects under ``<user_cache_dir>/datamanifest/cached/<project-id>``)."""
    pyproject = os.path.join(project_root or "", "pyproject.toml")
    if project_root and os.path.isfile(pyproject):
        try:
            with open(pyproject, "rb") as f:
                name = tomllib.load(f).get("project", {}).get("name")
            if isinstance(name, str) and name:
                return name
        except (OSError, ValueError):
            pass
    import hashlib
    return hashlib.sha256(
        os.path.abspath(project_root or "").encode("utf-8")).hexdigest()[:16]


def _candidate_datacache_roots(project_root, env):
    """Roots to scan for *this project's* already-produced ``@cached`` artifacts:
    the v4 repo-local default, the configured ``DATAMANIFEST_DATACACHE_DIR``, and
    the legacy global cache **scoped to this project** by its spec-v3 project-id
    (the global cache is shared, so it must not be scanned wholesale)."""
    roots = [os.path.join(project_root, "cached")]
    val = env.get("DATAMANIFEST_DATACACHE_DIR")
    if val:
        roots.append(val)
    roots.append(os.path.join(
        platformdirs.user_cache_dir("datamanifest"), "cached",
        _legacy_project_id(project_root),
    ))
    return _dedupe_abspaths(roots)


def _portable(path, project_root):
    """Relative to the manifest dir when under it, else absolute (the state-file
    convention)."""
    ap = os.path.abspath(path)
    rt = os.path.abspath(project_root) if project_root else ""
    if rt and (ap == rt or ap.startswith(rt + os.sep)):
        return os.path.relpath(ap, rt)
    return ap


def _interactive(no_input):
    return not no_input and sys.stdin.isatty()


def _choose_location(name, hits, *, no_input):
    """Pick which discovered copy of dataset *name* to adopt. A single hit is
    returned directly; multiple hits prompt a menu on a TTY, else auto-pick
    (preferring a repo-local ``datasets/`` copy, then the first). ``None`` skips."""
    if len(hits) == 1:
        return hits[0]
    default = next((h for h in hits if f"{os.sep}datasets{os.sep}" in h), hits[0])
    if not _interactive(no_input):
        return default
    print(f"\nDataset {name!r} found in {len(hits)} locations:")
    for i, h in enumerate(hits, 1):
        print(f"  {i}) {h}")
    print("  s) skip (leave to re-download)")
    while True:
        choice = input(f"Adopt which? [1-{len(hits)}/s] (default 1): ").strip().lower()
        if choice == "":
            return hits[0]
        if choice == "s":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(hits):
            return hits[int(choice) - 1]


def _confirm(question, *, no_input):
    """Yes/no prompt. Non-interactive (``--no-input`` / no TTY) defaults to **no**
    — a config change to the committed manifest needs an explicit yes."""
    if not _interactive(no_input):
        return False
    return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")


def _discover_and_record(db, project_root, env, *, dry_run, no_input,
                         dataset_roots=None, datacache_roots=None):
    """Probe candidate roots for existing data, record finds in the state file,
    and (when one location dominates) propose a host-scoped ``datasets_dir``.
    Returns a list of human-readable summary lines. Writes nothing on *dry_run*.
    *dataset_roots* / *datacache_roots* override the built-in candidate roots."""
    from .cache import CachedIndex

    lines = []
    roots = (dataset_roots if dataset_roots is not None
             else _candidate_dataset_roots(project_root, env))
    default_root = os.path.join(os.path.abspath(project_root), "datasets")

    # Find, per dataset, the candidate locations that actually hold bytes.
    chosen = {}                       # name -> adopted absolute location
    for name, entry in db.datasets.items():
        if entry.skip_download or entry.storage_path:
            continue                  # external / user-managed: leave as declared
        hits = _dedupe_abspaths(
            [os.path.join(r, entry.key) for r in roots
             if os.path.exists(os.path.join(r, entry.key))]
        )
        if not hits:
            continue
        loc = _choose_location(name, hits, no_input=no_input)
        if loc:
            chosen[name] = loc

    # The root each adopted object sits under (strip the trailing /<key>).
    root_of = {}
    for name, loc in chosen.items():
        key = db.datasets[name].key
        suffix = os.sep + key.replace("/", os.sep)
        root_of[name] = loc[:-len(suffix)] if loc.endswith(suffix) else os.path.dirname(loc)

    # Dominant non-default root → offer to set datasets_dir for this host.
    host_dir = None
    if root_of:
        top_root, top_n = Counter(root_of.values()).most_common(1)[0]
        if top_n == len(root_of) and top_n > 0 and top_root != default_root:
            if _confirm(
                f"All {top_n} discovered dataset(s) are under {top_root!r}. "
                "Send new downloads there too (set datasets_dir for this host)?",
                no_input=no_input,
            ):
                storage = db.extra.setdefault("_STORAGE", {})
                host = socket.gethostname()
                storage.setdefault("_HOST", {}).setdefault(host, {})["datasets_dir"] = top_root
                host_dir = top_root
                lines.append(
                    f'[_STORAGE._HOST."{host}"].datasets_dir = {top_root!r}  '
                    "(new downloads land here on this host)"
                )

    # Record EVERY discovered dataset's real location in the state file — a
    # complete inventory (transparency + a complete migration), regardless of
    # whether it also resolves via datasets_dir. (host_dir, when set, only governs
    # where *new* data is written.)
    base = os.path.dirname(db.datasets_toml) or os.path.abspath(project_root)
    idx = CachedIndex.read_or_empty(base)
    touched = False
    for name, loc in chosen.items():
        entry = db.datasets[name]
        sp = _portable(loc, project_root)
        sha = "" if (db.skip_checksum or entry.skip_checksum) else (entry.sha256 or "")
        idx.register_dataset(key=entry.key, storage_path=sp, sha256=sha)
        touched = True
        lines.append(f"{name} → {sp}")

    touched |= _discover_cached(idx, project_root, env, lines, roots=datacache_roots)

    if touched and not dry_run:
        idx.write()
    return lines


def _discover_cached(idx, project_root, env, lines, roots=None):
    """Scan candidate datacache roots for produced artifacts and record any not
    already in the state file. Returns whether anything was added. *roots*
    overrides the built-in candidate datacache roots."""
    from .cache import CachedIndex, read_config
    from .cache._inspect import _guess_format, find_produced_artifacts

    added = False
    scan_roots = roots if roots is not None else _candidate_datacache_roots(project_root, env)
    for root in scan_roots:
        for artifact_dir, _key in find_produced_artifacts(root):
            try:
                meta = read_config(artifact_dir).get("_META", {})
            except Exception:  # noqa: BLE001 - skip an unreadable artifact
                continue
            ctype, h = meta.get("cachetype", ""), meta.get("hash", "")
            version = meta.get("version", "")
            if not (ctype and h) or CachedIndex._VERSION_SEP in ctype:
                continue
            if idx.has_instance(cachetype=ctype, version=version, hash=h):
                continue
            sp = _portable(artifact_dir, project_root)
            idx.register(cachetype=ctype, hash=h, version=version,
                         storage_path=sp, format=_guess_format(artifact_dir))
            added = True
            lines.append(f"{ctype}{('@' + version) if version else ''}/{h[:8]} → {sp}")
    return added
