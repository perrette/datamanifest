"""Storage-root resolution for the spec-v1.1 portable storage model.

This module is a *pure* resolver: given a store name (``data`` / ``cache`` /
``repo`` / ...) it returns the absolute root directory under which datasets of
that store live. It performs no I/O beyond reading ``platformdirs`` defaults and
the (injectable) environment.

`platformdirs` is the **normative reference** for the default roots — every
other implementation of the spec must resolve to the identical paths:

- ``data``  = ``platformdirs.user_data_dir("datamanifest")/Datasets``
- ``cache`` = ``platformdirs.user_cache_dir("datamanifest")/Datasets``
- ``repo``  = ``<project_root>/datasets``

Per-store precedence (highest first):

1. ``DATAMANIFEST_<STORE>_DIR`` environment variable.
2. ``[_STORAGE._PROFILE.<profile>].<store>`` — only when a profile is active.
3. ``[_STORAGE._HOST.<glob>].<store>`` — first glob (``fnmatch``) matching the
   host.
4. ``[_STORAGE].<store>`` base value.
5. The ``platformdirs`` / ``project_root`` default.

The chosen value has ``~`` and ``$VAR`` expanded; a relative ``repo`` value is
resolved against ``project_root``.
"""

import contextlib
import fnmatch
import logging
import os
import re
import socket

import platformdirs

__all__ = [
    "store_root",
    "folder_root",
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


def _default_root(store, project_root, env):
    """The built-in default root for *store*, before env/config overrides."""
    if store == "repo":
        return os.path.join(project_root or "", "datasets")
    with _patched_environ(env):
        if store == "cache":
            base = platformdirs.user_cache_dir("datamanifest")
        else:  # "data" (and any other store) default under the data root
            base = platformdirs.user_data_dir("datamanifest")
    return os.path.join(base, "Datasets")


def _normalize(value, store, project_root, env):
    """Expand ``~``/``$VAR`` (honouring *env*) and resolve a relative ``repo``
    value against *project_root*."""
    with _patched_environ(env):
        expanded = os.path.expandvars(os.path.expanduser(value))
    if store == "repo" and not os.path.isabs(expanded):
        expanded = os.path.join(project_root or "", expanded)
    return expanded


def store_root(store, *, project_root="", storage_config=None, env=os.environ,
               host=None, profile=None):
    """Resolve *store* to its absolute root directory.

    Parameters
    ----------
    store:
        Store name (``data`` / ``cache`` / ``repo`` / ...). Empty ⇒ ``data``.
    project_root:
        Project root used for the ``repo`` store and relative ``repo`` values.
    storage_config:
        Parsed ``[_STORAGE]`` table (base keys plus ``_HOST`` / ``_PROFILE``
        sub-tables). ``None`` ⇒ no config.
    env:
        Environment mapping (defaults to ``os.environ``; injectable for tests).
    host:
        Hostname for ``_HOST`` glob matching (defaults to
        ``socket.gethostname()``).
    profile:
        Active profile name (defaults to ``$DATAMANIFEST_PROFILE``). Empty ⇒ no
        profile overrides applied.
    """
    store = store or "data"
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()
    if profile is None:
        profile = env.get("DATAMANIFEST_PROFILE", "")

    raw = None

    # 1. DATAMANIFEST_<STORE>_DIR environment override.
    raw = env.get(f"DATAMANIFEST_{store.upper()}_DIR")

    # 2. Active profile override.
    if raw is None and profile:
        prof = storage_config.get("_PROFILE", {}).get(profile, {})
        if isinstance(prof, dict) and isinstance(prof.get(store), str):
            raw = prof[store]

    # 3. First matching host glob.
    if raw is None:
        for pattern, mapping in storage_config.get("_HOST", {}).items():
            if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                    and isinstance(mapping.get(store), str):
                raw = mapping[store]
                break

    # 4. [_STORAGE].<store> base value.
    if raw is None and isinstance(storage_config.get(store), str):
        raw = storage_config[store]

    # 5. Built-in default.
    if raw is None:
        raw = _default_root(store, project_root, env)

    return _normalize(raw, store, project_root, env)


def _folder_raw(name, storage_config, env, host, profile):
    """Walk ladder rungs 1–4 for *name*, returning the first raw (un-expanded)
    value found, or ``None`` if no rung defines it."""
    # 1. DATAMANIFEST_<NAME>_DIR environment override.
    raw = env.get(f"DATAMANIFEST_{name.upper()}_DIR")
    if raw is not None:
        return raw

    # 2. Active profile override.
    if profile:
        prof = storage_config.get("_PROFILE", {}).get(profile, {})
        if isinstance(prof, dict) and isinstance(prof.get(name), str):
            return prof[name]

    # 3. First matching host glob.
    for pattern, mapping in storage_config.get("_HOST", {}).items():
        if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                and isinstance(mapping.get(name), str):
            return mapping[name]

    # 4. [_STORAGE].<name> base value.
    if isinstance(storage_config.get(name), str):
        return storage_config[name]

    return None


def _folder_is_defined(name, storage_config, env, host, profile):
    """Whether *name* names a resolvable folder variable (built-in, or defined
    on some ladder rung) as opposed to a plain environment variable."""
    if name in _BUILTIN_FOLDERS:
        return True
    return _folder_raw(name, storage_config, env, host, profile) is not None


def _interpolate(value, *, project_root, storage_config, env, host, profile,
                 resolving):
    """Expand a path expression: ``~`` → home, and each ``$NAME`` / ``${NAME}``
    → folder variable *NAME* (resolved via :func:`folder_root`) if one is
    defined, else environment variable *NAME*, else left verbatim."""
    with _patched_environ(env):
        value = os.path.expanduser(value)

    def repl(match):
        var = match.group(1) or match.group(2)
        if _folder_is_defined(var, storage_config, env, host, profile):
            return folder_root(
                var, project_root=project_root, storage_config=storage_config,
                env=env, host=host, profile=profile, _resolving=resolving,
            )
        if var in env:
            return env[var]
        return match.group(0)

    return _TOKEN_RE.sub(repl, value)


def folder_root(name, *, project_root="", storage_config=None, env=os.environ,
                host=None, profile=None, _resolving=()):
    """Resolve folder variable *name* to its absolute root directory.

    Like :func:`store_root` this walks the spec ladder
    ``DATAMANIFEST_<NAME>_DIR`` → ``_PROFILE`` → ``_HOST`` → ``[_STORAGE].<name>``
    → built-in default, but it understands the spec-v2 storage model:

    - **Any** folder variable resolves, not just ``data`` / ``cache`` / ``repo``.
      A user-defined name with no definition on any rung is an error (only the
      built-ins have a default).
    - Values are **path expressions**: ``$NAME`` / ``${NAME}`` interpolates
      folder variable *NAME* (recursively) when one is defined, otherwise the
      environment variable *NAME*; ``~`` expands to home.
    - A folder variable whose value references itself (directly or through a
      cycle) raises :class:`ValueError`.

    For the built-in folders this returns exactly the paths :func:`store_root`
    returns today.

    Parameters mirror :func:`store_root`. ``_resolving`` is an internal tuple of
    folder names currently being expanded, used for cycle detection.
    """
    if not name:
        raise ValueError("folder_root requires a non-empty folder name")
    if storage_config is None:
        storage_config = {}
    if host is None:
        host = socket.gethostname()
    if profile is None:
        profile = env.get("DATAMANIFEST_PROFILE", "")

    if name in _resolving:
        cycle = " -> ".join((*_resolving, name))
        raise ValueError(f"folder variable ${name} references itself ({cycle})")

    raw = _folder_raw(name, storage_config, env, host, profile)

    if raw is None:
        if name not in _BUILTIN_FOLDERS:
            raise ValueError(
                f"undefined folder variable ${name}: define it in [_STORAGE], a "
                f"_HOST/_PROFILE override, or DATAMANIFEST_{name.upper()}_DIR"
            )
        # Built-in default roots are already absolute, real paths.
        return _default_root(name, project_root, env)

    expanded = _interpolate(
        raw, project_root=project_root, storage_config=storage_config,
        env=env, host=host, profile=profile, resolving=(*_resolving, name),
    )
    if name == "repo" and not os.path.isabs(expanded):
        expanded = os.path.join(project_root or "", expanded)
    return expanded


# Storage selectors named with a bare (non-``$``) name already warned about, so
# the transition-shim deprecation notice fires only once per name per process.
_BARE_SELECTORS_WARNED = set()


def _warn_bare_selector_once(selector):
    """One-time deprecation notice that a bare ``store`` selector was resolved
    as if it were ``$<selector>``. Strict rejection lands in a later migration
    step; until then this keeps old manifests working."""
    if selector in _BARE_SELECTORS_WARNED:
        return
    _BARE_SELECTORS_WARNED.add(selector)
    _logger.warning(
        "Storage selector %r is bare (no '$'); resolving it as '$%s'. Bare "
        "selectors are deprecated under spec-v2 and will be rejected — run "
        "`datamanifest migrate` to rewrite it to '$%s'.",
        selector, selector, selector,
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
                     env=os.environ, host=None, profile=None):
    """Resolve a storage *selector* to its absolute directory path.

    A selector is ``$folder[/subpath]``: the leading ``$folder`` (or
    ``${folder}``) names a folder variable resolved via :func:`folder_root`, and
    any trailing ``/subpath`` is appended beneath the folder's root.

    As a **transition shim**, a bare (non-``$``) selector ``name`` is resolved as
    if it were ``$name`` and logs a one-time deprecation warning. Strict
    rejection of bare selectors lands in a later migration step; until then this
    keeps spec-v1.1 manifests resolving.

    Parameters mirror :func:`folder_root`.
    """
    if not selector:
        raise ValueError("resolve_selector requires a non-empty selector")

    if selector.startswith("$"):
        body = selector[1:]
    else:
        _warn_bare_selector_once(selector)
        body = selector

    name, _, subpath = body.partition("/")
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1]
    if not name:
        raise ValueError(f"malformed selector {selector!r}: empty folder name")

    root = folder_root(
        name, project_root=project_root, storage_config=storage_config,
        env=env, host=host, profile=profile,
    )
    if subpath:
        root = os.path.join(root, subpath)
    return root
