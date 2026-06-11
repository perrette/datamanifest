"""Storage-path resolution for the spec-v5 storage model.

Storage reduces to **two folder fields** in ``[_STORAGE]``:

- ``datasets_dir``  — where fetched datasets go. Default: the machine-wide
  **shared keyed store** ``$user_data_dir/datamanifest/shared/datasets``. A
  dataset key (``host/path[#version]``) is globally unique, so one shared store
  dedups across projects; it coincides with a default read pool, so the pool
  self-populates.
- ``datacache_dir`` — where the produced cache goes. Default: the per-project
  cache ``$user_cache_dir/datamanifest/projects/$project/cached``
  (``cachetype/hash`` is *not* globally unique, hence the ``$project`` segment).

A fetched dataset lands at ``<datasets_dir>/<key>``; a produced artifact at
``<datacache_dir>/<cachetype>/[<version>/]<hash>/``. No scope, no content prefix,
no derived name in between — the folder you set **is** the location.

A folder path may interpolate ``$``-symbols (``$NAME`` / ``${NAME}``):

- **Predefined** — ``$user_data_dir`` / ``$user_cache_dir`` (straight from
  ``platformdirs``, **bare**: no app name), ``$repo`` (the project root) and
  ``$project`` (the project name; default: the basename of the project root,
  overridable like any field).
- **The two fields** are themselves referenceable: ``$datasets_dir`` /
  ``$datacache_dir``.
- **``$key``** — the dataset's storage key (only meaningful in a ``storage_path``).
- **User-defined** — any other bare ``[_STORAGE]`` key.
- Otherwise the environment variable ``NAME``, else left verbatim. ``~`` → home.

Scoped configuration (git-config style): besides the committed manifest's
``[_STORAGE]``, two ``[_STORAGE]``-shaped TOML files configure storage without
touching the manifest — folder directives are per-machine, so they get
per-machine homes:

- ``<repo>/.datamanifest/config.toml`` — per-checkout, git-ignored (personal).
- ``$XDG_CONFIG_HOME/datamanifest/config.toml`` — user-global.

Both may carry ``_HOST`` sections (home dirs / checkouts often live on
filesystems shared across cluster nodes). The full resolution ladder for a
symbol/field *NAME* (first match wins; more specific scope wins):

1. ``DATAMANIFEST_<NAME>`` environment variable.
2. ``.datamanifest/config.toml``         (checkout: ``_HOST`` glob, then base).
3. manifest ``[_STORAGE._HOST.<glob>]``  (committed, shared infrastructure).
4. manifest ``[_STORAGE]`` base          (committed project intent).
5. ``~/.config/datamanifest/config.toml`` (user: ``_HOST`` glob, then base).
6. the predefined default (``$user_*_dir`` / ``$repo`` / ``$project``) or the
   field default.

The ``storage_config`` argument the resolvers take is either a plain dict (one
layer — the manifest's ``[_STORAGE]``, the historical form) or a
:class:`ScopedConfig` carrying all three layers (what :class:`Database` builds
via :func:`load_scoped_config`).

A relative resolved path is taken relative to the project root.
"""

import contextlib
import fnmatch
import os
import re
import socket
import subprocess

import platformdirs

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

__all__ = [
    "resolve_symbol",
    "interpolate",
    "resolve_path",
    "datasets_dir",
    "datacache_dir",
    "datasets_pools",
    "datacache_pools",
    "resolve_pool_exprs",
    "dataset_path",
    "is_local_path",
    "is_user_managed",
    "FIELD_DEFAULTS",
    "POOL_DEFAULTS",
    "PREDEFINED_SYMBOLS",
    "tmp_path",
    "lock_path",
    "marker_path",
    "ScopedConfig",
    "load_scoped_config",
    "override_fields",
    "config_value",
    "config_scalar",
    "read_config_file",
    "local_config_path",
    "user_config_path",
    "ensure_ignored_dir",
    "PRIVATE_DIR_NAME",
]

# The two storage fields and their built-in (machine-global) defaults: a shared
# keyed dataset store, and a per-project produced cache (see module docstring).
FIELD_DEFAULTS = {
    "datasets_dir": "$user_data_dir/datamanifest/shared/datasets",
    "datacache_dir": "$user_cache_dir/datamanifest/projects/$project/cached",
}

# Predefined symbols resolved from the platform / project root.
PREDEFINED_SYMBOLS = ("user_data_dir", "user_cache_dir", "repo", "project")

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
    """The built-in resolution of a predefined symbol (the last ladder rung)."""
    if name == "repo":
        return project_root or ""
    if name == "project":
        # The project name namespacing the per-project cache: the basename of
        # the project root (the checkout's folder name; the cwd when rootless).
        # Overridable on the ordinary ladder (a committed ``project = "..."``
        # is shared intent). Renames are safe: the state file keeps finding old
        # artifacts at their recorded locations.
        return os.path.basename(os.path.abspath(project_root or os.getcwd()))
    with _patched_environ(env):
        if name == "user_cache_dir":
            return platformdirs.user_cache_dir()
        return platformdirs.user_data_dir()


# ----- scoped configuration (git-config style layers) -------------------------

# The per-checkout private directory (git-ignored as a whole): holds the local
# config file and the state file.
PRIVATE_DIR_NAME = ".datamanifest"
_LOCAL_CONFIG_SUBPATH = os.path.join(PRIVATE_DIR_NAME, "config.toml")


class ScopedConfig:
    """The three storage-config layers, most specific first: per-checkout
    (``.datamanifest/config.toml``), the manifest's committed ``[_STORAGE]``,
    and user-global (``~/.config/datamanifest/config.toml``).

    Each layer is an ``[_STORAGE]``-shaped dict (folder fields, ``$symbols``,
    ``*_pools``, ``project``, ``default_remote``, plus ``_HOST`` glob tables).
    The resolvers walk the layers in order — within a layer the ``_HOST`` glob
    wins over the base value.
    """

    __slots__ = ("local", "manifest", "user", "env", "host")

    def __init__(self, local=None, manifest=None, user=None, env=None, host=None):
        self.local = dict(local) if local else {}
        self.manifest = dict(manifest) if manifest else {}
        self.user = dict(user) if user else {}
        # A FROZEN config also captures the environment and host it is resolved
        # against (set by ``load_scoped_config(freeze=True)`` at Database
        # materialization); ``None`` => the resolvers use their live defaults.
        self.env = dict(env) if env is not None else None
        self.host = host

    @property
    def layers(self):
        return (self.local, self.manifest, self.user)

    def __eq__(self, other):
        if not isinstance(other, ScopedConfig):
            return NotImplemented
        return self.layers == other.layers

    def __repr__(self):
        return (f"ScopedConfig(local={self.local!r}, manifest={self.manifest!r}, "
                f"user={self.user!r})")


def _frozen_env_host(storage_config, env, host):
    """A frozen :class:`ScopedConfig` is **authoritative**: its captured
    env/host replace the resolver inputs, so every ladder lookup against it —
    environment rung included — is deterministic for its lifetime. Resolving in
    another context (e.g. a remote machine — :func:`sync.remote_root`) means
    building that context's own frozen config, not overriding pieces of this
    one."""
    if isinstance(storage_config, ScopedConfig) and storage_config.env is not None:
        return storage_config.env, (storage_config.host or socket.gethostname())
    return env, (host if host is not None else socket.gethostname())


def local_config_path(project_root):
    """The per-checkout config file path (``<root>/.datamanifest/config.toml``),
    or ``""`` when there is no project root."""
    if not project_root:
        return ""
    return os.path.join(project_root, _LOCAL_CONFIG_SUBPATH)


def user_config_path(env=os.environ):
    """The user-global config file path
    (``$XDG_CONFIG_HOME/datamanifest/config.toml``, default ``~/.config/…``)."""
    xdg = env.get("XDG_CONFIG_HOME", "")
    if not xdg:
        with _patched_environ(env):
            xdg = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "datamanifest", "config.toml")


def read_config_file(path):
    """Read a scoped config file — an ``[_STORAGE]``-shaped table at the TOML
    top level. Returns ``{}`` when absent or unreadable (a broken config file
    must not block path resolution)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:  # noqa: BLE001 - malformed config contributes nothing
        return {}


def _main_checkout_dir(d):
    """The directory in the **main checkout** corresponding to *d* when *d* lives
    inside a linked ``git worktree``; ``""`` when *d* is the main checkout itself,
    is not in a git repository, the main repository is bare, the mapped directory
    does not exist, or ``git`` is unavailable (spec-v5.1). Resolved by asking the
    ``git`` executable — the on-disk worktree layout is git internal — so any
    failure simply disables the fallback."""
    d = os.path.abspath(d)
    if not os.path.isdir(d):
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", d, "rev-parse",
             "--git-dir", "--git-common-dir", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = out.strip().splitlines()
    if len(lines) != 3:
        return ""
    # Relative outputs are relative to *d* (the ``git -C`` working directory).
    gitdir, commondir, toplevel = (
        os.path.normpath(p if os.path.isabs(p) else os.path.join(d, p))
        for p in lines
    )
    if gitdir == commondir:  # main checkout (not a linked worktree)
        return ""
    if os.path.basename(commondir) != ".git":  # bare main repository
        return ""
    mapped = os.path.normpath(
        os.path.join(os.path.dirname(commondir), os.path.relpath(d, toplevel)))
    return mapped if os.path.isdir(mapped) else ""


def _locate_local_config(project_root):
    """The checkout-config file to read for *project_root*: the project's own
    ``.datamanifest/config.toml`` when present; in a linked ``git worktree``
    without one, the corresponding file in the **main checkout** (a worktree
    starts without the git-ignored ``.datamanifest/`` directory, so worktrees
    share the per-checkout scope — the same rationale as the spec-v5.1
    state-file fallback). A config file present in the worktree itself always
    wins."""
    path = local_config_path(project_root)
    if not path or os.path.isfile(path):
        return path
    main = _main_checkout_dir(os.path.abspath(project_root))
    if not main:
        return path
    mainpath = local_config_path(main)
    return mainpath if os.path.isfile(mainpath) else path


def load_scoped_config(project_root="", manifest_config=None, env=os.environ,
                       freeze=False):
    """Build the full :class:`ScopedConfig` ladder for a project: the checkout's
    ``.datamanifest/config.toml`` (in a linked git worktree without one, the main
    checkout's — see :func:`_locate_local_config`), the manifest's ``[_STORAGE]``
    table (*manifest_config*), and the user-global config file.

    With *freeze* the returned config is a **snapshot**: it also captures *env*
    and the host, which every resolver then uses in place of its live defaults —
    the whole ladder, environment rung included, is evaluated against load-time
    state (Database materialization)."""
    return ScopedConfig(
        local=read_config_file(_locate_local_config(project_root)),
        manifest=manifest_config,
        user=read_config_file(user_config_path(env)),
        env=env if freeze else None,
        host=socket.gethostname() if freeze else None,
    )


def ensure_ignored_dir(dirpath):
    """Create *dirpath* (the ``.datamanifest/`` private dir) with a self-ignoring
    ``.gitignore`` (``*``), so the whole directory stays out of git without
    editing the project's own ``.gitignore``."""
    os.makedirs(dirpath, exist_ok=True)
    gi = os.path.join(dirpath, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")


def config_value(name, *, storage_config=None, env=os.environ, host=None):
    """The raw (un-interpolated) ladder value of config field *name* — env var,
    then per layer ``_HOST`` glob / base — or ``""`` when nowhere defined.

    For non-path fields like ``default_remote`` (a push/pull target operand),
    where interpolation / anchoring would be wrong."""
    env, host = _frozen_env_host(storage_config, env, host)
    raw = _symbol_raw(name, storage_config or {}, env, host)
    return raw if isinstance(raw, str) else ""


def config_scalar(name, *, storage_config=None, env=os.environ, host=None):
    """The raw ladder value of config field *name* accepting any TOML scalar
    (str / int / float / bool) — ``DATAMANIFEST_<NAME>`` env var (a string)
    first, then per layer ``_HOST`` glob / base — or ``None`` when nowhere
    defined.

    The scalar sibling of :func:`config_value`, whose string-only contract fits
    path expressions but would drop a TOML number (e.g. ``lock_stale_age = 30``).
    """
    env, host = _frozen_env_host(storage_config, env, host)
    raw = env.get(f"DATAMANIFEST_{name.upper()}")
    if raw is not None:
        return raw
    scalar = (str, int, float, bool)
    for layer in _layers(storage_config):
        for pattern, mapping in layer.get("_HOST", {}).items():
            if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                    and isinstance(mapping.get(name), scalar):
                return mapping[name]
        if isinstance(layer.get(name), scalar):
            return layer[name]
    return None


def override_fields(storage_config, **fields):
    """A copy of *storage_config* with *fields* forced in its most-specific
    layer (for per-call overrides like an explicit ``datasets_folder``)."""
    if isinstance(storage_config, ScopedConfig):
        return ScopedConfig(local={**storage_config.local, **fields},
                            manifest=storage_config.manifest,
                            user=storage_config.user)
    return {**(storage_config or {}), **fields}


def _layers(storage_config):
    """The config layers to walk, most specific first — a plain dict is the
    single (manifest) layer; ``None`` is no config at all."""
    if storage_config is None:
        return ({},)
    if isinstance(storage_config, ScopedConfig):
        return storage_config.layers
    return (storage_config,)


def _symbol_raw(name, storage_config, env, host):
    """Walk the ladder for *name* (env, then per layer: ``_HOST`` glob, base),
    returning the first raw (un-expanded) value, or ``None`` if no rung defines
    it."""
    # 1. DATAMANIFEST_<NAME> environment override.
    raw = env.get(f"DATAMANIFEST_{name.upper()}")
    if raw is not None:
        return raw

    for layer in _layers(storage_config):
        # First matching host glob within this layer, then its base value.
        for pattern, mapping in layer.get("_HOST", {}).items():
            if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                    and isinstance(mapping.get(name), str):
                return mapping[name]
        if isinstance(layer.get(name), str):
            return layer[name]

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
    env, host = _frozen_env_host(storage_config, env, host)

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
                f"override, a config file (datamanifest config set), or "
                f"DATAMANIFEST_{name.upper()}"
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
    env, host = _frozen_env_host(storage_config, env, host)
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
# reused instead of re-downloaded): the repo-local layout (pre-existing data in
# unconfigured projects keeps being found, never re-downloaded), the shared
# store (the default ``datasets_dir``, so the pool self-populates), and the
# legacy locations. An explicit (possibly empty) ``datasets_pools`` replaces
# these.
POOL_DEFAULTS = (
    "$repo/datasets",
    "$user_data_dir/datamanifest/shared/datasets",
    "$user_data_dir/datamanifest/datasets",
    "~/.cache/Datasets",
)


def _pools_raw(field, storage_config, env, host):
    """The raw value of a ``*_pools`` *field* (a list of path expressions, or
    ``None`` when undefined) via the env > per-layer ``_HOST`` glob > base
    ladder. ``DATAMANIFEST_<FIELD>`` is ``os.pathsep``-separated."""
    raw = env.get(f"DATAMANIFEST_{field.upper()}")
    if raw is not None:
        return [p for p in raw.split(os.pathsep) if p]
    for layer in _layers(storage_config):
        for pattern, mapping in layer.get("_HOST", {}).items():
            if isinstance(mapping, dict) and fnmatch.fnmatch(host, pattern) \
                    and field in mapping:
                v = mapping[field]
                return list(v) if isinstance(v, (list, tuple)) else [v]
        if field in layer:
            v = layer[field]
            return list(v) if isinstance(v, (list, tuple)) else [v]
    return None


def resolve_pool_exprs(exprs, *, project_root="", storage_config=None,
                       env=os.environ, host=None):
    """Resolve an explicit list of pool path *exprs* to deduplicated absolute
    directories (each interpolated: ``$``-symbols / ``~`` / env). Used both for
    configured pools and for an explicit per-invocation override."""
    if storage_config is None:
        storage_config = {}
    out = []
    for expr in exprs:
        # A $repo-rooted pool is meaningless without a project root (it would
        # resolve to a bogus filesystem-root path) — skip it.
        if not project_root and ("$repo" in expr or "${repo}" in expr):
            continue
        try:
            p = resolve_path(expr, project_root=project_root,
                             storage_config=storage_config, env=env, host=host)
        except Exception:  # noqa: BLE001 - a malformed pool entry is skipped
            continue
        ap = os.path.abspath(p)
        if ap not in out:
            out.append(ap)
    return out


def _resolve_pools(field, defaults, *, project_root, storage_config, env, host):
    """Resolve a ``*_pools`` *field* to a list of absolute directories: the
    configured value (host-composable via ``_HOST`` / ``DATAMANIFEST_<FIELD>``),
    or *defaults* when undefined; an explicit empty list disables them. Each entry
    is a path expression."""
    if storage_config is None:
        storage_config = {}
    env, host = _frozen_env_host(storage_config, env, host)
    raw = _pools_raw(field, storage_config, env, host)
    exprs = list(defaults) if raw is None else raw
    return resolve_pool_exprs(
        exprs, project_root=project_root, storage_config=storage_config,
        env=env, host=host,
    )


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
