"""Layer 0 storage substrate.

The cross-cutting primitives both the fetch layer (``datamanifest.database`` /
``datamanifest.pipelines``) and the cache layer (``datamanifest.cache``) consume:

- **location resolution** (``locations``) — host/profile-aware ``$``-folder
  selector resolution + the safe-materialization path helpers;
- **safe materialization** (``materialize``) — atomic publish + pidfile lock +
  completion marker;
- **loader dispatch** (``loaders``) — format → ``path -> value`` loader ladder.

The import arrow points **up only**: this package imports nothing from the fetch
or cache layers (stdlib + platformdirs + lazy format readers only).
"""

from . import loaders, locations, materialize
from .loaders import default_loader
from .locations import (
    folder_root,
    legacy_data_root,
    lock_path,
    marker_path,
    project_default,
    resolve_selector,
    store_root,
    tmp_path,
)
from .materialize import is_complete, remove_path
from .materialize import materialize as materialize_target

__all__ = [
    "loaders",
    "locations",
    "materialize",
    "default_loader",
    "folder_root",
    "legacy_data_root",
    "lock_path",
    "marker_path",
    "project_default",
    "resolve_selector",
    "store_root",
    "tmp_path",
    "is_complete",
    "remove_path",
    "materialize_target",
]
