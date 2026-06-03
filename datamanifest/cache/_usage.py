"""The depot-level usage log — known index/manifest paths + last-seen.

Garbage collection is a **root-reachability** collector: the roots are the
``datasets.toml`` / ``cached.toml`` index files. To find the live root set
without scanning the whole filesystem, the cache layer keeps a depot-level
**usage log** — a single ``usage.toml`` recording every index/manifest path it
has read, each with a ``last_seen`` timestamp (RFC 3339 UTC). This is the
analogue of Julia's ``manifest_usage.toml``. The spec leaves the *mechanics*
implementation-defined; the *concept* (the set of paths + a last-seen stamp) is
fixed.

**Location (this implementation's choice).** The log lives at
``platformdirs.user_state_dir("datamanifest")/usage.toml`` — a per-user,
machine-local *state* directory (on Linux ``$XDG_STATE_HOME/datamanifest``,
default ``~/.local/state/datamanifest``). State is the right category: it is
neither user data (the datasets themselves) nor a reclaimable cache (it must
survive a cache wipe, or GC would forget its roots). ``DATAMANIFEST_USAGE_LOG``
overrides the path (used by tests and for an explicit per-project log).

Layering: stdlib + ``platformdirs`` only; no fetch/cache-layer imports.
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
