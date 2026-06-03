"""Produced-dataset sidecars: ``config.toml`` / ``metadata.toml`` I/O.

A produced artifact directory is *self-describing*:

- ``config.toml`` records the re-hashable hash inputs (the key table) plus a
  ``[_META]`` block (``schema`` / ``cachetype`` / ``hash``), so any tool can
  recompute :func:`param_hash` over the key table and confirm the directory's
  identity. The key table is written at the root and first (TOML requires
  root-table keys to precede any table header), so ``[_META]`` comes last;
  reading back, the key table is every root key except ``[_META]``.
- ``metadata.toml`` records provenance (``created`` / ``tool`` / ``host`` /
  ``user`` / ``[git]`` / optional ``[origin]``). It is **never hashed** and is
  **never an authority for cache validity**; it is **write-if-absent** (a cache
  hit does not re-stamp it).

Both carry their own ``_META.schema = 1`` (additive over the manifest's schema,
which stays ``1``).
"""

import datetime
import getpass
import os
import socket
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
import tomli_w

from ..store import sort_recursive as _canonical
from ._hash import key_table_from_kwargs, param_hash  # noqa: F401  (param_hash re-used)

__all__ = [
    "CONFIG_NAME",
    "METADATA_NAME",
    "write_config",
    "read_config",
    "config_key_table",
    "config_is_valid",
    "write_metadata",
    "read_metadata",
]

CONFIG_NAME = "config.toml"
METADATA_NAME = "metadata.toml"


# ``_canonical`` is the Layer 0 canonical key sort (``store.sort_recursive``),
# imported above — produced sidecars share the same byte ordering as
# ``datasets.toml`` / ``cached.toml`` without duplicating the sort.


# ----- config.toml -----------------------------------------------------------

def write_config(directory: str, cachetype: str, hash: str, key_table: dict,
                 version: str = "") -> str:
    """Write ``<directory>/config.toml`` for a produced artifact.

    The file is the *key table* verbatim (the hash-affecting parameters) plus a
    ``[_META]`` block (``schema``, ``cachetype``, ``hash``, and the spec-v3
    recipe ``version`` when set), so any tool can recompute :func:`param_hash`
    over the key table and confirm the directory's identity. ``version`` is
    recorded in ``[_META]`` (never in the key table), so it does not affect the
    recomputed hash. Returns the path written.
    """
    os.makedirs(directory, exist_ok=True)
    data = dict(key_table)
    meta = {"schema": 1, "cachetype": cachetype, "hash": hash}
    if version:
        meta["version"] = version
    data["_META"] = meta
    path = os.path.join(directory, CONFIG_NAME)
    with open(path, "wb") as f:
        tomli_w.dump(_canonical(data), f)
    return path


def read_config(directory: str) -> dict:
    """Read a produced artifact's ``config.toml`` (its directory or the file)."""
    path = (
        directory
        if directory.endswith(CONFIG_NAME)
        else os.path.join(directory, CONFIG_NAME)
    )
    with open(path, "rb") as f:
        return tomllib.load(f)


def config_key_table(config: dict) -> dict:
    """Return the hash-affecting key table from a parsed ``config.toml`` (drops
    the ``[_META]`` block)."""
    return {k: v for k, v in config.items() if k != "_META"}


def config_is_valid(directory: str) -> bool:
    """True iff *directory* has a ``config.toml`` whose recorded ``_META.hash``
    equals the recomputed :func:`param_hash` of its key table.

    A directory that is missing, unreadable, or whose recomputed hash differs
    from ``_META.hash`` is **not** a valid cache hit (the caller re-produces).
    """
    try:
        config = read_config(directory)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    recorded = config.get("_META", {}).get("hash")
    try:
        return recorded == param_hash(config_key_table(config))
    except ValueError:
        return False


# ----- metadata.toml ---------------------------------------------------------

def _tool_version() -> str:
    """Return this tool's version string (lazy import to avoid an import cycle)."""
    try:
        from .. import __version__

        return __version__
    except Exception:  # noqa: BLE001 - provenance is best-effort
        return "unknown"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - provenance is best-effort
        return ""


def _git_provenance(project_root: str) -> dict:
    """Return ``{commit, branch, dirty}`` when *project_root* is a git repo, else
    an empty dict (provenance is best-effort and never required)."""
    if not project_root:
        return {}

    def _git(*args):
        return subprocess.run(
            ["git", "-C", project_root, *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    try:
        commit = _git("rev-parse", "HEAD")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
    except (subprocess.CalledProcessError, OSError):
        return {}
    return {"commit": commit, "branch": branch, "dirty": dirty}


def write_metadata(
    directory: str,
    *,
    project_root: str = "",
    origin: dict = None,
    created: str = "",
    write_if_absent: bool = True,
) -> str:
    """Write ``<directory>/metadata.toml`` (provenance / audit only).

    Records ``created`` (RFC 3339 UTC), ``tool`` (``datamanifestpy <version>``),
    ``host``, ``user``, a ``[git]`` block (commit/branch/dirty) when
    *project_root* is a repo, and an ``[origin]`` block when *origin* is given.
    None of this is ever hashed or an authority for cache validity.

    **Write-if-absent** (the spec default): a cache hit does not re-stamp an
    existing ``metadata.toml``. When ``write_if_absent`` is true and the file
    already exists, this returns its path without rewriting. Returns the path.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, METADATA_NAME)
    if write_if_absent and os.path.exists(path):
        return path
    if not created:
        created = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    meta = {
        "_META": {"schema": 1},
        "created": created,
        "tool": f"datamanifestpy {_tool_version()}",
        "host": socket.gethostname(),
        "user": _current_user(),
    }
    git = _git_provenance(project_root)
    if git:
        meta["git"] = git
    if origin:
        meta["origin"] = dict(origin)
    with open(path, "wb") as f:
        tomli_w.dump(_canonical(meta), f)
    return path


def read_metadata(directory: str) -> dict:
    """Read a produced artifact's ``metadata.toml`` (its directory or the file)."""
    path = (
        directory
        if directory.endswith(METADATA_NAME)
        else os.path.join(directory, METADATA_NAME)
    )
    with open(path, "rb") as f:
        return tomllib.load(f)
