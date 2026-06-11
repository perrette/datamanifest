"""Cross-machine sync — ``push`` / ``pull`` of a stored object over rsync+ssh.

This module is the spec-v3 ``sync`` capability (SCHEMA.md §Cross-machine sync):
a stored object — a fetched dataset or an expensive produced artifact — is
transferred between two machines instead of being re-downloaded or recomputed,
because every object has a **machine-independent address**: the same logical
address everywhere, only the physical store-folder root differs.

Layering
--------
``sync`` is a **composition root** (like the ``datamanifest list`` maintenance
path): it MAY import ``database`` / ``cache`` / ``store``. It resolves the
object's address, computes the local and remote absolute paths, probes the
remote environment, builds the rsync/ssh commands, and runs them through an
**injectable runner** so tests capture argv (or supply canned output) instead of
executing real ssh/rsync. It is deliberately *not* under ``datamanifest/cache/``
— that package's import rule forbids reaching the fetch layer.

Addressing (machine-independent)
---------------------------------
An object's address (``rel``) is its **logical identity**, independent of any
machine's folders:

- fetched   → the dataset ``key`` (addressed by ``name`` / ``alias`` / ``doi``);
- produced  → ``<cachetype>/[<version>/]<hash>`` (addressed by
  ``cachetype[/version]/hash`` — full or an unambiguous hash prefix).

The same ``rel`` re-attaches under each end's own folder::

    local_abs  = <local datasets_dir|datacache_dir>/<rel>
    remote_abs = <remote datasets_dir|datacache_dir>/<rel>

Remote root resolution (precedence)
-----------------------------------
The remote ``datasets_dir`` / ``datacache_dir`` is resolved with the **existing**
field ladder, fed two inputs in this order of authority:

1. **Best-effort remote-env probe.** Over ssh we run
   ``source ~/.bashrc >/dev/null 2>&1; env`` (via the injectable runner) and
   parse the ``DATAMANIFEST_*`` variables out of the output. ``.bashrc`` very
   often early-returns for non-interactive shells, and ssh itself may fail — so
   this is **best-effort**: an empty or failed capture is *normal* and must not
   error. When it succeeds, the captured ``DATAMANIFEST_*`` map is passed as
   ``env=`` and the existing ladder honours the remote
   ``DATAMANIFEST_DATASETS_DIR`` / ``DATAMANIFEST_DATACACHE_DIR`` rung.
2. **``[_STORAGE._HOST.<glob>]`` host overrides** — selected by passing
   ``host=<remote-host>``. This is the **deterministic cross-machine config**:
   the remote-env probe is only a convenience; the ``_HOST`` table is the
   reliable, declared mapping.

We never re-implement the ladder; we only choose its ``env`` / ``host`` inputs.

Targets (the generalized operand grammar)
-----------------------------------------
Every datamanifest transfer is **store ↔ location**: one end is always the
project store (it supplies the selection, the machine-independent ``rel`` and
the checksums); the other end — the operand — names a location:

- ``HOST:``      — the remote machine's *store* (folders resolved remotely);
- ``HOST:PATH``  — an explicit folder on an ssh host;
- ``PATH``       — a local folder, keyed layout (``push`` = raw export,
  ``pull`` = adopt-by-copy).

rsync's colon rule: a colon means remote. The historical bare-host form
(``push ID host``, no colon) is still accepted with a deprecation warning.

Contract
--------
- **Writes no manifest** (bytes only). A ``pull`` records the received object
  in the state file; a pushed object lands in the receiving store as an orphan,
  usable via read-resolution.
- **Local (``$repo``-relative) objects are out of scope for the store-resolved
  ``HOST:`` form** — re-attaching ``rel`` under a remote store needs a
  machine-global location. The refusal **lifts for explicit-path targets**
  (``PATH`` / ``HOST:PATH``), where the destination is given outright.
- **Integrity is rsync's** (a byte copy locally); **idempotent** (a no-op when
  the target already holds the object complete).
- **Symmetric** push/pull; only the transfer direction differs.
"""

import os
import shutil
import subprocess

from . import store
from .config import logger
from .database import (
    list_alternative_keys,
    search_datasets,
)

__all__ = [
    "SyncObject",
    "SyncTarget",
    "parse_target",
    "resolve_object",
    "resolve_objects",
    "sync_object_from_location",
    "remote_env",
    "remote_root",
    "transfer",
    "RemoteRepoError",
    "AmbiguousIdError",
]

# Injectable subprocess runner: tests replace this (module-level) or pass a
# ``runner=`` argument so no real ssh/rsync ever runs. The signature mirrors the
# subset of ``subprocess.run`` we use: ``runner(argv, **kwargs)`` returning a
# completed-process-like object (``.returncode`` / ``.stdout``).
_runner = subprocess.run


class RemoteRepoError(ValueError):
    """Raised when a ``$repo``-stored object is asked to sync (out of scope)."""


class AmbiguousIdError(ValueError):
    """Raised when an ``<id>`` resolves to more than one object without
    ``--batch``."""


class SyncTarget:
    """A parsed push/pull operand — the non-store end of a transfer.

    ``kind`` is one of:

    - ``"store"``       — the remote machine's store (``HOST:``); folders are
      resolved in the remote context.
    - ``"remote-path"`` — an explicit folder on an ssh host (``HOST:PATH``).
    - ``"local-path"``  — a local folder, keyed layout (``PATH``).
    - ``"git-remote"``  — a git remote naming a peer **checkout** (``NAME:``);
      ``host`` / ``path`` are the peer's ssh host and checkout path, ``name``
      the remote's name.
    """

    __slots__ = ("kind", "host", "path", "raw", "name")

    def __init__(self, *, kind, host="", path="", raw="", name=""):
        self.kind = kind
        self.host = host
        self.path = path
        self.raw = raw
        self.name = name

    def __repr__(self):
        return f"<SyncTarget {self.kind} host={self.host!r} path={self.path!r}>"


def _parse_ssh_url(url):
    """``(host, path)`` for an ssh-like git URL (``ssh://[user@]host/path`` or
    the scp-like ``[user@]host:path``), or ``None`` for any other scheme."""
    if url.startswith("ssh://"):
        rest = url[len("ssh://"):]
        hostport, _, path = rest.partition("/")
        host = hostport.partition(":")[0]       # drop an explicit port
        return (host, "/" + path) if host and path else None
    if "://" not in url and ":" in url and not url.startswith("/"):
        host, _, path = url.partition(":")
        return (host, path) if host and path else None
    return None


def _git_remote_url(name, project_root, runner=None):
    """The URL of git remote *name* in the *project_root* checkout, or ``""``."""
    run = runner or _runner
    try:
        proc = run(["git", "-C", project_root or ".", "remote", "get-url", name],
                   capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 - no git / no repo means no git remotes
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return (getattr(proc, "stdout", "") or "").strip()


def _git_remote_target(name, url, raw):
    """Build the ``git-remote`` target for remote *name* at *url*, validating
    that it is usable as a data target (ssh-like, non-bare)."""
    parsed = _parse_ssh_url(url)
    if parsed is None:
        raise ValueError(
            f"git remote {name!r} ({url}) is not usable as a data target: only "
            "an ssh-like remote pointing at a checked-out repo carries a peer "
            "checkout path. An https remote has no filesystem access — push "
            "data to an ssh host (HOST: / HOST:PATH) instead."
        )
    host, path = parsed
    if path.rstrip("/").endswith(".git"):
        raise ValueError(
            f"git remote {name!r} ({url}) looks like a bare repository — a "
            "data target must be a checked-out working tree (its "
            ".datamanifest/ and config live there)."
        )
    return SyncTarget(kind="git-remote", host=host, path=path.rstrip("/"),
                      raw=raw, name=name)


def parse_target(operand, *, project_root="", runner=None) -> "SyncTarget":
    """Parse a push/pull *operand* per the colon rule: a colon means remote
    (``NAME:`` = a git remote's checkout when NAME names one, else ``HOST:`` =
    the remote store; ``HOST:PATH`` = an explicit remote folder); no colon
    means a local folder.

    A ``NAME:`` operand matching a **git remote** in the *project_root*
    checkout resolves to that remote's checkout (git-remote names take
    precedence over ssh hosts on collision); the reserved ``git:NAME`` /
    ``ssh:HOST[:PATH]`` prefixes disambiguate explicitly. The historical
    bare-host form (``push ID host``, no colon) is still recognized and warned
    as deprecated: write ``host:`` instead.
    """
    if isinstance(operand, SyncTarget):
        return operand
    # Explicit disambiguators (reserved prefixes).
    if operand.startswith("git:"):
        name = operand[len("git:"):].rstrip(":")
        url = _git_remote_url(name, project_root, runner)
        if not url:
            raise ValueError(f"no git remote named {name!r} in this repository")
        return _git_remote_target(name, url, operand)
    if operand.startswith("ssh:") and not operand.startswith("ssh://"):
        rest = operand[len("ssh:"):]
        host, _, path = rest.partition(":")
        if not host:
            raise ValueError(f"invalid target {operand!r}: empty host")
        if path:
            return SyncTarget(kind="remote-path", host=host, path=path,
                              raw=operand)
        return SyncTarget(kind="store", host=host, raw=operand)
    if ":" in operand:
        host, _, path = operand.partition(":")
        if not host:
            raise ValueError(f"invalid target {operand!r}: empty host")
        if path:
            return SyncTarget(kind="remote-path", host=host, path=path,
                              raw=operand)
        # The bare NAME: form: a git remote of that name wins over an ssh host.
        url = _git_remote_url(host, project_root, runner)
        if url:
            return _git_remote_target(host, url, operand)
        return SyncTarget(kind="store", host=host, raw=operand)
    # No colon: a local folder — except the deprecated bare-host form (an
    # ssh-looking word with no path separator that names nothing on disk).
    looks_like_path = (os.sep in operand or operand in (".", "..")
                       or operand.startswith(("~", ".")) or os.path.exists(operand))
    if "@" in operand or not looks_like_path:
        logger.warning(
            "bare host target %r is deprecated (a colon now distinguishes a "
            "remote from a local folder): write %r.", operand, operand + ":",
        )
        return SyncTarget(kind="store", host=operand, raw=operand)
    return SyncTarget(kind="local-path",
                      path=os.path.abspath(os.path.expanduser(operand)),
                      raw=operand)


class SyncObject:
    """A single resolved syncable object and its store-relative address.

    Attributes
    ----------
    id:
        The identifier the user gave (a dataset name/alias/doi, or a produced
        ``cachetype[/version]/hash`` / hash-prefix).
    kind:
        ``"datasets"`` (fetched) or ``"cached"`` (produced).
    rel:
        The machine-independent address — a fetched dataset's ``key``, or a
        produced artifact's ``cachetype/[version/]hash``. It re-attaches under
        the receiver's ``datasets_dir`` / ``datacache_dir``.
    local_abs:
        The resolved absolute local path.
    is_dir:
        Whether the local object is a directory (marker lives inside) versus a
        file (sibling ``<file>.complete`` marker).
    size:
        Total bytes on disk (best-effort; ``0`` when not present locally).
    """

    __slots__ = ("id", "kind", "rel", "local_abs", "is_dir", "size")

    def __init__(self, *, id, kind, rel, local_abs, is_dir, size):
        self.id = id
        self.kind = kind
        self.rel = rel
        self.local_abs = local_abs
        self.is_dir = is_dir
        self.size = size

    def __repr__(self):
        return (
            f"<SyncObject {self.kind} {self.id!r} rel={self.rel!r} "
            f"local={self.local_abs!r}>"
        )


# ---------------------------------------------------------------------------
# machine-independent address
# ---------------------------------------------------------------------------

def _dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _object_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    if os.path.isdir(path):
        return _dir_size(path)
    return 0


# ---------------------------------------------------------------------------
# address resolution
# ---------------------------------------------------------------------------

def _resolve_fetched(db, entry, ident, project_root, *, allow_local=False):
    """Build a :class:`SyncObject` for a fetched dataset *entry*.

    Refuses a **local** (``$repo``-relative) dataset unless *allow_local* (an
    explicit-path target supplies the destination outright, so the refusal
    lifts). The machine-independent address is the dataset's ``key``,
    re-attached under the receiver's ``datasets_dir``."""
    expr = entry.storage_path or "$datasets_dir/$key"
    if not allow_local and store.is_local_path(
        expr, key=entry.key, project_root=project_root,
        storage_config=db.storage_config,
    ):
        raise RemoteRepoError(
            f"dataset {ident!r} is stored locally (under the project root); a "
            "repo-relative object is out of scope for sync — only machine-global "
            "locations ($user_data_dir / $user_cache_dir / user-defined) sync. "
            "Point datasets_dir at a machine-global folder to sync it."
        )
    local_abs = store.dataset_path(
        entry.storage_path, entry.key,
        project_root=project_root, storage_config=db.storage_config,
    )
    is_dir = os.path.isdir(local_abs)
    return SyncObject(
        id=ident, kind="datasets", rel=entry.key,
        local_abs=local_abs, is_dir=is_dir, size=_object_size(local_abs),
    )


def _produced_key_from_id(ident, cache_root):
    """Resolve a produced-artifact *ident* against the on-disk ``datacache_dir``.

    *ident* is ``cachetype[/version]/hash`` with a full hash or an unambiguous
    hash **prefix**. Returns ``(key, artifact_dir)`` where ``key`` is the full
    ``cachetype/[version/]hash``; raises :class:`AmbiguousIdError` /
    ``ValueError`` when the id matches several / no artifacts.
    """
    from .cache import find_produced_artifacts

    # Enumerate every produced artifact and match the id against its
    # cachetype/[version/]hash address. The id's last segment is a (possibly
    # partial) hash; the leading segments are cachetype[/version].
    parts = [p for p in ident.split("/") if p]
    if not parts:
        raise ValueError(f"empty produced-artifact id {ident!r}")
    id_hash = parts[-1]
    id_head = parts[:-1]  # cachetype, or cachetype/version

    matches = []
    for artifact_dir, key in find_produced_artifacts(cache_root):
        # key == "<cachetype>/<hash>"; the on-disk dir path carries the version
        # segment when present. Recover (cachetype, version, hash) from the dir.
        ctype, _, h = key.partition("/")
        # Derive version from the artifact dir: it sits between cachetype and
        # hash in the relative path (prefix/[scope/]cachetype/[version/]hash).
        rel = os.path.relpath(artifact_dir, cache_root).split(os.sep)
        # the trailing path segment is the hash; the one before, if not the
        # cachetype, is the version.
        version = ""
        if len(rel) >= 2 and rel[-2] != ctype:
            version = rel[-2]
        # Match the head (cachetype[/version]) when the user supplied one.
        head_ok = True
        if id_head:
            if len(id_head) == 1:
                head_ok = id_head[0] == ctype
            elif len(id_head) == 2:
                head_ok = id_head[0] == ctype and id_head[1] == version
            else:
                head_ok = False
        if head_ok and h.startswith(id_hash):
            full_key = f"{ctype}/{version}/{h}" if version else f"{ctype}/{h}"
            matches.append((full_key, artifact_dir, h))

    if not matches:
        raise ValueError(
            f"no produced artifact found for {ident!r} under {cache_root}"
        )
    # An exact full-hash match is unambiguous even if it is also a prefix of a
    # longer string (impossible for SHA-256, but keep it well-defined).
    exact = [m for m in matches if m[2] == id_hash]
    if len(exact) == 1:
        full_key, artifact_dir, _ = exact[0]
        return full_key, artifact_dir
    if len(matches) > 1:
        listing = "\n- ".join(m[0] for m in matches)
        raise AmbiguousIdError(
            f"produced-artifact id {ident!r} is ambiguous; it matches:\n- "
            f"{listing}\nGive a longer hash prefix, or pass --batch to transfer "
            "all matches."
        )
    full_key, artifact_dir, _ = matches[0]
    return full_key, artifact_dir


def _all_produced_for_id(ident, cache_root):
    """Every ``(full_key, artifact_dir)`` matching a produced-artifact *ident*
    (the ``--batch`` form — an ambiguous id transfers all matches)."""
    from .cache import find_produced_artifacts

    parts = [p for p in ident.split("/") if p]
    id_hash = parts[-1] if parts else ""
    id_head = parts[:-1]
    out = []
    for artifact_dir, key in find_produced_artifacts(cache_root):
        ctype, _, h = key.partition("/")
        rel = os.path.relpath(artifact_dir, cache_root).split(os.sep)
        version = ""
        if len(rel) >= 2 and rel[-2] != ctype:
            version = rel[-2]
        head_ok = True
        if id_head:
            if len(id_head) == 1:
                head_ok = id_head[0] == ctype
            elif len(id_head) == 2:
                head_ok = id_head[0] == ctype and id_head[1] == version
            else:
                head_ok = False
        if head_ok and h.startswith(id_hash):
            full_key = f"{ctype}/{version}/{h}" if version else f"{ctype}/{h}"
            out.append((full_key, artifact_dir))
    return out


def _resolve_produced(db, ident, project_root, *, full_key, artifact_dir,
                      allow_local=False):
    """Build a :class:`SyncObject` for a produced artifact addressed by
    *full_key* (``cachetype/[version/]hash``) living at *artifact_dir*.

    Refuses a **local** (repo-relative) ``datacache_dir`` unless *allow_local*
    (explicit-path target). The machine-independent address is *full_key*,
    re-attached under the receiver's ``datacache_dir``."""
    if not allow_local and store.is_local_path(
        "$datacache_dir", project_root=project_root,
        storage_config=db.storage_config,
    ):
        raise RemoteRepoError(
            f"artifact {ident!r} is in a local (repo-relative) datacache_dir; a "
            "repo-relative object is out of scope for sync — point datacache_dir "
            "at a machine-global folder ($user_cache_dir/…) to sync it."
        )
    local_abs = os.path.abspath(artifact_dir)
    is_dir = os.path.isdir(local_abs)
    return SyncObject(
        id=ident, kind="cached", rel=full_key,
        local_abs=local_abs, is_dir=is_dir, size=_object_size(local_abs),
    )


def resolve_object(db, ident, *, project_root=None, allow_local=False):
    """Resolve a single *ident* to exactly one :class:`SyncObject`.

    Tries the fetched-dataset addressing first (``name`` / ``alias`` / ``doi``);
    if no dataset matches, tries produced-artifact addressing
    (``cachetype[/version]/hash`` or an unambiguous hash prefix) against the
    on-disk ``$cache`` store. Raises :class:`AmbiguousIdError` when the id
    resolves to more than one object (use :func:`resolve_objects` with
    ``batch=True`` to transfer all matches). *allow_local* lifts the
    repo-local refusal (explicit-path targets).
    """
    if project_root is None:
        project_root = db.get_project_root()

    matches = search_datasets(db, ident)
    if len(matches) > 1:
        listing = "\n- ".join(
            " | ".join(list_alternative_keys(ds)) for _, ds in matches
        )
        raise AmbiguousIdError(
            f"id {ident!r} is ambiguous; it matches datasets:\n- {listing}\n"
            "Disambiguate, or pass --batch."
        )
    if len(matches) == 1:
        _name, entry = matches[0]
        return _resolve_fetched(db, entry, ident, project_root,
                                allow_local=allow_local)

    # No dataset — try a produced artifact.
    cache_root = store.datacache_dir(
        project_root=project_root, storage_config=db.storage_config,
    )
    full_key, artifact_dir = _produced_key_from_id(ident, cache_root)
    return _resolve_produced(
        db, ident, project_root, full_key=full_key, artifact_dir=artifact_dir,
        allow_local=allow_local,
    )


def resolve_objects(db, ident, *, project_root=None, batch=False,
                    allow_local=False):
    """Resolve *ident* to a list of :class:`SyncObject` (the bulk / ``--batch``
    form). Without *batch*, an ambiguous id raises; with it, all matches are
    returned (datasets *or* produced artifacts)."""
    if project_root is None:
        project_root = db.get_project_root()

    if not batch:
        return [resolve_object(db, ident, project_root=project_root,
                               allow_local=allow_local)]

    objects = []
    for _name, entry in search_datasets(db, ident):
        try:
            objects.append(_resolve_fetched(db, entry, ident, project_root,
                                            allow_local=allow_local))
        except RemoteRepoError:
            raise
    cache_root = store.datacache_dir(
        project_root=project_root, storage_config=db.storage_config,
    )
    for full_key, artifact_dir in _all_produced_for_id(ident, cache_root):
        objects.append(_resolve_produced(
            db, ident, project_root, full_key=full_key,
            artifact_dir=artifact_dir, allow_local=allow_local,
        ))
    if not objects:
        raise ValueError(f"no object found for id {ident!r}")
    return objects


def sync_object_from_location(db, *, kind, ident, location, project_root=None,
                              allow_local=False):
    """Build a :class:`SyncObject` from an already-resolved on-disk *location*.

    Used by the bulk ``list --push/--pull`` path, where the maintenance
    enumeration has already produced the object's kind / absolute location: the
    machine-independent ``rel`` is the location stripped of the local root
    (``datasets_dir`` for fetched, ``datacache_dir`` for produced). Refuses a
    **local** (repo-relative) root unless *allow_local* (explicit-path target)."""
    if project_root is None:
        project_root = db.get_project_root()
    field = "$datacache_dir" if kind == "cached" else "$datasets_dir"
    if not allow_local and store.is_local_path(
        field, project_root=project_root, storage_config=db.storage_config,
    ):
        raise RemoteRepoError(
            f"object {ident!r} is in a local (repo-relative) "
            f"{field[1:]}; repo-relative objects are out of scope for sync."
        )
    root = (store.datacache_dir if kind == "cached" else store.datasets_dir)(
        project_root=project_root, storage_config=db.storage_config,
    )
    local_abs = os.path.abspath(location)
    rel = os.path.relpath(local_abs, root)
    return SyncObject(
        id=ident, kind=kind, rel=rel,
        local_abs=local_abs, is_dir=os.path.isdir(local_abs),
        size=_object_size(local_abs),
    )


# ---------------------------------------------------------------------------
# remote env probe + remote root
# ---------------------------------------------------------------------------

def remote_env(host, *, runner=None):
    """Best-effort probe of the remote ``DATAMANIFEST_*`` environment over ssh.

    Runs ``ssh <host> 'source ~/.bashrc >/dev/null 2>&1; env'`` through the
    injectable *runner* and parses the ``DATAMANIFEST_*`` variables out of the
    output. ``.bashrc`` commonly early-returns for non-interactive shells and
    ssh may fail outright — so this is **best-effort**: an empty or failed
    capture is normal and returns ``{}`` (never raises). The returned map is fed
    to the field resolver as ``env=``, letting the existing ladder honour the
    remote's ``DATAMANIFEST_DATASETS_DIR`` / ``DATAMANIFEST_DATACACHE_DIR`` rung.
    """
    run = runner or _runner
    argv = ["ssh", host, "source ~/.bashrc >/dev/null 2>&1; env"]
    try:
        proc = run(argv, capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 - ssh unavailable / network down is normal
        return {}
    if getattr(proc, "returncode", 1) != 0:
        return {}
    out = getattr(proc, "stdout", "") or ""
    env = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        # DATAMANIFEST_* drive the env rung; HOME / XDG_* make the remote's
        # platform defaults ($user_data_dir & co) resolve to *its* folders when
        # the ladder is evaluated locally on the remote's behalf.
        if name.startswith("DATAMANIFEST_") or name in (
                "HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
            env[name] = value
    return env


def remote_root(obj, host, *, db, project_root, runner=None):
    """Resolve the **remote** root folder for *obj* on *host* — the remote's
    ``datasets_dir`` (fetched) or ``datacache_dir`` (produced).

    Precedence:

    1. best-effort remote-env probe (``remote_env`` → ``DATAMANIFEST_*`` rung);
    2. ``[_STORAGE._HOST.<glob>]`` overrides for *host* (the deterministic
       cross-machine config);
    3. the shared default.

    The remote context is its **own frozen config**: the committed manifest
    layer (shared across checkouts) plus the probed remote environment and the
    remote hostname (the host part of the ssh target, ``user@host`` →
    ``host``). The local checkout / user-global layers are this machine's
    personal configuration and do not apply to the remote.
    """
    env = remote_env(host, runner=runner)
    match_host = host.split("@", 1)[1] if "@" in host else host
    cfg = store.locations.ScopedConfig(
        manifest=db.storage_config.manifest, env=env, host=match_host)
    resolver = store.datacache_dir if obj.kind == "cached" else store.datasets_dir
    return resolver(project_root=project_root, storage_config=cfg)


# ---------------------------------------------------------------------------
# git remotes as transfer targets (a peer checkout)
# ---------------------------------------------------------------------------

def _ssh_read(host, path, runner=None):
    """The text content of *path* on *host* over ssh, or ``""`` (best-effort —
    an absent file / failed ssh reads as empty). A ``~``-prefixed path expands
    in the remote shell."""
    run = runner or _runner
    try:
        proc = run(["ssh", host, "cat", path], capture_output=True, text=True,
                   check=False)
    except Exception:  # noqa: BLE001
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return getattr(proc, "stdout", "") or ""


def _ssh_read_toml(host, path, runner=None):
    """Parse a remote TOML file over ssh (``{}`` when absent/unreadable)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    text = _ssh_read(host, path, runner)
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except Exception:  # noqa: BLE001 - a malformed remote file contributes nothing
        return {}


# Manifest filenames probed in a peer checkout (canonical + legacy aliases).
_MANIFEST_NAMES = ("datamanifest.toml", "datasets.toml", "Datasets.toml")
# State-file paths probed in a peer checkout (canonical + legacy siblings).
_STATE_SUBPATHS = (".datamanifest/state.toml", ".datamanifest-state.toml",
                   "cached.toml")


def git_remote_recorded(obj, target, *, runner=None):
    """The recorded absolute location of *obj* in the peer checkout's state
    file — the **pull** side of a git-remote target: the peer's
    ``.datamanifest/state.toml`` holds resolved locations, so nothing needs
    resolving at all. Raises when the peer does not record the object."""
    from .cache import CachedIndex

    text = ""
    for sub in _STATE_SUBPATHS:
        text = _ssh_read(target.host, f"{target.path}/{sub}", runner)
        if text:
            break
    if not text:
        raise ValueError(
            f"remote {target.name!r} ({target.host}:{target.path}) has no "
            "state file — nothing recorded to pull from. The peer checkout "
            "must have materialized data (or run `datamanifest refresh`)."
        )
    idx = CachedIndex.loads(text)
    if obj.kind == "datasets":
        sp = idx.dataset_path_of(obj.rel)
    else:
        parts = [p for p in obj.rel.split("/") if p]
        if len(parts) == 3:
            ct, ver, h = parts
        elif len(parts) == 2:
            (ct, h), ver = parts, ""
        else:
            ct = ver = h = ""
        sp = idx.instance_path_of(cachetype=ct, version=ver, hash=h) \
            if ct and h else ""
    if not sp:
        raise ValueError(
            f"remote {target.name!r} does not record {obj.id!r} in its state "
            "file — it has no copy to pull."
        )
    # Relative records anchor to the peer checkout (the state-file convention).
    if not sp.startswith("/"):
        sp = f"{target.path}/{sp}"
    return sp


def git_remote_root(obj, target, *, runner=None):
    """The peer checkout's ``datasets_dir`` / ``datacache_dir`` — the **push**
    side of a git-remote target: the directive ladder resolved **in the remote
    context**, never guessed from local config.

    Preferred mechanism: run ``datamanifest where --<field>`` in the peer
    checkout over ssh (the scriptable form — the remote evaluates its own
    ladder). Fallback: read the peer's config files over ssh (its
    ``.datamanifest/config.toml``, manifest ``[_STORAGE]`` and user-global
    config) and evaluate the ladder locally, fed the remote env probe (so the
    remote's ``HOME`` / ``XDG_*`` / ``DATAMANIFEST_*`` apply)."""
    run = runner or _runner
    field = "datacache_dir" if obj.kind == "cached" else "datasets_dir"
    flag = "--" + field.replace("_", "-")
    try:
        proc = run(
            ["ssh", target.host,
             f"cd {target.path} && datamanifest where {flag}"],
            capture_output=True, text=True, check=False,
        )
        out = (getattr(proc, "stdout", "") or "").strip()
        if getattr(proc, "returncode", 1) == 0 and out:
            return out.splitlines()[-1].strip()
    except Exception:  # noqa: BLE001 - fall through to the file-based fallback
        pass

    local_cfg = _ssh_read_toml(
        target.host, f"{target.path}/.datamanifest/config.toml", runner)
    manifest = {}
    for name in _MANIFEST_NAMES:
        manifest = _ssh_read_toml(target.host, f"{target.path}/{name}", runner)
        if manifest:
            break
    user_cfg = _ssh_read_toml(
        target.host, "~/.config/datamanifest/config.toml", runner)
    env = remote_env(target.host, runner=runner)
    match_host = target.host.split("@", 1)[1] if "@" in target.host \
        else target.host
    # The remote checkout's own frozen config: its three config layers (read
    # over ssh) plus its probed environment and hostname.
    cfg = store.locations.ScopedConfig(
        local=local_cfg,
        manifest=manifest.get("_STORAGE", {}),
        user=user_cfg,
        env=env,
        host=match_host,
    )
    resolver = (store.datacache_dir if obj.kind == "cached"
                else store.datasets_dir)
    return resolver(project_root=target.path, storage_config=cfg)


def remote_abs(obj, target, *, db, project_root, runner=None):
    """The object's path at the non-store end of the transfer.

    For a store target (``HOST:``) this is ``<remote-root>/<rel>`` with the
    root resolved in the remote context; for an explicit path target
    (``HOST:PATH`` / ``PATH``) the given folder replaces the root outright —
    nothing is resolved."""
    target = parse_target(target)
    if target.kind in ("remote-path", "local-path"):
        return os.path.join(target.path, obj.rel)
    root = remote_root(
        obj, target.host, db=db, project_root=project_root, runner=runner,
    )
    return os.path.join(root, obj.rel)


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

def copy_object_bytes(src, dst):
    """Copy an object's bytes (a file or a directory tree) from *src* to *dst*
    via a staging sibling + atomic rename — *dst* never appears half-written.
    A pre-existing *dst* is replaced. Returns *dst*."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    tmp = store.tmp_path(dst)
    if os.path.isdir(tmp) and not os.path.islink(tmp):
        shutil.rmtree(tmp)
    elif os.path.exists(tmp):
        os.remove(tmp)
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, tmp, symlinks=True)
    else:
        shutil.copy2(src, tmp)
    if os.path.isdir(dst) and not os.path.islink(dst):
        shutil.rmtree(dst)
    elif os.path.exists(dst):
        os.remove(dst)
    os.rename(tmp, dst)
    return dst


def _transfer_local(obj, root, *, direction, dry_run=False):
    """The ``PATH`` target: a plain local byte copy under the keyed layout.

    ``push`` exports the object to ``<root>/<rel>`` (a raw export — the result
    is itself a read pool); ``pull`` adopts ``<root>/<rel>`` by copy into the
    local store (the caller records it in the state file). A file object's
    sibling ``.complete`` marker travels along; a directory's inner marker
    travels with the tree."""
    other = os.path.join(root, obj.rel)
    plan = {
        "id": obj.id,
        "kind": obj.kind,
        "direction": direction,
        "local": obj.local_abs,
        "remote": other,
        "host": "",
        "size": obj.size,
        "argv": [],
    }
    if dry_run:
        return plan

    src, dst = ((obj.local_abs, other) if direction == "push"
                else (other, obj.local_abs))
    if not os.path.exists(src):
        raise ValueError(
            f"{obj.id!r}: nothing found at {src} to {direction}"
        )
    copy_object_bytes(src, dst)
    if os.path.isfile(dst) and os.path.isfile(src + ".complete"):
        shutil.copy2(src + ".complete", dst + ".complete")
    return plan


def _operands(obj, host, remote_path, *, direction):
    """Build the rsync source/destination operands for *obj*.

    For a **directory** object the marker lives inside (a recursive ``rsync -a``
    of the directory carries ``.complete``); for a **file** object the marker is
    a sibling ``<file>.complete`` and is transferred alongside. Returns a list of
    ``(sources, dest)`` rsync invocations (one for a dir, two for a file: the
    file and its sibling marker)."""
    pairs = []
    if direction == "push":
        if obj.is_dir:
            pairs.append(([obj.local_abs], f"{host}:{remote_path}"))
        else:
            pairs.append(([obj.local_abs], f"{host}:{remote_path}"))
            pairs.append((
                [obj.local_abs + ".complete"],
                f"{host}:{remote_path}.complete",
            ))
    else:  # pull
        if obj.is_dir:
            pairs.append(([f"{host}:{remote_path}"], obj.local_abs))
        else:
            pairs.append(([f"{host}:{remote_path}"], obj.local_abs))
            pairs.append((
                [f"{host}:{remote_path}.complete"],
                obj.local_abs + ".complete",
            ))
    return pairs


def transfer(db, obj, target, *, direction, project_root=None, dry_run=False,
             runner=None):
    """Transfer *obj* to/from *target* (``direction`` is ``"push"`` / ``"pull"``).

    *target* is a :class:`SyncTarget` or an operand string (see
    :func:`parse_target`): ``HOST:`` (the remote store), ``HOST:PATH`` (an
    explicit remote folder), or a local ``PATH`` (a plain byte copy).

    - resolves the other end's path: the remote root (best-effort remote-env
      probe → ``_HOST`` → default) for a store target, the explicit folder
      otherwise, composing ``<root>/<rel>``;
    - **push:** ``ssh <host> mkdir -p <remote-parent>`` then
      ``rsync -a -e ssh <local_abs> <host>:<remote_abs>``;
    - **pull:** ``os.makedirs(<local-parent>)`` then
      ``rsync -a -e ssh <host>:<remote_abs> <local_abs>``;
    - a local ``PATH`` target copies bytes directly (staging + atomic rename);
    - a **file** object also transfers its sibling ``<file>.complete`` marker; a
      **directory** carries its inner marker via the recursive copy;
    - **idempotent:** a real run is a near-no-op when the target is already
      complete (rsync skips unchanged bytes);
    - **``dry_run``:** resolves and returns the plan without invoking the runner
      for any transfer.

    Returns a dict describing the resolved plan (``id`` / ``kind`` / ``local`` /
    ``remote`` (the other end's path) / ``host`` (``""`` for a local target) /
    ``size`` / ``argv`` — the list of rsync argv that ran, empty on dry run or
    for a local copy).
    """
    if direction not in ("push", "pull"):
        raise ValueError(f"direction must be 'push' or 'pull', got {direction!r}")
    if project_root is None:
        project_root = db.get_project_root()
    target = parse_target(target)
    if target.kind == "local-path":
        return _transfer_local(obj, target.path, direction=direction,
                               dry_run=dry_run)
    host = target.host
    run = runner or _runner

    if target.kind == "git-remote":
        # The peer checkout: pull reads its state file (recorded resolved
        # locations); push resolves the directive ladder in the remote context.
        if direction == "pull":
            rpath = git_remote_recorded(obj, target, runner=runner)
        else:
            rpath = f"{git_remote_root(obj, target, runner=runner)}/{obj.rel}"
    else:
        rpath = remote_abs(
            obj, target, db=db, project_root=project_root, runner=runner,
        )
    remote_parent = os.path.dirname(rpath)

    plan = {
        "id": obj.id,
        "kind": obj.kind,
        "direction": direction,
        "local": obj.local_abs,
        "remote": rpath,
        "host": host,
        "size": obj.size,
        "argv": [],
    }
    if dry_run:
        return plan

    argv_log = []
    if direction == "push":
        mkdir_argv = ["ssh", host, "mkdir", "-p", remote_parent]
        run(mkdir_argv, check=True)
        argv_log.append(mkdir_argv)
    else:
        local_parent = os.path.dirname(obj.local_abs)
        if local_parent:
            os.makedirs(local_parent, exist_ok=True)

    for sources, dest in _operands(obj, host, rpath, direction=direction):
        rsync_argv = ["rsync", "-a", "-e", "ssh", *sources, dest]
        run(rsync_argv, check=True)
        argv_log.append(rsync_argv)

    plan["argv"] = argv_log
    return plan
