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
datamanifest init                  # create datamanifest.toml here
datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
datamanifest list                  # what's tracked, and where it lives
datamanifest path co2              # resolve the on-disk path (for a script)
datamanifest storage               # where data goes on this host; `storage set` to change
```

The `add` above downloaded the Mauna Loa CO₂ record and wrote one entry to
`datamanifest.toml` — a plain TOML file you can read and edit by hand:

```toml
[co2]
sha256 = "0058b3788040b5c27b2b5c1dd6d26226b7e4deef85e34c153e64806c37df7c75"
uri = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
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

df = datamanifest.load_dataset("co2")          # download on first use, then load
                                               # (pandas/xarray/… per format)
path = datamanifest.get_dataset_path("co2")    # just the on-disk path
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
up from the working directory; `DATAMANIFEST_TOML` overrides). To bypass
auto-discovery, create an explicit `db = datamanifest.Database("datamanifest.toml")`
and use its methods (`db.register_dataset(...)`) and the db-taking functions in
`datamanifest.pipelines`. The CLI's verbs are also available in code —
`datamanifest.add(...)` is handy in a notebook — see the docstrings
(`help(datamanifest)`) and the [design notes](docs/design-notes.md).

For library code that wants checksummed downloads into a folder it controls —
an OS-appropriate data dir, say — a **file-less database** skips the manifest
entirely: no `datamanifest.toml`, no state file, nothing written but the data.
The folder accepts the same `$`-symbols as the storage model:

```python
from datamanifest import Database
from datamanifest.pipelines import download_dataset

db = Database(datasets_folder="$user_data_dir/mylib", persist=False)
db.register_dataset("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
path = download_dataset(db, "co2")   # → ~/.local/share/mylib/gml.noaa.gov/…/co2_annmean_mlo.csv
```

## Use cases

### Manage datasets from the CLI

```bash
datamanifest add https://host/path/file.nc                     # a direct URL
datamanifest add 10.5281/zenodo.1234567 --pick "*.csv"         # a Zenodo record's files
datamanifest add "https://github.com/u/repo/archive/v2.1.zip" --extract
datamanifest add s3://bucket/key.zarr --lazy                   # open in place, no download

datamanifest list                       # one styled line each, clickable locations
datamanifest show co2                   # full entry detail
datamanifest remove old_entry           # drop an entry

datamanifest verify                     # re-check all checksums (e.g. before submission)
datamanifest update-checksums           # recompute them after regenerating data

python analysis.py --data "$(datamanifest path co2)"   # composable in shell
```

A concrete run — continuing from the [quick start](#quick-start)'s CO₂ record,
add the HadCRUT5 global temperature series next to it:

```console
$ datamanifest add "https://www.metoffice.gov.uk/hadobs/hadcrut5/data/HadCRUT.5.0.2.0/analysis/diagnostics/HadCRUT.5.0.2.0.analysis.summary_series.global.annual.csv" --name temperature
$ datamanifest list
Datasets
● co2          csv         3.1 KiB  …webdata/ccgg/trends/co2/co2_annmean_mlo.csv
● temperature  csv         6.9 KiB  …0.analysis.summary_series.global.annual.csv

Cached
◆ myproj.load_anomaly  pickle  2×  768 B
    40384c4db019  grid=10x10                                         386 B
    50f04896d3ee  grid=5x5                                           382 B
```

`temperature` now loads from code just like `co2` —
`datamanifest.load_dataset("temperature")` — and the **Cached** group lists the
`load_anomaly(grid=…)` results from the
[`@cached` example](#use-it-from-your-code) above, grouped by function with
their parameters.

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
datamanifest refresh --scan --datasets-pools ~/other-project/datasets /shared/data \
                            --datacache-pools /shared/cache   # extend the scan to extra folders
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

- [`fatiando/pooch`](https://www.fatiando.org/pooch/) — the established tool
  for fetching and verifying data from Python code (it backs SciPy,
  scikit-image, and many others). `datamanifest` covers that ground and centers
  on three things Pooch doesn't aim for: an explicit, cross-language manifest
  file as the single source of truth; a CLI that manages the whole dataset
  lifecycle — add, verify, repair, sync — without touching code; and the
  [`@cached`](#use-it-from-your-code) cache for your own computed results —
  orthogonal to fetching, but sharing the same storage and bookkeeping. Already
  using Pooch?
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
