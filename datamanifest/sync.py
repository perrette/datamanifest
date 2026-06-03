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
An object's address is its path **relative to the store-folder root**:

- fetched   → ``<datasets-prefix>/[<datasets-scope>/]<key>`` (scope empty by
  default), addressed by ``name`` / ``alias`` / ``doi``;
- produced  → ``<cached-prefix>/[<cached-scope>/]<cachetype>/[<version>/]<hash>``,
  addressed by ``cachetype[/version]/hash`` (full or an unambiguous hash prefix).

The same store-relative ``rel`` composes against either root::

    local_abs  = <local-root>/<rel>
    remote_abs = <remote-root>/<rel>

``rel`` is derived from the existing ``content_prefix`` / ``content_scope`` +
key composition — there is one composition, no duplication: we resolve the full
``composed_path`` against the *local* store and strip the local root to obtain
``rel``, then re-attach ``rel`` to the resolved remote root.

Remote root resolution (precedence)
-----------------------------------
The remote store root is resolved with the **existing** folder ladder
(:func:`datamanifest.store.folder_base`), fed three inputs in this order of
authority:

1. **Best-effort remote-env probe.** Over ssh we run
   ``source ~/.bashrc >/dev/null 2>&1; env`` (via the injectable runner) and
   parse the ``DATAMANIFEST_*`` variables out of the output. ``.bashrc`` very
   often early-returns for non-interactive shells, and ssh itself may fail — so
   this is **best-effort**: an empty or failed capture is *normal* and must not
   error. When it succeeds, the captured ``DATAMANIFEST_*`` map is passed as
   ``env=`` to ``folder_base`` and the existing ladder turns it into the remote
   root via the remote ``DATAMANIFEST_<NAME>_DIR`` / ``DATAMANIFEST_DIR`` rung.
2. **``[_STORAGE._HOST.<glob>]`` host overrides** — selected by passing
   ``host=<remote-host>`` to ``folder_base``. This is the **deterministic
   cross-machine config**: the remote-env probe is only a convenience; the
   ``_HOST`` table is the reliable, declared mapping.
3. **The shared default** — ``platformdirs`` (``$data`` / ``$cache``), which both
   machines agree on by construction.

We never re-implement the ladder; we only choose its ``env`` / ``host`` inputs.

Contract
--------
- **Target = an SSH address** (``user@host`` or ``host``): transport (rsync over
  ssh) + host identity. No remote registry.
- **``$repo`` is out of scope.** A fetched dataset whose resolved ``store``
  selector names ``$repo`` is refused — only machine-global ``$data`` / ``$cache``
  / user-defined folders sync.
- **Writes no manifest** (bytes only). A received object lands as an orphan,
  usable via read-resolution.
- **Integrity is rsync's**; **idempotent** (a no-op when the target already holds
  the object complete — its ``.complete`` marker is present).
- **Symmetric** push/pull; only the transfer direction differs.
"""

import os
import subprocess

from . import store
from .database import (
    list_alternative_keys,
    search_datasets,
)

__all__ = [
    "SyncObject",
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


class SyncObject:
    """A single resolved syncable object and its store-relative address.

    Attributes
    ----------
    id:
        The identifier the user gave (a dataset name/alias/doi, or a produced
        ``cachetype[/version]/hash`` / hash-prefix).
    kind:
        ``"datasets"`` (fetched) or ``"cached"`` (produced).
    selector:
        The ``$folder[/subpath]`` store selector the object lives under.
    rel:
        The store-relative address — the path under the bare store-folder root
        (``<prefix>/[<scope>/]<key>``).
    local_abs:
        The resolved absolute local path.
    is_dir:
        Whether the local object is a directory (marker lives inside) versus a
        file (sibling ``<file>.complete`` marker).
    size:
        Total bytes on disk (best-effort; ``0`` when not present locally).
    """

    __slots__ = ("id", "kind", "selector", "rel", "local_abs", "is_dir", "size")

    def __init__(self, *, id, kind, selector, rel, local_abs, is_dir, size):
        self.id = id
        self.kind = kind
        self.selector = selector
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
# store-relative address
# ---------------------------------------------------------------------------

def _rel_from_composed(composed, root):
    """Store-relative address: *composed* (the full ``composed_path``) with the
    bare *root* stripped off. Both are absolute; the relpath is the
    machine-independent ``<prefix>/[<scope>/]<key>``."""
    rel = os.path.relpath(composed, root)
    # Normalize to forward slashes — it is an rsync operand suffix, not a local
    # path, and the remote may use a different separator convention only in
    # theory; in practice both ends are POSIX. ``relpath`` already uses os.sep.
    return rel


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

def _resolve_fetched(db, entry, ident, project_root):
    """Build a :class:`SyncObject` for a fetched dataset *entry*.

    Refuses a ``$repo``-stored dataset (out of scope for sync). The
    store-relative address is the ``datasets/`` composition stripped of the bare
    store root."""
    selector = entry.store or store.project_default(db.storage_config)
    # Resolve the folder name out of the selector ($folder[/subpath]).
    folder_name = selector[1:].split("/", 1)[0] if selector.startswith("$") else ""
    if folder_name.startswith("{") and folder_name.endswith("}"):
        folder_name = folder_name[1:-1]
    if folder_name == "repo":
        raise RemoteRepoError(
            f"dataset {ident!r} is stored in $repo (project-relative); $repo is "
            "out of scope for sync — only $data / $cache / user-defined folders "
            "are machine-global. Move it to a global store to sync it."
        )

    local_abs = store.composed_path(
        selector, entry.key, kind="datasets",
        project_root=project_root, storage_config=db.storage_config,
    )
    root = store.folder_base(
        folder_name, project_root=project_root,
        storage_config=db.storage_config,
    )
    rel = _rel_from_composed(local_abs, root)
    is_dir = os.path.isdir(local_abs)
    return SyncObject(
        id=ident, kind="datasets", selector=selector, rel=rel,
        local_abs=local_abs, is_dir=is_dir, size=_object_size(local_abs),
    )


def _produced_key_from_id(ident, cache_root, prefix):
    """Resolve a produced-artifact *ident* against the on-disk ``$cache`` store.

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


def _resolve_produced(db, ident, project_root, *, full_key, artifact_dir):
    """Build a :class:`SyncObject` for a produced artifact addressed by
    *full_key* (``cachetype/[version/]hash``) living at *artifact_dir*."""
    selector = "$cache"
    local_abs = store.composed_path(
        selector, full_key, kind="cached",
        project_root=project_root, storage_config=db.storage_config,
        meta=db.extra.get("_META"),
    )
    # find_produced_artifacts already located the dir; prefer it (it accounts
    # for the actual on-disk scope) but fall back to the composed path.
    if artifact_dir and os.path.isdir(artifact_dir):
        local_abs = os.path.abspath(artifact_dir)
    folder_name = "cache"
    root = store.folder_base(
        folder_name, project_root=project_root,
        storage_config=db.storage_config,
    )
    rel = _rel_from_composed(local_abs, root)
    is_dir = os.path.isdir(local_abs)
    return SyncObject(
        id=ident, kind="cached", selector=selector, rel=rel,
        local_abs=local_abs, is_dir=is_dir, size=_object_size(local_abs),
    )


def resolve_object(db, ident, *, project_root=None):
    """Resolve a single *ident* to exactly one :class:`SyncObject`.

    Tries the fetched-dataset addressing first (``name`` / ``alias`` / ``doi``);
    if no dataset matches, tries produced-artifact addressing
    (``cachetype[/version]/hash`` or an unambiguous hash prefix) against the
    on-disk ``$cache`` store. Raises :class:`AmbiguousIdError` when the id
    resolves to more than one object (use :func:`resolve_objects` with
    ``batch=True`` to transfer all matches).
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
        return _resolve_fetched(db, entry, ident, project_root)

    # No dataset — try a produced artifact.
    cache_root = store.resolve_selector(
        "$cache", project_root=project_root, storage_config=db.storage_config,
    )
    full_key, artifact_dir = _produced_key_from_id(
        ident, cache_root, store.content_prefix(
            "cached", storage_config=db.storage_config,
        ),
    )
    return _resolve_produced(
        db, ident, project_root, full_key=full_key, artifact_dir=artifact_dir,
    )


def resolve_objects(db, ident, *, project_root=None, batch=False):
    """Resolve *ident* to a list of :class:`SyncObject` (the bulk / ``--batch``
    form). Without *batch*, an ambiguous id raises; with it, all matches are
    returned (datasets *or* produced artifacts)."""
    if project_root is None:
        project_root = db.get_project_root()

    if not batch:
        return [resolve_object(db, ident, project_root=project_root)]

    objects = []
    for _name, entry in search_datasets(db, ident):
        try:
            objects.append(_resolve_fetched(db, entry, ident, project_root))
        except RemoteRepoError:
            raise
    cache_root = store.resolve_selector(
        "$cache", project_root=project_root, storage_config=db.storage_config,
    )
    for full_key, artifact_dir in _all_produced_for_id(ident, cache_root):
        objects.append(_resolve_produced(
            db, ident, project_root, full_key=full_key,
            artifact_dir=artifact_dir,
        ))
    if not objects:
        raise ValueError(f"no object found for id {ident!r}")
    return objects


def sync_object_from_location(db, *, kind, ident, location, selector=None,
                              project_root=None):
    """Build a :class:`SyncObject` from an already-resolved on-disk *location*.

    Used by the bulk ``list --push/--pull`` path, where the maintenance
    enumeration has already produced the object's kind / key / absolute
    location: the store-relative ``rel`` is the location stripped of the bare
    store-folder root (``$data`` for fetched, ``$cache`` for produced — the
    selector the enumeration walked). Refuses a ``$repo`` location."""
    if project_root is None:
        project_root = db.get_project_root()
    if selector is None:
        selector = "$cache" if kind == "cached" else "$data"
    folder_name = selector[1:].split("/", 1)[0] if selector.startswith("$") else "data"
    if folder_name.startswith("{") and folder_name.endswith("}"):
        folder_name = folder_name[1:-1]
    if folder_name == "repo":
        raise RemoteRepoError(
            f"object {ident!r} is under $repo; $repo is out of scope for sync."
        )
    root = store.folder_base(
        folder_name, project_root=project_root,
        storage_config=db.storage_config,
    )
    local_abs = os.path.abspath(location)
    rel = _rel_from_composed(local_abs, root)
    return SyncObject(
        id=ident, kind=kind, selector=selector, rel=rel,
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
    to :func:`datamanifest.store.folder_base` as ``env=``, letting the existing
    folder ladder honour the remote's ``DATAMANIFEST_<NAME>_DIR`` /
    ``DATAMANIFEST_DIR`` rung.
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
    """Resolve the **remote** bare store-folder root for *obj* on *host*.

    Precedence (all via the existing :func:`datamanifest.store.folder_base`
    ladder — we only pick its ``env`` / ``host`` inputs):

    1. best-effort remote-env probe (``remote_env`` → ``DATAMANIFEST_*`` rung);
    2. ``[_STORAGE._HOST.<glob>]`` overrides for *host* (the deterministic
       cross-machine config);
    3. the shared ``platformdirs`` default.

    The remote *hostname* used for ``_HOST`` matching is the host part of the ssh
    target (``user@host`` → ``host``).
    """
    env = remote_env(host, runner=runner)
    match_host = host.split("@", 1)[1] if "@" in host else host
    folder_name = obj.selector[1:].split("/", 1)[0] if obj.selector.startswith("$") \
        else "data"
    if folder_name.startswith("{") and folder_name.endswith("}"):
        folder_name = folder_name[1:-1]
    return store.folder_base(
        folder_name, project_root=project_root,
        storage_config=db.storage_config, env=env, host=match_host,
    )


def remote_abs(obj, host, *, db, project_root, runner=None):
    """The object's absolute path on *host*: ``<remote-root>/<rel>``."""
    root = remote_root(
        obj, host, db=db, project_root=project_root, runner=runner,
    )
    return os.path.join(root, obj.rel)


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

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


def transfer(db, obj, host, *, direction, project_root=None, dry_run=False,
             runner=None):
    """Transfer *obj* to/from *host* (``direction`` is ``"push"`` / ``"pull"``).

    - resolves the remote root (best-effort remote-env probe → ``_HOST`` →
      default), composes ``remote_abs = <remote-root>/<rel>``;
    - **push:** ``ssh <host> mkdir -p <remote-parent>`` then
      ``rsync -a -e ssh <local_abs> <host>:<remote_abs>``;
    - **pull:** ``os.makedirs(<local-parent>)`` then
      ``rsync -a -e ssh <host>:<remote_abs> <local_abs>``;
    - a **file** object also transfers its sibling ``<file>.complete`` marker; a
      **directory** carries its inner marker via the recursive copy;
    - **idempotent:** a real run is a near-no-op when the target is already
      complete (rsync skips unchanged bytes);
    - **``dry_run``:** resolves and returns the plan without invoking the runner
      for any transfer.

    Returns a dict describing the resolved plan (``id`` / ``kind`` / ``local`` /
    ``remote`` / ``size`` / ``argv`` — the list of rsync argv that ran, empty on
    dry run).
    """
    if direction not in ("push", "pull"):
        raise ValueError(f"direction must be 'push' or 'pull', got {direction!r}")
    if project_root is None:
        project_root = db.get_project_root()
    run = runner or _runner

    rpath = remote_abs(
        obj, host, db=db, project_root=project_root, runner=runner,
    )
    remote_parent = os.path.dirname(rpath)

    plan = {
        "id": obj.id,
        "kind": obj.kind,
        "direction": direction,
        "local": obj.local_abs,
        "remote": rpath,
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
