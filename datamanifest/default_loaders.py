"""Back-compat shim: ``datamanifest.default_loaders`` moved to
:mod:`datamanifest.store.loaders` (Layer 0 substrate extraction).

Importing ``datamanifest.default_loaders`` continues to work unchanged
(including the ``default_loaders.importlib`` / ``default_loaders.default_loader``
patch points used by the tests). New code should import from
:mod:`datamanifest.store.loaders`.
"""

from .store.loaders import *  # noqa: F401,F403  (public API re-export)
from .store.loaders import (  # noqa: F401  (test patch points / helpers)
    default_loader,
    importlib,
    tomllib,
)
