<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup-dark.svg">
    <img src="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup.svg" alt="datamanifest.toml" height="76">
  </picture>
</p>

# datamanifest

[![pypi](https://img.shields.io/pypi/v/datamanifestpy)](https://pypi.org/project/datamanifestpy)
![python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fperrette%2Fdatamanifest%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)
[![CI](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml/badge.svg)](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml)

Keep track of datasets used in a scientific project: data dependencies and internal caching.

`datamanifest` provides a simple way to declare data dependencies — URLs, git repositories, checksums, formats — in a `datasets.toml` file, and handles download, verification, extraction, and loading. It can now also cache your own computed results (versioned), reusing the same infrastructure. `datamanifest` started as a Python port of [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) (same author), sharing its manifest format and feature surface; it has since grown a CLI and now develops in parallel as the Python implementation of a [multi-language specification](https://github.com/perrette/datamanifest.toml).

## Installation

```bash
pip install datamanifestpy
```

With optional loader backends:

```bash
pip install "datamanifestpy[csv]"       # pandas CSV
pip install "datamanifestpy[parquet]"   # pandas + pyarrow
pip install "datamanifestpy[nc]"        # xarray + netcdf4
pip install "datamanifestpy[yaml]"      # pyyaml
pip install "datamanifestpy[all]"       # all of the above
```

## API quickstart

```python
import datamanifest

# Add a dataset (registers + downloads + auto-fills sha256)
datamanifest.add(
    "https://github.com/jesstierney/lgmDA/archive/refs/tags/v2.1.zip",
    name="jesstierney/lgmDA",
    extract=True,
)

# Resolve the on-disk path
path = datamanifest.get_dataset_path("jesstierney/lgmDA")

# Download and load in one step
ds = datamanifest.load_dataset("my_nc_entry")  # returns xarray.Dataset for nc format

# Explicit database (no pyproject.toml / env-var lookup)
db = datamanifest.Database("datasets.toml", "my-data-folder")
datamanifest.add(db, "https://zenodo.org/record/.../file.csv")
path = datamanifest.get_dataset_path(db, "file")
```

The module-level functions (`add`, `download_dataset`, `load_dataset`, `get_dataset_path`, …) look up a process-wide default `Database` via `pyproject.toml` discovery, the `DATAMANIFEST_TOML` / `DATASETS_TOML` environment variables, or a `datasets.toml` / `datamanifest.toml` file in the working tree. Pass an explicit `db` as the first argument to bypass auto-discovery.

### Produce-or-load caching (`@cached`)

Cache the result of an expensive computation, keyed by its keyword arguments:

```python
from datamanifest.cache import cached

@cached(cachetype="esm_anomaly", format="nc")
def load_anomaly(*, grid="5x5", skip_models=()):
    ...  # expensive; returns an xarray.Dataset
    return ds

ds = load_anomaly(grid="5x5")          # computes, materializes, registers it
ds = load_anomaly(grid="5x5")          # cache hit: loads and returns
ds = load_anomaly(grid="5x5", cached=False)  # force recompute
```

The keyword arguments (minus `_`-prefixed runtime knobs) are hashed into a portable key — values may be strings, integers, finite floats, booleans, or nested lists/dicts of those (`None` and non-finite floats are rejected). The artifact and its `config.toml` / `metadata.toml` sidecars land under your cache directory at `<cache>/cached/<project-id>/<cachetype>/[<version>/]<hash>`. An optional `version=` string adds a path segment — recorded in the sidecars but **not** part of the key hash — so a change to a function's *logic* (same parameters) can't read a stale result. Produced datasets are **not** written into `datasets.toml`; they are indexed in a sibling `cached.toml`, and `datamanifest list --orphan --delete` (dry run by default, `--yes` to apply) is the maintenance command. The cache layer (`datamanifest.cache`) sits over the shared `datamanifest.store` substrate and never touches the fetch path.

## CLI usage

```
datamanifest COMMAND [OPTIONS]
```

| Command | Description |
|---|---|
| `list [--present\|--missing\|--all] [--kind K] [--scope S] [--orphan] [--older-than AGE] [--format F] [--fields ...] [--delete\|--move DIR] [--yes]` | List datasets and cached artifacts; with `--delete`/`--move` becomes the maintenance command (dry run by default; `--yes` to apply) |
| `download [NAME ...] [--all] [--overwrite]` | Download specific datasets or all of them |
| `path NAME` | Print the resolved on-disk path (composable in shell) |
| `add URI [--name N] [--no-download] [--extract]` | Register and (by default) download a dataset |
| `remove NAME [--keep-cache]` | Delete an entry, optionally preserving cached files |
| `show NAME` | Print full entry detail in TOML style |
| `verify [NAME ...]` | Re-check sha256 checksums; exits nonzero on any mismatch |
| `update-checksums [NAME ...] [--dry-run]` | Recompute stored checksums from what's on disk |
| `init [--folder PATH] [--force]` | Create a fresh `datasets.toml` in the current directory |
| `where` | Print active `datasets_toml` and `datasets_folder` paths |
| `migrate FILE` | Update an older manifest in place (move legacy flat fields into `_LANG`; rewrite bare `store = "x"` to `$`-selectors) |

Examples:

```bash
# Set up a new project
datamanifest init

# Add and download a dataset
datamanifest add "https://zenodo.org/record/.../file.zip" --extract

# Use the path in a shell pipeline
python analysis.py --data "$(datamanifest path file)"

# Verify all checksums before a paper submission
datamanifest verify

# Recompute stored checksums from what's on disk (e.g. after regenerating data)
datamanifest update-checksums --dry-run   # preview which would change
datamanifest update-checksums             # write the new checksums

# Inspect and clean up @cached artifacts
datamanifest list --kind cached --orphan          # dry-run: list orphaned cached artifacts
datamanifest list --kind cached --orphan --delete --yes  # delete them
datamanifest list --older-than 30d --delete       # preview artifacts older than 30 days

# Where is the active manifest?
datamanifest where
```

## Features

| Feature | Supported |
|---|---|
| HTTP / HTTPS download with progress | yes |
| Partial-download resume (Range header) | yes |
| git clone (`git://`, `ssh+git://`, `*.git`) | yes |
| SSH / rsync (`ssh://`, `sshfs://`, `rsync://`) | yes |
| Local file copy (`file://`) | yes |
| Multi-URI batch entries (`uris=`) | yes |
| SHA-256 checksum verification + auto-fill | yes |
| ZIP / tar / tar.gz extraction | yes |
| `requires=` dependency graph (topological order) | yes |
| Shell template hook (`shell=`) | yes |
| Python entry-point hook (`python=`) | yes |
| Named + default loaders (csv, parquet, nc, json, yaml, toml, zip, tar) | yes |
| TOML manifest round-trip (read `tomllib`, write `tomli_w`) | yes |
| Project-root auto-discovery (`pyproject.toml` walk, env vars) | yes |
| CLI (`list/download/path/add/remove/show/verify/update-checksums/init/where/migrate/format`) | yes |
| `_LANG` namespace for per-language bindings (read + write) | yes |
| Fetch ladder: own Python fetcher → shell template → URI | yes |
| Load ladder: own Python loader → manifest default → built-in | yes |
| Lossless round-trip of foreign `_LANG.*` subtrees | yes |
| Manifest migration (`datamanifest migrate`) | yes |
| Portable storage model (folder variables, `$`-selectors, `[_STORAGE]` with per-host overrides, `platformdirs` roots) | yes |
| Parameterized bindings (`{ ref, args, kwargs }` + `$var` substitution) | yes |
| Safe concurrent materialization (`.tmp` → atomic publish → `.complete` marker) | yes |
| Verify-once integrity (checksum only at fetch; `.complete` entry skips re-hash) | yes |
| Canonical key ordering (stable, cross-tool byte-identical output) | yes |
| Produce-or-load cache (`@cached`: parameter-hash keying, optional `version=`, `config.toml`/`metadata.toml` sidecars) | yes |
| `cached.toml` index + `datamanifest list` inspect/maintenance (`--orphan`, `--delete`, `--move`) | yes |

## Storage model

> **Behavior change from earlier releases.** Earlier versions stored datasets under a
> `/Datasets`-suffixed root (e.g. `~/.local/share/datamanifest/Datasets`). Now folder
> variables resolve to **bare** roots, and content is composed as `<root>/datasets/<key>`
> (downloads) or `<root>/cached/<project-id>/<cachetype>/[<version>/]<hash>` (produced
> artifacts). A legacy read-only probe still finds datasets at the old `/Datasets`-suffixed
> locations unless `DATAMANIFEST_DATA_DIR` or `DATAMANIFEST_DIR` is set.

Each dataset entry carries an optional `store` field — a **`$`-selector**
(`$folder` or `$folder/subpath`) referencing a named **folder variable**. The
built-in folder variables are `$data`, `$cache`, and `$repo`. User-defined folders
are declared in `[_STORAGE]`.

A `[_STORAGE]` table lets you define folder variables, set a project-wide default
selector, and override roots per host (glob):

```toml
[_STORAGE]
default = "$data"                        # project-wide default store selector
data    = "~/data"                       # override built-in $data bare root
cache   = "~/.cache/datamanifest"       # override built-in $cache bare root
repo    = "."                            # relative → <project_root>
scratch = "/tmp/$USER/scratch"          # user-defined folder variable

[_STORAGE._HOST."login*.hpc.edu"]
data    = "/scratch/$USER"              # path expressions: $folder/$ENV/~ expand

[bigsim]                                # default selector ($data) → $data/datasets/bigsim
uri   = "https://example.com/bigsim.nc"

[scratch_run]
store = "$cache"                        # disposable, re-fetchable → $cache/datasets/scratch_run
uri   = "https://example.com/scratch.nc"

[derived_table]
store = "$repo"                         # lives under <project_root>/datasets/derived_table
format = "csv"

[hpc_output]
store = "$scratch/results"             # user-defined folder + subpath
format = "nc"
```

**Per-folder-variable precedence** (highest first):
1. `DATAMANIFEST_<FOLDER>_DIR` environment variable (e.g. `DATAMANIFEST_DATA_DIR`).
2. First `[_STORAGE._HOST.<glob>].<folder>` where the glob matches the hostname.
3. `[_STORAGE].<folder>` base value.
4. Built-in: `$data`/`$cache` = `DATAMANIFEST_DIR` if set, else `platformdirs.user_{data,cache}_dir("datamanifest")`; `$repo` = `<project_root>`.
   User-defined folders with no definition on any rung are an error.

`_PROFILE` is accepted and round-tripped verbatim but is not applied during resolution.

**Content path composition** (added by the consuming layer, not the selector):
- Fetched datasets: `<root>[/subpath]/datasets/<key>`
- Produced artifacts: `<root>/cached/<project-id>/<cachetype>/[<version>/]<hash>`

**Read resolution** probes built-in roots under their `datasets/` prefix (`$repo → $data → $cache`), then a legacy read-only probe for old locations (skipped when `DATAMANIFEST_DATA_DIR`/`DATAMANIFEST_DIR` is set).

**Migrating older manifests:** if you have manifests with bare `store = "cache"` entries,
run `datamanifest migrate datasets.toml` to rewrite them to `store = "$cache"` (and similar
for other stores). The `$data` default is elided on write.

## Per-language bindings (`_LANG`)

Language-specific bindings live in a dedicated `_LANG` namespace, so a single manifest can serve multiple language implementations without conflicts.

```toml
[_META]
schema = 1

[mydata._LANG.python]
fetcher = "mypkg.fetch:download_mydata"   # entry-point ref; resolved via importlib
loader  = "mypkg.load:load_mydata"

[_LANG.python.loaders]
csv = "mypkg.loaders:load_csv"            # per-format default for this manifest

[mydata._LANG.julia]
fetcher = "MyPkg.fetch_mydata"            # preserved verbatim; Python never touches it
```

**Fetch ladder** (per dataset, in order):
1. Own `_LANG.python.fetcher` entry-point
2. Own `_LANG.shell.fetcher` template
3. Plain `uri` download
4. Error — no source available

**Load ladder** (per dataset, in order):
1. Own `_LANG.python.loader` entry-point
2. Manifest `[_LANG.python.loaders][format]` default
3. Built-in format default (csv, parquet, nc, …)
4. Error

Delegation to peer CLIs is **not yet implemented** — the ladder stops at built-ins.

### Parameterized bindings

Python `fetcher`/`loader` values may be a `{ ref, args, kwargs }` table instead
of a plain string, allowing the same entry-point to be reused across datasets
that differ only in arguments:

```toml
[esm_5x5._LANG.python.loader]
ref    = "mypkg.load:esm"
kwargs = { grid = "5x5" }

[esm_10x10._LANG.python.loader]
ref    = "mypkg.load:esm"
kwargs = { grid = "10x10" }
```

String values in `args` and `kwargs` undergo `$var` substitution before the
call. Available variables: `$download_path` (fetcher), `$path` (loader),
`$key`, `$version`, `$doi`, `$format`, `$branch`, `$uri`, `$project_root`.

A bare string `fetcher`/`loader` keeps the conventional keyword-argument call.

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim on every read→write cycle; Python never modifies them. Unknown structural tables (any `_*` key that Python does not recognise) are similarly passed through.

### Migration

```bash
datamanifest migrate datasets.toml
```

Updates a manifest in place through all outstanding steps:

- **Legacy flat fields:** moves per-dataset `python=`/`callable=`/`loader=` into `[<ds>._LANG.python]`, moves `[_LOADERS]` into `[_LANG.python.loaders]`, and adds the `[_META]` header. Foreign keys are left verbatim.
- **Storage selectors:** rewrites bare `store = "x"` entries to `store = "$x"` (`"data"`/`""` are elided, leaving the project default). `[_STORAGE]` folder *definitions* (bare keys like `data = "…"`) are left untouched.

Reading an older manifest without migrating still works for most operations, but a manifest with bare `store` values will error on resolution. A one-time deprecation warning is logged for legacy flat fields.

## Python adaptations

The Python port uses the same manifest format as `DataManifest.jl`. The `_LANG` namespace is the preferred form; legacy flat fields are still accepted for backwards compatibility.

**Legacy fields** (still accepted on read):

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`) resolved via `importlib`. The callable receives keyword arguments `(download_path, project_root, entry, uri, key, version, doi, format, branch, requires_paths)`. No inline code execution (`exec`/`eval`) anywhere.
- **`loader=`** — format→ref mapping for the dataset's loader.
- **`python_includes=`** — list of directory paths prepended to `sys.path` during ref resolution.
- **`[_LOADERS]`** — manifest-wide format→ref loader defaults.

These all move into `_LANG.python` / `_LANG.python.loaders`; `datamanifest migrate` performs the conversion.

A single `datasets.toml` can be consumed by both tools: each reads the common fields and ignores the other's extension keys. See [docs/conformance.md](docs/conformance.md) for the shared manifest format and what this implementation supports.

## Related projects

**The DataManifest family (one manifest, many languages).** `datamanifest` shares its [`datasets.toml` format](https://github.com/perrette/datamanifest.toml) with sibling implementations in other languages, so a project in any of them reads the same declaration:

- [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) — the Julia implementation this port is based on, sharing the same `datasets.toml` via the `_LANG` namespace.

(See [docs/conformance.md](docs/conformance.md) for the shared format and the supported feature set.)

**Python alternatives** (single-language; closest established tools for parts of what `datamanifest` does):

- [`fatiando/pooch`](https://www.fatiando.org/pooch/) — the closest established tool; covers the download / SHA-256 verification / unzip layer in pure Python (see [How it compares to Pooch](#how-it-compares-to-pooch)). `datamanifest` adds a load layer, a `requires=` dependency graph, and the cross-language manifest above.
- [`intake`](https://intake.readthedocs.io) — catalog of data sources with drivers that load into pandas/xarray/dask; overlaps with the loader half of `datamanifest`.
- [`cthoyt/pystow`](https://github.com/cthoyt/pystow) — lightweight reproducible download + cached storage with an OS-appropriate data dir; code-driven rather than manifest-driven.

## How it compares to Pooch

If you know [Pooch](https://www.fatiando.org/pooch/), think *"Pooch, but with a richer manifest that also loads the data and works across languages."* Pooch is the established, widely-used tool for the fetch-verify-extract layer (it backs SciPy, scikit-image, and many others), and `datamanifest` covers that same ground — HTTP/Zenodo downloads, SHA-256 verification, unzip/untar. Pooch already has a *registry file* (flat lines of `filename  sha256  [url]`); the three things `datamanifest` adds on top:

1. **A structured manifest that fetches *and* loads.** Beyond filename+hash, one `datasets.toml` carries format, extraction, per-language hooks, and how to turn each dataset into a `pandas`/`xarray` object (the loader ladder) — where Pooch deliberately stops at "here's the verified path."
2. **A dependency graph.** `requires=` resolves datasets in topological order, so derived datasets can be built from others.
3. **A cross-language manifest.** This is the core differentiator: the same `datasets.toml` is consumed by sibling implementations in other languages (today [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) for Julia) via the `_LANG` namespace, so projects in different languages share one declaration without stepping on each other. None of the Python tools above target this.

If you only need download-and-checksum in pure Python, Pooch is the more mature choice. `datamanifest` is aimed at multi-dataset, multi-language scientific projects that want the whole dependency declaration in one file.

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
