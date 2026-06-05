"""Import datasets declared by other data-management tools into a datamanifest.

Currently supports **pooch** (https://www.fatiando.org/pooch). A pooch *registry
file* maps ``filename -> [algo:]hash`` with an optional per-file URL as a third
column — the exact grammar :meth:`pooch.Pooch.load_registry` accepts. The two
things a registry file does **not** carry (they live in the ``pooch.create(...)``
call) are the **base URL** prepended to plain filenames and the **local cache
directory**; both are supplied as arguments.

The import is offline: it parses the registry and writes standard dataset entries.
When a cache directory is given, each already-downloaded file is **adopted in
place** via a state-file record (checksum-verified) so nothing is re-downloaded.

Note on archives: pooch extracts via fetch-time *processors* (``Unzip`` / ``Untar``
/ ``Decompress``) that are not recorded in the registry, and a registry hash is
always the hash of the *raw* downloaded file. We therefore import the raw file
(``extract`` left off) so the declared ``sha256`` stays consistent with what is on
disk; add ``extract = true`` by hand if you want datamanifest to unpack it.
"""

import hashlib
import os
import shlex

from .config import logger, sha256_path
from .database import record_dataset_state


def parse_pooch_registry(path):
    """Parse a pooch registry file into ``[(filename, algo, hexhash, url)]``.

    Each non-comment line is ``filename [algo:]hash [url]`` (``shlex`` quoting,
    ``#`` line comments). *algo* defaults to ``sha256`` (pooch's default when the
    hash has no ``algo:`` prefix); *url* is ``""`` when the third column is absent.
    """
    entries = []
    with open(path, encoding="utf-8") as fin:
        for lineno, raw in enumerate(fin, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = shlex.split(line)
            if len(parts) not in (2, 3):
                raise ValueError(
                    f"{path}:{lineno}: expected 'filename hash [url]', got {line!r}"
                )
            filename, checksum = parts[0], parts[1].lower()
            url = parts[2] if len(parts) == 3 else ""
            algo, sep, hexhash = checksum.partition(":")
            if not sep:                      # bare hash → sha256 (pooch's default)
                algo, hexhash = "sha256", algo
            entries.append((filename, algo, hexhash, url))
    return entries


def _file_hash(path, algo):
    """The *algo* hex digest of the file at *path* (for non-sha256 verification)."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dataset_name(filename, taken):
    """A friendly, unique dataset name from a registry *filename* (its basename
    without extension), de-duplicated against the names already *taken*."""
    base = os.path.splitext(os.path.basename(filename.rstrip("/")))[0] or filename
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def import_pooch(db, registry_path, *, base_url="", cache_dir="", dry_run=False,
                 overwrite=False):
    """Import a pooch registry into *db*'s manifest; return a human-readable summary.

    Each registry entry becomes a dataset whose ``uri`` is the entry's own URL
    (third column) or ``base_url + filename``, and whose ``sha256`` is the entry's
    hash when it is sha256, else the sha256 computed from the cached file (when
    *cache_dir* holds it), else empty (filled on first download). When *cache_dir*
    holds the file **and its checksum matches**, the existing copy is adopted in
    place through a state-file record — no re-download.
    """
    entries = parse_pooch_registry(registry_path)
    if not base_url and any(not url for _, _, _, url in entries):
        raise ValueError(
            "a base URL is required (--base-url) for registry entries that have "
            "no explicit URL column"
        )

    taken = set(db.datasets)
    rows, declared, adopted, skipped = [], 0, 0, 0
    for filename, algo, hexhash, url in entries:
        uri = url or f"{base_url.rstrip('/')}/{filename.lstrip('/')}"
        name = _dataset_name(filename, taken)
        sha = hexhash if algo == "sha256" else ""

        cache_file = os.path.join(cache_dir, filename) if cache_dir else ""
        have = bool(cache_file) and os.path.exists(cache_file)
        verified = False
        if have:
            try:
                actual = sha256_path(cache_file) if algo == "sha256" \
                    else _file_hash(cache_file, algo)
                verified = (actual == hexhash)
                if verified and not sha:
                    sha = sha256_path(cache_file)   # fill sha256 from an md5/sha1 file
            except (OSError, ValueError):
                verified = False

        if have and not verified:
            tag = " [cache checksum mismatch — not adopted]"
            skipped += 1
        elif have:
            tag = " [adopt cache]"
        elif cache_dir:
            tag = " [not in cache]"
        else:
            tag = ""
        rows.append(f"  {name}  ->  {uri}{tag}")
        declared += 1

        if dry_run:
            continue
        _, entry = db.register_dataset(uri, name=name, sha256=sha, persist=False,
                                       overwrite=overwrite)
        if have and verified:
            record_dataset_state(db, entry, cache_file)
            adopted += 1

    if not dry_run:
        db.write(db.datasets_toml)

    verb = "Would import" if dry_run else "Imported"
    head = f"{verb} {declared} dataset(s) from pooch registry {os.path.basename(registry_path)}"
    notes = []
    if cache_dir:
        notes.append(f"{adopted} adopted from the cache (no re-download)")
    if skipped:
        notes.append(f"{skipped} cache file(s) failed checksum and were not adopted")
    if notes:
        head += " — " + "; ".join(notes)
    return head + ":\n" + "\n".join(rows)


# Tool name → importer entry point, for the pluggable ``datamanifest import <tool>``.
IMPORTERS = {"pooch": import_pooch}
