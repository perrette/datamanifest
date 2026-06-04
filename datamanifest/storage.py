"""Back-compat shim: ``datamanifest.storage`` moved to
:mod:`datamanifest.store.locations` (Layer 0 substrate extraction).

Importing ``datamanifest.storage`` continues to work unchanged. New code should
import from :mod:`datamanifest.store` / :mod:`datamanifest.store.locations`.
"""

from .store.locations import *  # noqa: F401,F403  (public API re-export)
from .store.locations import (  # noqa: F401  (private names used by database.py)
    _folder_is_defined,
    _folder_raw,
    _interpolate,
    _patched_environ,
)
from .store.locations import __all__  # noqa: F401
