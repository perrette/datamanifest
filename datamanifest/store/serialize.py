"""Canonical TOML key ordering (Layer 0 substrate).

The normative cross-tool serialization rule (``byte-identity`` capability) is a
single recursive key sort: every mapping's keys are emitted in Unicode
code-point lexicographic order at every nesting level. This is the one home for
that ordering — both the fetch layer (``datamanifest.database`` / ``Database.write``)
and the cache layer (``datamanifest.cache`` — ``cached.toml`` index and the
``config.toml`` / ``metadata.toml`` sidecars) route their writes through it, so
the byte form is identical regardless of which layer produced the file.

Keeping it in ``store`` lets the cache layer obtain canonical ordering without
importing ``database`` (the one-way import arrow: ``cache`` → ``store`` only).
"""

__all__ = ["sort_recursive"]


def sort_recursive(obj):
    """Sort dict keys recursively by Unicode code point at every nesting level.

    Lists are recursed into but never reordered (array element order is
    significant data); scalars are returned unchanged.
    """
    if isinstance(obj, dict):
        return {k: sort_recursive(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [sort_recursive(v) for v in obj]
    return obj
