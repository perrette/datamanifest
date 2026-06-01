"""Download + load pipeline.

Port of DataManifest.jl's ``PipeLines.jl`` (download orchestration).

- ``download_dataset`` / ``download_datasets`` — orchestration + checksum.
- ``_download_dataset`` — full scheme dispatch (HTTP, git, ssh/rsync, file).
- ``extract_file`` — ``zip`` / ``tar`` / ``tar.gz`` extraction.

Multi-URI batch entries, ``requires=`` topological ordering, and the
``shell`` / ``python`` download hooks are added in later items.

Downloads are synchronous (``httpx.Client(follow_redirects=True)`` streaming
with a ``tqdm`` progress bar), mirroring Julia's ``Downloads.download`` model
(``PipeLines.jl:224-313``). A partial download is written to a ``.download``
sidecar file and resumed via an HTTP ``Range:`` header when re-run.
"""

import os
import shutil
import socket
import subprocess

import httpx
from tqdm import tqdm

from .config import logger
from .database import get_dataset_path, search_dataset, verify_checksum

_CHUNK_SIZE = 65536


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
) -> None:
    os.makedirs(os.path.dirname(download_path) or ".", exist_ok=True)

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

    Port of ``PipeLines.jl:333-401`` restricted to the HTTP path. ``requires=``
    ordering and the shell/python hooks are added in later items.
    """
    if isinstance(dataset, str):
        _, dataset = search_dataset(db, dataset)

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
        _download_dataset(dataset, download_path, overwrite=overwrite)
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
