"""Import datasets declared by other tools / data repositories into a datamanifest.

Every importer reduces its source to a list of **dataset specs** and hands them to
:func:`_declare_specs`, which declares each as a standard manifest entry and — when
a local copy is given and its checksum matches — adopts it in place via a state-file
record (no re-download). So all sources share one declaration + cache-adoption path.

Two CLI verbs sit on top (see ``datamanifest/cli.py``):

- ``add <reference>`` — a pointer to data (a direct URL, or a **Zenodo** DOI/record
  that expands to its files). :func:`import_zenodo`.
- ``import <tool> <file>`` — another tool's catalog: **pooch** registry, a generic
  **csv** / **urls** list, **intake** catalog, or **DVC** files. :data:`IMPORTERS`.

None of this changes the on-the-wire download protocol: every entry uses an
already-supported scheme (HTTP, git, ssh, file). Sources that publish md5 (Zenodo,
DVC) declare no ``sha256``; it is computed from the file on adoption / first download.
"""

import csv as _csv
import fnmatch
import hashlib
import os
import re
import shlex

from .config import logger, sha256_path
from .database import record_dataset_state


# ----- shared spec → manifest entry + in-place cache adoption ----------------

def _file_hash(path, algo):
    """The *algo* hex digest of the file at *path* (for non-sha256 verification)."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_name(base, taken):
    """*base* made unique against the names already *taken* (``foo`` → ``foo_2`` …)."""
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def _name_from_path(path, taken):
    """A friendly unique dataset name from a path/URL: its basename without
    extension (query string stripped)."""
    base = os.path.splitext(os.path.basename(path.split("?")[0].rstrip("/")))[0]
    return _unique_name(base or path, taken)


def _declare_specs(db, specs, *, dry_run=False, overwrite=False):
    """Declare each *spec* (a dict) as a manifest entry; adopt a present, verified
    cached copy in place. Returns ``(rows, declared, adopted, skipped)``.

    Spec keys: ``name``, ``uri`` (required); ``sha256`` (declared, may be ``""``);
    ``cache_file`` (a local copy to adopt, or ``""``); ``hash_algo``/``hash_value``
    (to verify ``cache_file``); optional ``doi`` / ``description`` / ``extract``.
    """
    rows, declared, adopted, skipped = [], 0, 0, 0
    for s in specs:
        name, uri = s["name"], s["uri"]
        sha = s.get("sha256", "") or ""
        cache_file = s.get("cache_file", "") or ""
        algo = s.get("hash_algo", "sha256") or "sha256"
        expected = s.get("hash_value", "") or ""

        have = bool(cache_file) and os.path.exists(cache_file)
        verified = None                      # None: no checksum to check against
        if have:
            try:
                actual_sha = sha256_path(cache_file)
                if expected:
                    actual = actual_sha if algo == "sha256" \
                        else _file_hash(cache_file, algo)
                    verified = (actual == expected)
                if verified is not False and not sha:
                    sha = actual_sha          # fill sha256 from the local file
            except (OSError, ValueError):
                verified = False

        if have and verified is False:
            tag, skipped = " [cache checksum mismatch — not adopted]", skipped + 1
        elif have:
            tag = " [adopt cache]"
        elif cache_file:
            tag = " [not in cache]"
        else:
            tag = ""
        rows.append(f"  {name}  ->  {uri}{tag}")
        declared += 1

        if dry_run:
            continue
        kwargs = {"sha256": sha}
        for f in ("doi", "description"):
            if s.get(f):
                kwargs[f] = s[f]
        if s.get("extract"):
            kwargs["extract"] = True
        _, entry = db.register_dataset(uri, name=name, persist=False,
                                       overwrite=overwrite, **kwargs)
        if have and verified is not False:
            record_dataset_state(db, entry, cache_file)
            adopted += 1

    if not dry_run:
        db.write(db.datasets_toml)
    return rows, declared, adopted, skipped


def _summary(source, rows, declared, adopted, skipped, *, dry_run, cache):
    """A human-readable import summary line + per-dataset rows."""
    verb = "Would import" if dry_run else "Imported"
    head = f"{verb} {declared} dataset(s) from {source}"
    notes = []
    if cache:
        notes.append(f"{adopted} adopted from the cache (no re-download)")
    if skipped:
        notes.append(f"{skipped} cache file(s) failed checksum and were not adopted")
    if notes:
        head += " — " + "; ".join(notes)
    return head + ":\n" + "\n".join(rows)


# ----- pooch ------------------------------------------------------------------

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


def import_pooch(db, registry_path, *, base_url="", cache_dir="", dry_run=False,
                 overwrite=False):
    """Import a pooch registry. ``uri`` is the entry's URL or ``base_url + filename``;
    ``--cache-dir`` (pooch's ``os_cache``) adopts already-downloaded files in place."""
    entries = parse_pooch_registry(registry_path)
    if not base_url and any(not url for _, _, _, url in entries):
        raise ValueError(
            "a base URL is required (--base-url) for registry entries that have "
            "no explicit URL column"
        )
    taken, specs = set(db.datasets), []
    for filename, algo, hexhash, url in entries:
        specs.append({
            "name": _name_from_path(filename, taken),
            "uri": url or f"{base_url.rstrip('/')}/{filename.lstrip('/')}",
            "sha256": hexhash if algo == "sha256" else "",
            "cache_file": os.path.join(cache_dir, filename) if cache_dir else "",
            "hash_algo": algo, "hash_value": hexhash,
        })
    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    return _summary(f"pooch registry {os.path.basename(registry_path)}",
                    rows, declared, adopted, skipped,
                    dry_run=dry_run, cache=bool(cache_dir))


# ----- generic CSV / URL list -------------------------------------------------

def import_csv(db, csv_path, *, base_url="", cache_dir="", dry_run=False,
               overwrite=False):
    """Import a CSV with a header row including at least a ``url`` column, plus
    optional ``name`` / ``sha256`` (case-insensitive). A relative ``url`` is joined
    onto ``--base-url``; ``--cache-dir`` adopts ``<dir>/<basename(url)>`` in place."""
    taken, specs = set(db.datasets), []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        if "url" not in cols:
            raise ValueError(
                f"{csv_path}: CSV needs a 'url' column (got {reader.fieldnames})")
        for row in reader:
            url = (row.get(cols["url"]) or "").strip()
            if not url:
                continue
            if base_url and "://" not in url:
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"
            sha = (row.get(cols["sha256"], "").strip() if "sha256" in cols else "")
            raw = (row.get(cols["name"], "").strip() if "name" in cols else "")
            name = _unique_name(raw, taken) if raw else _name_from_path(url, taken)
            basename = os.path.basename(url.split("?")[0])
            specs.append({
                "name": name, "uri": url, "sha256": sha,
                "cache_file": os.path.join(cache_dir, basename) if cache_dir else "",
                "hash_algo": "sha256", "hash_value": sha,
            })
    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    return _summary(f"CSV {os.path.basename(csv_path)}", rows, declared, adopted,
                    skipped, dry_run=dry_run, cache=bool(cache_dir))


def import_urls(db, list_path, *, base_url="", cache_dir="", dry_run=False,
                overwrite=False):
    """Import a plain list of URLs/paths, one per line (``#`` comments). A relative
    line is joined onto ``--base-url``; ``--cache-dir`` adopts by basename."""
    taken, specs = set(db.datasets), []
    with open(list_path, encoding="utf-8") as f:
        for raw in f:
            url = raw.strip()
            if not url or url.startswith("#"):
                continue
            if base_url and "://" not in url:
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"
            basename = os.path.basename(url.split("?")[0])
            specs.append({
                "name": _name_from_path(url, taken), "uri": url,
                "cache_file": os.path.join(cache_dir, basename) if cache_dir else "",
            })
    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    return _summary(f"URL list {os.path.basename(list_path)}", rows, declared,
                    adopted, skipped, dry_run=dry_run, cache=bool(cache_dir))


# ----- Zenodo (by DOI / record URL) ------------------------------------------

def zenodo_record_id(ref):
    """The Zenodo record id from a DOI (``10.5281/zenodo.<id>``) or a ``zenodo.org``
    record URL, or ``""`` when *ref* is not a Zenodo reference."""
    ref = (ref or "").strip()
    m = re.search(r"zenodo\.org/records?/(\d+)", ref, re.I) \
        or re.search(r"\bzenodo\.(\d+)\b", ref, re.I)
    return m.group(1) if m else ""


def parse_zenodo_record(record, *, name_prefix="", picks=None):
    """Turn a Zenodo record JSON dict into dataset specs (pure; no network).

    Each file becomes a dataset with the record's DOI + title attached and its md5
    carried for verification (sha256 is computed on download/adoption)."""
    meta = record.get("metadata", {}) or {}
    doi = record.get("doi") or meta.get("doi", "") or ""
    title = meta.get("title", "") or ""
    taken, specs = set(), []
    for f in record.get("files", []) or []:
        key = f.get("key") or f.get("filename") or ""
        if not key or (picks and not any(fnmatch.fnmatch(key, p) for p in picks)):
            continue
        links = f.get("links", {}) or {}
        uri = links.get("self") or links.get("download") or ""
        algo, _, hexv = (f.get("checksum", "") or "").partition(":")
        base = f"{name_prefix}/{key}" if name_prefix else os.path.splitext(key)[0]
        specs.append({
            "name": _unique_name(base, taken), "uri": uri,
            "doi": doi, "description": title,
            "sha256": hexv if algo == "sha256" else "",
            "hash_algo": algo or "md5", "hash_value": hexv,
        })
    return specs


def import_zenodo(db, ref, *, fetch_json=None, name_prefix="", picks=None,
                  dry_run=False, overwrite=False):
    """Resolve a Zenodo DOI / record URL through the Zenodo API and declare one
    dataset per file (declare-only — a record can be large; run ``download`` to
    fetch). *fetch_json* is injectable for testing."""
    rid = zenodo_record_id(ref)
    if not rid:
        raise ValueError(f"{ref!r} is not a recognizable Zenodo DOI or record URL")
    if fetch_json is None:
        def fetch_json(url):
            import httpx
            r = httpx.get(url, follow_redirects=True, timeout=30.0)
            r.raise_for_status()
            return r.json()
    record = fetch_json(f"https://zenodo.org/api/records/{rid}")
    specs = parse_zenodo_record(record, name_prefix=name_prefix, picks=picks)
    if not specs:
        return f"Zenodo record {rid}: no files matched."
    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    return _summary(f"Zenodo record {rid}", rows, declared, adopted, skipped,
                    dry_run=dry_run, cache=False)


# Tool name → importer, for the pluggable ``datamanifest import <tool>``.
# (Zenodo is reached through ``add`` instead — it's a reference, not a catalog.)
IMPORTERS = {
    "pooch": import_pooch,
    "csv": import_csv,
    "urls": import_urls,
}
