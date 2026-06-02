import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("datamanifest")

XDG_CACHE_HOME = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
DEFAULT_DATASETS_FOLDER_PATH = os.path.join(XDG_CACHE_HOME, "Datasets")
COMPRESSED_FORMATS = ["zip", "tar.gz", "tar"]
HIDE_STRUCT_FIELDS = {"host", "path", "scheme"}


def sha256_file(file_path: str) -> str:
    ctx = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            buf = f.read(65536)
            if not buf:
                break
            ctx.update(buf)
    return ctx.hexdigest()


def sha256_folder(folder_path: str) -> str:
    ctx = hashlib.sha256()
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


def sha256_path(path: str) -> str:
    if os.path.isfile(path):
        return sha256_file(path)
    elif os.path.isdir(path):
        return sha256_folder(path)
    else:
        raise FileNotFoundError(f"Path does not exist: {path}")


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


TOML_FILENAMES = ["datasets.toml", "Datasets.toml", "datamanifest.toml"]


def _find_default_toml(start: str) -> str:
    """Walk up from *start* looking for a datasets toml file.

    A directory containing any of :data:`TOML_FILENAMES` is treated as a
    project root and its toml file is returned. As a fallback, a directory
    containing ``pyproject.toml`` is also treated as a project root, in which
    case the default (lowercase ``datasets.toml``) path is returned even if
    the file does not exist yet — this lets a fresh project initialise one.
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
        "No datasets.toml or pyproject.toml found in parent directories. "
        "Cannot infer default datasets_toml path. In-memory database will be used."
    )
    return ""
