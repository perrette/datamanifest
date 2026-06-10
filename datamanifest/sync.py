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
    """

    __slots__ = ("kind", "host", "path", "raw")

    def __init__(self, *, kind, host="", path="", raw=""):
        self.kind = kind
        self.host = host
        self.path = path
        self.raw = raw

    def __repr__(self):
        return f"<SyncTarget {self.kind} host={self.host!r} path={self.path!r}>"


def parse_target(operand) -> "SyncTarget":
    """Parse a push/pull *operand* per the colon rule: a colon means remote
    (``HOST:`` = the remote store, ``HOST:PATH`` = an explicit remote folder);
    no colon means a local folder.

    The historical bare-host form (``push ID host``, no colon) is still
    recognized — an operand with no path separator that does not exist locally
    (or any ``user@host``) — and warned as deprecated: write ``host:`` instead.
    """
    if isinstance(operand, SyncTarget):
        return operand
    if ":" in operand:
        host, _, path = operand.partition(":")
        if not host:
            raise ValueError(f"invalid target {operand!r}: empty host")
        if path:
            return SyncTarget(kind="remote-path", host=host, path=path,
                              raw=operand)
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
        if name.startswith("DATAMANIFEST_"):
            env[name] = value
    return env


def remote_root(obj, host, *, db, project_root, runner=None):
    """Resolve the **remote** root folder for *obj* on *host* — the remote's
    ``datasets_dir`` (fetched) or ``datacache_dir`` (produced).

    Precedence (all via the existing field resolver — we only pick its ``env`` /
    ``host`` inputs):

    1. best-effort remote-env probe (``remote_env`` → ``DATAMANIFEST_*`` rung);
    2. ``[_STORAGE._HOST.<glob>]`` overrides for *host* (the deterministic
       cross-machine config);
    3. the shared default.

    The remote *hostname* used for ``_HOST`` matching is the host part of the ssh
    target (``user@host`` → ``host``).
    """
    env = remote_env(host, runner=runner)
    match_host = host.split("@", 1)[1] if "@" in host else host
    resolver = store.datacache_dir if obj.kind == "cached" else store.datasets_dir
    return resolver(
        project_root=project_root,
        storage_config=db.storage_config, env=env, host=match_host,
    )


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
