"""Import datasets declared by other tools / data repositories into a datamanifest.

Every importer reduces its source to a list of **dataset specs** and hands them to
:func:`_declare_specs`, which declares each as a standard manifest entry and — when
a local copy is given and its checksum matches — adopts it in place via a state-file
record (no re-download). So all sources share one declaration + cache-adoption path.

Two CLI verbs sit on top (see ``datamanifest/cli.py``):

- ``add <reference>`` — a pointer to data (a direct URL, a **Zenodo** DOI/record, or
  a **PANGAEA** DOI, each of which expands to its files). :func:`import_zenodo`,
  :func:`import_pangaea`.
- ``import <tool> <file>`` — another tool's catalog: **pooch** registry, a generic
  **csv** / **urls** list, **intake** catalog, or **DVC** files. :data:`IMPORTERS`.

None of this changes the on-the-wire download protocol: every entry uses an
already-supported scheme (HTTP, git, ssh, file). A source's published digest is
carried verbatim as ``checksum = "<algo>:<hex>"`` (so a Zenodo/PANGAEA/DVC md5 is
preserved, not dropped); a source with no digest gets one computed (as sha256) on
adoption / first download.
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

    Spec keys: ``name``, ``uri`` (required); ``hash_algo``/``hash_value`` (the
    source's published digest — becomes ``checksum = "<algo>:<hex>"`` and verifies
    ``cache_file``) or an explicit ``checksum``/``sha256``; ``cache_file`` (a local
    copy to adopt, or ``""``); optional ``doi`` / ``description`` / ``extract``.
    """
    rows, declared, adopted, skipped = [], 0, 0, 0
    for s in specs:
        name, uri = s["name"], s.get("uri", "")
        bundle = s.get("uris")                 # a multi-file (uris=) dataset
        cache_file = s.get("cache_file", "") or ""
        algo = s.get("hash_algo", "sha256") or "sha256"
        expected = s.get("hash_value", "") or ""
        # The declared checksum, as `<algo>:<hex>`: from the source's hash (any
        # algorithm — a published md5 is now carried, not dropped), else an
        # explicit `checksum`/`sha256` spec key.
        if expected:
            chk = f"{algo}:{expected}"
        elif s.get("checksum"):
            chk = s["checksum"]
        elif s.get("sha256"):
            chk = f"sha256:{s['sha256']}"
        else:
            chk = ""

        if bundle:
            rows.append(f"  {name}  ->  [{len(bundle)} files]")
            declared += 1
            if not dry_run:
                kwargs = {k: s[k] for k in ("doi", "description") if s.get(k)}
                db.register_dataset("", name=name, uris=bundle, persist=False,
                                    overwrite=overwrite, **kwargs)
            continue

        have = bool(cache_file) and os.path.exists(cache_file)
        verified = None                      # None: no checksum to check against
        if have:
            try:
                actual_sha = sha256_path(cache_file)
                if expected:
                    actual = actual_sha if algo == "sha256" \
                        else _file_hash(cache_file, algo)
                    verified = (actual == expected)
                if verified is not False and not chk:
                    chk = f"sha256:{actual_sha}"   # fill sha256 from the local file
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
        kwargs = {"checksum": chk}
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


def _slug(text, limit=40):
    """A short, filesystem-friendly slug from free text (or ``""``)."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].rstrip("-")


def import_zenodo(db, ref, *, fetch_json=None, name="", picks=None, split=False,
                  dry_run=False, overwrite=False):
    """Resolve a Zenodo DOI / record URL through the Zenodo API and declare its
    files (declare-only — a record can be large; run ``download`` to fetch).

    By default the record's files are **bundled** into one dataset (``uri`` for a
    single file, else ``uris=``), named *name* or a slug of the record title.
    ``--split`` instead declares one dataset per file. *picks* filters the files;
    *fetch_json* is injectable for testing."""
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
    files = parse_zenodo_record(record, name_prefix=(name if split else ""),
                                picks=picks)
    if not files:
        return f"Zenodo record {rid}: no files matched."

    if split:
        specs = files
    else:
        doi = files[0].get("doi", "")
        desc = files[0].get("description", "")
        bundle_name = name or _slug(desc) or f"zenodo-{rid}"
        uris = [f["uri"] for f in files]
        common = {"name": bundle_name, "doi": doi, "description": desc}
        # A single file is tidier as a plain `uri=` (a directly-loadable file)
        # than a one-element `uris=` bundle (which would be a directory).
        specs = [{**common, "uri": uris[0]}] if len(uris) == 1 \
            else [{**common, "uris": uris}]

    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    return _summary(f"Zenodo record {rid}", rows, declared, adopted, skipped,
                    dry_run=dry_run, cache=False)


# ----- PANGAEA (by DOI / doi.pangaea.de URL) ---------------------------------

_PANGAEA_JSONLD = "https://doi.pangaea.de/10.1594/PANGAEA.{id}?format=metadata_jsonld"
_PANGAEA_TEXTFILE = "https://doi.pangaea.de/10.1594/PANGAEA.{id}?format=textfile"
_PANGAEA_FILE = "https://download.pangaea.de/dataset/{id}/files/{name}"
_PANGAEA_ES = ("https://ws.pangaea.de/es/pangaea/panmd/_search"
               "?q=parentIdDataSet:{id}&size=1000")


def pangaea_dataset_id(ref):
    """The PANGAEA dataset id from a DOI (``10.1594/PANGAEA.<id>``) or a
    ``doi.pangaea.de`` / ``doi.org`` DOI URL, or ``""`` when *ref* is not a PANGAEA
    reference. A ref that already pins a ``?format=`` representation is treated as a
    plain URL (returns ``""``): the caller chose a concrete file, so honor it
    verbatim rather than re-resolving the dataset."""
    ref = (ref or "").strip()
    if "format=" in ref.lower():
        return ""
    m = re.search(r"10\.1594/PANGAEA\.(\d+)", ref, re.I)
    return m.group(1) if m else ""


def classify_pangaea(jsonld):
    """Classify a PANGAEA dataset from its schema.org JSON-LD ``distribution``.

    Returns ``(kind, url)``:

    - ``"file"``  — a single uploaded file; *url* is its direct download URL.
    - ``"table"`` — a ``?format=textfile`` representation; *url* is that URL. This is
      EITHER a tabular dataset (the textfile is the data) OR a file collection (the
      textfile is a matrix listing the files); the two are only distinguishable from
      the textfile's own column header (see :func:`pangaea_is_filelist`).
    - ``"zip"``   — only a zip representation; *url* is the ``?format=zip`` URL. A
      publication-series parent or a login-gated bundle; the caller checks for child
      datasets before falling back to the zip.
    - ``("", "")`` — no recognizable distribution.
    """
    dists = jsonld.get("distribution") or []
    if isinstance(dists, dict):
        dists = [dists]
    textfile = zipurl = directfile = ""
    for d in dists:
        url = (d or {}).get("contentUrl", "") or ""
        enc = ((d or {}).get("encodingFormat", "") or "").lower()
        low = url.lower()
        if "format=textfile" in low or "tab-separated" in enc:
            textfile = textfile or url
        elif "format=zip" in low or enc == "application/zip":
            zipurl = zipurl or url
        elif "/files/" in low and "format=" not in low:
            directfile = directfile or url
    if directfile:
        return "file", directfile
    if textfile:
        return "table", textfile
    if zipurl:
        return "zip", zipurl
    return "", ""


def pangaea_restricted(jsonld):
    """A human-readable access restriction if the dataset is not freely accessible
    (PANGAEA flags these in JSON-LD), else ``""``."""
    if jsonld.get("isAccessibleForFree") is False:
        return jsonld.get("conditionsOfAccess") or "restricted access"
    return ""


def _split_pangaea_header(lines):
    """Consume a PANGAEA textfile's ``/* DATA DESCRIPTION ... */`` comment block from
    *lines* (a line iterator) and return ``(column_header, rows)`` — the data column
    header line and an iterator over the remaining data rows. Returns ``("", iter)``
    if the comment block is unterminated."""
    it = iter(lines)
    in_comment = False
    for raw in it:
        s = raw.rstrip("\r\n")
        if not in_comment and s.lstrip().startswith("/*"):
            in_comment = not s.rstrip().endswith("*/")
            continue
        if in_comment:
            if s.rstrip().endswith("*/"):
                in_comment = False
            continue
        return s, it                       # first line past the comment = columns
    return "", it


def pangaea_is_filelist(column_header):
    """True when a PANGAEA textfile's column header is the file-collection schema:
    a leading ``Binary`` column plus a ``Binary (Hash)`` (MD5) column. A plain
    tabular dataset has data columns here instead."""
    cols = [c.strip() for c in column_header.split("\t")]
    return bool(cols) and cols[0] == "Binary" and "Binary (Hash)" in cols


def parse_pangaea_filelist(column_header, rows, dataset_id, *, picks=None):
    """Parse a file-collection textfile into ``[(filename, md5)]``. The ``Binary``
    column is the filename and ``Binary (Hash)`` the MD5; *picks* (globs) filter by
    filename. Drains the *rows* iterator."""
    cols = [c.strip() for c in column_header.split("\t")]
    i_name, i_hash = cols.index("Binary"), cols.index("Binary (Hash)")
    out = []
    for raw in rows:
        parts = raw.rstrip("\r\n").split("\t")
        if len(parts) <= max(i_name, i_hash):
            continue
        name, md5 = parts[i_name].strip(), parts[i_hash].strip()
        if not name or (picks and not any(fnmatch.fnmatch(name, p) for p in picks)):
            continue
        out.append((name, md5))
    return out


def _pangaea_specs(dataset_id, *, fetch_json, get_lines, picks, split, name, taken,
                   notes, visited):
    """Resolve one PANGAEA dataset id into dataset specs (recursing into a parent
    series' children). Pure dispatch over the injected fetchers; appends human notes
    (restricted / fell-back-to-zip / truncated) to *notes*."""
    if dataset_id in visited:
        return []
    visited.add(dataset_id)
    doi = f"10.1594/PANGAEA.{dataset_id}"
    jsonld = fetch_json(_PANGAEA_JSONLD.format(id=dataset_id))
    title = jsonld.get("name", "") or ""
    restricted = pangaea_restricted(jsonld)
    if restricted:
        notes.append(f"{doi}: {restricted} — skipped")
        return []
    kind, url = classify_pangaea(jsonld)

    if kind == "file":
        nm = name or _unique_name(_slug(title) or f"pangaea-{dataset_id}", taken)
        return [{"name": nm, "uri": url, "doi": doi, "description": title}]

    if kind == "table":
        column_header, rows = _split_pangaea_header(get_lines(url))
        if not pangaea_is_filelist(column_header):
            # A plain tabular dataset: the textfile IS the data. Store its URL; the
            # rows iterator is left undrained so the stream closes without download.
            nm = name or _unique_name(_slug(title) or f"pangaea-{dataset_id}", taken)
            return [{"name": nm, "uri": url, "doi": doi, "description": title}]
        files = parse_pangaea_filelist(column_header, rows, dataset_id, picks=picks)
        if not files:
            notes.append(f"{doi}: no files matched")
            return []
        if split:
            specs = []
            for fname, md5 in files:
                base = name + "/" + fname if name else os.path.splitext(fname)[0]
                specs.append({
                    "name": _unique_name(base, taken),
                    "uri": _PANGAEA_FILE.format(id=dataset_id, name=fname),
                    "doi": doi, "description": title,
                    "hash_algo": "md5", "hash_value": md5,
                })
            return specs
        # Bundle the collection's files into one uris= dataset (per-file md5s are
        # not retained on a bundle — use --split to keep them as checksums).
        bundle_name = name or _slug(title) or f"pangaea-{dataset_id}"
        uris = [_PANGAEA_FILE.format(id=dataset_id, name=fname) for fname, _ in files]
        if len(uris) == 1:
            return [{"name": _unique_name(bundle_name, taken), "uri": uris[0],
                     "doi": doi, "description": title}]
        return [{"name": _unique_name(bundle_name, taken), "uris": uris,
                 "doi": doi, "description": title}]

    if kind == "zip":
        es = fetch_json(_PANGAEA_ES.format(id=dataset_id))
        hits = (es.get("hits", {}) or {}).get("hits", []) or []
        total = (es.get("hits", {}) or {}).get("total", len(hits))
        if isinstance(total, dict):                 # ES 7+ returns {value, relation}
            total = total.get("value", len(hits))
        if hits:
            if total > len(hits):
                notes.append(f"{doi}: {total} child datasets, only the first "
                             f"{len(hits)} enumerated")
            specs = []
            for h in hits:
                cid = str(h.get("_id", "")).strip()
                if cid:
                    specs += _pangaea_specs(
                        cid, fetch_json=fetch_json, get_lines=get_lines, picks=picks,
                        split=split, name="", taken=taken, notes=notes,
                        visited=visited)
            return specs
        # Zip-only with no enumerable children: keep the zip as one dataset.
        notes.append(f"{doi}: stored as a single zip (no child datasets found)")
        nm = name or _unique_name(_slug(title) or f"pangaea-{dataset_id}", taken)
        return [{"name": nm, "uri": url, "doi": doi, "description": title,
                 "extract": True}]

    notes.append(f"{doi}: no downloadable representation found — skipped")
    return []


def import_pangaea(db, ref, *, fetch_json=None, get_lines=None, name="", picks=None,
                   split=False, dry_run=False, overwrite=False):
    """Resolve a PANGAEA DOI / ``doi.pangaea.de`` URL through PANGAEA's web services
    and declare it (declare-only — run ``download`` to fetch). Classifies the
    dataset from its JSON-LD:

    - a tabular dataset → one entry whose ``uri`` is the ``?format=textfile`` data;
    - a single uploaded file → one entry pointing at the file;
    - a file collection → its files (bundled into one ``uris=`` dataset by default;
      ``--split`` makes one dataset per file, each carrying the file's MD5; ``--pick``
      filters by filename);
    - a publication-series parent → one entry per child dataset (each child's own
      DOI), or the series zip when no children are enumerable.

    *fetch_json* / *get_lines* are injectable for testing (JSON-LD + Elasticsearch,
    and the streamed textfile, respectively)."""
    dataset_id = pangaea_dataset_id(ref)
    if not dataset_id:
        raise ValueError(f"{ref!r} is not a recognizable PANGAEA DOI or dataset URL")
    if fetch_json is None:
        def fetch_json(url):
            import httpx
            r = httpx.get(url, follow_redirects=True, timeout=30.0)
            r.raise_for_status()
            return r.json()
    if get_lines is None:
        def get_lines(url):
            import httpx
            with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as r:
                r.raise_for_status()
                yield from r.iter_lines()

    notes, taken = [], set(db.datasets)
    specs = _pangaea_specs(dataset_id, fetch_json=fetch_json, get_lines=get_lines,
                           picks=picks, split=split, name=name, taken=taken,
                           notes=notes, visited=set())
    if not specs:
        msg = f"PANGAEA dataset {dataset_id}: nothing to import."
        return msg + ("\n  (" + "; ".join(notes) + ")" if notes else "")

    rows, declared, adopted, skipped = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    summary = _summary(f"PANGAEA dataset {dataset_id}", rows, declared, adopted,
                       skipped, dry_run=dry_run, cache=False)
    if notes:
        summary += "\n  (" + "; ".join(notes) + ")"
    return summary


def _require_yaml():
    try:
        import yaml
    except ImportError as e:
        raise ValueError(
            "this importer needs PyYAML. Install with: pip install "
            "'datamanifest[yaml]' (or: pip install pyyaml)."
        ) from e
    return yaml


# ----- intake catalogs --------------------------------------------------------

def import_intake(db, catalog_path, *, base_url="", cache_dir="", dry_run=False,
                  overwrite=False):
    """Import an intake catalog (``catalog.yml``): each ``sources.<name>`` whose
    ``args.urlpath`` is a single concrete file path/URL becomes a dataset (``uri`` =
    that urlpath; intake carries no checksums). Sources whose urlpath is a glob /
    template / list / non-string are reported and skipped."""
    yaml = _require_yaml()
    with open(catalog_path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
    sources = catalog.get("sources", {}) or {}
    taken, specs, skipped = set(db.datasets), [], []
    for src_name, src in sources.items():
        args = (src or {}).get("args", {}) or {}
        urlpath = args.get("urlpath")
        if not isinstance(urlpath, str) or not urlpath \
                or any(c in urlpath for c in "*{"):
            skipped.append(src_name)
            continue
        uri = urlpath
        if base_url and "://" not in uri:
            uri = f"{base_url.rstrip('/')}/{uri.lstrip('/')}"
        basename = os.path.basename(uri.split("?")[0])
        specs.append({
            "name": _unique_name(src_name, taken), "uri": uri,
            "description": (src or {}).get("description", "") or "",
            "cache_file": os.path.join(cache_dir, basename) if cache_dir else "",
        })
    rows, declared, adopted, skip_cnt = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    summary = _summary(f"intake catalog {os.path.basename(catalog_path)}", rows,
                       declared, adopted, skip_cnt, dry_run=dry_run,
                       cache=bool(cache_dir))
    if skipped:
        summary += (f"\n  (skipped {len(skipped)} source(s) without a single-file "
                    f"urlpath: {', '.join(sorted(skipped))})")
    return summary


# ----- DVC --------------------------------------------------------------------

def _dvc_files(target):
    """The ``.dvc`` files (and ``dvc.lock``) to read from *target* (a file or dir)."""
    if os.path.isdir(target):
        out = sorted(os.path.join(target, n) for n in os.listdir(target)
                     if n.endswith(".dvc"))
        lock = os.path.join(target, "dvc.lock")
        if os.path.isfile(lock):
            out.append(lock)
        return out
    return [target]


def _dvc_root(target):
    """The DVC project root (the dir containing ``.dvc/``) at or above *target*."""
    d = os.path.abspath(target if os.path.isdir(target) else os.path.dirname(target))
    while True:
        if os.path.isdir(os.path.join(d, ".dvc")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


def _dvc_default_remote(root):
    """The URL of the DVC default remote from ``.dvc/config`` (+ ``config.local``)."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read([os.path.join(root, ".dvc", "config"),
              os.path.join(root, ".dvc", "config.local")])
    name = cfg.get("core", "remote", fallback="")
    if not name:
        return ""
    # DVC writes remote sections git-config style, quoted: ['remote "storage"'].
    # configparser keeps the literal section name (incl. the outer quotes), so
    # normalize before matching.
    for section in cfg.sections():
        norm = section.strip().strip("'")            # only DVC's outer single quotes
        if norm in (f'remote "{name}"', f"remote {name}") \
                and cfg.has_option(section, "url"):
            return cfg.get(section, "url")
    return ""


def _dvc_remote_uri(remote_url, md5):
    """The content-addressed URL of *md5* in a DVC remote (3.x ``files/md5`` layout),
    or ``""`` when there is no usable remote URL."""
    if not remote_url or len(md5) < 3 or "://" not in remote_url:
        return ""
    return f"{remote_url.rstrip('/')}/files/md5/{md5[:2]}/{md5[2:]}"


def _dvc_cache_file(cache_dir, md5):
    """The local DVC cache path for *md5*, probing the 3.x and 2.x layouts."""
    if not cache_dir or len(md5) < 3:
        return ""
    for cand in (os.path.join(cache_dir, "files", "md5", md5[:2], md5[2:]),
                 os.path.join(cache_dir, md5[:2], md5[2:])):
        if os.path.exists(cand):
            return cand
    return ""


def _dvc_outs(doc):
    """Yield ``(out_dict, dep_url)`` for every tracked out in a parsed ``.dvc`` /
    ``dvc.lock`` doc. *dep_url* is the URL of an ``import-url`` dependency, if any."""
    def _dep_url(deps):
        for d in deps or []:
            p = (d or {}).get("path", "")
            if isinstance(p, str) and "://" in p:
                return p
        return ""

    if "stages" in doc:                                   # dvc.lock
        for stage in (doc.get("stages", {}) or {}).values():
            dep_url = _dep_url((stage or {}).get("deps"))
            for out in (stage or {}).get("outs", []) or []:
                yield out, dep_url
    else:                                                 # a single .dvc file
        dep_url = _dep_url(doc.get("deps"))
        for out in doc.get("outs", []) or []:
            yield out, dep_url


def import_dvc(db, target, *, base_url="", cache_dir="", dry_run=False,
               overwrite=False):
    """Import DVC outs from a ``.dvc`` file, a ``dvc.lock``, or a directory of them.

    Each tracked out (by md5) becomes a dataset; the local ``.dvc/cache`` copy is
    adopted in place by hash when present (sha256 computed from it). The ``uri`` is
    the ``import-url`` dependency when the out has one, else the default DVC remote's
    content-addressed URL (``s3://`` / ``gs://`` / ``https://`` …, now fetchable);
    outs with neither are reported and skipped."""
    yaml = _require_yaml()
    root = _dvc_root(target)
    cache = cache_dir or (os.path.join(root, ".dvc", "cache") if root else "")
    remote = _dvc_default_remote(root) if root else ""
    taken, specs, skipped = set(db.datasets), [], []
    for fp in _dvc_files(target):
        with open(fp, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        for out, dep_url in _dvc_outs(doc):
            md5 = out.get("md5") or out.get("checksum") or ""
            path = out.get("path", "")
            if not md5 or not path:
                continue
            uri = dep_url or _dvc_remote_uri(remote, md5)
            if not uri:
                skipped.append(path)
                continue
            specs.append({
                "name": _unique_name(
                    os.path.splitext(os.path.basename(path))[0] or path, taken),
                "uri": uri, "sha256": "",
                "cache_file": _dvc_cache_file(cache, md5),
                "hash_algo": "md5", "hash_value": md5,
            })
    rows, declared, adopted, skip_cnt = _declare_specs(
        db, specs, dry_run=dry_run, overwrite=overwrite)
    summary = _summary(f"DVC {os.path.basename(os.path.abspath(target))}", rows,
                       declared, adopted, skip_cnt, dry_run=dry_run, cache=True)
    if skipped:
        summary += (f"\n  (skipped {len(skipped)} out(s) with no resolvable URL — "
                    f"no import-url dep and no usable default remote: "
                    f"{', '.join(sorted(skipped))})")
    return summary


# Tool name → importer, for the pluggable ``datamanifest import <tool>``.
# (Zenodo is reached through ``add`` instead — it's a reference, not a catalog.)
IMPORTERS = {
    "pooch": import_pooch,
    "csv": import_csv,
    "urls": import_urls,
    "intake": import_intake,
    "dvc": import_dvc,
}
