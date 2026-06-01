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


def _find_project_root(start: str) -> str:
    current = Path(start).resolve()
    while True:
        if (current / "pyproject.toml").exists():
            return str(current)
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

    root = _find_project_root(os.getcwd())
    if root:
        candidates = [
            os.path.join(root, "datasets.toml"),
            os.path.join(root, "Datasets.toml"),
            os.path.join(root, "datamanifest.toml"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return candidates[0]
    else:
        logger.warning(
            "No pyproject.toml found in parent directories. "
            "Cannot infer default datasets_toml path. In-memory database will be used."
        )
        return ""
