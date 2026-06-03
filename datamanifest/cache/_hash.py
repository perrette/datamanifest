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

Hash-input values are strings, ints, bools, **finite floats**, and arrays/objects
of those. Two values are **rejected** (``ValueError``) anywhere in the structure:
``None`` (an absent key and a ``null`` key must not collide) and **non-finite**
floats (``nan`` / ``inf``: not representable in JSON and not hash-stable). Finite
floats are encoded by Python's JSON number formatter (shortest round-tripping
``repr``); within this implementation the same value always yields the same digest.

Reference vector::

    param_hash({"grid": "5x5", "skip_models": ["CESM.*", "FGOALS.*"]})
    == "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b"
"""

import hashlib
import json
import math

__all__ = [
    "param_hash",
    "key_table_from_kwargs",
]


def _reject_invalid(value, path=""):
    """Recursively assert *value* is a valid parameter-hash input.

    Accepted: strings, ints, ``bool`` (an ``int`` subclass, intentionally
    allowed), **finite** floats, and arrays/objects of those. Rejected
    (``ValueError`` naming the offending path): ``None`` (an absent key and a
    ``null`` key must not collide) and non-finite floats (``nan`` / ``inf``,
    which JSON cannot represent and which are not hash-stable).
    """
    if value is None:
        raise ValueError(
            f"None is not a valid parameter-hash input (at {path or '<root>'})"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(
            f"non-finite float {value!r} is not a valid parameter-hash input "
            f"(at {path or '<root>'}); use a finite float or a string"
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_invalid(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_invalid(v, f"{path}[{i}]")


def param_hash(key_table: dict) -> str:
    """Return the lowercase-hex SHA-256 of the canonical JSON of *key_table*.

    Finite floats are accepted; raises ``ValueError`` if ``None`` or a
    non-finite float appears anywhere in the table (see module docstring).
    """
    _reject_invalid(key_table)
    canonical = json.dumps(
        key_table, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def key_table_from_kwargs(kwargs: dict) -> dict:
    """Return *kwargs* with every ``_``-prefixed key removed.

    ``_``-prefixed keys are control/escape-hatch arguments (e.g. ``_parallel``)
    that must not affect the parameter hash.
    """
    return {k: v for k, v in kwargs.items() if not k.startswith("_")}
