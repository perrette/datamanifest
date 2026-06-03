"""Parameter-hash keying for produced (function-backed) datasets.

A *produced* dataset is identified **by its keyword parameters**: its hash
inputs are the keyword arguments passed to its producing function (minus
``_``-prefixed control keys), and its cache key is
``<cachetype>/<param_hash(kwargs)>``. This is the normative counterpart to a
*fetched* dataset's content identity (the ``sha256`` of its bytes).

The hash is the lowercase-hex SHA-256 of the **canonical JSON** encoding of the
key table::

    json.dumps(key_table, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False).encode("utf-8")

``sort_keys=True`` makes the encoding insensitive to key order; the compact
separators and explicit ``ensure_ascii=False`` pin the byte sequence so every
implementation of the spec produces the identical digest.

Only stable, exactly-representable scalar types may appear in a key table:
strings, ints, bools, and ``None``-free arrays and objects. Floats and ``None``
are **rejected** (``ValueError``) anywhere in the structure — floats because
their textual representation is not portable, ``None`` because an absent key and
a ``null`` key must not collide.

Reference vector::

    param_hash({"grid": "5x5", "skip_models": ["CESM.*", "FGOALS.*"]})
    == "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b"
"""

import hashlib
import json

__all__ = [
    "param_hash",
    "key_table_from_kwargs",
]


def _reject_floats_and_none(value, path=""):
    """Recursively assert that *value* contains no ``float`` or ``None``.

    ``bool`` is intentionally accepted (it is a valid hash input even though it
    is an ``int`` subclass). Raises ``ValueError`` naming the offending path.
    """
    if value is None:
        raise ValueError(
            f"None is not a valid parameter-hash input (at {path or '<root>'})"
        )
    # bool is a subclass of int and is allowed; guard floats explicitly.
    if isinstance(value, float):
        raise ValueError(
            f"float is not a valid parameter-hash input (at {path or '<root>'}); "
            "hash inputs are strings/ints/bools/arrays/objects only"
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_floats_and_none(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_floats_and_none(v, f"{path}[{i}]")


def param_hash(key_table: dict) -> str:
    """Return the lowercase-hex SHA-256 of the canonical JSON of *key_table*.

    Raises ``ValueError`` if a ``float`` or ``None`` appears anywhere in the
    table (see module docstring).
    """
    _reject_floats_and_none(key_table)
    canonical = json.dumps(
        key_table, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def key_table_from_kwargs(kwargs: dict) -> dict:
    """Return *kwargs* with every ``_``-prefixed key removed.

    ``_``-prefixed keys are control/escape-hatch arguments (e.g. ``_parallel``)
    that must not affect the parameter hash.
    """
    return {k: v for k, v in kwargs.items() if not k.startswith("_")}
