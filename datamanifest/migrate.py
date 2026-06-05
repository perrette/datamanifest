"""spec-v3 → spec-v4 storage migration — *freeze* existing locations in place.

spec-v4 flips the storage default to repo-local ``./datasets/`` and ``./cached/``.
Rather than relocate any bytes, :func:`migrate_manifest` records where data
**already** lives so it keeps resolving after the upgrade:

- it sets ``[_STORAGE].datasets_dir`` / ``datacache_dir`` to the old spec-v3
  effective roots (so the common, default-store datasets/cache resolve unchanged);
- it adds a per-dataset ``storage_path`` **only** where a dataset deviated from
  the old default (a custom ``store`` selector or an exact ``local_path``), so
  that one keeps pointing at its real bytes;
- it strips the retired ``scope`` / ``store`` from a sibling ``cached.toml``.

It **moves nothing on disk**. The legacy path formula below is self-contained (a
one-shot bridge), so the rest of the package keeps no spec-v3 back-compat.
"""

import hashlib
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import platformdirs

# The spec-v3 application-name segment platformdirs roots carried.
_LEGACY_APPNAME = "datamanifest"


def _legacy_user_root(kind, env):
    """The spec-v3 bare app root for ``$data`` / ``$cache`` (with the
    ``datamanifest`` appname), honouring ``DATAMANIFEST_DIR`` /
    ``DATAMANIFEST_<KIND>_DIR`` as the old resolver did."""
    per_kind = env.get(f"DATAMANIFEST_{kind.upper()}_DIR")
    if per_kind:
        return os.path.expanduser(os.path.expandvars(per_kind))
    base = env.get("DATAMANIFEST_DIR")
    if base:
        return os.path.expanduser(os.path.expandvars(base))
    if kind == "cache":
        return platformdirs.user_cache_dir(_LEGACY_APPNAME)
    return platformdirs.user_data_dir(_LEGACY_APPNAME)


def _legacy_project_id(project_root, env):
    """The spec-v3 cached scope: ``[project].name`` from ``pyproject.toml``, else a
    stable hash of the absolute project-root path."""
    pyproject = os.path.join(project_root or "", "pyproject.toml")
    if project_root and os.path.isfile(pyproject):
        try:
            with open(pyproject, "rb") as f:
                name = tomllib.load(f).get("project", {}).get("name")
            if isinstance(name, str) and name:
                return name
        except (OSError, ValueError):
            pass
    abspath = os.path.abspath(project_root or "")
    return hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:16]


def _legacy_folder_root(name, project_root, old_cfg, env):
    """Resolve a spec-v3 bare folder root for selector folder *name*
    (``data`` / ``cache`` / ``repo`` / a user-defined ``[_STORAGE]`` key)."""
    if name == "repo":
        return project_root or ""
    raw = old_cfg.get(name)
    if isinstance(raw, str) and raw:
        return _legacy_interpolate(raw, project_root, old_cfg, env)
    return _legacy_user_root(name, env)


def _legacy_interpolate(expr, project_root, old_cfg, env):
    """Expand ``~`` and ``$NAME`` (folder or env var) in a spec-v3 path
    expression, anchoring a relative result to *project_root*."""
    expr = os.path.expanduser(expr)
    out, i = [], 0
    while i < len(expr):
        if expr[i] == "$":
            j = i + 1
            braced = j < len(expr) and expr[j] == "{"
            if braced:
                j += 1
            k = j
            while k < len(expr) and (expr[k].isalnum() or expr[k] == "_"):
                k += 1
            var = expr[j:k]
            if braced and k < len(expr) and expr[k] == "}":
                k += 1
            if var in ("data", "cache", "repo") or var in old_cfg:
                out.append(_legacy_folder_root(var, project_root, old_cfg, env))
            elif var in env:
                out.append(env[var])
            else:
                out.append(expr[i:k])
            i = k
        else:
            out.append(expr[i])
            i += 1
    resolved = "".join(out)
    if not os.path.isabs(resolved) and project_root:
        resolved = os.path.join(project_root, resolved)
    return resolved


def _legacy_dataset_path(entry, store_sel, local_path, project_root, old_cfg, env):
    """The spec-v3 absolute on-disk path for a dataset (``""`` when unknowable —
    a ``skip_download`` entry whose URI is the path)."""
    if local_path:
        return _legacy_interpolate(local_path, project_root, old_cfg, env)
    if entry.skip_download:
        return ""
    selector = store_sel or old_cfg.get("default") or "$data"
    name = selector[1:].split("/", 1)[0] if selector.startswith("$") else "data"
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1]
    subpath = selector[1:].split("/", 1)[1] if "/" in selector[1:] else ""
    root = _legacy_folder_root(name, project_root, old_cfg, env)
    if subpath:
        root = os.path.join(root, subpath)
    # spec-v3 fetched layout: <root>/datasets/<key> (scope empty for datasets).
    return os.path.join(root, "datasets", entry.key)


def migrate_manifest(toml_path, *, env=None, dry_run=False):
    """Freeze a manifest's spec-v3 locations into the spec-v4 two-field model.

    Returns a human-readable summary of what changed (or would change). Moves no
    bytes. A sibling ``cached.toml`` has its retired ``scope`` / ``store`` keys
    stripped on rewrite.
    """
    from .cache import CACHED_INDEX_NAME, CachedIndex
    from .database import Database

    env = os.environ if env is None else env
    toml_path = os.path.abspath(toml_path)
    project_root = os.path.dirname(toml_path)

    with open(toml_path, "rb") as f:
        old_cfg = dict(tomllib.load(f).get("_STORAGE", {}))

    db = Database(datasets_toml=toml_path, persist=False)
    project_id = _legacy_project_id(project_root, env)

    # The spec-v3 effective roots, expressed against the bare v4 $user_*_dir
    # symbols so the rewritten manifest stays portable.
    datasets_dir = f"$user_data_dir/{_LEGACY_APPNAME}/datasets"
    datacache_dir = f"$user_cache_dir/{_LEGACY_APPNAME}/cached/{project_id}"
    # What the new datasets_dir resolves to — the old default datasets root
    # (``user_data_dir("datamanifest")/datasets``). A dataset whose old path
    # equals ``<this>/<key>`` is covered by datasets_dir and needs no storage_path.
    new_datasets_root = os.path.join(_legacy_user_root("data", env), "datasets")

    changes = [
        f"[_STORAGE].datasets_dir  = {datasets_dir!r}",
        f"[_STORAGE].datacache_dir = {datacache_dir!r}",
    ]

    for name, entry in db.datasets.items():
        old_store = entry.extra.pop("store", "")
        old_local = entry.extra.pop("local_path", "")
        old_abs = _legacy_dataset_path(
            entry, old_store, old_local, project_root, old_cfg, env,
        )
        if not old_abs:
            continue
        default_abs = os.path.join(new_datasets_root, entry.key)
        if os.path.abspath(old_abs) != os.path.abspath(default_abs):
            entry.storage_path = old_abs
            changes.append(f"{name}.storage_path = {old_abs!r}")

    # Rebuild [_STORAGE]: the two fields, preserving any _HOST table and
    # user-defined folder symbols; drop the retired default / _SCOPE / _PREFIX.
    new_cfg = {"datasets_dir": datasets_dir, "datacache_dir": datacache_dir}
    if isinstance(old_cfg.get("_HOST"), dict):
        new_cfg["_HOST"] = old_cfg["_HOST"]
    for k, v in old_cfg.items():
        if k in ("datasets_dir", "datacache_dir", "default",
                 "_HOST", "_SCOPE", "_PREFIX", "_PROFILE", "data", "cache"):
            continue
        new_cfg.setdefault(k, v)
    db.extra["_STORAGE"] = new_cfg

    cached_toml = os.path.join(project_root, CACHED_INDEX_NAME)
    cached_note = ""
    if os.path.isfile(cached_toml):
        cached_note = f"stripped scope/store from {CACHED_INDEX_NAME}"
        changes.append(cached_note)

    if not dry_run:
        db.write(toml_path)
        if os.path.isfile(cached_toml):
            CachedIndex.read(cached_toml).write(cached_toml)

    verb = "Would update" if dry_run else "Updated"
    return f"{verb} {toml_path}:\n  " + "\n  ".join(changes)
