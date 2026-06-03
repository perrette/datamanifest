"""Layer 1b — the produce-or-load (``@cached``) cache layer (Phase 1).

A produced dataset is identified by its **keyword parameters** (a parameter
hash), materialized once into the ``$cache`` folder, and reloaded on subsequent
calls. This layer reuses the Layer 0 substrate (``datamanifest.store``) for
location resolution, safe materialization, and loader dispatch.

Phase 1 ships the ``@cached`` decorator plus the per-artifact ``config.toml`` /
``metadata.toml`` sidecars. The ``cached.toml`` index and garbage collection are
Phase 2 and are intentionally **not** present here.

**Import rule (hard invariant):** this package imports only ``datamanifest.store``
(+ stdlib). It never imports the fetch layer (the manifest / download modules) —
the fetch layer never sees a produced dataset.
"""

from ._decorator import cached
from ._hash import key_table_from_kwargs, param_hash
from ._sidecars import (
    config_is_valid,
    config_key_table,
    read_config,
    read_metadata,
    write_config,
    write_metadata,
)

__all__ = [
    "cached",
    "param_hash",
    "key_table_from_kwargs",
    "write_config",
    "read_config",
    "config_key_table",
    "config_is_valid",
    "write_metadata",
    "read_metadata",
]
