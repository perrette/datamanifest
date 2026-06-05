"""Storage-path resolution for the spec-v4 storage model.

Storage reduces to **two folder fields** in ``[_STORAGE]``:

- ``datasets_dir``  (default ``"datasets"``) — where fetched datasets go.
- ``datacache_dir`` (default ``"cached"``)   — where the produced cache goes.

Both default to **relative** paths ⇒ resolved against the project root (``$repo``)
⇒ visible local ``./datasets/`` and ``./cached/``. A fetched dataset lands at
``<datasets_dir>/<key>``; a produced artifact at
``<datacache_dir>/<cachetype>/[<version>/]<hash>/``. No scope, no content prefix,
no derived name, no application name in between — the folder you set **is** the
location.

A folder path may interpolate ``$``-symbols (``$NAME`` / ``${NAME}``):

- **Predefined** — ``$user_data_dir`` / ``$user_cache_dir`` (straight from
  ``platformdirs``, **bare**: no app name) and ``$repo`` (the project root).
- **The two fields** are themselves referenceable: ``$datasets_dir`` /
  ``$datacache_dir``.
- **``$key``** — the dataset's storage key (only meaningful in a ``storage_path``).
- **User-defined** — any other bare ``[_STORAGE]`` key.
- Otherwise the environment variable ``NAME``, else left verbatim. ``~`` → home.

Resolution ladder for a symbol/field *NAME* (first match wins):

1. ``DATAMANIFEST_<NAME>`` environment variable.
2. ``[_STORAGE._HOST.<glob>].<name>`` — first host glob (``fnmatch``) matching the
   hostname.
3. ``[_STORAGE].<name>`` base value.
4. the predefined default (``$user_*_dir`` / ``$repo``) or the field default
   (``datasets_dir="datasets"``, ``datacache_dir="cached"``).

A relative resolved path is taken relative to the project root.
"""

import contextlib
import fnmatch
import os
import re
import socket

import platformdirs

__all__ = [
    "resolve_symbol",
    "interpolate",
    "resolve_path",
    "datasets_dir",
    "datacache_dir",
    "datasets_pools",
    "datacache_pools",
    "dataset_path",
    "is_local_path",
    "is_user_managed",
    "FIELD_DEFAULTS",
    "POOL_DEFAULTS",
    "PREDEFINED_SYMBOLS",
    "tmp_path",
    "lock_path",
    "marker_path",
]

# The two storage fields and their built-in (relative ⇒ repo-local) defaults.
FIELD_DEFAULTS = {"datasets_dir": "datasets", "datacache_dir": "cached"}

# Predefined symbols resolved from the platform / project root.
PREDEFINED_SYMBOLS = ("user_data_dir", "user_cache_dir", "repo")

# Path-expression token: ``$NAME`` or ``${NAME}`` (a defined symbol, the runtime
# ``$key``, or an environment variable).
_TOKEN_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def tmp_path(target):
    """Staging path for *target* (a sibling on the same filesystem / partition, so
    the eventual publish is an atomic rename)."""
    return target + ".tmp"


def lock_path(target):
    """Pidfile-lock path guarding concurrent materialization of *target*."""
    return target + ".lock"


def marker_path(target):
    """Completion-marker path for *target*.

    A directory target carries its marker *inside* it (``<target>/.complete``) so
    the marker travels with the published tree; a file target carries a sibling
    marker (``<target>.complete``). A not-yet-existing target is treated as a file.
    """
    if os.path.isdir(target):
        return os.path.join(target, ".complete")
    return target + ".complete"


@contextlib.contextmanager
def _patched_environ(env):
    """Temporarily expose *env* as ``os.environ`` so that ``platformdirs`` and
    ``os.path.expand{user,vars}`` honour an injected environment. A no-op when
    *env* is the live ``os.environ`` (the common case)."""
    if env is os.environ:
        yield
        return
    saved = os.environ.copy()
    os.environ.clear()
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _predefined_default(name, project_root, env):
    """The built-in resolution of a predefined symbol (rung 4)."""
    if name == "repo":
        return project_root or ""
    with _patched_environ(env):
        if name == "user_cache_dir":
            return platformdirs.user_cache_dir()
        return platformdirs.user_data_dir()


def _symbol_raw(name, storage_config, env, host):
    """Walk ladder rungs 1–3 for *name*, returning the first raw (un-expanded)
    value, or ``None`` if no rung defines it."""
    # 1. DATAMANIFEST_<NAME> environment override.
    raw = env.get(f"DATAMANIFEST_{name.upper()}")
    if raw is not None:
        return raw

    # 2. First matching host glob.
    for pattern, mapping in storage_config.get("_HOST", {}).items():
        if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                and isinstance(mapping.get(name), str):
            return mapping[name]

    # 3. [_STORAGE].<name> base value.
    if isinstance(storage_config.get(name), str):
        return storage_config[name]

    return None


def _is_symbol(name, storage_config, env, host):
    """Whether *name* names a resolvable storage symbol (predefined, a field, or
    defined on some ladder rung) as opposed to a plain environment variable."""
    if name in PREDEFINED_SYMBOLS or name in FIELD_DEFAULTS:
        return True
    return _symbol_raw(name, storage_config, env, host) is not None


def resolve_symbol(name, *, key="", project_root="", storage_config=None,
                   env=os.environ, host=None, _resolving=()):
    """Resolve a single storage symbol *NAME* to a path (may be relative).

    Applies the resolution ladder; a predefined symbol with no override returns
    its platform/project-root default verbatim, otherwise the raw value is
    interpolated (it may reference other symbols, ``$key``, ``~`` and env vars).
    Relative results are *not* anchored here — the caller (:func:`resolve_path`)
    applies the relative-to-project-root rule once, at the top level.
    """
    if not name:
        raise ValueError("resolve_symbol requires a non-empty symbol name")
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()

    if name in _resolving:
        cycle = " -> ".join((*_resolving, name))
        raise ValueError(f"symbol ${name} references itself ({cycle})")

    raw = _symbol_raw(name, storage_config, env, host)
    if raw is None:
        if name in FIELD_DEFAULTS:
            raw = FIELD_DEFAULTS[name]
        elif name in PREDEFINED_SYMBOLS:
            return _predefined_default(name, project_root, env)
        else:
            raise ValueError(
                f"undefined symbol ${name}: define it in [_STORAGE], a _HOST "
                f"override, or DATAMANIFEST_{name.upper()}"
            )

    return interpolate(
        raw, key=key, project_root=project_root, storage_config=storage_config,
        env=env, host=host, _resolving=(*_resolving, name),
    )


def interpolate(expr, *, key="", project_root="", storage_config=None,
                env=os.environ, host=None, _resolving=()):
    """Expand a path expression: ``~`` → home; ``$key`` → the dataset *key*; each
    ``$NAME`` / ``${NAME}`` → a defined storage symbol (resolved via
    :func:`resolve_symbol`) if one is defined, else the environment variable
    *NAME*, else left verbatim. Does **not** anchor a relative result."""
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()
    with _patched_environ(env):
        expr = os.path.expanduser(expr)

    def repl(match):
        var = match.group(1) or match.group(2)
        if var == "key":
            return key
        if _is_symbol(var, storage_config, env, host):
            return resolve_symbol(
                var, key=key, project_root=project_root,
                storage_config=storage_config, env=env, host=host,
                _resolving=_resolving,
            )
        if var in env:
            return env[var]
        return match.group(0)

    return _TOKEN_RE.sub(repl, expr)


def resolve_path(expr, *, key="", project_root="", storage_config=None,
                 env=os.environ, host=None):
    """Interpolate *expr* and anchor a relative result against the project root.

    Absolute / ``~`` / ``$symbol``-rooted results are returned as written; a
    relative result is joined to *project_root* (left as-is when there is none).
    """
    path = interpolate(
        expr, key=key, project_root=project_root, storage_config=storage_config,
        env=env, host=host,
    )
    if os.path.isabs(path):
        return path
    if project_root:
        return os.path.join(project_root, path)
    return path


def datasets_dir(*, project_root="", storage_config=None, env=os.environ,
                 host=None):
    """The resolved absolute ``datasets_dir`` (the fetched-datasets folder)."""
    return resolve_path(
        "$datasets_dir", project_root=project_root,
        storage_config=storage_config, env=env, host=host,
    )


def datacache_dir(*, project_root="", storage_config=None, env=os.environ,
                  host=None):
    """The resolved absolute ``datacache_dir`` (the produced-cache folder)."""
    return resolve_path(
        "$datacache_dir", project_root=project_root,
        storage_config=storage_config, env=env, host=host,
    )


# Built-in read pools probed when ``[_STORAGE].datasets_pools`` is **undefined** —
# well-known machine-wide locations where datasets may already live (so they are
# reused instead of re-downloaded). An explicit (possibly empty) ``datasets_pools``
# replaces these.
POOL_DEFAULTS = (
    "$user_data_dir/datamanifest/datasets",
    "~/.cache/Datasets",
)


def _pools_raw(field, storage_config, env, host):
    """The raw value of a ``*_pools`` *field* (a list of path expressions, or
    ``None`` when undefined) via the env > ``_HOST`` glob > base ladder.
    ``DATAMANIFEST_<FIELD>`` is ``os.pathsep``-separated."""
    raw = env.get(f"DATAMANIFEST_{field.upper()}")
    if raw is not None:
        return [p for p in raw.split(os.pathsep) if p]
    for pattern, mapping in storage_config.get("_HOST", {}).items():
        if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                and field in mapping:
            v = mapping[field]
            return list(v) if isinstance(v, (list, tuple)) else [v]
    if field in storage_config:
        v = storage_config[field]
        return list(v) if isinstance(v, (list, tuple)) else [v]
    return None


def _resolve_pools(field, defaults, *, project_root, storage_config, env, host):
    """Resolve a ``*_pools`` *field* to a list of absolute directories: the
    configured value (host-composable via ``_HOST`` / ``DATAMANIFEST_<FIELD>``),
    or *defaults* when undefined; an explicit empty list disables them. Each entry
    is a path expression."""
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()
    raw = _pools_raw(field, storage_config, env, host)
    exprs = list(defaults) if raw is None else raw
    out = []
    for expr in exprs:
        try:
            p = resolve_path(expr, project_root=project_root,
                             storage_config=storage_config, env=env, host=host)
        except Exception:  # noqa: BLE001 - a malformed pool entry is skipped
            continue
        ap = os.path.abspath(p)
        if ap not in out:
            out.append(ap)
    return out


def datasets_pools(*, project_root="", storage_config=None, env=os.environ,
                   host=None):
    """Resolved absolute **fetched-dataset read pools** — extra read-only
    locations probed for an already-present ``<pool>/<key>`` before downloading,
    so a dataset another project already fetched is reused in place.

    ``[_STORAGE].datasets_pools`` (host-composable via ``_HOST``, or
    ``DATAMANIFEST_DATASETS_POOLS``) gives the pools; when **undefined** the
    built-in :data:`POOL_DEFAULTS` are used; an explicit **empty** list disables
    them. Each entry is a path expression (``$``-symbols / ``~`` / env).
    """
    return _resolve_pools(
        "datasets_pools", POOL_DEFAULTS, project_root=project_root,
        storage_config=storage_config, env=env, host=host,
    )


def datacache_pools(*, project_root="", storage_config=None, env=os.environ,
                    host=None):
    """Resolved absolute **produced-artifact read pools** — extra read-only
    locations probed for an already-produced ``<pool>/<cachetype>[/<version>]/
    <hash>`` before recomputing, so a ``@cached`` result another project already
    produced is reused in place.

    ``[_STORAGE].datacache_pools`` (host-composable via ``_HOST``, or
    ``DATAMANIFEST_DATACACHE_POOLS``); **opt-in** — undefined means *no* pools
    (there is no de-facto shared compute cache, and produced artifacts carry no
    content checksum, only their ``cachetype``/``version``/``hash`` identity and
    ``config.toml`` validation). An empty list is likewise none.
    """
    return _resolve_pools(
        "datacache_pools", (), project_root=project_root,
        storage_config=storage_config, env=env, host=host,
    )


def dataset_path(storage_path, key, *, project_root="", storage_config=None,
                 env=os.environ, host=None):
    """Resolve where a dataset lives on disk.

    *storage_path* is the dataset's ``storage_path`` field (its default,
    ``"$datasets_dir/$key"``, is applied when empty). A ``$key``-bearing
    expression yields a tool-managed keyed location; an exact path without
    ``$key`` is a user-managed location used verbatim.
    """
    expr = storage_path or "$datasets_dir/$key"
    return resolve_path(
        expr, key=key, project_root=project_root, storage_config=storage_config,
        env=env, host=host,
    )


def is_user_managed(storage_path):
    """Whether a ``storage_path`` denotes a **user-managed** location — an exact
    path with no ``$key`` — which maintenance (delete / clean) must never touch.

    An empty ``storage_path`` (the default ``$datasets_dir/$key``) and any
    ``$key``-bearing expression are tool-managed/keyed and may be acted on.
    """
    return bool(storage_path) and "$key" not in storage_path \
        and "${key}" not in storage_path


def is_local_path(expr, *, key="", project_root="", storage_config=None,
                  env=os.environ, host=None):
    """Whether *expr* resolves under the project root (``$repo``) — i.e. a local,
    non-syncable location. Relative paths and ``$repo``-rooted paths are local;
    absolute paths elsewhere (``$user_data_dir`` / user folders) are not."""
    if not project_root:
        return False
    resolved = os.path.abspath(resolve_path(
        expr, key=key, project_root=project_root,
        storage_config=storage_config, env=env, host=host,
    ))
    root = os.path.abspath(project_root)
    return resolved == root or resolved.startswith(root + os.sep)
