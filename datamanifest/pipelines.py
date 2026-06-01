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

import os
import shlex
import shutil
import socket
import subprocess

import httpx
from tqdm import tqdm

from .config import logger, project_root_from_paths
from .database import get_dataset_path, parse_uri_metadata, search_dataset, verify_checksum

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


def _download_dataset(
    dataset,
    download_path: str,
    project_root: str = "",
    overwrite: bool = False,
    required_paths_by_ref=None,
    required_paths_ordered=None,
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

    # Shell hook: run the expanded template instead of a URI download
    # (PipeLines.jl:259-270). No shell=True — the command is tokenized with
    # shlex and run directly, mirroring Julia's ``Cmd(split(...))``.
    if dataset.shell != "":
        cmd_expanded = expand_shell_template(
            dataset.shell,
            dataset,
            download_path,
            project_root,
            required_paths_by_ref=required_paths_by_ref,
            required_paths_ordered=required_paths_ordered,
        )
        subprocess.run(shlex.split(cmd_expanded), cwd=project_root or None, check=True)
        return

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
        dest_dir = os.path.dirname(download_path).rstrip("/") + "/"
        subprocess.run(
            ["rsync", "-arvzL", f"{dataset.host}:{dataset.path}", dest_dir],
            check=True,
        )
        return

    # Local file:// (PipeLines.jl:299-302)
    if scheme == "file":
        src = dataset.path
        if src != download_path:
            remote_host = dataset.host or ""
            if remote_host and remote_host != socket.gethostname():
                dest_dir = os.path.dirname(download_path).rstrip("/") + "/"
                subprocess.run(
                    ["rsync", "-arvzL", f"{remote_host}:{src}", dest_dir],
                    check=True,
                )
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

    if dataset.skip_download:
        logger.info(
            "Skipping download for dataset: %s (skip_download=true)", dataset.uri
        )
        path = get_dataset_path(dataset, db.datasets_folder, extract=extract)
        if not (os.path.isfile(path) or os.path.isdir(path)):
            _missing_dataset_error(dataset, path)
        return path

    local_path = get_dataset_path(dataset, db.datasets_folder, extract=extract)
    download_path = get_dataset_path(dataset, db.datasets_folder, extract=False)

    if not overwrite and (os.path.isfile(local_path) or os.path.isdir(local_path)):
        logger.info("Dataset already exists at: %s", local_path)
        verify_checksum(db, dataset, extract=extract)
        return local_path

    if overwrite or not (os.path.isfile(download_path) or os.path.isdir(download_path)):
        logger.info("Downloading dataset: %s to %s", dataset.uri, download_path)
        project_root = project_root_from_paths(db.datasets_toml)
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
