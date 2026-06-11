# Adding datasets from external sources

datamanifest has two commands for bringing datasets into the manifest,
distinguished by what you hand them:

| Verb | You give it… | Yields | Examples |
|---|---|---|---|
| `add` | a **reference to data** (URL, DOI) | one dataset, or all files of a record | direct URL, Zenodo DOI, PANGAEA DOI |
| `import` | **another tool's catalog file** | many datasets | pooch, intake, DVC |

If the argument is another tool's manifest, use `import`; if it is a pointer
to data, use `add`.

Both commands produce standard `datamanifest.toml` entries (`uri`, `checksum`,
optional `doi`/`description`/`extract`). Where a local copy of a file already
exists — for example in another tool's download cache — it is **adopted**:
verified against its checksum and recorded in the state file (the
machine-generated record of where each dataset lives on disk), so nothing is
re-downloaded.

A note on checksums: the `checksum` field carries its algorithm as
`<algo>:<hex>`. A digest published by the source (an md5 from a pooch
registry, a DVC file, a Zenodo or PANGAEA file listing) is recorded as
`checksum = "md5:…"` and verified in md5 — not re-hashed to sha256. When the
source publishes no digest, one is computed (as `sha256:…`) on the first
download or adoption.

---

## `add` — add dataset(s) from a reference

### A direct URL

```bash
datamanifest add https://www.ncei.noaa.gov/woa/temperature.nc
datamanifest add "https://zenodo.org/records/1234567/files/grid.zip" --extract
```

Declares one dataset and downloads it, recording its `checksum` (as
`sha256:…`). Options:

- `--name N` — name the entry instead of deriving a name from the URL.
- `--extract` — unpack the archive after download.
- `--no-download` — declare only.
- `--lazy` — for an object-store URI (`s3://`, `gs://`, …): never download;
  mark the entry `lazy_access` and open it in place through the built-in
  fsspec loader.

### A Zenodo DOI or record URL

```bash
datamanifest add 10.5281/zenodo.1234567               # by DOI
datamanifest add https://zenodo.org/records/1234567   # or by record URL
```

Resolves the record through Zenodo's API and declares its files (declare-only —
records can be large; run `datamanifest download` to fetch). By default the
record becomes **one dataset**: a plain `uri` entry for a single file, a
`uris = [...]` bundle for several, named after `--name` or the record title.
Every entry carries the record DOI (`doi`) and title (`description`).

Options:

- `--split` — one dataset per file instead of the bundle. Each split entry
  carries the file's published md5 as its `checksum`; a bundle's checksum is
  computed on first download instead.
- `--name N` — name the bundle, or (with `--split`) name the datasets
  `N/<filename>`.
- `--pick GLOB` (repeatable) — add only the files matching a glob
  (e.g. `--pick '*.nc'`).

### A PANGAEA DOI

```bash
datamanifest add 10.1594/PANGAEA.930590                          # by DOI
datamanifest add https://doi.pangaea.de/10.1594/PANGAEA.930590   # or by URL
```

Resolves the dataset through PANGAEA's web services (declare-only, like
Zenodo) and classifies it from its metadata:

- a **tabular dataset** → one entry whose `uri` is the `?format=textfile`
  (tab-delimited) data;
- a **single uploaded file** → one entry pointing straight at the file on
  `download.pangaea.de`;
- a **file collection** (a dataset whose rows *are* files — NetCDF/GeoTIFF/zip
  members) → its files, bundled into one `uris=` dataset by default;
- a **publication series** (a parent DOI over many child datasets) → one entry
  per child dataset, each keeping its own DOI, enumerated through PANGAEA's
  search service. When no children can be enumerated, the series zip is kept
  as a single dataset.

Options (file collections / series):

- `--split` — one dataset per file instead of the `uris=` bundle. Each split
  entry carries the file's MD5 (from PANGAEA's file listing) as its
  `checksum`; a bundle does not retain per-file digests.
- `--pick GLOB` (repeatable) — for a collection, add only the files matching a
  glob (e.g. `--pick '*.nc'`).
- `--name` — names the single/tabular entry, or the bundle.

A reference that already pins a representation — e.g.
`https://doi.pangaea.de/10.1594/PANGAEA.930512?format=zip` — is treated as a
plain URL (you chose that file), not re-resolved.

---

## `import` — bulk-import another tool's catalog

All importers share `--cache-dir` (adopt already-downloaded files in place,
checksum-verified), `--base-url` (root URL for entries without one),
`--dry-run`, and `--overwrite`.

### pooch registries

```bash
datamanifest import pooch registry.txt --base-url URL [--cache-dir DIR]
```

Each `filename [algo:]hash [url]` line becomes a dataset. `--cache-dir`
(e.g. `pooch.os_cache('pkg')`) adopts already-downloaded files in place.

### intake catalogs

```bash
datamanifest import intake catalog.yml
```

An intake catalog lists several named *sources*, each with a driver and an
`args.urlpath`. Each source whose `urlpath` is a single concrete file path or
URL becomes a dataset (`uri = urlpath`). intake catalogs carry no checksums,
so `checksum` is computed (as `sha256:…`) on first download. Sources whose
urlpath is a glob, a template, or a list are reported and skipped. Requires
the `yaml` extra (`pip install 'datamanifest[yaml]'`).

### DVC

```bash
datamanifest import dvc <path-or-dir> [--cache-dir .dvc/cache]
```

Parses `*.dvc` files and `dvc.lock`. Each tracked *out* becomes a dataset
carrying its `md5` as the `checksum`. Because DVC stores content addressed by
hash under `.dvc/cache`, an existing cache copy is adopted in place by hash.
The `uri` is the URL of an `import-url` dependency when the out has one, else
the default DVC remote's content-addressed URL (`s3://`, `gs://`, `https://`,
…); outs with neither are reported and skipped.

### Generic CSV / URL list

```bash
datamanifest import csv files.csv            # header row with a url column; name, sha256 optional
datamanifest import urls list.txt --base-url URL   # one path/URL per line
```

For exporting from anything else. These use the same pipeline as the pooch
importer, including `--cache-dir` adoption.

---

## What maps where (summary)

| Source | Verb | URL | Checksum | Adopt local cache |
|---|---|---|---|---|
| direct URL | `add` | given | computed on download | — |
| Zenodo DOI | `add` | API | md5 (with `--split`), else computed | — |
| PANGAEA DOI | `add` | web services | md5 (with `--split`), else computed | — |
| pooch registry | `import` | base_url + filename / 3rd column | sha256 or md5 from the registry | `--cache-dir` (`os_cache`) |
| intake catalog | `import` | urlpath | computed on download | `--cache-dir` |
| DVC | `import` | import-url dep / remote config | md5 from the `.dvc` file | `.dvc/cache` (by hash) |
| CSV / URL list | `import` | the file | optional sha256 column | `--cache-dir` |
