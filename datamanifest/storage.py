"""Back-compat shim: ``datamanifest.storage`` moved to
:mod:`datamanifest.store.locations` (Layer 0 substrate extraction).

Importing ``datamanifest.storage`` continues to work unchanged. New code should
import from :mod:`datamanifest.store` / :mod:`datamanifest.store.locations`.
"""

from .store.locations import *  # noqa: F401,F403  (public API re-export)
from .store.locations import (  # noqa: F401  (private helpers used by callers)
    _is_symbol,
    _patched_environ,
    _symbol_raw,
)
from .store.locations import __all__  # noqa: F401
