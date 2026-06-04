"""The ``@cached`` produce-or-load decorator (Phase 1: no index, no GC).

``cached`` is the ergonomic surface over the produced-dataset machinery: it
wraps a *producing function* (one that returns a value) so that, keyed by the
call's keyword arguments, the result is materialized once into the ``$cache``
folder and reloaded on every subsequent call. The on-disk artifact is a
directory ``<cache>/cached/<project-id>/<cachetype>/[<version>/]<param_hash>/``
(spec-v3: the ``cached/`` content prefix + project-id scope, composed via
:func:`datamanifest.store.locations.composed_path`) holding the serialized value
(``<basename>.<ext>``) plus the self-describing ``config.toml`` /
``metadata.toml`` sidecars.

Layering: this module imports **only** the Layer 0 substrate
(:mod:`datamanifest.store`) plus stdlib — never the fetch layer (the manifest /
download modules). The materialize primitive and the loader ladder are consumed
from ``store``; the artifact directory is composed via
``store.locations.composed_path(kind="cached")`` (bare ``$cache`` root + the
``cached/`` prefix + project-id scope). The produce path does **not** route
through the fetch download path.
"""

import functools
import importlib
import json
import os

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

__all__ = ["cached"]

DATA_NAME_STEM = "data"

# The default folder a produced artifact materializes into (spec: store="$cache").
DEFAULT_STORE = "$cache"

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


def _produced_key(cachetype, version, hash_) -> str:
    """The per-artifact key path under the ``cached`` scope:
    ``<cachetype>/[<version>/]<hash>``.

    *version* (the optional recipe version) becomes a path segment between the
    cachetype and the parameter hash when set; it is **not** part of *hash_*.
    """
    parts = [cachetype]
    if version:
        parts.append(version)
    parts.append(hash_)
    return os.path.join(*parts)


def _artifact_dir(*, cache_dir, store, cachetype, version, hash_,
                  project_root, storage_config) -> str:
    """Resolve a produced artifact's directory (spec-v3).

    Default composition (:func:`datamanifest.store.locations.composed_path` with
    ``kind="cached"``): ``<cache>/cached/<project-id>/<cachetype>/[<version>/]<hash>``
    — the bare *store* root (default ``$cache``), the ``cached/`` content prefix,
    the project-id scope, then the per-artifact key. *project_root* /
    *storage_config* are threaded to the resolver, and ``DATAMANIFEST_CACHE_DIR``
    / ``DATAMANIFEST_DIR`` still apply through it.

    An explicit per-call *cache_dir* is used **verbatim**
    (``<cache_dir>/<cachetype>/[<version>/]<hash>``), bypassing the folder /
    prefix / scope composition entirely.
    """
    key_path = _produced_key(cachetype, version, hash_)
    if cache_dir:
        return os.path.join(cache_dir, key_path)
    return locations.composed_path(
        store or DEFAULT_STORE, key_path, kind="cached",
        project_root=project_root, storage_config=storage_config,
    )


def _resolve_project_root(project_root) -> str:
    """Resolve the project root used for scope / artifact-path / index placement.

    An explicit *project_root* is honored verbatim. Otherwise it is discovered
    the way the rest of the tool resolves a default manifest: walk up from the
    current directory for a ``datasets.toml`` / ``Datasets.toml`` /
    ``datamanifest.toml`` (or a ``pyproject.toml``-bearing root) and use that
    directory (:func:`datamanifest.config._find_default_toml`). This makes the
    scope resolve to the project's ``pyproject.toml`` ``[project].name`` — a
    stable, human-readable id — instead of a path hash of whatever directory the
    call happened to run from. Falls back to ``""`` when nothing is found.
    """
    if project_root:
        return project_root
    from ..config import _find_default_toml, project_root_from_paths
    return project_root_from_paths(_find_default_toml(os.getcwd()))


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


def _is_registered(
    cached_toml_path, name, *, cachetype, hash_, scope, version,
) -> bool:
    """Whether *cached_toml_path* already lists *name* with a matching identity
    (``cachetype`` / ``hash`` / ``scope`` / ``version``).

    Used on a cache hit to decide whether the registry needs self-healing: a
    hand-deleted index, a missing entry, or one pointing elsewhere returns
    ``False`` so the hit re-registers it.
    """
    if not os.path.isfile(cached_toml_path):
        return False
    try:
        entry = CachedIndex.read(cached_toml_path).entries.get(name)
    except Exception:  # noqa: BLE001 - an unreadable index roots nothing
        return False
    if not entry:
        return False
    return (
        entry.get("cachetype", "") == cachetype
        and entry.get("hash", "") == hash_
        and entry.get("scope", "") == scope
        and entry.get("version", "") == version
    )


def _register_produced(
    cached_toml_path, name, *, cachetype, hash_, ref, format, scope="",
    version="",
) -> str:
    """Register a freshly-produced artifact into its ``cached.toml`` and record
    that index in the depot usage log. Returns the index path written.

    Reads any existing index (so a register adds/updates one entry without
    dropping the rest), registers the portable key (plus the spec-v3
    *scope* and recipe *version* when set), writes canonically, and
    stamps the usage log so GC can discover this root.
    """
    index = CachedIndex.read_or_empty(cached_toml_path)
    index.register(
        name,
        cachetype=cachetype,
        hash=hash_,
        ref=ref,
        format=format or "",
        store=DEFAULT_STORE,
        scope=scope,
        version=version,
    )
    written = index.write(cached_toml_path)
    record_path(written)
    return written


def cached(
    *,
    cachetype,
    format=None,
    key=None,
    basename="",
    version="",
    store=DEFAULT_STORE,
    project_root="",
    storage_config=None,
    cached_toml="",
    name="",
):
    """Produce-or-load decorator for a function that returns a cacheable value.

    The wrapped function is **keyword-only** (the spec's produced-dataset rule):
    its keyword arguments are the hash inputs. On each call the key table is
    derived from those kwargs (minus ``_``-prefixed control keys, or via the
    *key* selector), :func:`param_hash`-ed, and composed (spec-v3) under the
    *store* folder (default ``$cache``) as
    ``<cache>/cached/<project-id>/<cachetype>/[<version>/]<hash>``. If a
    complete, hash-valid artifact already exists it is **loaded** (via *format*'s
    default loader) and returned; otherwise the function **runs**, its result is
    serialized into the artifact directory next to the ``config.toml`` /
    ``metadata.toml`` sidecars, and the freshly-computed result is returned.

    The wrapper gains two per-call escape hatches: ``cached: bool = True``
    (``cached=False`` forces a recompute regardless of an existing hit) and
    ``cache_dir: str = ""`` (an explicit directory used **verbatim** as
    ``<cache_dir>/<cachetype>/[<version>/]<hash>``, bypassing the folder /
    prefix / scope composition). Positional arguments are rejected (produced
    datasets are identified by their keyword parameters).

    Parameters
    ----------
    cachetype:
        Namespace for the produced artifact (the first path component under the
        cache root). Appears in the on-disk path and ``config.toml`` ``[_META]``.
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
    store:
        Storage selector the artifact lands under (default ``"$cache"``). An
        explicit override wins.
    project_root, storage_config:
        Threaded through to the ``$``-folder resolver / git provenance (optional;
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
    ``format`` / ``store="$cache"`` / the ``scope`` / the ``version``
    when set), that index is recorded in the depot usage log, and
    ``metadata.toml``'s ``[origin].cached_toml`` back-pointer names it.
    A cache **hit** does not re-stamp ``metadata.toml``, but it **self-heals the
    registry**: if the artifact is present yet its ``cached.toml`` entry is
    missing (the index was deleted by hand, or never written), the hit
    re-registers it, so the index rebuilds itself simply by re-running.
    """

    fmt = format if format is not None else DEFAULT_FORMAT

    def decorator(fn):
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
            root = _resolve_project_root(project_root)
            artifact_dir = _artifact_dir(
                cache_dir=cache_dir, store=store, cachetype=cachetype,
                version=version, hash_=hash_, project_root=root,
                storage_config=storage_config,
            )
            data_path = os.path.join(artifact_dir, _data_name(basename, fmt))

            # The portable registry coordinates (shared by the hit self-heal and
            # the miss registration).
            ref = f"{fn.__module__}:{fn.__qualname__}"
            reg_name = name or fn.__name__
            scope = locations.project_id(root)
            index_path = _locate_cached_toml(cached_toml, root)

            # A hit requires not just a complete, hash-valid artifact but the
            # expected data file for *this* format on disk. Two recipes that
            # share a cachetype and hash to the same key (e.g. both take no
            # kwargs) land in the same directory; if they use different formats
            # their data files (``data.txt`` vs ``data.pickle``) coexist, but a
            # stale-format mismatch must recompute rather than fail to read.
            if (
                cached
                and materialize.is_complete(artifact_dir)
                and config_is_valid(artifact_dir)
                and os.path.exists(data_path)
            ):
                # Self-heal the registry: if the artifact is on disk but its
                # cached.toml entry is gone (index deleted by hand, or never
                # written), re-register it so the index rebuilds itself just by
                # re-running. A read-only index must never break a hit.
                if not _is_registered(
                    index_path, reg_name, cachetype=cachetype, hash_=hash_,
                    scope=scope, version=version,
                ):
                    try:
                        _register_produced(
                            index_path, reg_name, cachetype=cachetype,
                            hash_=hash_, ref=ref, format=fmt, scope=scope,
                            version=version,
                        )
                    except OSError:
                        pass
                return _load_value(data_path, fmt)

            result = fn(**kwargs)
            write_value = _default_writer(fmt)
            data_filename = _data_name(basename, fmt)

            # Register the produced artifact in the project's cached.toml (the
            # liveness root for GC) and record that index in the depot usage log.
            written_index = _register_produced(
                index_path, reg_name,
                cachetype=cachetype, hash_=hash_, ref=ref, format=fmt,
                scope=scope, version=version,
            )

            def write_fn(tmp: str) -> None:
                os.makedirs(tmp, exist_ok=True)
                write_value(os.path.join(tmp, data_filename), result)
                write_config(tmp, cachetype, hash_, key_table, version=version)
                # [origin].cached_toml back-pointer (audit only) → the index.
                write_metadata(
                    tmp, project_root=root,
                    origin={"cached_toml": written_index},
                )

            materialize.materialize(artifact_dir, write_fn)
            return result

        return wrapper

    return decorator
