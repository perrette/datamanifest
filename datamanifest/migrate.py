"""spec-v3 → spec-v4 storage migration — reshape ``[_STORAGE]`` to the two-field
model and write its defaults.

spec-v4 replaces the spec-v3 scope/prefix/store machinery with two folder fields,
``[_STORAGE].datasets_dir`` (default ``"datasets"``) and ``datacache_dir``
(default ``"cached"``), repo-local by default. :func:`migrate_manifest`:

- writes those two fields with their **defaults** into ``[_STORAGE]`` (so the
  result is valid spec-v4 and the user has a visible knob to edit), preserving a
  ``_HOST`` table and any user-defined folder symbols;
- drops the retired ``scope`` / ``store`` / ``default`` / ``_SCOPE`` / ``_PREFIX``
  / ``_PROFILE`` rungs;
- carries each dataset's explicit ``local_path`` over to the spec-v4
  ``storage_path`` (lossless), and drops the retired per-dataset ``store``;
- strips the retired ``scope`` / ``store`` from a sibling state file.

It **moves no bytes** and does not try to *freeze* the old platformdirs-derived
locations: the v4 defaults are repo-local, so if data currently lives elsewhere
the user edits ``datasets_dir`` / ``datacache_dir`` (or a per-dataset
``storage_path``) — the returned summary says so and points at the docs. Any
dataset that used a ``store`` selector is surfaced for manual attention rather
than silently translated.
"""

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from .store import locations

# Retired spec-v3 [_STORAGE] keys, dropped on migration.
_RETIRED_STORAGE_KEYS = (
    "default", "scope", "store", "_SCOPE", "_PREFIX", "_PROFILE", "data", "cache",
)


def migrate_manifest(toml_path, *, env=None, dry_run=False):
    """Upgrade a manifest to the current form: promote v0 inline-code language
    bindings to ``[_LANG.*]`` **and** reshape a spec-v3 ``[_STORAGE]`` to the
    spec-v4 two-field model.

    Language upgrade (v0 → v1): each dataset's inline ``python=`` → its
    ``[<ds>._LANG.python].fetcher`` and a flat ``julia=`` →
    ``[<ds>._LANG.julia].fetcher`` (see :func:`datamanifest.database.migrate_v0_to_v1`).
    Storage reshape: write the two folder fields with their defaults, drop the
    retired keys, and carry each dataset's explicit ``local_path`` over to
    ``storage_path``. Moves no bytes. Returns a human-readable summary (of what
    changed, or — with *dry_run* — what would change).
    """
    from .cache import CACHED_INDEX_NAME, CachedIndex
    from .database import Database, migrate_v0_to_v1

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

    # Language upgrade (v0 → v1): promote inline python=/julia= into [_LANG.*].
    # Scan first so the summary can report each promotion, then apply in-place.
    for name, entry in db.datasets.items():
        if entry.python and not entry.lang_python_fetcher:
            changes.append(f"{name}.python → [{name}._LANG.python].fetcher")
        julia_inline = entry.extra.get("julia")
        if isinstance(julia_inline, str) and julia_inline:
            changes.append(f"{name}.julia → [{name}._LANG.julia].fetcher")
    migrate_v0_to_v1(db)

    for name, entry in db.datasets.items():
        old_store = entry.extra.pop("store", "")
        old_local = entry.extra.pop("local_path", "")
        if old_local:
            entry.storage_path = old_local
            changes.append(f"{name}.storage_path = {old_local!r}  (was local_path)")
        elif old_store:
            needs_attention.append(f"{name}  (had store = {old_store!r})")

    # Rebuild [_STORAGE]: the two fields at their defaults, preserving a _HOST
    # table and user-defined folder symbols; drop the retired rungs.
    new_cfg = {"datasets_dir": datasets_dir, "datacache_dir": datacache_dir}
    if isinstance(old_cfg.get("_HOST"), dict):
        new_cfg["_HOST"] = old_cfg["_HOST"]
    for k, v in old_cfg.items():
        if k in ("datasets_dir", "datacache_dir", "_HOST") or k in _RETIRED_STORAGE_KEYS:
            continue
        new_cfg.setdefault(k, v)
    db.extra["_STORAGE"] = new_cfg

    cached_toml = os.path.join(project_root, CACHED_INDEX_NAME)
    if os.path.isfile(cached_toml):
        changes.append(f"stripped retired keys from {CACHED_INDEX_NAME}")

    if not dry_run:
        db.write(toml_path)
        if os.path.isfile(cached_toml):
            CachedIndex.read(cached_toml).write(cached_toml)

    verb = "Would update" if dry_run else "Updated"
    summary = [f"{verb} {toml_path}:", "  " + "\n  ".join(changes)]
    if needs_attention:
        summary.append(
            "\nNeeds manual attention — these datasets used a retired `store` "
            "selector that was dropped:\n  " + "\n  ".join(needs_attention)
        )
    summary.append(
        "\nThe spec-v4 defaults are repo-local (./datasets/, ./cached/). If your "
        "data lives elsewhere, edit [_STORAGE].datasets_dir / datacache_dir (or a "
        "dataset's storage_path) — see the Storage model section of the README "
        "for $-symbols and platform-dependent ($user_data_dir/$user_cache_dir) "
        "defaults. No bytes were moved."
    )
    return "\n".join(summary)
