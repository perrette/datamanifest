# Adding datasets

This page covers how datasets get into the manifest: the principle, which
sources are supported (and which are not), and the per-source detail for the
`add` and `import` commands.

## A dataset is a URI plus a checksum

A dataset is declared by a **URI** — where the bytes live — and verified by a
**checksum** — a content hash, written as `<algo>:<hex>`. Adding a dataset
means writing one entry to the manifest; download, verification and sharing
all follow from that entry.

=== "CLI"

    ```bash
    datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
    ```

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")
    db.add("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")
    add(db, "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"; name="co2")
    ```

=== "Manifest"

    ```toml
    [co2]
    checksum = "sha256:0058b3788040b5c27b2b5c1dd6d26226b7e4deef85e34c153e64806c37df7c75"
    uri = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
    ```

In every client, `add` writes the entry and downloads right away. Pass
`--no-download` (CLI) or `skip_download=true` (Python / Julia) to only
declare it — the download then happens on first use
(`load_dataset` / `download_dataset`) or with `datamanifest download`.

The `checksum` carries its algorithm. A digest published by the source (an md5
from a Zenodo or PANGAEA file listing, a pooch registry, a DVC file) is
recorded as `checksum = "md5:…"` and verified in md5 — never re-hashed to
sha256. When the source publishes no digest, one is computed (as `sha256:…`)
on the first download or adoption and written back to the manifest.

Since the manifest is plain TOML, you can also write the entry by hand —
`[name]` plus `uri = "…"` — and let the checksum fill in on first download.

## Supported sources

### Direct downloads (https / http)

Any plain `https://` or `http://` URL works out of the box. Downloads are
streamed with a standard HTTP client, redirects are followed, and an
interrupted download resumes from the partial file.

### DOIs

Two repositories have fine-tuned importers behind `add`: **Zenodo**
(`10.5281/zenodo.<id>` or a `zenodo.org/records/<id>` URL) and **PANGAEA**
(`10.1594/PANGAEA.<id>` or a `doi.pangaea.de` URL). Both resolve the record
through the repository's API, declare its files (one bundled dataset by
default, one dataset per file with `--split`), and attach the DOI and title to
each entry. See the [reference below](#a-zenodo-doi-or-record-url) for the
options.

Any other DOI is **not** auto-resolved. Follow the DOI yourself and `add` the
direct file URL it leads to.

### Object stores

Object-store URIs — `s3://`, `gs://`/`gcs://`, `az://`/`abfs://`/`abfss://`,
`adl://`, `gdrive://` — are valid dataset URIs, but they are not handled by
the core HTTP downloader:

- **Python / CLI** can fetch them through [fsspec](https://filesystem-spec.readthedocs.io/),
  an *optional* dependency (`pip install 'datamanifest[fsspec]'` plus the
  scheme's backend, e.g. `s3fs` / `gcsfs` / `adlfs`). A single object or a
  whole prefix (e.g. a zarr store) is mirrored, then verified by sha256 like
  any download. Credentials follow fsspec's normal resolution (environment
  variables, config files, instance metadata).
- **`add --lazy`** never downloads: it marks the entry `lazy_access = true`
  and binds the built-in fsspec loader (a Python-only binding), so the URI is
  opened in place. No local copy means no checksum.
- **Julia** has no native object-store fetcher: a `lazy_access` entry with a
  scheme-aware loader works, or the fetch is
  [delegated](language-bindings.md) to a peer datamanifest tool that
  implements the scheme.

### Local, SSH and git sources

- `file:///path/to/data` — a local file or folder, copied into the store.
- `ssh://host/path` — fetched with `rsync`; when `host` is the local machine
  it is treated as a local file.
- `git://`, `ssh+git://`, or an `https://…/*.git` URL — the repository is
  cloned. Folders (clones, extracted archives) get a checksum computed over
  the whole tree.

### Other tools' catalogs

When the datasets are already listed in another tool's catalog file — a pooch
registry, an intake catalog, DVC metadata, or a plain CSV/URL list — `import`
converts the whole catalog to manifest entries in one go, adopting
already-downloaded files in place. See the
[reference below](#reference-import-another-tools-catalog) and
[importing from other tools](importing.md).

### Summary

| Source type | Example | How it's added | Checksum | Notes |
|---|---|---|---|---|
| Direct URL | `https://host/file.nc` | `add URL` | computed (`sha256:…`) on first download | redirects followed; interrupted downloads resume |
| Zenodo record | `10.5281/zenodo.1234567` | `add DOI` (declare-only) | published md5 per file with `--split`, else computed | one bundle by default; `--split`, `--pick` |
| PANGAEA dataset | `10.1594/PANGAEA.930590` | `add DOI` (declare-only) | published md5 per file with `--split`, else computed | tabular / file / collection / series detected |
| Other DOI | `10.1029/…` | not auto-resolved — `add` the file URL it leads to | computed on first download | only Zenodo and PANGAEA resolvers are built in |
| Object store | `s3://bucket/key` | `add s3://…` (needs the `fsspec` extra) or `add --lazy` | sha256 after fetch; none when lazy | lazy entries open in place; Julia needs a loader or delegation |
| Local file / folder | `file:///data/file.nc` | `add file://…` | computed on adoption | copied into the store |
| SSH host | `ssh://host/path` | `add ssh://…` | computed on fetch | rsync; the local hostname resolves to `file` |
| Git repository | `https://host/repo.git` | `add URL` | computed over the cloned tree | folder checksums can be disabled (`skip_checksum_folders`) |
| Another tool's catalog | `registry.txt`, `catalog.yml`, `*.dvc` | `import pooch/intake/dvc/csv/urls` | taken from the catalog when it publishes one | adopts existing download caches |

## What can get in the way

Some sources cannot be fetched by a generic downloader. The manifest can still
*declare* them — the limits below are about who produces the bytes.

### Hosts behind bot protection

Downloads use a plain HTTP client. A host behind Cloudflare or similar
bot protection may answer it with a challenge page or an outright refusal,
even though the same URL works in a browser. The dataset can still be
recorded:

```bash
datamanifest add https://protected.example.org/data.csv --name data --no-download
datamanifest path data        # where the file is expected
```

Fetch the file yourself (browser, `wget` with cookies, …) and place it at
that path — the next `datamanifest download` adopts the existing copy and
records its checksum instead of re-fetching. Alternatively, bind a
[fetcher](language-bindings.md) that knows how to talk to the host, and
`download` runs it.

### Services that require authentication

Credentials are never stored in the manifest; you provision them on each
machine. Two patterns:

- **Object stores** — credentials resolve through fsspec's normal chain
  (environment variables, config files, instance metadata); nothing
  datamanifest-specific.
- **API-gated services** — wrap the service's own client in a **fetcher
  binding**: a function that produces the dataset's bytes, declared in the
  manifest in place of a downloadable `uri`. For example, Copernicus CDS data
  needs an account key in `~/.cdsapirc`; a small function calling the
  `cdsapi` client, bound as the dataset's fetcher, makes the dataset
  reproducible for anyone who has their own key. Any API client can be
  wrapped this way — see [language bindings](language-bindings.md).

### Rate limits and very large datasets

Zenodo and PANGAEA records are declared without downloading for a reason:
records can hold many gigabytes, and repositories rate-limit bulk access. Use
`--pick` to declare only the files you need, and run `datamanifest download`
when you actually want the bytes — HTTP downloads stream to disk and resume
from a partial file after an interruption. An object-store prefix (e.g. a
zarr store) is mirrored recursively: mind its total size before fetching
rather than using `--lazy`.

## Reference: `add` sources in detail

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

## Reference: `import` — another tool's catalog

Where `add` takes a pointer to data, `import` takes **another tool's catalog
file** and converts every entry. All importers share `--cache-dir` (adopt
already-downloaded files in place, checksum-verified), `--base-url` (root URL
for entries without one), `--dry-run`, and `--overwrite`.

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
