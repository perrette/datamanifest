"""Storage-root resolution for the spec-v3 portable storage model.

This module is a *pure* resolver: given a store name (``data`` / ``cache`` /
``repo`` / ...) it returns the absolute **bare** root directory. The consuming
layer composes ``<root>[/subpath]/<prefix>/[<scope>/]<key>`` on top via
:func:`composed_path` — prefix ``datasets/`` (fetch) or ``cached/`` (produce)
plus an optional scope.

`platformdirs` is the **normative reference** for the default roots — every
other implementation of the spec must resolve to the identical paths:

- ``data``  = ``platformdirs.user_data_dir("datamanifest")``
- ``cache`` = ``platformdirs.user_cache_dir("datamanifest")``
- ``repo``  = ``<project_root>``

Per-store precedence (highest first):

1. ``DATAMANIFEST_<STORE>_DIR`` environment variable.
2. ``[_STORAGE._HOST.<glob>].<store>`` — first glob (``fnmatch``) matching the
   host.
3. ``[_STORAGE].<store>`` base value.
4. The ``platformdirs`` / ``project_root`` default.

The chosen value has ``~`` and ``$VAR`` expanded; a relative ``repo`` value is
resolved against ``project_root``.

(``[_STORAGE._PROFILE.<name>]`` was a spec-v2 rung — a named, env-activated
override bundle — **shelved in spec-v3** in favour of ``_HOST`` (auto-matched by
hostname, no env var). The key stays *reserved* so spec-v2 manifests still parse
and it is not mistaken for a folder definition, but it has no effect here.)
"""

import contextlib
import fnmatch
import hashlib
import logging
import os
import re
import socket

import platformdirs

__all__ = [
    "folder_root",
    "folder_base",
    "content_prefix",
    "content_scope",
    "project_id",
    "composed_path",
    "resolve_selector",
    "project_default",
    "legacy_data_root",
    "tmp_path",
    "lock_path",
    "marker_path",
]

_logger = logging.getLogger("datamanifest")

# Built-in folder variables with a platformdirs/project-root default. Any other
# folder name must be defined on the resolution ladder or it is an error.
_BUILTIN_FOLDERS = ("data", "cache", "repo")

# Reserved ``[_STORAGE]`` sub-tables — structural keys, never folder variables.
# ``_HOST``/``_PROFILE`` are spec-v2; ``_PREFIX``/``_SCOPE`` are spec-v3 (they
# parametrize :func:`content_prefix` / :func:`content_scope`). They are TOML
# tables, so the string-valued folder ladder already skips them; this tuple
# documents the set and lets the bare-root resolver reject them explicitly.
_RESERVED_SUBTABLES = ("_HOST", "_PROFILE", "_PREFIX", "_SCOPE")

# Built-in content prefixes per kind (spec-v3): the segment inserted between a
# bare folder root and the key (``datasets/`` for fetched, ``cached/`` for
# produced artifacts).
_BUILTIN_PREFIXES = {"datasets": "datasets", "cached": "cached"}

# Path-expression token: ``$NAME`` or ``${NAME}`` (a folder variable if defined,
# else an environment variable).
_TOKEN_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def legacy_data_root(env=os.environ):
    """The pre-v1.1 default datasets folder — ``$XDG_CACHE_HOME/Datasets``
    (default ``~/.cache/Datasets``).

    This is a **read-only** back-compat probe location: spec-v1.1 moved the
    default ``data`` store to ``platformdirs.user_data_dir`` (under a
    ``datamanifest/`` namespace), orphaning datasets downloaded by older
    versions here. Read resolution probes it last so old downloads still
    resolve; new writes never land here. Returns the absolute path.
    """
    with _patched_environ(env):
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
    return os.path.join(xdg, "Datasets")


def tmp_path(target):
    """Staging path for *target* (a sibling on the same filesystem, so the
    eventual publish is an atomic rename)."""
    return target + ".tmp"


def lock_path(target):
    """Pidfile-lock path guarding concurrent materialization of *target*."""
    return target + ".lock"


def marker_path(target):
    """Completion-marker path for *target*.

    A directory target carries its marker *inside* it (``<target>/.complete``)
    so the marker travels with the published tree; a file target carries a
    sibling marker (``<target>.complete``). A not-yet-existing target is treated
    as a file.
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


def _folder_raw(name, storage_config, env, host):
    """Walk the ladder rungs for *name*, returning the first raw (un-expanded)
    value found, or ``None`` if no rung defines it."""
    # 1. DATAMANIFEST_<NAME>_DIR environment override.
    raw = env.get(f"DATAMANIFEST_{name.upper()}_DIR")
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


def _folder_is_defined(name, storage_config, env, host):
    """Whether *name* names a resolvable folder variable (built-in, or defined
    on some ladder rung) as opposed to a plain environment variable."""
    if name in _BUILTIN_FOLDERS:
        return True
    return _folder_raw(name, storage_config, env, host) is not None


def _interpolate(value, *, project_root, storage_config, env, host, resolving):
    """Expand a path expression: ``~`` → home, and each ``$NAME`` / ``${NAME}``
    → folder variable *NAME* (resolved via :func:`folder_root`) if one is
    defined, else environment variable *NAME*, else left verbatim."""
    with _patched_environ(env):
        value = os.path.expanduser(value)

    def repl(match):
        var = match.group(1) or match.group(2)
        if _folder_is_defined(var, storage_config, env, host):
            return folder_root(
                var, project_root=project_root, storage_config=storage_config,
                env=env, host=host, _resolving=resolving,
            )
        if var in env:
            return env[var]
        return match.group(0)

    return _TOKEN_RE.sub(repl, value)


def folder_root(name, *, project_root="", storage_config=None, env=os.environ,
                host=None, _resolving=()):
    """Resolve folder variable *name* to its absolute bare root directory.

    Delegates to :func:`folder_base` (spec-v3 bare-root resolver).
    ``_resolving`` is an internal tuple used for cycle detection.
    """
    return folder_base(
        name, project_root=project_root, storage_config=storage_config,
        env=env, host=host, _resolving=_resolving,
    )


def project_default(storage_config=None):
    """Return the project-wide default storage selector.

    This is ``[_STORAGE].default`` when set (a selector such as ``$cache`` or
    ``$scratch/sub``), otherwise the built-in default ``$data``. The result is a
    *selector string*, not a resolved path — pass it through
    :func:`resolve_selector` to obtain a directory.
    """
    if storage_config is None:
        storage_config = {}
    default = storage_config.get("default")
    if isinstance(default, str) and default:
        return default
    return "$data"


def resolve_selector(selector, *, project_root="", storage_config=None,
                     env=os.environ, host=None):
    """Resolve a storage *selector* to its absolute directory path.

    A selector is ``$folder[/subpath]``: the leading ``$folder`` (or
    ``${folder}``) names a folder variable resolved via :func:`folder_root`, and
    any trailing ``/subpath`` is appended beneath the folder's root.

    Parameters mirror :func:`folder_root`.
    """
    if not selector:
        raise ValueError("resolve_selector requires a non-empty selector")

    if selector.startswith("$"):
        body = selector[1:]
    else:
        raise ValueError(
            f"Storage selector {selector!r} is bare (no '$'). "
            "Bare selectors are not valid under spec-v2 — run "
            "`datamanifest migrate` to rewrite it to '$%s'." % selector
        )

    name, _, subpath = body.partition("/")
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1]
    if not name:
        raise ValueError(f"malformed selector {selector!r}: empty folder name")

    root = folder_root(
        name, project_root=project_root, storage_config=storage_config,
        env=env, host=host,
    )
    if subpath:
        root = os.path.join(root, subpath)
    return root


# ---------------------------------------------------------------------------
# spec-v3 composition primitives (bare roots + content prefix/scope)
#
# spec-v2 ``folder_root`` resolves a folder variable to a root that *already*
# carries the ``/Datasets`` content suffix. spec-v3 separates the two concerns:
# ``folder_base`` resolves the **bare** application/repo root, and the consuming
# layer composes ``<root>[/subpath]/<prefix>/[<scope>/]<key>`` on top via
# ``composed_path`` — prefix ``datasets/`` (fetch) or ``cached/`` (produce) plus
# an optional scope (empty for fetch; the project-id for produce). These are
# added additively: ``folder_root`` / ``resolve_selector`` keep their v2
# behavior until the fetch and cache layers are migrated onto ``composed_path``.
# ---------------------------------------------------------------------------


def _bare_default_root(name, project_root, env):
    """Built-in **bare** default root for *name* (no ``/Datasets`` suffix).

    ``data``/``cache`` default to ``$DATAMANIFEST_DIR`` (the unified application
    base) when set, else ``platformdirs.user_{data,cache}_dir("datamanifest")``;
    ``repo`` defaults to ``<project_root>``.
    """
    if name == "repo":
        return project_root or ""
    base = env.get("DATAMANIFEST_DIR")
    if base:
        with _patched_environ(env):
            return os.path.expandvars(os.path.expanduser(base))
    with _patched_environ(env):
        if name == "cache":
            return platformdirs.user_cache_dir("datamanifest")
        return platformdirs.user_data_dir("datamanifest")


def _interpolate_base(value, *, project_root, storage_config, env, host,
                      resolving):
    """Like :func:`_interpolate`, but ``$NAME`` folder references resolve through
    :func:`folder_base` (bare roots) rather than :func:`folder_root`."""
    with _patched_environ(env):
        value = os.path.expanduser(value)

    def repl(match):
        var = match.group(1) or match.group(2)
        if _folder_is_defined(var, storage_config, env, host):
            return folder_base(
                var, project_root=project_root, storage_config=storage_config,
                env=env, host=host, _resolving=resolving,
            )
        if var in env:
            return env[var]
        return match.group(0)

    return _TOKEN_RE.sub(repl, value)


def folder_base(name, *, project_root="", storage_config=None, env=os.environ,
                host=None, _resolving=()):
    """Resolve folder variable *name* to its **bare** root directory (spec-v3).

    Unlike :func:`folder_root` (the spec-v2 resolver, which appends the
    ``/Datasets`` content suffix to the built-in app roots), this returns the
    bare application / repo root; the consuming layer composes the
    ``datasets/`` / ``cached/`` content prefix and an optional scope on top via
    :func:`composed_path`.

    Ladder (highest precedence first):

    1. ``DATAMANIFEST_<NAME>_DIR`` environment override.
    2. ``[_STORAGE._HOST.<glob>].<name>`` — first matching host glob.
    3. ``[_STORAGE].<name>`` base value.
    4. Built-in default: ``data``/``cache`` → ``$DATAMANIFEST_DIR`` when set,
       else ``platformdirs.user_{data,cache}_dir("datamanifest")`` (**no**
       ``/Datasets``); ``repo`` → ``<project_root>``.

    There is **no** ``_PROFILE`` rung (shelved in spec-v3); ``_HOST`` and the
    base ``[_STORAGE]`` defs still apply. Values are path expressions (``~`` and
    ``$NAME`` interpolation, the latter resolved via :func:`folder_base`).
    """
    if not name:
        raise ValueError("folder_base requires a non-empty folder name")
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()

    if name in _resolving:
        cycle = " -> ".join((*_resolving, name))
        raise ValueError(f"folder variable ${name} references itself ({cycle})")

    raw = _folder_raw(name, storage_config, env, host)

    if raw is None:
        if name not in _BUILTIN_FOLDERS:
            raise ValueError(
                f"undefined folder variable ${name}: define it in [_STORAGE], a "
                f"_HOST override, or DATAMANIFEST_{name.upper()}_DIR"
            )
        return _bare_default_root(name, project_root, env)

    expanded = _interpolate_base(
        raw, project_root=project_root, storage_config=storage_config,
        env=env, host=host, resolving=(*_resolving, name),
    )
    if name == "repo" and not os.path.isabs(expanded):
        expanded = os.path.join(project_root or "", expanded)
    return expanded


def content_prefix(kind, *, storage_config=None, env=os.environ):
    """Resolve the content prefix for *kind* (``"datasets"`` / ``"cached"``).

    Ladder: ``DATAMANIFEST_PREFIX_<KIND>`` → ``[_STORAGE._PREFIX].<kind>`` →
    built-in (``datasets`` / ``cached``). An empty override yields no prefix
    segment.
    """
    if storage_config is None:
        storage_config = {}

    raw = env.get(f"DATAMANIFEST_PREFIX_{kind.upper()}")
    if raw is None:
        table = storage_config.get("_PREFIX", {})
        if isinstance(table, dict) and isinstance(table.get(kind), str):
            raw = table[kind]
    if raw is None:
        raw = _BUILTIN_PREFIXES.get(kind)
    if raw is None:
        raise ValueError(
            f"unknown content kind {kind!r} (expected 'datasets' or 'cached')"
        )
    return raw


def content_scope(kind, *, scope=None, project_root="", storage_config=None,
                  meta=None, env=os.environ):
    """Resolve the content scope segment for *kind*.

    Ladder: an explicit *scope* (highest — e.g. ``@cached(scope=...)``) →
    ``DATAMANIFEST_SCOPE_<KIND>`` → ``[_STORAGE._SCOPE].<kind>`` → built-in. The
    built-in default is empty for ``datasets`` (no scope segment) and
    :func:`project_id` for ``cached`` (project isolation). An empty string at any
    rung is a valid value meaning *no scope segment* (a global, shared store);
    ``scope=None`` means "not provided — fall through to the ladder".
    """
    if scope is not None:
        return scope
    if storage_config is None:
        storage_config = {}

    raw = env.get(f"DATAMANIFEST_SCOPE_{kind.upper()}")
    if raw is None:
        table = storage_config.get("_SCOPE", {})
        if isinstance(table, dict) and isinstance(table.get(kind), str):
            raw = table[kind]
    if raw is not None:
        return raw

    if kind == "cached":
        return project_id(project_root, meta)
    return ""


def _path_safe_segment(value):
    """Render *value* as a single path-safe path segment (no separators)."""
    segment = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return segment or "_"


def _pyproject_name(project_root):
    """``[project].name`` from ``<project_root>/pyproject.toml``, or ``None``."""
    if not project_root:
        return None
    path = os.path.join(project_root, "pyproject.toml")
    if not os.path.isfile(path):
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return None
    name = data.get("project", {}).get("name")
    return name if isinstance(name, str) and name else None


def project_id(project_root="", meta=None):
    """Resolve the project identity used as the ``cached`` scope segment.

    Precedence: ``meta["project"]`` → ``[project].name`` in
    ``<project_root>/pyproject.toml`` → a stable hash of the absolute
    project-root path. The result is a single path-safe segment.
    """
    if meta and isinstance(meta.get("project"), str) and meta["project"]:
        return _path_safe_segment(meta["project"])

    name = _pyproject_name(project_root)
    if name:
        return _path_safe_segment(name)

    abspath = os.path.abspath(project_root or "")
    return hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:16]


def composed_path(selector, key, *, kind, scope=None, project_root="",
                  storage_config=None, meta=None, env=os.environ, host=None):
    """Compose a spec-v3 content path: ``<root>[/subpath]/<prefix>/[<scope>/]<key>``.

    *selector* is ``$folder[/subpath]`` (the bare root resolved via
    :func:`folder_base`); *kind* selects the content prefix/scope
    (``"datasets"`` or ``"cached"``). For ``kind="cached"`` the default scope is
    :func:`project_id` (using *project_root* / *meta*); for ``"datasets"`` it is
    empty. Prefix and scope are each overridable / suppressible via
    :func:`content_prefix` / :func:`content_scope`; an explicit *scope* (e.g. a
    pre-resolved value) takes precedence over the scope ladder.
    """
    if not selector:
        raise ValueError("composed_path requires a non-empty selector")
    if not selector.startswith("$"):
        raise ValueError(
            f"Storage selector {selector!r} is bare (no '$'). "
            "Bare selectors are not valid under spec-v3."
        )

    body = selector[1:]
    name, _, subpath = body.partition("/")
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1]
    if not name:
        raise ValueError(f"malformed selector {selector!r}: empty folder name")

    root = folder_base(
        name, project_root=project_root, storage_config=storage_config,
        env=env, host=host,
    )

    parts = [root]
    if subpath:
        parts.append(subpath)
    prefix = content_prefix(kind, storage_config=storage_config, env=env)
    if prefix:
        parts.append(prefix)
    scope = content_scope(
        kind, scope=scope, project_root=project_root,
        storage_config=storage_config, meta=meta, env=env,
    )
    if scope:
        parts.append(scope)
    parts.append(key)
    return os.path.join(*parts)
