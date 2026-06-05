"""The ``@cached`` produce-or-load decorator (Phase 1: no index, no GC).

``cached`` is the ergonomic surface over the produced-dataset machinery: it
wraps a *producing function* (one that returns a value) so that, keyed by the
call's keyword arguments, the result is materialized once into the
``datacache_dir`` folder and reloaded on every subsequent call. The on-disk
artifact is a directory ``<datacache_dir>/<cachetype>/[<version>/]<param_hash>/``
(spec-v4: the manifest's ``datacache_dir``, default ``cached`` ⇒ repo-local
``./cached/`` — no scope, no prefix) holding the serialized value
(``<basename>.<ext>``) plus the self-describing ``config.toml`` /
``metadata.toml`` sidecars.

Layering: this module imports **only** the Layer 0 substrate
(:mod:`datamanifest.store`) plus stdlib — never the fetch layer (the manifest /
download modules). The materialize primitive and the loader ladder are consumed
from ``store``; the artifact directory is resolved via
:func:`datamanifest.store.locations.datacache_dir`. The produce path does
**not** route through the fetch download path.
"""

import functools
import importlib
import json
import os
import sys
from typing import NamedTuple

try:
    import tomllib  # noqa: F401  (kept for symmetry; tomli_w used for writing)
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # noqa: F401
import tomli_w

from ..store import loaders, locations, materialize
from ._hash import key_table_from_kwargs, param_hash
from ._index import CACHED_INDEX_NAME, CachedIndex
from ._sidecars import config_is_valid, write_config, write_metadata
from ._usage import record_path

__all__ = ["cached", "Recipe", "registered_recipes", "CacheTypeConflict"]


class Recipe(NamedTuple):
    """Decoration-time metadata for one ``@cached`` function.

    Captures what is known the moment the decorator runs — the recipe, not an
    instance. The parameter ``hash`` and the ``scope`` are deliberately absent:
    they depend on the call's kwargs and the caller's working directory, so they
    only exist once the function is actually called.
    """

    ref: str          # "module:qualname"
    name: str         # registry name in cached.toml (defaults to __name__)
    cachetype: str
    format: str
    version: str


# Process-local registry of decorated module-level ``@cached`` recipes, keyed by
# ``ref`` so functions from different submodules never blur together (and a
# re-import overwrites rather than duplicates). Populated at decoration time with
# **no disk writes**: importing a module records its recipes here only. A CLI
# that does not import user code won't see them, and two projects run as separate
# processes keep separate registries — so this never entangles caches.
#
# ``_CACHETYPE_OWNERS`` maps each live ``(cachetype, version)`` to the ``ref`` that
# claimed it, for load-time conflict detection (see :func:`_register_recipe`).
# Nested/local functions (``<locals>`` in the qualname) are transient and dynamic,
# so they are exempt from both the registry and the conflict check.
_RECIPES = {}
_CACHETYPE_OWNERS = {}


def registered_recipes():
    """Return the :class:`Recipe` for every module-level ``@cached`` function
    decorated in this process (introspection only — never written to disk)."""
    return list(_RECIPES.values())


def _is_local(qualname: str) -> bool:
    """Whether *qualname* names a nested/locally-defined function (a closure or a
    function defined inside another) — exempt from the registry / conflict check."""
    return "<locals>" in qualname


class CacheTypeConflict(ValueError):
    """Two distinct ``@cached`` functions claim the same ``(cachetype, version)``
    while simultaneously live in one process."""


def _resolve_cachetype(fn, explicit):
    """Resolve a function's ``cachetype``: *explicit* when given, else its
    fully-qualified importable name (``module.qualname``).

    A function defined in ``__main__`` has no ``__module__`` identity, but the
    launch may still carry one: ``python -m pkg.mod`` records ``pkg.mod`` on
    ``__main__.__spec__.name`` (so it resolves like ``import pkg.mod`` and shares
    the cache), whereas a loose script (``python path/mod.py``), ``python -c``,
    the REPL and notebooks leave ``__spec__`` ``None``. In that latter,
    identity-less case we refuse to guess and require an explicit *cachetype*.
    """
    if explicit:
        if "@" in explicit:
            raise ValueError(
                f"cachetype {explicit!r} may not contain '@' (reserved as the "
                "cached.toml version separator)"
            )
        return explicit
    module = fn.__module__
    if module == "__main__":
        spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        name = getattr(spec, "name", None)
        if not name:
            raise ValueError(
                f"@cached function {fn.__qualname__!r} is defined in __main__ "
                "with no importable module identity (a loose script, python -c, "
                "the REPL, or a notebook), so its cachetype cannot be derived. "
                "Pass an explicit cachetype=, or move it into an importable "
                "module (recommended) — running via 'python -m pkg.module' also "
                "resolves it."
            )
        module = name
    return f"{module}.{fn.__qualname__}"


def _register_recipe(recipe):
    """Record *recipe* at decoration time and enforce cachetype uniqueness.

    Conflict rule (load-time, in-process): if a **different** function (distinct
    ``ref``) already holds this ``(cachetype, version)``, raise
    :class:`CacheTypeConflict`. Re-decorating the same ``ref`` overwrites. The
    same ``cachetype`` with a different ``version`` is allowed (e.g. v1/v2 of one
    recipe). Local/nested functions are skipped. ``scope`` never participates —
    a cachetype must be unique regardless of which project owns the copy.
    """
    if _is_local(recipe.ref.split(":", 1)[-1]):
        return
    key = (recipe.cachetype, recipe.version)
    owner = _CACHETYPE_OWNERS.get(key)
    if owner is not None and owner != recipe.ref:
        ver = f" version={recipe.version!r}" if recipe.version else ""
        raise CacheTypeConflict(
            f"cachetype {recipe.cachetype!r}{ver} is already used by {owner!r}; "
            f"{recipe.ref!r} cannot also claim it. Give one an explicit, distinct "
            "cachetype= (or version=)."
        )
    _CACHETYPE_OWNERS[key] = recipe.ref
    _RECIPES[recipe.ref] = recipe


DATA_NAME_STEM = "data"


# The default serialization format when none is given. Pickle is Python's
# general-purpose self-saver: any picklable return value round-trips without the
# caller having to pick a format. Explicit formats (txt, json, nc, …) win.
DEFAULT_FORMAT = "pickle"


def _data_name(basename: str, format: str) -> str:
    """The filename of the serialized value inside an artifact directory.

    ``<basename>.<ext>`` (e.g. ``data.txt``, ``data.nc``); plain ``<basename>``
    when no format is given. Mirrors the format string so the matching default
    loader / writer round-trips. *basename* defaults to ``data``.
    """
    stem = basename or DATA_NAME_STEM
    fmt = (format or "").strip().lstrip(".")
    return f"{stem}.{fmt}" if fmt else stem


def _default_writer(format: str):
    """Return a ``(path, value) -> None`` writer that is the inverse of
    :func:`datamanifest.store.loaders.default_loader` for *format*.

    A produced value is serialized with the writer here and read back with the
    matching default loader, so produce-then-reload is a faithful round-trip for
    the documented formats. Optional dependencies (pandas / xarray / pyyaml) are
    imported lazily, matching the loaders.
    """
    f = (format or "").strip().lower()
    if not f:
        raise ValueError(
            "Cannot materialize a produced value without a format. Pass "
            "format=... to @cached (e.g. format='txt', 'json', 'nc')."
        )
    if f in ("md", "txt"):
        def _w(path, value):
            with open(path, "w") as fh:
                fh.write(value)
        return _w
    if f in ("pickle", "pkl"):
        def _w(path, value):
            import pickle
            with open(path, "wb") as fh:
                pickle.dump(value, fh)
        return _w
    if f == "json":
        def _w(path, value):
            with open(path, "w") as fh:
                json.dump(value, fh)
        return _w
    if f == "toml":
        def _w(path, value):
            with open(path, "wb") as fh:
                tomli_w.dump(value, fh)
        return _w
    if f in ("yaml", "yml"):
        def _w(path, value):
            yaml = importlib.import_module("yaml")
            with open(path, "w") as fh:
                yaml.safe_dump(value, fh)
        return _w
    if f == "csv":
        def _w(path, value):
            value.to_csv(path, index=False)
        return _w
    if f == "parquet":
        def _w(path, value):
            value.to_parquet(path)
        return _w
    if f in ("nc", "dimstack"):
        def _w(path, value):
            value.to_netcdf(path)
        return _w
    raise ValueError(
        f'No default writer for format "{format}". @cached can materialize '
        "txt, md, json, toml, yaml, csv, parquet, nc, pickle."
    )


def _load_value(path: str, format: str):
    """Load a produced value from *path* with the format's default loader."""
    return loaders.default_loader(format)(path)


def _key_table_for_call(key, call_kwargs: dict) -> dict:
    """Build the hash-affecting key table for a ``@cached`` call.

    By default the table is the call's keyword arguments minus ``_``-prefixed
    control keys (:func:`key_table_from_kwargs`). An optional *key* selector
    overrides this: a callable receives the cleaned kwargs and returns the table;
    a sequence of names selects that subset.
    """
    base = key_table_from_kwargs(call_kwargs)
    if key is None:
        return base
    if callable(key):
        return dict(key(base))
    return {k: base[k] for k in key if k in base}


def _recipe_storage_path(*, cache_dir, storage_path, cachetype, version,
                         project_root, storage_config) -> str:
    """The recipe's ``storage_path`` (spec-v4) — the **direct parent of the hash
    dirs**, with the version baked in (it is known per recipe), so the artifact
    dir is just ``<storage_path>/<hash>``.

    Precedence: an explicit ``storage_path=`` is used **verbatim** (the user
    owns the layout, version included) → a per-call ``cache_dir=`` gives
    ``<cache_dir>/<cachetype>[/<version>]`` (keeps the cachetype subfolder) → the
    default ``<datacache_dir>/<cachetype>[/<version>]``. Returns an absolute path.
    """
    if storage_path:
        return locations.resolve_path(
            storage_path, project_root=project_root, storage_config=storage_config,
        )
    if cache_dir:
        base = os.path.join(cache_dir, cachetype)
    else:
        base = os.path.join(
            locations.datacache_dir(
                project_root=project_root, storage_config=storage_config,
            ),
            cachetype,
        )
    return os.path.join(base, version) if version else base


def _artifact_dir(storage_path, hash_) -> str:
    """``<storage_path>/<hash>`` — the artifact directory under its recipe's
    ``storage_path`` (which already carries the version; see
    :func:`_recipe_storage_path`)."""
    return os.path.join(storage_path, hash_)


def _record_storage_path(parent, project_root) -> str:
    """Render a recipe *parent* for recording in ``cached.toml``: relative to the
    manifest dir when it lives under the project root (portable across clones),
    absolute otherwise. Mirrors a dataset's ``storage_path`` convention."""
    if project_root:
        ap, rt = os.path.abspath(parent), os.path.abspath(project_root)
        if ap == rt or ap.startswith(rt + os.sep):
            return os.path.relpath(ap, rt)
    return parent


def _discover_manifest(project_root):
    """Resolve ``(project_root, manifest_toml)`` for scope / artifact-path / index
    placement and storage configuration.

    An explicit *project_root* is honored; the manifest is then the nearest
    ``datasets.toml`` / ``Datasets.toml`` / ``datamanifest.toml`` at or above it.
    Otherwise both are discovered by walking up from the current directory
    (:func:`datamanifest.config._find_default_toml`) — the same resolution the
    rest of the tool uses, so the scope resolves to the project's
    ``pyproject.toml`` ``[project].name`` rather than a path hash. The manifest
    path is ``""`` when none is found.
    """
    from ..config import _find_default_toml, project_root_from_paths
    if project_root:
        return project_root, _find_default_toml(project_root)
    toml = _find_default_toml(os.getcwd())
    return project_root_from_paths(toml), toml


def _load_storage_config(manifest_toml) -> dict:
    """The ``[_STORAGE]`` table from *manifest_toml* (empty when absent/unreadable).

    Reading the table is a plain TOML load — **no** ``Database`` / fetch layer —
    so the same centralized storage settings that drive fetched datasets (folder
    roots, ``_HOST`` / ``_PROFILE`` per-machine overrides, ``_SCOPE`` / ``_PREFIX``)
    also drive produced artifacts: ``$cache`` and friends resolve identically for
    cache and fetch. Mirrors ``Database.storage_config`` (``dict(extra["_STORAGE"])``).
    """
    if not manifest_toml or not os.path.isfile(manifest_toml):
        return {}
    try:
        with open(manifest_toml, "rb") as f:
            data = tomllib.load(f)
    except Exception:  # noqa: BLE001 - a malformed manifest contributes no config
        return {}
    table = data.get("_STORAGE", {})
    return dict(table) if isinstance(table, dict) else {}


def _locate_cached_toml(cached_toml, project_root) -> str:
    """Resolve the ``cached.toml`` path a produced artifact is registered in.

    Precedence (the spec's "sibling of ``datasets.toml``" convention, with
    pragmatic fallbacks):

    1. an explicit *cached_toml* path (a file, or a directory holding the
       default-named index);
    2. a ``datasets.toml`` sibling — when *project_root* holds one, register
       ``<project_root>/cached.toml`` alongside it;
    3. ``<project_root>/cached.toml`` when a *project_root* is given;
    4. ``<cwd>/cached.toml``.
    """
    if cached_toml:
        if os.path.isdir(cached_toml):
            return os.path.join(cached_toml, CACHED_INDEX_NAME)
        return cached_toml
    if project_root:
        return os.path.join(project_root, CACHED_INDEX_NAME)
    return os.path.join(os.getcwd(), CACHED_INDEX_NAME)


def _register_produced(
    cached_toml_path, *, cachetype, hash_, params, ref, format, storage_path,
    version="",
) -> str:
    """Register a freshly-produced variation into its ``cached.toml`` and record
    that index in the depot usage log. Returns the index path written.

    Reads any existing index (so a register **adds** this variation without
    dropping the rest), records the parameter ``hash`` + ``params`` under the
    recipe ``(cachetype, version)`` along with the recipe's ``storage_path``
    (its parent dir), writes canonically, and stamps the usage log so GC can
    discover this root.
    """
    index = CachedIndex.read_or_empty(cached_toml_path)
    index.register(
        cachetype=cachetype,
        hash=hash_,
        params=params,
        ref=ref,
        format=format or "",
        storage_path=storage_path,
        version=version,
    )
    written = index.write(cached_toml_path)
    record_path(written)
    return written


def _heal_on_hit(
    cached_toml_path, *, cachetype, hash_, params, ref, format, storage_path,
    version,
) -> None:
    """Self-heal the registry on a cache hit (best-effort, never raises).

    If this variation is missing from ``cached.toml`` (the index was deleted by
    hand, or never written), re-register it; if the variation is present but the
    recipe's ``ref`` has drifted (the producing function was refactored), refresh
    it. Otherwise do nothing. A read-only or malformed index must never break a
    hit, so any error is swallowed.
    """
    try:
        index = CachedIndex.read_or_empty(cached_toml_path)
        present = index.has_instance(
            cachetype=cachetype, version=version, hash=hash_,
        )
        ref_current = index.ref_of(
            cachetype=cachetype, version=version,
        ) == ref
        if present and ref_current:
            return
        index.register(
            cachetype=cachetype, hash=hash_, params=params, ref=ref,
            format=format or "", storage_path=storage_path, version=version,
        )
        record_path(index.write(cached_toml_path))
    except Exception:  # noqa: BLE001 - a hit must never fail on index issues
        pass


def cached(
    _fn=None,
    *,
    cachetype=None,
    format=None,
    key=None,
    basename="",
    version="",
    storage_path="",
    project_root="",
    storage_config=None,
    cached_toml="",
    name="",
):
    """Produce-or-load decorator for a function that returns a cacheable value.

    Usable bare (``@cached``, all defaults) or configured (``@cached(...)``).

    The wrapped function is **keyword-only** (the spec's produced-dataset rule):
    its keyword arguments are the hash inputs. On each call the key table is
    derived from those kwargs (minus ``_``-prefixed control keys, or via the
    *key* selector), :func:`param_hash`-ed, and composed (spec-v3) under the
    manifest's ``datacache_dir`` folder (default ``cached`` ⇒ repo-local
    ``./cached/``) as ``<datacache_dir>/<cachetype>/[<version>/]<hash>``. If a
    complete, hash-valid artifact already exists it is **loaded** (via *format*'s
    default loader) and returned; otherwise the function **runs**, its result is
    serialized into the artifact directory next to the ``config.toml`` /
    ``metadata.toml`` sidecars, and the freshly-computed result is returned.

    The wrapper gains two per-call escape hatches: ``cached: bool = True``
    (``cached=False`` forces a recompute regardless of an existing hit) and
    ``cache_dir: str = ""`` (an explicit directory used **verbatim** as
    ``<cache_dir>/<cachetype>/[<version>/]<hash>``, bypassing folder
    resolution). Positional arguments are rejected (produced datasets are
    identified by their keyword parameters).

    Parameters
    ----------
    cachetype:
        Namespace for the produced artifact (the first path component under the
        cache scope). Appears in the on-disk path and ``config.toml`` ``[_META]``.
        **Defaults to the function's fully-qualified importable name**
        (``module.qualname``) so distinct functions never collide; an explicit
        value overrides it (a stable hand-picked name, or to group several
        functions). Auto and explicit cachetypes share one namespace, and two
        *distinct* functions claiming the same ``(cachetype, version)`` while live
        in one process raise :class:`CacheTypeConflict` at decoration. A function
        defined in ``__main__`` resolves via ``python -m pkg.mod`` (→
        ``pkg.mod.func``); a loose script / ``python -c`` / REPL / notebook has no
        importable identity, so an explicit *cachetype* is **required** there.
    format:
        Serialization format (e.g. ``"txt"``, ``"json"``, ``"nc"``) — drives the
        default writer and the matching default loader.
    key:
        Optional key-table selector (a callable ``kwargs -> table`` or a sequence
        of names) narrowing the hash-affecting parameters.
    basename:
        Filename stem of the serialized value (default ``"data"``).
    version:
        Optional recipe version (spec-v3). When set it becomes a
        ``<cachetype>/<version>/<hash>`` path segment and is recorded in
        ``config.toml`` (``[_META].version``) and the ``cached.toml`` entry, but
        is **never** part of the parameter hash — same kwargs hash to the same
        ``<hash>`` with or without a version. Bumping it isolates artifacts
        across recipe revisions (e.g. preventing stale cross-branch hits).
    storage_path:
        The recipe's location — the **direct parent of the hash dirs**, version
        already baked in; the artifact lands at ``<storage_path>/<hash>`` (no
        auto-appended version, so a new ``version=`` may point somewhere
        entirely different). Used **verbatim** (resolved: relative ⇒ manifest
        dir, absolute as-is), unlike the per-call ``cache_dir=`` which composes
        ``<cache_dir>/<cachetype>[/<version>]``. The resolved value is recorded
        in ``cached.toml`` and preferred on the next hit. Default ``""`` ⇒
        ``<datacache_dir>/<cachetype>[/<version>]``.
    project_root, storage_config:
        Threaded through to the ``$``-symbol resolver / git provenance (optional;
        no ``Database`` is required).
    cached_toml:
        Explicit path to the ``cached.toml`` index this artifact is registered
        in on produce. Defaults to ``<project_root>/cached.toml`` (the manifest
        sibling), else ``<cwd>/cached.toml`` (see :func:`_locate_cached_toml`).
    name:
        Portable registry name under which the produced artifact is listed in
        ``cached.toml``. Defaults to the wrapped function's ``__name__``.

    On a **produce** (miss), the artifact is registered into the resolved
    ``cached.toml`` (``cachetype`` / ``hash`` / ``ref`` = ``module:qualname`` /
    ``format`` / the ``version`` when set), that index is recorded in the depot
    usage log, and
    ``metadata.toml``'s ``[origin].cached_toml`` back-pointer names it.
    A cache **hit** does not re-stamp ``metadata.toml``, but it **self-heals the
    registry**: if the artifact is present yet its ``cached.toml`` entry is
    missing (the index was deleted by hand, or never written), the hit
    re-registers it, so the index rebuilds itself simply by re-running.
    """

    fmt = format if format is not None else DEFAULT_FORMAT

    def decorator(fn):
        # Resolve the cachetype once, at decoration time (raises for an
        # identity-less __main__ function), so it is stable across calls and
        # available for the recipe registry + conflict check.
        ct = _resolve_cachetype(fn, cachetype)

        @functools.wraps(fn)
        def wrapper(*args, cached=True, cache_dir="", **kwargs):
            if args:
                raise TypeError(
                    f"@cached function {fn.__name__!r} is keyword-only — a "
                    "produced dataset is identified by its keyword parameters; "
                    f"got positional arguments {args!r}."
                )

            key_table = _key_table_for_call(key, kwargs)
            hash_ = param_hash(key_table)
            root, manifest_toml = _discover_manifest(project_root)
            # Storage resolution is centralized in the manifest's [_STORAGE]: when
            # the caller passes no storage_config, load it from the discovered
            # manifest (the datacache_dir field + $-symbols / _HOST), so the
            # produced cache lands under the same datacache_dir the rest of the
            # tool reads.
            sconf = (
                storage_config if storage_config is not None
                else _load_storage_config(manifest_toml)
            )
            ref = f"{fn.__module__}:{fn.__qualname__}"
            index_path = _locate_cached_toml(cached_toml, root)
            data_filename = _data_name(basename, fmt)

            # The recipe's storage_path under the current config — the direct
            # parent of the hash dirs, version baked in (storage_path= verbatim;
            # cache_dir ⇒ <cache_dir>/<cachetype>[/<version>]; else
            # <datacache_dir>/<cachetype>[/<version>]) — and its portable form.
            derived_parent = _recipe_storage_path(
                cache_dir=cache_dir, storage_path=storage_path, cachetype=ct,
                version=version, project_root=root, storage_config=sconf,
            )
            sp_record = _record_storage_path(derived_parent, root)

            # Hit search prefers the location **recorded in cached.toml** (where
            # this recipe's artifacts were actually written — which may differ
            # from the current default if the config changed), then falls back to
            # the machine-derived parent.
            recorded_sp = ""
            if os.path.isfile(index_path):
                try:
                    recorded_sp = CachedIndex.read(index_path).storage_path_of(
                        cachetype=ct, version=version,
                    )
                except Exception:  # noqa: BLE001 - a broken index never blocks
                    recorded_sp = ""
            search_parents = []
            if recorded_sp:
                search_parents.append(locations.resolve_path(
                    recorded_sp, project_root=root, storage_config=sconf))
            if derived_parent not in search_parents:
                search_parents.append(derived_parent)

            # A hit requires not just a complete, hash-valid artifact but the
            # expected data file for *this* format on disk (two recipes sharing a
            # cachetype+hash but different formats coexist; a stale-format
            # mismatch must recompute rather than fail to read).
            if cached:
                for parent in search_parents:
                    adir = _artifact_dir(parent, hash_)
                    dpath = os.path.join(adir, data_filename)
                    if (
                        materialize.is_complete(adir)
                        and config_is_valid(adir)
                        and os.path.exists(dpath)
                    ):
                        # Self-heal: re-register a lost variation / drifted ref,
                        # recording the location it was actually found at.
                        _heal_on_hit(
                            index_path, cachetype=ct, hash_=hash_,
                            params=key_table, ref=ref, format=fmt,
                            storage_path=_record_storage_path(parent, root),
                            version=version,
                        )
                        return _load_value(dpath, fmt)

            # Miss → produce at the derived (current-config) location.
            artifact_dir = _artifact_dir(derived_parent, hash_)
            result = fn(**kwargs)
            write_value = _default_writer(fmt)

            # Register the produced variation in the project's cached.toml (the
            # liveness root for GC) and record that index in the depot usage log.
            written_index = _register_produced(
                index_path,
                cachetype=ct, hash_=hash_, params=key_table, ref=ref,
                format=fmt, storage_path=sp_record, version=version,
            )

            def write_fn(tmp: str) -> None:
                os.makedirs(tmp, exist_ok=True)
                write_value(os.path.join(tmp, data_filename), result)
                write_config(tmp, ct, hash_, key_table, version=version)
                # [origin].cached_toml back-pointer (audit only) → the index.
                write_metadata(
                    tmp, project_root=root,
                    origin={"cached_toml": written_index},
                )

            materialize.materialize(artifact_dir, write_fn)
            return result

        # Record the recipe at decoration time (no disk writes) and enforce
        # cachetype uniqueness, then expose it on the wrapper as ``.recipe``.
        recipe = Recipe(
            ref=f"{fn.__module__}:{fn.__qualname__}",
            name=name or fn.__name__,
            cachetype=ct, format=fmt, version=version,
        )
        _register_recipe(recipe)
        wrapper.recipe = recipe
        return wrapper

    # Support both bare ``@cached`` (defaults) and ``@cached(...)`` (configured).
    return decorator(_fn) if _fn is not None else decorator
