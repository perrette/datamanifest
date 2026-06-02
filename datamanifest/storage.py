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
import os
import socket

import platformdirs

__all__ = ["store_root", "tmp_path", "lock_path", "marker_path"]


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
