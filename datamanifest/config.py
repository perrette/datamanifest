import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("datamanifest")

XDG_CACHE_HOME = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
DEFAULT_DATASETS_FOLDER_PATH = os.path.join(XDG_CACHE_HOME, "Datasets")
COMPRESSED_FORMATS = ["zip", "tar.gz", "tar"]
HIDE_STRUCT_FIELDS = {"host", "path", "scheme"}


def hash_file(file_path: str, algo: str = "sha256") -> str:
    ctx = hashlib.new(algo)
    with open(file_path, "rb") as f:
        while True:
            buf = f.read(65536)
            if not buf:
                break
            ctx.update(buf)
    return ctx.hexdigest()


def hash_folder(folder_path: str, algo: str = "sha256") -> str:
    ctx = hashlib.new(algo)
    for root, dirs, files in os.walk(folder_path):
        dirs.sort()
        for fname in sorted(files):
            with open(os.path.join(root, fname), "rb") as f:
                while True:
                    data = f.read(65536)
                    if not data:
                        break
                    ctx.update(data)
    return ctx.hexdigest()


def hash_path(path: str, algo: str = "sha256") -> str:
    if os.path.isfile(path):
        return hash_file(path, algo)
    elif os.path.isdir(path):
        return hash_folder(path, algo)
    else:
        raise FileNotFoundError(f"Path does not exist: {path}")


# Back-compat wrappers (sha256 is the default checksum algorithm; the state file
# and any sha256-specific caller keep using these).
def sha256_file(file_path: str) -> str:
    return hash_file(file_path, "sha256")


def sha256_folder(folder_path: str) -> str:
    return hash_folder(folder_path, "sha256")


def sha256_path(path: str) -> str:
    return hash_path(path, "sha256")


def get_extract_path(path: str) -> str:
    for fmt in COMPRESSED_FORMATS:
        if path.endswith(f".{fmt}"):
            return path[: -(len(fmt) + 1)]
        if f"?format={fmt}" in path:
            return path.replace(f"?format={fmt}", "?").rstrip("?")
    return path + ".d"


def project_root_from_paths(datasets_toml_path: str, current_project_path=None) -> str:
    if datasets_toml_path:
        return os.path.abspath(os.path.dirname(datasets_toml_path))
    if current_project_path:
        return os.path.abspath(os.path.dirname(current_project_path))
    return ""


# The canonical manifest name is ``datamanifest.toml``; ``DataManifest.toml``,
# ``datasets.toml`` and ``Datasets.toml`` are recognized legacy aliases (read
# when present). The discovery order is shared with the Julia tool: the first
# existing name wins.
TOML_FILENAMES = ["datamanifest.toml", "DataManifest.toml", "datasets.toml",
                  "Datasets.toml"]


def _find_default_toml(start: str) -> str:
    """Walk up from *start* looking for a datasets toml file.

    A directory containing any of :data:`TOML_FILENAMES` is treated as a
    project root and its toml file is returned. As a fallback, a directory
    containing ``pyproject.toml`` is also treated as a project root, in which
    case the canonical ``datamanifest.toml`` path is returned even if the file
    does not exist yet — this lets a fresh project initialise one.
    """
    current = Path(start).resolve()
    while True:
        for name in TOML_FILENAMES:
            candidate = current / name
            if candidate.is_file():
                return str(candidate)
        if (current / "pyproject.toml").is_file():
            return str(current / TOML_FILENAMES[0])
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ""


def get_default_toml() -> str:
    for envvar in ["DATAMANIFEST_TOML", "DATASETS_TOML"]:
        val = os.environ.get(envvar, "")
        if val:
            if not os.path.isfile(val):
                logger.warning(
                    "Environment variable %s points to a non-existing file: %s.",
                    envvar,
                    val,
                )
            return val

    toml = _find_default_toml(os.getcwd())
    if toml:
        return toml

    logger.warning(
        "No datamanifest.toml or pyproject.toml found in parent directories. "
        "Cannot infer default manifest path. In-memory database will be used."
    )
    return ""
