# Adding datasets from external sources

> This page is the **user-facing command surface** for onboarding datasets. Most
> sources here are implemented: direct URLs, Zenodo/figshare/OSF/Dryad DOIs,
> PANGAEA DOIs, and the `import` catalogs (pooch, intake, DVC, CSV/URL lists). Each
> is pure *import* (declaration parsing) over an already-supported download scheme.

datamanifest distinguishes two verbs by **what you hand the command**:

| Verb | You give it… | Yields | Examples |
|---|---|---|---|
| `add` | a **reference to data** (URL, DOI) | one dataset, or all files of a record | direct URL, Zenodo/figshare DOI, PANGAEA DOI |
| `import` | **another tool's catalog/registry file** | many datasets | pooch, intake, DVC |

The test: is the argument *another tool's manifest* → `import`; is it *a pointer to
data* → `add`.

Both verbs end at the same place: standard `datamanifest.toml` entries (`uri`,
`checksum`, optional `doi`/`description`/`extract`), plus — where a local copy
already exists — an in-place adoption recorded in the state file so nothing is
re-downloaded.

A note on checksums: the `checksum` field carries its algorithm as `<algo>:<hex>`.
Sources that publish **md5** (Zenodo, PANGAEA, DVC) are recorded as
`checksum = "md5:…"` and verified in md5 on first download (or against an adopted
local file) — not re-hashed to sha256. A source with **no** published digest gets
one computed (as `sha256:…`) at that point.

---

## `add` — add dataset(s) from a reference

### A direct URL

```bash
datamanifest add https://www.ncei.noaa.gov/woa/temperature.nc
datamanifest add "https://zenodo.org/records/1234567/files/grid.zip" --extract
```

One dataset; downloads and records its `checksum` (as `sha256:…`) by default (`--no-download` to defer).

### A DOI or data-repository record — Zenodo / figshare / OSF / Dryad

```bash
datamanifest add 10.5281/zenodo.1234567          # by DOI
datamanifest add https://zenodo.org/records/1234567   # or by record URL
```

Resolves the record through the repository's API and adds **one dataset per file**
in it. Each entry gets:

- `uri` — the file's direct download link,
- `doi` — the record DOI (so the provenance is first-class, not just a label),
- `description` — the record title,
- `checksum` — the published md5 recorded as `md5:…` and verified on download
  (a digest-less source is computed as `sha256:…` instead).

Options:

- `--name PREFIX` — name the datasets `PREFIX/<filename>` instead of bare filenames.
- `--pick GLOB` (repeatable) — add only the files matching a glob (e.g. `--pick '*.nc'`).
- `--no-download` — declare only.

Why this beats today: instead of pasting every file URL by hand and losing the DOI
link, you hand over the record once and get all its files with checksums and
provenance.

### A PANGAEA DOI

```bash
datamanifest add 10.1594/PANGAEA.930590              # by DOI
datamanifest add https://doi.pangaea.de/10.1594/PANGAEA.930590   # or by URL
```

Resolves the dataset through PANGAEA's web services and classifies it from its
metadata:

- a **tabular dataset** → one entry whose `uri` is the `?format=textfile`
  (tab-delimited) data;
- a **single uploaded file** → one entry pointing straight at the file on
  `download.pangaea.de`;
- a **file collection** (a dataset whose rows *are* files — NetCDF/GeoTIFF/zip
  members) → its files, **bundled into one `uris=` dataset** by default. Each file
  carries its MD5 from PANGAEA's data matrix;
- a **publication series** (a parent DOI over many child datasets) → **one entry
  per child dataset**, each keeping its own DOI, enumerated through PANGAEA's
  search service. When no children are enumerable the series zip is kept as a
  single dataset.

Options (file collections / series):

- `--split` — one dataset per file instead of the default `uris=` bundle; a split
  file keeps its MD5 as the verification checksum.
- `--pick GLOB` (repeatable) — for a collection, add only the files matching a glob
  (e.g. `--pick '*.nc'`).
- `--name` — names the single/tabular entry, or the bundle.

A reference that already pins a representation — e.g.
`https://doi.pangaea.de/10.1594/PANGAEA.930512?format=zip` — is treated as a plain
URL (you chose that file), not re-resolved.

---

## `import` — bulk-import another tool's catalog

### pooch (implemented)

```bash
datamanifest import pooch registry.txt --base-url URL [--cache-dir DIR]
```

Each `filename [algo:]hash [url]` line becomes a dataset. `--cache-dir`
(e.g. `pooch.os_cache('pkg')`) adopts already-downloaded files in place,
checksum-verified — no re-download. See the README for full detail.

### intake catalogs

```bash
datamanifest import intake catalog.yml [--driver csv,netcdf,...]
```

An intake catalog lists several named *sources*, each with a `driver` and
`args.urlpath`. Each source with a concrete file `urlpath` becomes a dataset
(`uri = urlpath`). intake catalogs carry **no checksums**, so `checksum` is computed
(as `sha256:…`) on first download. Sources whose driver/urlpath isn't a fetchable file (templated
parameters, server protocols) are reported and skipped; `--driver` narrows to
selected drivers.

### DVC

```bash
datamanifest import dvc <path-or-dir> [--cache-dir .dvc/cache]
```

Parses `*.dvc` / `dvc.lock`. Each tracked *out* (with its `md5`/hash) becomes a
dataset. Because DVC stores content addressed by hash under `.dvc/cache`, the
**existing cache is adopted in place** by hash — the strongest reuse story after
pooch. The download `uri` is reconstructed from the DVC **remote** config when
possible (HTTP remotes, or `import-url` stages that record an explicit URL);
outs backed by a non-URL remote (S3, gdrive, ssh) are declared with the hash but
left for DVC/you to resolve, and reported as such.

### Generic CSV / URL list (universal escape hatch)

```bash
datamanifest import csv files.csv            # columns: name,url,sha256 (sha256 optional)
datamanifest import urls list.txt --base-url URL   # one path/URL per line
```

For exporting from anything. Reuses the whole pooch pipeline, including
`--cache-dir` adoption.

---

## What maps where (summary)

| Source | Verb | URL | Checksum | Adopt local cache |
|---|---|---|---|---|
| direct URL | `add` | given | computed on download | — |
| Zenodo/figshare/OSF DOI | `add` | API | md5 carried as `checksum` | — |
| PANGAEA DOI | `add` | web services | md5 (collections) carried as `checksum` | — |
| pooch registry | `import` | base_url + filename / 3rd col | sha256 (or md5) | `os_cache` (✓ implemented) |
| intake catalog | `import` | urlpath | none | — |
| DVC | `import` | remote config (partial) | md5/hash | `.dvc/cache` (by hash) |
| CSV / URL list | `import` | the file | optional | `--cache-dir` |
