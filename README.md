# datamanifest

[![pypi](https://img.shields.io/pypi/v/datamanifestpy)](https://pypi.org/project/datamanifestpy)
![python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fperrette%2Fdatamanifest%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)
[![CI](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml/badge.svg)](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml)

Keep track of datasets used in a scientific project.

`datamanifest` provides a simple way to declare data dependencies — URLs, git repositories, checksums, formats — in a `datasets.toml` file, and handles download, verification, extraction, and loading. It is a Python port of [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) (same author), with the same manifest format and feature surface.

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
| `migrate FILE` | Rewrite a v0 manifest to schema v1 (`_LANG` form) in-place |

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
| CLI (`datamanifest list/download/path/add/remove/show/verify/init/where/migrate`) | yes |
| Schema v1 `_LANG` namespace (read + write) | yes |
| Fetch ladder: own Python fetcher → shell template → URI | yes |
| Load ladder: own Python loader → manifest default → built-in | yes |
| Lossless round-trip of foreign `_LANG.*` subtrees | yes |
| v0 → v1 migration (`datamanifest migrate`) | yes |

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

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim on every read→write cycle; Python never modifies them. Unknown structural tables (any `_*` key that Python does not recognise) are similarly passed through.

### v0 → v1 migration

```bash
datamanifest migrate datasets.toml
```

Rewrites a v0 flat manifest in-place: moves per-dataset `python=`/`callable=`/`loader=` into `[<ds>._LANG.python]`, moves `[_LOADERS]` into `[_LANG.python.loaders]`, and adds `[_META] schema = 1`. Foreign keys are left verbatim. Reading a v0 file without migrating still works (legacy forms are accepted silently), but a one-time deprecation warning is logged.

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

This release targets **spec-v1.0** of the shared [datamanifest.toml schema](https://github.com/perrette/datamanifest.toml).

Implemented capabilities:

| Capability | Status |
|---|---|
| `lang-read` — parse `_LANG` namespace on read | yes |
| `lang-write` — regenerate `_LANG.python`, preserve foreign `_LANG.*` verbatim | yes |
| `shell-fetch` — `_LANG.shell.fetcher` template in the fetch ladder | yes |
| `delegation` — peer-CLI runtime (delegate fetch/load to another tool) | not yet |

The conformance test suite (`tests/test_conformance.py`) downloads the pinned spec-v1.0 fixture tarball, verifies every file against a recorded per-file SHA-256 hash (`tests/conformance_pin.toml`), and runs only the fixtures whose `capabilities` are a subset of the above set, skipping the rest with a reason.

## Related projects

- [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) — the Julia implementation this port is based on.
- [`perrette/datamanifest.toml`](https://github.com/perrette/datamanifest.toml) — the shared TOML schema spec consumed by both implementations.

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
