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

from . import loaders, locations, materialize, serialize
from .loaders import default_loader
from .locations import (
    composed_path,
    content_prefix,
    content_scope,
    folder_base,
    folder_root,
    legacy_data_root,
    lock_path,
    marker_path,
    project_default,
    project_id,
    resolve_selector,
    store_root,
    tmp_path,
)
from .materialize import is_complete, remove_path
from .materialize import materialize as materialize_target
from .serialize import sort_recursive

__all__ = [
    "loaders",
    "locations",
    "materialize",
    "serialize",
    "default_loader",
    "composed_path",
    "content_prefix",
    "content_scope",
    "folder_base",
    "folder_root",
    "legacy_data_root",
    "lock_path",
    "marker_path",
    "project_default",
    "project_id",
    "resolve_selector",
    "store_root",
    "tmp_path",
    "is_complete",
    "remove_path",
    "materialize_target",
    "sort_recursive",
]
