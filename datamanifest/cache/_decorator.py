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
from ._sidecars import config_is_valid, write_config, write_metadata

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


def cached(
    *,
    cachetype,
    format=None,
    key=None,
    basename="",
    store=DEFAULT_STORE,
    project_root="",
    storage_config=None,
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

            def write_fn(tmp: str) -> None:
                os.makedirs(tmp, exist_ok=True)
                write_value(os.path.join(tmp, data_filename), result)
                write_config(tmp, cachetype, hash_, key_table)
                write_metadata(tmp, project_root=project_root)

            materialize.materialize(artifact_dir, write_fn)
            return result

        return wrapper

    return decorator
