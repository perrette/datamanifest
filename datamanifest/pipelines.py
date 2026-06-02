"""Download + load pipeline.

Port of DataManifest.jl's ``PipeLines.jl`` (download orchestration).

- ``download_dataset`` / ``download_datasets`` — orchestration + checksum,
  with ``requires=`` dependencies downloaded first in topological order.
- ``_download_dataset`` — full scheme dispatch (HTTP, git, ssh/rsync, file)
  plus the ``shell`` template download hook.
- ``extract_file`` — ``zip`` / ``tar`` / ``tar.gz`` extraction.

The ``python`` download hook is added in a later item.

Downloads are synchronous (``httpx.Client(follow_redirects=True)`` streaming
with a ``tqdm`` progress bar), mirroring Julia's ``Downloads.download`` model
(``PipeLines.jl:224-313``). A partial download is written to a ``.download``
sidecar file and resumed via an HTTP ``Range:`` header when re-run.
"""

import contextlib
import importlib
import os
import shlex
import shutil
import socket
import subprocess
import sys

import httpx
from tqdm import tqdm

from . import default_loaders, storage
from .config import COMPRESSED_FORMATS, logger, project_root_from_paths
from .database import (
    get_dataset_path,
    parse_uri_metadata,
    resolve_fetcher,
    resolve_loader_ref,
    search_dataset,
    verify_checksum,
)

_CHUNK_SIZE = 65536


# ----- requires= topological order + shell template (PipeLines.jl:10-81) -----

def _sanitize_ref(ref: str) -> str:
    """Sanitize a dependency reference for use in shell variable names (PipeLines.jl:10)."""
    return ref.replace("/", "_").replace(".", "_")


def _name_for_entry(db, entry) -> str:
    """Return the registry name under which *entry* is stored (PipeLines.jl:315-321)."""
    for name, e in db.datasets.items():
        if e is entry:
            return name
    name, _ = search_dataset(db, entry.key)
    return name


def _get_download_order(db, name: str):
    """Return dataset names in topological order (dependencies first).

    Port of ``PipeLines.jl:15-55``: builds the requires-graph reachable from
    *name*, then Kahn-sorts it. Raises ``ValueError`` on a dependency cycle.
    """
    graph: dict = {}
    seen: set = set()

    def collect_deps(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        _, entry = search_dataset(db, n)
        deps = []
        for ref in entry.requires:
            dep_name, _ = search_dataset(db, ref)
            deps.append(dep_name)
            collect_deps(dep_name)
        graph[n] = deps

    collect_deps(name)

    in_degree = {n: len(deps) for n, deps in graph.items()}
    queue = [n for n in graph if in_degree[n] == 0]
    order = []
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v, deps in graph.items():
            if u in deps:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

    if len(order) != len(graph):
        raise ValueError(f"Circular dependency in dataset requires: {name}")

    return order


def expand_shell_template(
    template: str,
    entry,
    download_path: str,
    project_root: str = "",
    required_paths_by_ref=None,
    required_paths_ordered=None,
) -> str:
    """Substitute ``$variable`` placeholders in a shell template (PipeLines.jl:57-81).

    Supported substitutions: ``$download_path``, ``$project_root``, ``$uri``,
    ``$key``, ``$version``, ``$doi``, ``$format``, ``$branch``,
    ``$path_<sanitized_ref>`` (per dependency), ``$requires_paths`` (all dep
    paths space-joined), and ``$path_<i>`` (1-based dependency index).
    """
    if required_paths_by_ref is None:
        required_paths_by_ref = {}
    if required_paths_ordered is None:
        required_paths_ordered = []

    if "$project_root" in template and project_root == "":
        raise ValueError(
            "Shell template contains $project_root but project root could not be "
            "determined. Use a Database with datasets_toml set."
        )

    result = template
    result = result.replace("$download_path", download_path)
    result = result.replace("$project_root", project_root)
    result = result.replace("$uri", entry.uri)
    result = result.replace("$key", entry.key)
    result = result.replace("$version", entry.version)
    result = result.replace("$doi", entry.doi)
    result = result.replace("$format", entry.format)
    result = result.replace("$branch", entry.branch)
    for sanitized_ref, path in required_paths_by_ref.items():
        result = result.replace(f"$path_{sanitized_ref}", path)
    result = result.replace("$requires_paths", " ".join(required_paths_ordered))
    for i, path in enumerate(required_paths_ordered, start=1):
        result = result.replace(f"$path_{i}", path)
    return result


def _binding_variables(entry, *, download_path=None, path=None, project_root=""):
    """Build the ``$var`` substitution set for a parameterized binding.

    Mirrors :func:`expand_shell_template`'s common variables. Fetchers expose
    ``$download_path``; loaders expose ``$path``; both share ``$project_root``,
    ``$uri``, ``$key``, ``$version``, ``$doi``, ``$format`` and ``$branch``.
    """
    variables = {
        "project_root": project_root,
        "uri": entry.uri,
        "key": entry.key,
        "version": entry.version,
        "doi": entry.doi,
        "format": entry.format,
        "branch": entry.branch,
    }
    if download_path is not None:
        variables["download_path"] = download_path
    if path is not None:
        variables["path"] = path
    return variables


def _substitute_vars(value, variables):
    """Recursively substitute ``$name`` placeholders in the string parts of *value*.

    Used for the parameterized ``{ ref, args, kwargs }`` binding form: every
    string element (scalar, or nested inside a list / dict) has each ``$name``
    from *variables* replaced; non-string scalars pass through unchanged; dict
    keys are kept verbatim. Longer names are substituted first so a shorter name
    can never partially clobber a longer one.
    """
    if isinstance(value, str):
        result = value
        for name in sorted(variables, key=len, reverse=True):
            result = result.replace(f"${name}", variables[name])
        return result
    if isinstance(value, list):
        return [_substitute_vars(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_vars(v, variables) for k, v in value.items()}
    return value


# ----- Loader registry + entry-point resolution (PipeLines.jl:111-198) -----
#
# Julia resolves loader strings either as a module path (`A.B.func`) via runtime
# `getfield`, or — failing that — by compiling arbitrary source with
# `Base.include_string`. The Python port deliberately drops the compile path:
# every loader value and every ``entry.python`` value is a ``"pkg.mod:func"``
# entry-point reference, resolved via ``importlib`` + ``getattr``. No dynamic
# code compilation anywhere (roadmap §C).


def _resolve_entry_point(ref: str, python_includes=None):
    """Resolve a ``"pkg.mod:func"`` reference to a callable.

    The portion before ``:`` is imported with :func:`importlib.import_module`;
    the portion after ``:`` is a (possibly dotted) attribute path looked up via
    :func:`getattr`. Directories in *python_includes* are temporarily prepended
    to ``sys.path`` so user-local modules next to ``datasets.toml`` resolve.
    """
    ref = ref.strip()
    if ":" not in ref:
        raise ValueError(
            f"Invalid entry-point reference {ref!r}: expected 'pkg.mod:func' "
            "(inline code is not allowed)."
        )
    module_path, _, attr_path = ref.partition(":")
    if not module_path or not attr_path:
        raise ValueError(
            f"Invalid entry-point reference {ref!r}: both a module and an "
            "attribute are required ('pkg.mod:func')."
        )

    added = []
    if python_includes:
        for inc in python_includes:
            p = os.path.abspath(inc)
            if p not in sys.path:
                sys.path.insert(0, p)
                added.append(p)
    try:
        obj = importlib.import_module(module_path)
        for part in attr_path.split("."):
            obj = getattr(obj, part)
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass

    if not callable(obj):
        raise ValueError(
            f"Entry point {ref!r} did not resolve to a callable (got {type(obj)})."
        )
    return obj


def _get_loader_function(db, name_or_code: str, cache_key=None, _alias_chain=None):
    """Resolve a named loader or entry-point reference to a callable.

    Port of ``PipeLines.jl:169-198`` (with the ``include_string`` compile path
    removed). ``name_or_code`` is either a key in ``db.loaders`` or a bare
    ``"pkg.mod:func"`` reference. A loader value that is exactly the name of
    another loader is treated as an alias and resolved transitively, with cycle
    detection. Results are memoized in ``db.loader_cache``.
    """
    if _alias_chain is None:
        _alias_chain = set()
    key = name_or_code if cache_key is None else cache_key
    if key in db.loader_cache:
        return db.loader_cache[key]

    code = db.loaders.get(name_or_code, name_or_code)
    # Alias: the value is exactly another loader name -> resolve it.
    if isinstance(code, str) and code in db.loaders and code != name_or_code:
        if name_or_code in _alias_chain:
            raise ValueError(
                f'Loader alias cycle involving "{name_or_code}" and "{code}".'
            )
        chain = _alias_chain | {name_or_code}
        fn = _get_loader_function(db, code, _alias_chain=chain)
        db.loader_cache[key] = fn
        return fn

    fn = _resolve_entry_point(code, db.loaders_python_includes)
    db.loader_cache[key] = fn
    return fn


def _run_python_hook(
    dataset,
    download_path: str,
    project_root: str = "",
    required_paths_ordered=None,
    python_includes=None,
    ref: str = "",
    args=None,
    kwargs=None,
):
    """Run the in-process Python fetcher as a download-phase hook (replaces Julia's
    ``_run_julia``).

    *ref* is the resolved entry-point reference (own ``_LANG.python.fetcher`` or
    legacy ``python=``); it defaults to ``dataset.python`` when not supplied.

    When the binding carries *args* / *kwargs* (the parameterized
    ``{ ref, args, kwargs }`` table form), ``$var`` placeholders in their string
    values are substituted and the callable is invoked as
    ``ref(*args, **kwargs)``. A bare-string binding (no args/kwargs) keeps the
    conventional call: the callable is invoked with the same keyword names the
    shell template exposes, so a single project can use either mechanism
    (``PipeLines.jl:83-114``).
    """
    fn = _resolve_entry_point(ref or dataset.python, python_includes)
    if args or kwargs:
        variables = _binding_variables(
            dataset, download_path=download_path, project_root=project_root
        )
        call_args = _substitute_vars(list(args or []), variables)
        call_kwargs = _substitute_vars(dict(kwargs or {}), variables)
        fn(*call_args, **call_kwargs)
        return
    fn(
        download_path=download_path,
        project_root=project_root,
        entry=dataset,
        uri=dataset.uri,
        key=dataset.key,
        version=dataset.version,
        doi=dataset.doi,
        format=dataset.format,
        branch=dataset.branch,
        requires_paths=list(required_paths_ordered or []),
    )


# ----- Multi-URI helpers (PipeLines.jl:200-222) -----

def _uri_relative_paths(uris):
    """Strip common leading directory segments from URI paths (PipeLines.jl:200-222).

    Example: ["/data/a/x.csv", "/data/b/x.csv"] → ["a/x.csv", "b/x.csv"].
    Never consumes the filename segment.
    """
    segments = [[s for s in parse_uri_metadata(u)["path"].split("/") if s] for u in uris]
    if any(not s for s in segments):
        return [os.path.basename(parse_uri_metadata(u)["path"]) for u in uris]
    min_len = min(len(s) for s in segments)
    n_common = 0
    for i in range(min_len - 1):  # never consume the filename
        if all(s[i] == segments[0][i] for s in segments):
            n_common = i + 1
        else:
            break
    return [os.path.join(*s[n_common:]) for s in segments]


def _download_uri(uri, dest_path):
    """Download a single URI to dest_path (used for each file in a multi-URI batch)."""
    parsed_scheme = parse_uri_metadata(uri)["scheme"]
    if parsed_scheme in ("http", "https"):
        _http_download(uri, dest_path)
    elif parsed_scheme == "file":
        from urllib.parse import urlparse
        src = urlparse(uri).path
        if os.path.isdir(src):
            shutil.copytree(src, dest_path)
        else:
            shutil.copy2(src, dest_path)
    else:
        raise NotImplementedError(
            f"Scheme {parsed_scheme!r} not supported in multi-URI batches. URI: {uri}"
        )


# ----- Archive extraction (Databases.jl:619-630) -----
def extract_file(download_path: str, dest: str, format: str) -> None:
    """Extract *download_path* into directory *dest* (Databases.jl:619-630).

    Julia shells out to ``unzip`` / ``tar``; the Python port uses the stdlib
    ``zipfile`` / ``tarfile`` modules so no external binaries are required.
    """
    os.makedirs(dest, exist_ok=True)
    if format == "zip":
        import zipfile

        with zipfile.ZipFile(download_path) as zf:
            zf.extractall(dest)
    elif format == "tar.gz":
        import tarfile

        with tarfile.open(download_path, "r:gz") as tf:
            tf.extractall(dest)
    elif format == "tar":
        import tarfile

        with tarfile.open(download_path, "r:") as tf:
            tf.extractall(dest)
    else:
        raise ValueError(f"Unknown format: {format}")


# ----- HTTP download with resume -----
def _http_download(uri: str, download_path: str) -> None:
    """Stream *uri* to *download_path* with a tqdm progress bar.

    Writes to a ``<download_path>.download`` sidecar and renames on success. If
    the sidecar already exists, attempts an HTTP ``Range:`` resume; falls back
    to a full re-download when the server ignores the range request.
    """
    partial = download_path + ".download"
    resume_pos = os.path.getsize(partial) if os.path.exists(partial) else 0

    headers = {}
    if resume_pos > 0:
        headers["Range"] = f"bytes={resume_pos}-"

    try:
        with httpx.Client(follow_redirects=True) as client:
            with client.stream("GET", uri, headers=headers) as resp:
                resp.raise_for_status()
                # Server honoured the range request -> append; otherwise start over.
                if resume_pos > 0 and resp.status_code == 206:
                    mode = "ab"
                else:
                    mode = "wb"
                    resume_pos = 0
                content_length = resp.headers.get("Content-Length")
                total = None
                if content_length is not None:
                    total = int(content_length) + resume_pos
                with open(partial, mode) as f:
                    with tqdm(
                        total=total,
                        initial=resume_pos,
                        unit="B",
                        unit_scale=True,
                        desc=os.path.basename(download_path),
                        leave=False,
                    ) as bar:
                        for chunk in resp.iter_bytes(_CHUNK_SIZE):
                            f.write(chunk)
                            bar.update(len(chunk))
    except Exception as e:  # noqa: BLE001 - re-raise with a helpful message
        raise RuntimeError(
            "Automatic download failed. Please manually download the file from\n"
            f"  {uri}\nand save it to\n  {download_path}\n\n"
            f"Original error: {e}"
        ) from e

    os.replace(partial, download_path)


# ----- Scheme dispatch (PipeLines.jl:272-312) -----

def _resolve_scheme(dataset) -> str:
    """Return the effective scheme, resolving ssh/sshfs → file for local host.

    Port of PipeLines.jl:274-283: when the target host matches the local
    machine name (or its short hostname), treat the path as a local file
    rather than an SSH remote.
    """
    scheme = dataset.scheme
    if scheme in ("ssh", "sshfs"):
        local = socket.gethostname()
        host = dataset.host or ""
        if host == local or host.split(".")[0] == local:
            scheme = "file"
    return scheme


def _rsync_into(host: str, src: str, download_path: str) -> None:
    """rsync ``host:src`` into ``download_path``'s directory (preserving the
    source basename, per Julia's ``rsync`` idiom) then move the landed entry to
    *download_path*.

    The basename-preserving form is kept so directory and file sources behave as
    before; the final rename is what lets the result land at the staging path
    chosen by :func:`materialize` (which differs from the source basename)."""
    dest_dir = os.path.dirname(download_path).rstrip("/") + "/"
    subprocess.run(["rsync", "-arvzL", f"{host}:{src}", dest_dir], check=True)
    landed = os.path.join(
        os.path.dirname(download_path), os.path.basename(src.rstrip("/"))
    )
    if landed != download_path:
        _remove_path(download_path)
        os.replace(landed, download_path)


# ----- Safe materialization (atomic publish + completion marker + lock) -----

def _pid_alive(pid: int) -> bool:
    """True when *pid* names a live process (best effort)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_pid(lock: str) -> int:
    """The PID recorded in *lock*, or ``0`` when unreadable/malformed."""
    try:
        with open(lock) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _acquire_lock(lock: str) -> bool:
    """Create *lock* as an exclusive pidfile, reclaiming it when its recorded
    PID is dead.

    Returns ``True`` when this process now owns the lock (and is therefore
    responsible for removing it), ``False`` when a live process already holds it
    — in which case the caller proceeds without exclusivity rather than
    deadlocking, since the completion marker still guards against acting on a
    partial publish.
    """
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = _read_lock_pid(lock)
            if pid and not _pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.remove(lock)
                continue
            return False
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True


def _remove_path(path: str) -> None:
    """Remove *path* whether a file, symlink, or directory (no error if absent)."""
    if os.path.islink(path) or os.path.isfile(path):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def materialize(target: str, write_fn) -> None:
    """Atomically publish *target*, holding a pidfile lock for the duration.

    ``write_fn(tmp)`` populates the staging path ``<target>.tmp`` (a file or a
    directory). On success the staging path is atomically moved into place via
    :func:`os.replace` and a completion marker is created
    (``<target>/.complete`` for a directory, ``<target>.complete`` for a file).
    A ``<target>.lock`` pidfile is held while writing and removed afterwards. A
    killed or failed write leaves no completion marker and no partial final
    entry — only a leftover ``.tmp``.
    """
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = storage.tmp_path(target)
    lock = storage.lock_path(target)
    owned = _acquire_lock(lock)
    try:
        _remove_path(tmp)
        write_fn(tmp)
        _remove_path(target)
        os.replace(tmp, target)
        with open(storage.marker_path(target), "w"):
            pass
    finally:
        if owned:
            with contextlib.suppress(OSError):
                os.remove(lock)


def is_complete(target: str) -> bool:
    """True when *target* exists and carries its completion marker.

    Readers treat a missing marker as absent (an interrupted or partial publish
    that must be re-fetched).
    """
    return os.path.exists(target) and os.path.exists(storage.marker_path(target))


def _download_dataset(
    dataset,
    download_path: str,
    project_root: str = "",
    overwrite: bool = False,
    required_paths_by_ref=None,
    required_paths_ordered=None,
    python_includes=None,
) -> None:
    """Safely materialize *dataset* at *download_path*.

    The fetch itself (:func:`_fetch_into_path`) writes into a
    ``<download_path>.tmp`` staging path, which :func:`materialize` then
    atomically publishes and marks complete under a pidfile lock. A killed or
    failed fetch therefore leaves no ``.complete`` marker and no partial final
    entry.
    """
    materialize(
        download_path,
        lambda tmp: _fetch_into_path(
            dataset,
            tmp,
            project_root=project_root,
            overwrite=overwrite,
            required_paths_by_ref=required_paths_by_ref,
            required_paths_ordered=required_paths_ordered,
            python_includes=python_includes,
        ),
    )


def _fetch_into_path(
    dataset,
    download_path: str,
    project_root: str = "",
    overwrite: bool = False,
    required_paths_by_ref=None,
    required_paths_ordered=None,
    python_includes=None,
) -> None:
    os.makedirs(os.path.dirname(download_path) or ".", exist_ok=True)

    # Multi-URI batch: download each URI to a relative sub-path (PipeLines.jl:231-249).
    if dataset.uris:
        os.makedirs(download_path, exist_ok=True)
        rel_paths = _uri_relative_paths(dataset.uris)
        for uri, rel in zip(dataset.uris, rel_paths):
            if not rel:
                raise ValueError(f"Cannot determine filename from URI: {uri}")
            file_path = os.path.join(download_path, rel)
            os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
            _download_uri(uri, file_path)
        return

    # Effective fetch binding via the v1 ladder (design §6): own in-process
    # Python fetcher → shell template → plain uri. Delegation is deferred.
    kind, value = resolve_fetcher(dataset)

    # Python hook: run the resolved entry-point callable instead of a URI
    # download (replaces Julia's _run_julia, PipeLines.jl:251-257).
    if kind == "python":
        _run_python_hook(
            dataset,
            download_path,
            project_root,
            required_paths_ordered=required_paths_ordered,
            python_includes=python_includes,
            ref=value,
            args=dataset.lang_python_fetcher_args,
            kwargs=dataset.lang_python_fetcher_kwargs,
        )
        return

    # Shell hook: run the expanded template instead of a URI download
    # (PipeLines.jl:259-270). No shell=True — the command is tokenized with
    # shlex and run directly, mirroring Julia's ``Cmd(split(...))``.
    if kind == "shell":
        cmd_expanded = expand_shell_template(
            value,
            dataset,
            download_path,
            project_root,
            required_paths_by_ref=required_paths_by_ref,
            required_paths_ordered=required_paths_ordered,
        )
        subprocess.run(shlex.split(cmd_expanded), cwd=project_root or None, check=True)
        return

    if kind is None:
        # No own fetcher, no shell fetcher, no uri — the ladder bottoms out.
        raise ValueError(
            f"No fetcher available for dataset {dataset.key!r}: it declares no "
            "in-process fetcher (_LANG.python.fetcher / python=), no shell "
            "fetcher (_LANG.shell.fetcher / shell=), and no uri to download."
        )

    # kind == "uri": plain URI download — dispatch on the resolved scheme.
    scheme = _resolve_scheme(dataset)

    # Git clone: git://, ssh+git://, or https://*.git (PipeLines.jl:285-294)
    if scheme in ("git", "ssh+git") or (
        scheme == "https" and dataset.path.endswith(".git")
    ):
        if overwrite and os.path.isdir(download_path):
            shutil.rmtree(download_path)
        cmd = ["git", "clone", "--depth", "1"]
        if dataset.branch:
            cmd += ["--branch", dataset.branch]
        cmd += [dataset.uri, download_path]
        subprocess.run(cmd, check=True)
        return

    # HTTP/HTTPS (non-git)
    if scheme in ("http", "https"):
        _http_download(dataset.uri, download_path)
        return

    # SSH / rsync remote (PipeLines.jl:296-298)
    if scheme in ("ssh", "sshfs", "rsync"):
        _rsync_into(dataset.host, dataset.path, download_path)
        return

    # Local file:// (PipeLines.jl:299-302)
    if scheme == "file":
        src = dataset.path
        if src != download_path:
            remote_host = dataset.host or ""
            if remote_host and remote_host != socket.gethostname():
                _rsync_into(remote_host, src, download_path)
            elif os.path.isdir(src):
                if overwrite and os.path.isdir(download_path):
                    shutil.rmtree(download_path)
                shutil.copytree(src, download_path)
            else:
                shutil.copy2(src, download_path)
        return

    raise ValueError(
        f"Unsupported download scheme {scheme!r}. URI: {dataset.uri}"
    )


def _missing_dataset_error(dataset, path: str) -> None:
    """Raise a descriptive error when an expected dataset path is absent
    (PipeLines.jl:323-331)."""
    msg = f"Dataset file or folder not found at `{path}`."
    if dataset.uris:
        msg += " Documented URIs: " + ", ".join(f"`{u}`" for u in dataset.uris) + "."
    elif dataset.uri != "":
        msg += f" The documented URI is `{dataset.uri}`."
    raise FileNotFoundError(msg)


# ----- Download orchestration (PipeLines.jl:333-421) -----
def download_dataset(db, dataset, extract=None, overwrite: bool = False):
    """Download *dataset* (a name or :class:`DatasetEntry`) and return its path.

    Port of ``PipeLines.jl:333-401``. Dependencies declared via ``requires=``
    are downloaded first in topological order; ``entry.shell`` templates are
    expanded with the resolved dependency paths. The ``python`` hook is added
    in a later item.
    """
    if isinstance(dataset, str):
        name, dataset = search_dataset(db, dataset)
    else:
        name = _name_for_entry(db, dataset)

    # Download dependencies first, in topological order (PipeLines.jl:336-342).
    reqs = dataset.requires
    if reqs:
        order = _get_download_order(db, name)
        for dep_name in order[:-1]:
            download_dataset(db, dep_name, extract=extract, overwrite=False)

    project_root = project_root_from_paths(db.datasets_toml)

    if dataset.skip_download:
        logger.info(
            "Skipping download for dataset: %s (skip_download=true)", dataset.uri
        )
        path = get_dataset_path(
            dataset, db.datasets_folder, extract=extract, project_root=project_root
        )
        if not (os.path.isfile(path) or os.path.isdir(path)):
            _missing_dataset_error(dataset, path)
        return path

    local_path = get_dataset_path(
        dataset, db.datasets_folder, extract=extract, project_root=project_root
    )
    download_path = get_dataset_path(
        dataset, db.datasets_folder, extract=False, project_root=project_root
    )

    if not overwrite and (os.path.isfile(local_path) or os.path.isdir(local_path)):
        logger.info("Dataset already exists at: %s", local_path)
        verify_checksum(db, dataset, extract=extract, skip_if_complete=True)
        return local_path

    if overwrite or not (os.path.isfile(download_path) or os.path.isdir(download_path)):
        logger.info("Downloading dataset: %s to %s", dataset.uri, download_path)
        req_paths_by_ref: dict = {}
        req_paths_ordered: list = []
        if reqs and (dataset.shell != "" or dataset.python != ""):
            order = _get_download_order(db, name)
            for ref in reqs:
                _, dep_entry = search_dataset(db, ref)
                dep_extract = extract if extract is not None else dep_entry.extract
                req_paths_by_ref[_sanitize_ref(ref)] = get_dataset_path(
                    dep_entry, db.datasets_folder, extract=dep_extract
                )
            for dep_name in order[:-1]:
                _, dep_entry = search_dataset(db, dep_name)
                dep_extract = extract if extract is not None else dep_entry.extract
                req_paths_ordered.append(
                    get_dataset_path(dep_entry, db.datasets_folder, extract=dep_extract)
                )
        _download_dataset(
            dataset,
            download_path,
            project_root=project_root,
            overwrite=overwrite,
            required_paths_by_ref=req_paths_by_ref,
            required_paths_ordered=req_paths_ordered,
            python_includes=db.loaders_python_includes,
        )
    else:
        logger.info("Dataset already exists at: %s", download_path)

    if dataset.extract:
        if overwrite and os.path.isdir(local_path):
            shutil.rmtree(local_path, ignore_errors=True)
        logger.info("Extracting dataset to: %s", local_path)
        extract_file(download_path, local_path, dataset.format)

    if not (os.path.isfile(local_path) or os.path.isdir(local_path)):
        _missing_dataset_error(dataset, local_path)

    verify_checksum(db, dataset, extract=extract)

    return local_path


def download_datasets(db, names=None, **kwargs):
    """Download several datasets (all of them when *names* is ``None``)."""
    if names is None:
        names = list(db.datasets.keys())
    for name in names:
        download_dataset(db, name, **kwargs)


# ----- Load pipeline (PipeLines.jl:425-491) -----

def default_loader(db, format: str):
    """Return a ``path -> value`` loader for *format*, consulting ``db.loaders`` first.

    Port of ``PipeLines.jl:434-443``. Resolution: (1) a named loader in
    ``db.loaders`` whose name matches *format* case-insensitively, resolved via
    :func:`_get_loader_function`; (2) else the built-in
    :func:`datamanifest.default_loaders.default_loader`.
    """
    f = format.strip().lower()
    if not f:
        raise ValueError(
            "No loader provided and dataset format is empty. "
            "Pass a loader function, e.g. loader=lambda path: open(path).read()."
        )
    for name in db.loaders:
        if name.lower() == f:
            return _get_loader_function(db, name)
    return default_loaders.default_loader(format)


def load_dataset(db, dataset, loader=None, **kwargs):
    """Download *dataset* then load it, returning the loaded value.

    Port of ``PipeLines.jl:462-491``, extended with the v1 load ladder
    (design §6). Resolution order for the loader: explicit *loader* arg → own
    loader (``_LANG.python.loader`` / legacy ``entry.loader``) → manifest
    ``[_LANG.python.loaders][format]`` → named loader in ``db.loaders`` matching
    ``entry.format`` → built-in default loader for ``entry.format``. Loaders are
    always resolved in-process; they never delegate to a subprocess.
    For an extracted archive (``entry.extract`` and ``entry.format`` in
    :data:`COMPRESSED_FORMATS`) the path is a directory, so the empty format is
    passed to :func:`default_loader` rather than the archive format.
    """
    if isinstance(dataset, str):
        _, entry = search_dataset(db, dataset)
    else:
        entry = dataset

    path = download_dataset(db, entry, **kwargs)

    if loader is not None and loader != "":
        if isinstance(loader, str):
            if loader in db.loaders:
                loader = _get_loader_function(db, loader)
            else:
                try:
                    loader = default_loaders.default_loader(loader)
                except ValueError:
                    raise ValueError(
                        "loader must be a callable or a loader name defined in "
                        "_LOADERS, or a built-in format (csv, parquet, nc, "
                        "dimstack, md, txt, json, yaml, yml, toml, zip, tar, "
                        f'tar.gz). Got: "{loader}"'
                    )
        return loader(path)

    # v1 load ladder (design §6): own loader (own _LANG.python.loader / legacy
    # entry.loader) → manifest [_LANG.python.loaders][format]. A resolved ref is
    # always run in-process — loaders never delegate.
    loader_ref = resolve_loader_ref(db, entry)
    if loader_ref != "":
        fn = _get_loader_function(db, loader_ref)
        if entry.lang_python_loader_args or entry.lang_python_loader_kwargs:
            variables = _binding_variables(
                entry, path=path, project_root=db.get_project_root()
            )
            call_args = _substitute_vars(list(entry.lang_python_loader_args), variables)
            call_kwargs = _substitute_vars(dict(entry.lang_python_loader_kwargs), variables)
            return fn(*call_args, **call_kwargs)
        return fn(path)

    # For an extracted archive the resolved path is a directory and there is no
    # single-file format to load; return the directory path directly
    # (PipeLines.jl:488 passes the empty format, which here means "identity").
    if entry.extract and entry.format in COMPRESSED_FORMATS:
        return path

    # Built-in / named _LOADERS default for the format (last ladder rung).
    return default_loader(db, entry.format)(path)


# ----- module-level convenience wrappers (Item 17) -----
# These use *_db* (a different signature: name-only, no explicit db arg)
# to avoid shadowing the db-taking implementations above.

def _get_default_db():
    from .database import get_default_database
    return get_default_database()


def _module_register_dataset(uri: str = "", name: str = "", **kwargs):
    """Register a dataset in the default database."""
    return _get_default_db().register_dataset(uri=uri, name=name, **kwargs)


def _module_add(uri: str = "", name: str = "", **kwargs):
    """Alias for register_dataset (shorter name for interactive use)."""
    return _module_register_dataset(uri=uri, name=name, **kwargs)


def _module_delete_dataset(name: str, **kwargs):
    """Delete a dataset from the default database."""
    from .database import delete_dataset as _delete_dataset
    return _delete_dataset(_get_default_db(), name, **kwargs)


def _module_get_dataset_path(name: str, **kwargs):
    """Return the on-disk path for a dataset in the default database."""
    db = _get_default_db()
    _, entry = search_dataset(db, name)
    from .database import get_dataset_path as _gdp
    return _gdp(entry, datasets_folder=db.datasets_folder,
                project_root=db.get_project_root(), **kwargs)


def _module_download_dataset(name, **kwargs):
    """Download a dataset from the default database."""
    return download_dataset(_get_default_db(), name, **kwargs)


def _module_download_datasets(names=None, **kwargs):
    """Download datasets from the default database."""
    return download_datasets(_get_default_db(), names=names, **kwargs)


def _module_load_dataset(name, loader=None, **kwargs):
    """Download and load a dataset from the default database."""
    return load_dataset(_get_default_db(), name, loader=loader, **kwargs)
