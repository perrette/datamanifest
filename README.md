<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup-dark.svg">
    <img src="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup.svg" alt="datamanifest.toml" height="76">
  </picture>
</p>

# datamanifest[py]

[![pypi](https://img.shields.io/pypi/v/datamanifestpy)](https://pypi.org/project/datamanifestpy)
![python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fperrette%2Fdatamanifest%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)
[![CI](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml/badge.svg)](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml)

Keep track of the datasets used in a scientific project. You declare your data
dependencies — URLs, git repositories, checksums, formats — in a
`datamanifest.toml` file; `datamanifest` downloads, verifies, extracts and loads
them, and caches your own computed results with the same machinery. The manifest
format is [shared across languages](https://github.com/perrette/datamanifest.toml),
so implementations in other languages (today Julia) read the same file.

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

## Quick start

```bash
datamanifest init                              # create datamanifest.toml here
datamanifest add https://example.com/data.csv  # register + download + record sha256
datamanifest list                              # what's tracked, and where it lives
datamanifest path data.csv                     # resolve the on-disk path (for a script)
datamanifest verify                            # re-check every checksum
```

**Commit `datamanifest.toml`** — it's the recipe (what to fetch and how). The
downloaded data and a local `.datamanifest-state.toml` (which records *where*
each file landed on this machine) stay git-ignored. A collaborator clones the
repo and runs `datamanifest download` to materialize everything. Data lives
under `./datasets/` and `./cached/` by default — point it elsewhere with
[`datamanifest storage`](#put-data-where-you-want-it).

The split: the **CLI manages** the project's data — set it up, share it,
maintain it. The **API consumes** it — your analysis code resolves and loads
what the manifest declares, and never edits it.

## Use it from your code

```python
import datamanifest

ds = datamanifest.load_dataset("temperature")          # download on first use, then load
                                                       # (pandas/xarray/… per format)
path = datamanifest.get_dataset_path("model_output")   # just the on-disk path
```

Cache an expensive computation, keyed by its keyword arguments:

```python
from datamanifest.cache import cached

@cached
def load_anomaly(*, grid="5x5"):
    ...        # expensive; returns e.g. an xarray.Dataset
    return ds

ds = load_anomaly(grid="5x5")                # first call: computes and stores
ds = load_anomaly(grid="5x5")                # later calls: loads and returns
ds = load_anomaly(grid="5x5", cached=False)  # force recompute
```

Each distinct keyword combination is stored separately. The result is saved
with `pickle` by default; pass `format="nc"`/`"csv"`/… to pick a serialization,
and `version="v2"` to invalidate when the function's *logic* changes.
`datamanifest list` shows cached results grouped by function with their
parameters; `datamanifest list --orphan --delete` cleans up.

The module-level functions find the project's manifest automatically (walking
up from the working directory; `DATAMANIFEST_TOML` overrides). Pass an explicit
`db = datamanifest.Database("datamanifest.toml")` as the first argument to
bypass auto-discovery. The CLI's verbs are also available in code —
`datamanifest.add(...)` is handy in a notebook — see the docstrings
(`help(datamanifest)`) and the [design notes](docs/design-notes.md).

## Use cases

### Manage datasets from the CLI

```bash
datamanifest add https://host/path/file.nc                     # a direct URL
datamanifest add 10.5281/zenodo.1234567 --pick "*.csv"         # a Zenodo record's files
datamanifest add "https://github.com/u/repo/archive/v2.1.zip" --extract
datamanifest add s3://bucket/key.zarr --on-the-fly             # open in place, no download

datamanifest list                       # one styled line each, clickable locations
datamanifest show file.nc               # full entry detail
datamanifest remove file.nc             # drop the entry

datamanifest verify                     # re-check all checksums (e.g. before submission)
datamanifest update-checksums           # recompute them after regenerating data

python analysis.py --data "$(datamanifest path file.nc)"   # composable in shell
```

### Repair: reassociate data on disk

The tool records where every file actually lives (a small git-ignored state
file), so moving data around by hand is recoverable — `refresh` reconciles the
records with disk, and `--scan` discovers copies elsewhere on the machine (e.g.
downloaded by another project) and adopts them, checksum-verified, instead of
re-downloading:

```bash
datamanifest list --dirty       # preview: records that disagree with disk
datamanifest refresh            # repoint moved files, drop deleted, adopt untracked
datamanifest refresh --scan     # also discover & adopt copies found elsewhere
datamanifest where              # the active manifest, state file, and data folders
datamanifest storage            # the resolved storage config (much the same view)
```

`refresh` only edits local state — never your data, never the manifest. To act
on the bytes themselves, filter with `list` and apply
`--delete` / `--move DIR` (`--dry-run` previews):

```bash
datamanifest list --cached --orphan --delete       # clean up orphaned cached artifacts
datamanifest list --older-than 30d --delete --dry-run
```

### Put data where you want it

Storage is two folders set in `[_STORAGE]` — `datasets_dir` (fetched data) and
`datacache_dir` (`@cached` results) — repo-local `./datasets/` and `./cached/`
by default. `datamanifest storage` edits them, per host if you like:

```bash
datamanifest storage set datasets_dir "/scratch/$USER/data"                  # this host only
datamanifest storage set datacache_dir "$user_cache_dir/myproj" --all-hosts  # project default
datamanifest storage                                                         # show resolved config
```

Pointing the folders at a machine directory (instead of the repo) shares data
across clones and projects. Path expressions, per-host rules, per-dataset
overrides and read pools: [storage model](docs/cli.md#storage-model).

### Sync between machines

Move a stored object between machines instead of re-downloading or recomputing
it. Objects are addressed machine-independently — a dataset by name, a cached
artifact by `function/hash` — and land in the receiver's own folders:

```bash
datamanifest push foo user@hpc             # copy dataset `foo` to the host (rsync over ssh)
datamanifest pull esm_anomaly/83425a3 hpc  # pull a cached artifact by hash prefix
datamanifest push foo user@hpc --dry-run   # preview resolved paths + size
datamanifest list --cached --push user@hpc # bulk: push a filtered selection
```

Sync is bytes-only and idempotent; it needs the data folders to be
machine-global (not repo-local) on both ends. Details:
[docs/cli.md](docs/cli.md#sync-between-machines).

### One manifest, several languages

A dataset can carry per-language `fetcher`/`loader` bindings under `_LANG`; each
implementation runs its own and preserves the others verbatim, so one manifest
serves a mixed Python/Julia project:

```toml
[mydata]
uri = "https://example.com/mydata.csv"

[mydata._LANG.python]
loader = "mypkg.load:load_mydata"      # how Python loads it

[mydata._LANG.julia]
loader = "MyPkg.load_mydata"           # Julia's binding; Python never touches it
```

A single-language project can skip the `_LANG` ceremony with bare
`fetcher` / `loader` / `shell` fields, and `[_LOADERS]` maps formats to project-wide
loaders. Resolution ladders, parameterized bindings (`{ ref, args, kwargs }`),
and fetching through another language's toolchain:
[docs/language-bindings.md](docs/language-bindings.md).

## CLI overview

| Command | What it does |
|---|---|
| `init` | Create a fresh `datamanifest.toml` |
| `add` | Register and download a dataset (URL, Zenodo DOI, `s3://`, …) |
| `import` | Bulk-import another tool's catalog ([below](#importing-from-other-tools)) |
| `list` | List datasets and cached artifacts; filters compose with `--delete` / `--move` / `--push` / `--pull` |
| `show` / `path` / `where` | Entry detail / on-disk path / active manifest and folders |
| `download` | Download declared datasets (e.g. after cloning) |
| `verify` / `update-checksums` | Re-check / recompute sha256 checksums |
| `refresh` | Reconcile the state file with disk; `--scan` adopts copies found elsewhere |
| `push` / `pull` | Transfer a stored object to / from an SSH host |
| `delete` / `move` | Delete / move a stored object's bytes (not its manifest entry) |
| `remove` | Delete a manifest entry |
| `storage` | Show or edit `[_STORAGE]`, per host if needed |
| `format` / `migrate` | Canonical rewrite / upgrade an older manifest in place |

Full flags and behaviour: **[docs/cli.md](docs/cli.md)**, or
`datamanifest COMMAND -h`.

## Importing from other tools

`add` takes a *reference to data*; `import` ingests *another tool's catalog*.
Both end at standard manifest entries, and already-downloaded files are adopted
in place (checksum-verified) — no re-download:

```bash
datamanifest import pooch registry.txt --base-url URL --cache-dir DIR   # adopts pooch's cache
datamanifest import csv files.csv                     # a name,url,sha256 table
datamanifest import urls list.txt --base-url URL      # a plain list of URLs
datamanifest import intake catalog.yml                # an intake catalog ([yaml] extra)
datamanifest import dvc path-or-dir                   # *.dvc / dvc.lock (+ .dvc/cache)
```

Per-source detail: **[docs/adding-datasets.md](docs/adding-datasets.md)**.

## Related projects

**The DataManifest family.** The [`datamanifest.toml` format](https://github.com/perrette/datamanifest.toml)
is shared across languages: [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)
(Julia, same author) reads the same manifest via the `_LANG` namespace. See
[docs/conformance.md](docs/conformance.md) for the shared format and what this
implementation supports.

**Python alternatives:**

- [`fatiando/pooch`](https://www.fatiando.org/pooch/) — the reference tool for
  the download / checksum / unzip layer (it backs SciPy, scikit-image, and many
  others). `datamanifest` covers the same ground and adds loading the data, the
  `@cached` result cache, a `requires=` dependency graph, and the cross-language
  manifest. Already using Pooch?
  `datamanifest import pooch registry.txt --cache-dir "$(python -c 'import pooch; print(pooch.os_cache("yourpkg"))')"`
  converts the registry and adopts your downloaded files in place.
- [`intake`](https://intake.readthedocs.io) — catalog of data sources with
  drivers that load into pandas/xarray/dask; overlaps with the loader half of
  `datamanifest`.
- [`cthoyt/pystow`](https://github.com/cthoyt/pystow) — lightweight reproducible
  download + cached storage with an OS-appropriate data dir; code-driven rather
  than manifest-driven.

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl),
written by the same author (Mahé Perrette). The Python port was implemented
with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
