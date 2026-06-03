"""The depot-level usage log + the produced-artifact last-access stamp.

Two best-effort, advisory facilities, both stdlib + ``platformdirs`` only (no
fetch/cache-layer imports):

1. **Usage log** — a single ``usage.toml`` recording every ``datasets.toml`` /
   ``cached.toml`` index path the cache layer has read, each with a
   ``last_seen`` RFC-3339 UTC stamp. The old automatic GC used this to find the
   live root set; spec-v3 retires the collector in favour of the explicit
   ``datamanifest list`` maintenance surface, but the log is still stamped on a
   produce (harmless, and a cheap index of where artifacts were registered).

2. **Last-access** — :func:`last_access` reports when a produced artifact was
   last *read*, and :func:`touch_last_access` bumps that stamp. The
   implementation is the filesystem access time of the artifact directory: the
   OS updates it whenever the artifact's value is loaded (a ``@cached`` hit opens
   the data file), so "touched on read" comes for free. It is **advisory**: a
   ``noatime`` mount or a manual ``utime`` can defeat it, and nothing depends on
   it for correctness.

**Usage-log location (this implementation's choice).** ``platformdirs.user_state_dir
("datamanifest")/usage.toml`` — a per-user, machine-local *state* directory (on
Linux ``$XDG_STATE_HOME/datamanifest``, default ``~/.local/state/datamanifest``).
``DATAMANIFEST_USAGE_LOG`` overrides the path (used by tests and for an explicit
per-project log).
"""

import datetime
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
import tomli_w
import platformdirs

from ..store import sort_recursive

__all__ = [
    "USAGE_LOG_NAME",
    "usage_log_path",
    "record_path",
    "read_usage",
    "known_paths",
    "prune_missing",
    "iso_from_mtime",
    "last_access",
    "touch_last_access",
]

USAGE_LOG_NAME = "usage.toml"
_ENV_OVERRIDE = "DATAMANIFEST_USAGE_LOG"


def usage_log_path(env=os.environ) -> str:
    """The absolute path of the depot usage log.

    ``$DATAMANIFEST_USAGE_LOG`` overrides; otherwise
    ``platformdirs.user_state_dir("datamanifest")/usage.toml``.
    """
    override = env.get(_ENV_OVERRIDE)
    if override:
        return override
    return os.path.join(platformdirs.user_state_dir("datamanifest"), USAGE_LOG_NAME)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_mtime(path: str) -> str:
    """RFC-3339 UTC stamp of *path*'s modification time (empty on error)."""
    try:
        ts = os.path.getmtime(path)
    except OSError:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def last_access(path: str) -> str:
    """The last time a produced artifact at *path* was read, as an RFC-3339 UTC
    stamp (empty when *path* is absent).

    Implemented as the filesystem **access time** of the artifact directory — the
    OS bumps it whenever the artifact's value is loaded (a ``@cached`` hit opens
    the data file). Advisory only: a ``noatime`` mount makes atime track mtime,
    in which case this still returns a sane (if coarse) stamp.
    """
    try:
        ts = os.path.getatime(path)
    except OSError:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def touch_last_access(path: str) -> None:
    """Bump *path*'s last-access stamp to now (best-effort, never raises).

    Sets the access time while preserving the modification time, so a
    touch-on-read does not masquerade as a fresh write. Silently does nothing
    when *path* is gone or the filesystem refuses the update (advisory).
    """
    try:
        mtime = os.path.getmtime(path)
        os.utime(path, (datetime.datetime.now().timestamp(), mtime))
    except OSError:
        pass


def read_usage(env=os.environ) -> dict:
    """Return the parsed usage log as ``{abspath: {"last_seen": <iso>}}`` (empty
    when the log does not exist or is unreadable)."""
    path = usage_log_path(env)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    paths = data.get("paths", {})
    return paths if isinstance(paths, dict) else {}


def record_path(index_path: str, *, env=os.environ, now: str = "") -> str:
    """Record *index_path* (a ``datasets.toml`` / ``cached.toml``) as seen now.

    The path is stored absolute, with a ``last_seen`` RFC-3339 UTC stamp. Called
    whenever the cache layer reads (or writes) an index it should keep as a GC
    root. Returns the absolute path recorded.
    """
    abspath = os.path.abspath(index_path)
    paths = read_usage(env)
    paths[abspath] = {"last_seen": now or _now_iso()}
    log = usage_log_path(env)
    os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
    with open(log, "wb") as f:
        tomli_w.dump(sort_recursive({"paths": paths}), f)
    return abspath


def known_paths(env=os.environ) -> list:
    """The list of recorded index/manifest paths (order-stable, sorted)."""
    return sorted(read_usage(env).keys())


def prune_missing(env=os.environ) -> list:
    """Drop usage-log entries whose path no longer exists on disk.

    Returns the pruned paths. A removed manifest/index is no longer a root, so
    forgetting it lets GC reclaim what it used to keep alive.
    """
    paths = read_usage(env)
    gone = [p for p in paths if not os.path.isfile(p)]
    if not gone:
        return []
    for p in gone:
        paths.pop(p, None)
    log = usage_log_path(env)
    os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
    with open(log, "wb") as f:
        tomli_w.dump(sort_recursive({"paths": paths}), f)
    return gone
