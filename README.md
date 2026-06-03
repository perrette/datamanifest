# datamanifest

[![pypi](https://img.shields.io/pypi/v/datamanifestpy)](https://pypi.org/project/datamanifestpy)
![python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fperrette%2Fdatamanifest%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)
[![CI](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml/badge.svg)](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml)

Keep track of datasets used in a scientific project.

`datamanifest` provides a simple way to declare data dependencies — URLs, git repositories, checksums, formats — in a `datasets.toml` file, and handles download, verification, extraction, and loading. It is a Python port of [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) (same author), with the same manifest format and feature surface.

### How it compares to Pooch

If you know [Pooch](https://www.fatiando.org/pooch/), think *"Pooch, but with a richer manifest that also loads the data and works across languages."* Pooch is the established, widely-used tool for the fetch-verify-extract layer (it backs SciPy, scikit-image, and many others), and `datamanifest` covers that same ground — HTTP/Zenodo downloads, SHA-256 verification, unzip/untar. Pooch already has a *registry file* (flat lines of `filename  sha256  [url]`); the three things `datamanifest` adds on top:

1. **A structured manifest that fetches *and* loads.** Beyond filename+hash, one `datasets.toml` carries format, extraction, per-language hooks, and how to turn each dataset into a `pandas`/`xarray` object (the loader ladder) — where Pooch deliberately stops at "here's the verified path."
2. **A dependency graph.** `requires=` resolves datasets in topological order, so derived datasets can be built from others.
3. **A cross-language manifest.** This is the core differentiator: `datamanifest` is one member of a multi-language *DataManifest family* built on a [shared TOML schema](https://github.com/perrette/datamanifest.toml). The same `datasets.toml` is consumed by sibling implementations in other languages (today [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) for Julia) via the `_LANG` namespace, so projects in different languages share one declaration without stepping on each other. None of the Python tools below target this.

If you only need download-and-checksum in pure Python, Pooch is the more mature choice. `datamanifest` is aimed at multi-dataset, multi-language scientific projects that want the whole dependency declaration in one file.

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

ds = load_anomaly(grid="5x5")          # computes, materializes under $cache, registers in cached.toml
ds = load_anomaly(grid="5x5")          # cache hit: loads and returns
ds = load_anomaly(grid="5x5", cached=False)  # force recompute
```

The keyword arguments (minus `_`-prefixed runtime knobs) are hashed (canonical JSON → SHA-256) into a portable `<cachetype>/<hash>` key; the artifact and its `config.toml` / `metadata.toml` sidecars live under `$cache`. Produced datasets are **not** written into `datasets.toml` — they are indexed in a sibling `cached.toml`, and `datamanifest gc` reclaims unreferenced ones. The cache layer (`datamanifest.cache`) is an in-repo layer over the shared `datamanifest.store` substrate and never touches the fetch path.

## CLI usage

```
datamanifest COMMAND [OPTIONS]
```

| Command | Description |
|---|---|
| `list [--present\|--missing\|--all]` | List datasets; default shows present first, then missing |
| `download [NAME ...] [--all] [--overwrite]` | Download specific datasets or all of them |
| `path NAME` | Print the resolved on-disk path (composable in shell) |
| `add URI [--name N] [--no-download] [--extract]` | Register and (by default) download a dataset |
| `remove NAME [--keep-cache]` | Delete an entry, optionally preserving cached files |
| `show NAME` | Print full entry detail in TOML style |
| `verify [NAME ...]` | Re-check sha256 checksums; exits nonzero on any mismatch |
| `init [--folder PATH] [--force]` | Create a fresh `datasets.toml` in the current directory |
| `where` | Print active `datasets_toml` and `datasets_folder` paths |
| `migrate FILE` | Rewrite a manifest to the current schema in-place (v0→v1 `_LANG` form; v1.1→v2 bare-store `"x"` → `"$x"`) |
| `gc [--dry-run] [--grace AGE]` | Reclaim unreferenced `@cached` artifacts under `$cache` (root-reachability; default grace `7d`) |

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

# Reclaim unreferenced @cached artifacts under $cache
datamanifest gc --dry-run                 # preview what would be collected
datamanifest gc --grace 30d               # delete orphans older than 30 days

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
| CLI (`datamanifest list/download/path/add/remove/show/verify/update-checksums/init/where/migrate/format/gc`) | yes |
| Schema v1 `_LANG` namespace (read + write) | yes |
| Fetch ladder: own Python fetcher → shell template → URI | yes |
| Load ladder: own Python loader → manifest default → built-in | yes |
| Lossless round-trip of foreign `_LANG.*` subtrees | yes |
| v0 → v1 migration (`datamanifest migrate`) | yes |
| Portable storage model (spec-v2: `$`-selectors, folder variables, `[_STORAGE]` + platformdirs roots) | yes |
| Parameterized bindings (`{ ref, args, kwargs }` + `$var` substitution) | yes |
| Safe concurrent materialization (`.tmp` → atomic publish → `.complete` marker) | yes |
| Verify-once integrity (checksum only at fetch; `.complete` entry skips re-hash) | yes |
| Recursive canonical key ordering / byte-identity (normative reference) | yes |
| Produce-or-load cache (`@cached`: param-hash keying, `config.toml`/`metadata.toml` sidecars) | yes |
| `cached.toml` index + `datamanifest gc` (root-reachability collector) | yes |

## Storage model (spec-v2)

> **Behavior change from earlier releases.** Prior releases stored all datasets
> under `$XDG_CACHE_HOME/Datasets` (typically `~/.cache/Datasets`).
> As of spec-v1.1, the default `$data` store resolves to
> `platformdirs.user_data_dir("datamanifest")/Datasets` (typically
> `~/.local/share/datamanifest/Datasets` on Linux), and the `$cache` store to
> `platformdirs.user_cache_dir("datamanifest")/Datasets`.
> If you have existing datasets at the old location, move them or pass an explicit
> `datasets_folder` to `Database`.

Each dataset entry carries an optional `store` field — a **`$`-selector**
(`$folder` or `$folder/subpath`) referencing a named **folder variable**. The
built-in folder variables are `$data`, `$cache`, and `$repo`. User-defined folders
are declared in `[_STORAGE]`.

A `[_STORAGE]` table lets you define folder variables, set a project-wide default
selector, and override roots per host (glob) or per profile:

```toml
[_STORAGE]
default = "$data"                        # project-wide default store selector
data    = "~/data/Datasets"             # override built-in $data root
cache   = "~/.cache/Datasets"           # override built-in $cache root
repo    = "datasets"                    # relative → <project_root>/datasets
scratch = "/tmp/$USER/scratch"          # user-defined folder variable

[_STORAGE._HOST."login*.hpc.edu"]
data    = "/scratch/$USER/Datasets"     # path expressions: $folder/$ENV/~ expand

[_STORAGE._PROFILE.cluster]
data    = "/work/proj/Datasets"         # activated by DATAMANIFEST_PROFILE=cluster

[bigsim]                                # default selector ($data) — store omitted
uri   = "https://example.com/bigsim.nc"

[scratch_run]
store = "$cache"                        # disposable, re-fetchable
uri   = "https://example.com/scratch.nc"

[derived_table]
store = "$repo"                         # lives under <project_root>/datasets
format = "csv"

[hpc_output]
store = "$scratch/results"             # user-defined folder + subpath
format = "nc"
```

**Per-folder-variable precedence** (highest first):
1. `DATAMANIFEST_<FOLDER>_DIR` environment variable (e.g. `DATAMANIFEST_DATA_DIR`).
2. `[_STORAGE._PROFILE.<name>].<folder>` — when `DATAMANIFEST_PROFILE` is set.
3. First `[_STORAGE._HOST.<glob>].<folder>` where the glob matches the hostname.
4. `[_STORAGE].<folder>` base value.
5. `platformdirs` default (`data`/`cache`) or `<project_root>/datasets` (`repo`).
   User-defined folders with no definition on any rung are an error.

**Read resolution** probes the entry's own selector folder first, then searches
`$repo → $data → $cache` (built-in probe order) and returns the first root where
`<root>/<key>` exists and has been successfully materialized (`.complete` marker
present). Falls back to the write path (selected store) when not found.

**v1.1 → v2 migration:** If you have existing manifests with bare `store = "cache"`
entries, run `datamanifest migrate datasets.toml` to rewrite them to `store = "$cache"`
(and similar for other stores). The `$data` default is elided on write.

## Schema v1 — `_LANG` namespace

Schema v1 separates language-specific bindings into a dedicated `_LANG` namespace so that a single manifest can serve multiple language implementations without conflicts.

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

### Parameterized bindings (spec-v1.1)

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

A bare string `fetcher`/`loader` keeps the conventional keyword-argument call
and requires no capability upgrade.

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim on every read→write cycle; Python never modifies them. Unknown structural tables (any `_*` key that Python does not recognise) are similarly passed through.

### Migration

```bash
datamanifest migrate datasets.toml
```

Rewrites a manifest in-place through all outstanding migration steps:

- **v0 → v1:** moves per-dataset `python=`/`callable=`/`loader=` into `[<ds>._LANG.python]`, moves `[_LOADERS]` into `[_LANG.python.loaders]`, and adds `[_META] schema = 1`. Foreign keys are left verbatim.
- **v1.1 → v2 (storage):** rewrites bare `store = "x"` entries to `store = "$x"` (`"data"`/`""` are elided, leaving the project default). `[_STORAGE]` folder *definitions* (bare keys like `data = "…"`) are left untouched.

Reading a v0 or v1.1 file without migrating still works for most operations, but a v1.1 manifest with bare `store` values will error on resolution (per spec-v2). A one-time deprecation warning is logged for v0 forms.

## Python adaptations

The Python port uses the same manifest format as `DataManifest.jl`. Schema v1 is the preferred form; schema v0 (flat fields) is still accepted for backwards compatibility.

**v0 / legacy fields** (still accepted on read):

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`) resolved via `importlib`. The callable receives keyword arguments `(download_path, project_root, entry, uri, key, version, doi, format, branch, requires_paths)`. No inline code execution (`exec`/`eval`) anywhere.
- **`loader=`** — format→ref mapping for the dataset's loader.
- **`python_includes=`** — list of directory paths prepended to `sys.path` during ref resolution.
- **`[_LOADERS]`** — manifest-wide format→ref loader defaults.

In schema v1 all of the above move into `_LANG.python` / `_LANG.python.loaders`. The `datamanifest migrate` command performs the conversion.

A single `datasets.toml` can be consumed by both tools: each reads the common fields and ignores the other's extension keys. The shared schema is documented at [perrette/datamanifest.toml](https://github.com/perrette/datamanifest.toml).

## Conformance

This release targets **spec-v2** of the shared [datamanifest.toml schema](https://github.com/perrette/datamanifest.toml).

Implemented capabilities:

| Capability | Status |
|---|---|
| `lang-read` — parse `_LANG` namespace on read | yes |
| `lang-write` — regenerate `_LANG.python`, preserve foreign `_LANG.*` verbatim | yes |
| `shell-fetch` — `_LANG.shell.fetcher` template in the fetch ladder | yes |
| `storage` — `$`-selectors, folder variables (`$data`/`$cache`/`$repo` + user-defined), `[_STORAGE].default`, path-expression interpolation, platformdirs roots, read-order resolution | yes |
| `binding-args` — `{ ref, args, kwargs }` table form with `$var` substitution | yes |
| `byte-identity` — recursive canonical key ordering (normative reference) | yes |
| `cache-produce` — `@cached` produce-or-load: param-hash keying + `config.toml`/`metadata.toml` sidecars | yes |
| `cache-gc` — `cached.toml` index + `datamanifest gc` root-reachability collector | yes |
| `delegation` — peer-CLI runtime (delegate fetch/load to another tool) | not yet |

The conformance test suite (`tests/test_conformance.py`) downloads the pinned spec fixture tarball, verifies every file against a recorded per-file SHA-256 hash (`tests/conformance_pin.toml`), and runs only the fixtures whose `capabilities` are a subset of the above set, skipping the rest with a reason. Re-pinning the fixture suite to the `spec-v2` tag is a manual post-merge step (requires network access).

## Related projects

**The DataManifest family (one manifest, many languages):**

- [`perrette/datamanifest.toml`](https://github.com/perrette/datamanifest.toml) — the shared TOML schema spec; the common contract every implementation reads.
- [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) — the Julia implementation this port is based on, sharing the same `datasets.toml` via the `_LANG` namespace.

**Python alternatives** (single-language; closest established tools for parts of what `datamanifest` does):

- [`fatiando/pooch`](https://www.fatiando.org/pooch/) — the closest established tool; covers the download / SHA-256 verification / unzip layer in pure Python (see [How it compares to Pooch](#how-it-compares-to-pooch)). `datamanifest` adds a load layer, a `requires=` dependency graph, and the cross-language manifest above.
- [`intake`](https://intake.readthedocs.io) — catalog of data sources with drivers that load into pandas/xarray/dask; overlaps with the loader half of `datamanifest`.
- [`cthoyt/pystow`](https://github.com/cthoyt/pystow) — lightweight reproducible download + cached storage with an OS-appropriate data dir; code-driven rather than manifest-driven.

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
