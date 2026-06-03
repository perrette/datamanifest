"""The ``@cached`` produce-or-load decorator (Phase 1: no index, no GC).

``cached`` is the ergonomic surface over the produced-dataset machinery: it
wraps a *producing function* (one that returns a value) so that, keyed by the
call's keyword arguments, the result is materialized once into the ``$cache``
folder and reloaded on every subsequent call. The on-disk artifact is a
directory ``<cache_root>/<cachetype>/<param_hash>/`` holding the serialized
value (``<basename>.<ext>``) plus the self-describing ``config.toml`` /
``metadata.toml`` sidecars.

Layering: this module imports **only** the Layer 0 substrate
(:mod:`datamanifest.store`) plus stdlib — never the fetch layer (the manifest /
download modules). The materialize primitive and the loader ladder are consumed
from ``store``; the cache root is resolved via the ``$``-selector API
(``store.resolve_selector("$cache", ...)``), with a ``folder_root("cache")``
fallback. The produce path does **not** route through the fetch download path.
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
        "txt, md, json, toml, yaml, csv, parquet, nc."
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


def _cache_root(store, project_root, storage_config) -> str:
    """Resolve the produced-artifact root folder.

    Resolves the *store* selector (default ``$cache``) via the spec ``$``-folder
    API (:func:`datamanifest.store.locations.resolve_selector`), falling back to
    ``folder_root("cache", ...)`` for a bare ``cache`` name. Honours an injected
    *project_root* / *storage_config* (e.g. when a caller threads a project's
    ``[_STORAGE]`` config), and the ``DATAMANIFEST_CACHE_DIR`` env override
    always applies through the resolver.
    """
    selector = store or DEFAULT_STORE
    if selector.startswith("$"):
        return locations.resolve_selector(
            selector, project_root=project_root, storage_config=storage_config
        )
    # Bare folder name (e.g. "cache") — resolve the folder root directly.
    return locations.folder_root(
        selector, project_root=project_root, storage_config=storage_config
    )


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
    cached_toml_path, name, *, cachetype, hash_, ref, format
) -> str:
    """Register a freshly-produced artifact into its ``cached.toml`` and record
    that index in the depot usage log. Returns the index path written.

    Reads any existing index (so a register adds/updates one entry without
    dropping the rest), registers the portable key, writes canonically, and
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
    *key* selector), :func:`param_hash`-ed, and the cache key
    ``<cachetype>/<hash>`` resolved against the *store* folder (default
    ``$cache``). If a complete, hash-valid artifact already exists it is
    **loaded** (via *format*'s default loader) and returned; otherwise the
    function **runs**, its result is serialized into the artifact directory next
    to the ``config.toml`` / ``metadata.toml`` sidecars, and the freshly-computed
    result is returned.

    The wrapper gains a ``cached: bool = True`` escape hatch — ``cached=False``
    forces a recompute regardless of an existing hit. Positional arguments are
    rejected (produced datasets are identified by their keyword parameters).

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
    ``format`` / ``store="$cache"``), that index is recorded in the depot usage
    log, and ``metadata.toml``'s ``[origin].cached_toml`` back-pointer names it.
    A cache **hit** re-registers nothing and re-stamps nothing.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, cached=True, **kwargs):
            if args:
                raise TypeError(
                    f"@cached function {fn.__name__!r} is keyword-only — a "
                    "produced dataset is identified by its keyword parameters; "
                    f"got positional arguments {args!r}."
                )

            key_table = _key_table_for_call(key, kwargs)
            hash_ = param_hash(key_table)
            cache_root = _cache_root(store, project_root, storage_config)
            artifact_dir = os.path.join(cache_root, cachetype, hash_)
            data_path = os.path.join(artifact_dir, _data_name(basename, format))

            if (
                cached
                and materialize.is_complete(artifact_dir)
                and config_is_valid(artifact_dir)
            ):
                return _load_value(data_path, format)

            result = fn(**kwargs)
            write_value = _default_writer(format)
            data_filename = _data_name(basename, format)

            # Register the produced artifact in the project's cached.toml (the
            # liveness root for GC) and record that index in the depot usage log.
            ref = f"{fn.__module__}:{fn.__qualname__}"
            reg_name = name or fn.__name__
            index_path = _locate_cached_toml(cached_toml, project_root)
            written_index = _register_produced(
                index_path, reg_name,
                cachetype=cachetype, hash_=hash_, ref=ref, format=format,
            )

            def write_fn(tmp: str) -> None:
                os.makedirs(tmp, exist_ok=True)
                write_value(os.path.join(tmp, data_filename), result)
                write_config(tmp, cachetype, hash_, key_table)
                # [origin].cached_toml back-pointer (audit only) → the index.
                write_metadata(
                    tmp, project_root=project_root,
                    origin={"cached_toml": written_index},
                )

            materialize.materialize(artifact_dir, write_fn)
            return result

        return wrapper

    return decorator
