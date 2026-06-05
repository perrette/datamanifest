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

Keep track of datasets used in a scientific project: data dependencies and internal caching.

`datamanifest` provides a simple way to declare data dependencies — URLs, git repositories, checksums, formats — in a `datamanifest.toml` file, and handles download, verification, extraction, and loading. It can now also cache your own computed results (versioned), reusing the same infrastructure. `datamanifest` started as a Python port of [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) (same author), sharing its manifest format and feature surface; it has since grown a CLI and now develops in parallel as the Python implementation of a [multi-language specification](https://github.com/perrette/datamanifest.toml).

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
under `./datasets/` and `./cached/` by default — point them elsewhere (e.g. a
scratch partition, per host) with [`datamanifest storage`](#storage-model).

New here? Skim the [API quickstart](#api-quickstart) and [`@cached`](#produce-or-load-caching-cached)
below; the full format and behaviour are in the
[specification](https://github.com/perrette/datamanifest.toml) and
[design notes](docs/design-notes.md).

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
db = datamanifest.Database("datamanifest.toml", "my-data-folder")
datamanifest.add(db, "https://zenodo.org/record/.../file.csv")
path = datamanifest.get_dataset_path(db, "file")
```

The module-level functions (`add`, `download_dataset`, `load_dataset`, `get_dataset_path`, …) look up a process-wide default `Database` via `pyproject.toml` discovery, the `DATAMANIFEST_TOML` / `DATASETS_TOML` environment variables, or a `datamanifest.toml` (or legacy `datasets.toml`) file in the working tree. Pass an explicit `db` as the first argument to bypass auto-discovery.

### Produce-or-load caching (`@cached`)

Cache the result of an expensive computation, keyed by its keyword arguments:

```python
from datamanifest.cache import cached

@cached
def load_anomaly(*, grid="5x5"):
    ...        # expensive; returns e.g. an xarray.Dataset
    return ds

ds = load_anomaly(grid="5x5")          # first call: computes and stores
ds = load_anomaly(grid="5x5")          # later calls: loads and returns
ds = load_anomaly(grid="5x5", cached=False)  # force recompute
```

The keyword arguments are the cache key — each distinct combination is stored separately. By default the result is saved with `pickle`; pass `format="nc"`/`"csv"`/… to pick a serialization, and `version="v2"` to invalidate when the function's *logic* changes.

Where things live is configured **once** in `datamanifest.toml` ([Storage model](#storage-model)) and applies to both downloaded and cached data. By default produced artifacts land in a visible `./cached/` under the project root; set `datacache_dir` in `[_STORAGE]` (e.g. to a scratch partition) to centralize them elsewhere.

`datamanifest list` shows cached results grouped by function with their parameters; `datamanifest list --orphan --delete` cleans up.

Advanced details — how the cache identity (`cachetype`) is derived, conflict detection, and the [state file](#state-file-datamanifest-statetoml) that records where each object lives — are in the [design notes](docs/design-notes.md) and the [state-file design note](docs/design-state-file.md).

## CLI usage

```
datamanifest COMMAND [OPTIONS]
```

| Command | Description |
|---|---|
| `list [SEARCH ...] [--cached\|--datasets] [--present\|--missing\|--all] [--orphan] [--dirty] [--outside] [--hash P ...] [--older-than AGE] [--format F] [--fields ...] [--delete\|--move DIR] [--dry-run]` | List datasets and cached artifacts, with their state↔disk status; with `--delete`/`--move` becomes the maintenance command. `--outside` narrows to tracked objects stored outside `datasets_dir`/`datacache_dir` and the read pools. The filtered selection applies directly (`--dry-run` previews); `--delete`/`--move` act on both artifacts and fetched datasets (protected data is skipped) |
| `refresh [--scan] [--dry-run]` | Reconcile the state file (`.datamanifest-state.toml`) with disk: relocate stale records, drop missing ones, adopt present-but-untracked datasets. `--scan` also probes the read pools (incl. legacy locations) and adopts pool-present datasets (checksum-gated; no downloads/copies) — the active twin of `where --scan`. Edits only local state, so it applies by default; `--dry-run` previews |
| `download [NAME ...] [--all] [--overwrite] [--delegate\|--no-delegate]` | Download specific datasets or all of them; `--no-delegate` disables the cross-language fetch rung for the run |
| `path NAME` | Print the resolved on-disk path (composable in shell) |
| `add URI\|DOI [--name N] [--pick GLOB] [--no-download] [--extract]` | Register and (by default) download a dataset. A **Zenodo** DOI / record URL expands to one dataset per file (declare-only; `--pick GLOB` selects a subset, `--name` is the name prefix) |
| `remove NAME [--keep-cache]` | Delete an entry, optionally preserving cached files |
| `show NAME` | Print full entry detail in TOML style |
| `verify [NAME ...]` | Re-check sha256 checksums; exits nonzero on any mismatch |
| `update-checksums [NAME ...] [--dry-run]` | Recompute stored checksums from what's on disk |
| `init [--folder PATH] [--force]` | Create a fresh `datamanifest.toml` in the current directory |
| `where [--manifest\|--state-file\|--datasets-dir\|--datacache-dir] [--scan]` | Show the active manifest, state file, and the data dirs resolved for this host with their read pools folded in; notes how many tracked objects live outside those folders (`list --outside` to inspect). A selector flag prints just that one bare path (scriptable); `--scan` probes read pools for datasets present there but not local |
| `storage [show]` / `storage set FIELD VALUE… [--host GLOB\|--all-hosts]` / `storage unset FIELD [...]` | Show or edit `[_STORAGE]` without hand-writing the `_HOST` syntax. `set`/`unset` target **this host** by default (a `[_STORAGE._HOST."<hostname>"]` override); `--host GLOB` targets a host pattern, `--all-hosts` the project-wide base. `FIELD` is `datasets_dir`/`datacache_dir`, a user `$symbol`, or a `datasets_pools`/`datacache_pools` list |
| `migrate FILE [--dry-run]` | Reshape a spec-v3 manifest's `[_STORAGE]` to the spec-v4 two-field model: write `datasets_dir`/`datacache_dir` at their defaults, drop the retired keys, carry `local_path` → `storage_path`. Moves no bytes |
| `import {pooch\|csv\|urls} SOURCE [--base-url URL] [--cache-dir DIR] [--overwrite] [--dry-run]` | Bulk-import datasets from another tool's catalog. **pooch**: a registry file (`filename [algo:]hash [url]`). **csv**: a `name,url,sha256` file. **urls**: a plain URL list. `uri` is the entry's URL or `base_url + name`; with `--cache-dir` already-downloaded files are adopted in place, checksum-verified — no re-download |
| `push ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **to** an SSH host (rsync over ssh), addressed by id (a dataset's `key`, or `cachetype[/version]/hash`) |
| `pull ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **from** an SSH host (rsync over ssh), same addressing |
| `delete ID [--dry-run] [--batch]` | Delete a stored object's **bytes** (and prune its state record), addressed by id like `push`/`pull` — *not* the manifest entry (use `remove` for that). Protected data is skipped |
| `move ID DEST [--dry-run] [--batch]` | Move a stored object's **bytes** under DEST and repoint its state record (the manifest is not edited), addressed by id |

Examples:

```bash
# Set up a new project
datamanifest init

# Add and download a dataset
datamanifest add "https://zenodo.org/record/.../file.zip" --extract

# List this project's datasets and cached artifacts (one styled line each,
# clickable file:// locations); --all also shows orphans / other projects'
datamanifest list
datamanifest list --all

# Use the path in a shell pipeline
python analysis.py --data "$(datamanifest path file)"

# Verify all checksums before a paper submission
datamanifest verify

# Recompute stored checksums from what's on disk (e.g. after regenerating data)
datamanifest update-checksums --dry-run   # preview which would change
datamanifest update-checksums             # write the new checksums

# Inspect and clean up @cached artifacts
datamanifest list --cached --orphan               # dry-run: list orphaned cached artifacts
datamanifest list --cached --orphan --delete      # delete them (the filter is the selection)
datamanifest list --older-than 30d --delete --dry-run  # preview artifacts older than 30 days

# Reconcile the state file with disk (after moving data around by hand)
datamanifest list --dirty                          # preview: objects whose record ≠ disk
datamanifest refresh                               # apply: relocate stale, drop missing, adopt untracked
datamanifest refresh --dry-run                     # same, but preview without writing

# Where is the active manifest?
datamanifest where

# Move a stored object between machines (rsync over ssh; no re-download/recompute)
datamanifest push foo user@hpc --dry-run            # preview: resolved paths + size
datamanifest push foo user@hpc                      # push the dataset `foo` to the host
datamanifest pull esm_anomaly/83425a3 user@hpc      # pull a produced artifact by hash prefix
datamanifest list --cached --push user@hpc          # bulk: push the filtered set
```

### Adding datasets from external sources

`add` takes a *reference to data* (one invocation, possibly several files);
`import` ingests *another tool's catalog*. Both end at standard manifest entries,
and reuse already-downloaded copies in place where possible.

```bash
# add — a reference to data
datamanifest add https://host/path/file.nc            # a direct URL
datamanifest add 10.5281/zenodo.1234567               # a Zenodo DOI / record URL (all its files)

# import — another tool's catalog
datamanifest import pooch registry.txt --base-url URL --cache-dir DIR   # adopts pooch's cache
datamanifest import csv files.csv                     # a name,url,sha256 table
datamanifest import urls list.txt --base-url URL      # a plain list of URLs
datamanifest import intake catalog.yml                # an intake catalog                         [planned]
datamanifest import dvc path-or-dir                   # *.dvc / dvc.lock (+ .dvc/cache)           [planned]
```

`intake` / `dvc` are still planned (they need a YAML parser); everything else
ships today. See **[docs/adding-datasets.md](docs/adding-datasets.md)** for the
full per-source detail.

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
| Shell template hook (bare `shell`, language-agnostic) | yes |
| Python entry-point hook (`_LANG.python.fetcher` / bare `fetcher` / legacy `python=`) | yes |
| Language-implicit (bare) `fetcher`/`loader` + `[_LOADERS]` map (fail-loud for present bindings) | yes |
| Named + default loaders (csv, parquet, nc, json, yaml, toml, zip, tar) | yes |
| TOML manifest round-trip (read `tomllib`, write `tomli_w`) | yes |
| Project-root auto-discovery (`pyproject.toml` walk, env vars) | yes |
| CLI (`list/download/path/add/remove/show/verify/update-checksums/init/where/migrate/format`) | yes |
| `_LANG` namespace for per-language bindings (read + write) | yes |
| Fetch ladder: own Python fetcher (explicit/bare/legacy) → bare shell → cross-language fetch → URI | yes |
| Load ladder: own Python loader (explicit/bare) → manifest default (`[_LANG.python.loaders]`/`[_LOADERS]`) → built-in | yes |
| Lossless round-trip of foreign `_LANG.*` subtrees | yes |
| Manifest migration (`datamanifest migrate`) | yes |
| Portable storage model (two `[_STORAGE]` folder fields `datasets_dir`/`datacache_dir`, repo-local by default, `$`-interpolation, per-host overrides, `platformdirs` roots) | yes |
| Parameterized bindings (`{ ref, args, kwargs }` + `$var` substitution) | yes |
| Safe concurrent materialization (`.tmp` → atomic publish → `.complete` marker) | yes |
| Verify-once integrity (checksum only at fetch; `.complete` entry skips re-hash) | yes |
| Canonical key ordering (stable, cross-tool byte-identical output) | yes |
| Produce-or-load cache (`@cached`: parameter-hash keying, optional `version=`, `config.toml`/`metadata.toml` sidecars) | yes |
| `.datamanifest-state.toml` state file (git-ignored inventory of fetched **and** produced object locations + checksums; read-first resolution; dirty states) | yes |
| `datamanifest list` inspect (`--orphan`, `--dirty`, `--delete`, `--move`) + `datamanifest refresh` state reconcile, over artifacts and datasets | yes |
| Cross-machine sync (`push`/`pull` a stored object over rsync+ssh; writes no manifest; idempotent) | yes |

## Storage model

> **Breaking storage-layout change (spec-v4).** The earlier scope / content-prefix /
> `$data`-`$cache`-selector machinery is gone, replaced by the two explicit folder fields
> below. Run `datamanifest migrate datasets.toml` to freeze an existing manifest's effective
> locations into the new model so on-disk data keeps resolving (it moves no bytes).

Storage is **two folder fields** set in `[_STORAGE]`:

- `datasets_dir` (default `"datasets"`) — where fetched datasets go.
- `datacache_dir` (default `"cached"`) — where the produced `@cached` cache goes.

Both default to **relative** paths, so they resolve against the project root (the
manifest's directory, `$repo`) and you get a visible local `./datasets/` and
`./cached/` with zero config. The layout is flat — there is no scope, no content
prefix, no project-id or app-name segment, no derived name. The folder you set **is**
the location, and adding a `pyproject.toml` no longer moves anything:

- fetched dataset → `<datasets_dir>/<key>`
- produced artifact → `<datacache_dir>/<cachetype>/[<version>/]<hash>`

A path expression may interpolate `$`-symbols: the predefined `$user_data_dir` /
`$user_cache_dir` (straight from `platformdirs`, **bare** — no app name) and `$repo`
(the project root); the two fields `$datasets_dir` / `$datacache_dir`; `$key` (a
dataset's storage key); any user-defined bare `[_STORAGE]` key; `$USER` / env vars;
and `~`. User-defined symbols can be made host-specific in
`[_STORAGE._HOST."<glob>"]`.

```toml
[_STORAGE]
datasets_dir  = "datasets"               # default: repo-local ./datasets/
datacache_dir = "$user_cache_dir/myproj" # produced artifacts on the machine cache dir
scratch       = "/tmp/$USER/scratch"     # user-defined symbol

[_STORAGE._HOST."login*.hpc.edu"]
scratch = "/scratch/$USER"              # host-specific override of a user symbol

[bigsim]                                # → datasets/bigsim  (default storage_path)
uri = "https://example.com/bigsim.nc"

[hpc_output]                            # per-dataset override (a path expression)
storage_path = "$scratch/results/$key"
format = "nc"
```

You rarely need to write the `_HOST` syntax by hand — `datamanifest storage`
edits `[_STORAGE]` for you, defaulting to **this host**:

```bash
datamanifest storage set datacache_dir "/scratch/$USER/cache"   # this host only
datamanifest storage set datacache_dir "$user_cache_dir/myproj" --all-hosts  # project default
datamanifest storage set datasets_dir /fast/data --host "login*.hpc.edu"     # a host glob
datamanifest storage                  # show the config resolved for this host + the raw rules
```

**Resolution ladder** for any field/symbol *name* (first match wins):

1. `DATAMANIFEST_<NAME>` environment variable.
2. First `[_STORAGE._HOST.<glob>].<name>` whose glob matches the hostname.
3. `[_STORAGE].<name>` base value.
4. The predefined symbol or field default.

The only two env vars of note are `DATAMANIFEST_DATASETS_DIR` and
`DATAMANIFEST_DATACACHE_DIR`.

**Per-dataset override.** A dataset may set `storage_path` — a path expression,
default `$datasets_dir/$key` — which **replaces** the old `store` and `local_path`
fields. A `storage_path` that contains `$key` is a tool-managed keyed location; an
exact path with no `$key` is a user-managed location used verbatim that maintenance
never touches.

**Centralizing / sharing.** Because the default folders are repo-local, point both
fields at a machine directory to share data across clones or projects — one explicit
edit:

```toml
[_STORAGE]
datasets_dir  = "$user_data_dir/myproj"
datacache_dir = "$user_cache_dir/myproj"
```

## State file (`.datamanifest-state.toml`)

`datamanifest.toml` is the **recipe** you write and commit: *what* data you want
and *how* to fetch it. Next to it, the tool keeps a small **state file**,
`.datamanifest-state.toml`, that just remembers *where each file ended up on this
machine* (and its checksum) — so it never loses track of your data and never
re-downloads something it can already find.

It's **local and disposable**: add it to your `.gitignore`; delete it and it
rebuilds itself as you use your data. You normally never edit it by hand.

```toml
[datasets."example.com/a.csv"]          # a fetched dataset: where it landed
storage_path = "datasets/example.com/a.csv"
sha256 = "abc123…"

[datacache."mypkg.run@v3".instances]    # a @cached result, by its parameter hash
"83b2…" = "cached/mypkg.run/v3/83b2…"
```

Because the tool records real locations, things keep working when you move data
around:

- **Moved a file?** It's found at its new spot first, ahead of any default path.
- **`datamanifest list`** flags a record that disagrees with disk
  (`missing` / `relocated` / `untracked`).
- **`datamanifest refresh`** fixes the state file to match disk in one go
  (re-point moved files, drop deleted ones, pick up data that's present but not yet
  recorded). It only touches the state file, never your data — so it just runs.
- **`datamanifest list --delete` / `--move`** act on the actual files and update
  the record; your committed `datamanifest.toml` is never changed.

(The older `cached.toml` — produced artifacts only — is still read and upgraded to
`.datamanifest-state.toml` automatically.)

### Read pools — don't re-download what you already have

If a dataset (or a `@cached` result) already exists somewhere else on your machine
— say another project downloaded it — datamanifest can **reuse that copy in
place** instead of fetching it again. It checks a few **read pools** (extra
read-only folders); on a match it records the location and uses it, while new
downloads still go to your own `datasets_dir`.

- **Datasets** are looked up in well-known shared folders by default (e.g.
  `~/.cache/Datasets`), and a found copy is **checksum-verified** before it's
  trusted.
- **`@cached` results** are not shared by default (opt-in) — there's no standard
  shared location for them, and they carry no content checksum.

Point the tool at your own shared folders with `datamanifest storage set
datasets_pools <dir> …`; see what's reusable with `datamanifest where --scan`
(report) or pull it all in with `datamanifest refresh --scan` (adopt). Pools can
differ per machine, and an empty list turns them off. `migrate`, `refresh --scan`
and `where --scan` also accept `--datasets-pools` / `--datacache-pools` to
override the folders for a single run (no values = none).

## Cross-machine sync

Move a stored object between machines instead of re-downloading or recomputing it. Every
syncable object has a machine-independent address — a fetched dataset by its `key`, a
produced artifact by `cachetype[/version]/hash` — and lands under the receiver's own
`datasets_dir`/`datacache_dir`, so only the physical folder differs per host:

```bash
datamanifest push foo user@hpc             # copy dataset `foo` to the host (rsync over ssh)
datamanifest pull esm_anomaly/83425a3 hpc  # pull a produced artifact by hash prefix
datamanifest push foo user@hpc --dry-run   # preview resolved paths + size, transfer nothing
datamanifest list --cached --push user@hpc        # bulk: push a filtered selection
```

- **Transport is rsync over SSH**, and the SSH target (`user@host`) is both the transport and
  the host identity — no remote registry.
- **The receiver's folders** (`datasets_dir`/`datacache_dir`) are resolved best-effort from
  the remote's own environment (the tool probes `DATAMANIFEST_*` via
  `ssh <host> 'source ~/.bashrc; env'`), then the manifest's `[_STORAGE._HOST]` rules for that
  host, then the default. A **local / `$repo`-relative object is not syncable** — and since the
  default folders are repo-local, you must point `datasets_dir`/`datacache_dir` at a
  machine-global location (e.g. `$user_data_dir/…`) for an object to be syncable.
- **Sync writes no manifest** — a transferred object lands in the destination store as an
  orphan (present, unreferenced) and is immediately usable; it is **idempotent** (a no-op when
  the target already holds the object complete).

## Per-language bindings (`_LANG`)

Language-specific bindings live in a dedicated `_LANG` namespace, so a single manifest can serve multiple language implementations without conflicts.

```toml
[_META]
schema = 1

[mydata._LANG.python]
fetcher = "mypkg.fetch:download_mydata"   # entry-point ref; resolved via importlib
loader  = "mypkg.load:load_mydata"

[_LANG.python.loaders]                    # project-wide format → loader defaults
csv = "pandas.io.parsers:read_csv"        # string form (a bare module:function ref)
nc  = { ref = "myclimate.loaders:load_nc", kwargs = { decode_times = false } }  # table form

[mydata._LANG.julia]
fetcher = "MyPkg.fetch_mydata"            # preserved verbatim; Python never touches it
```

**Fetch ladder** (per dataset, in order):
1. Own Python fetcher — explicit `_LANG.python.fetcher`, else the bare `fetcher`, else legacy `python=`
2. Bare `shell` command template (else legacy `_LANG.shell.fetcher`)
3. Cross-language fetch (rung 3) — run a fetcher defined in another language
4. Plain `uri` download
5. Error — no source available

**Load ladder** (per dataset, in order):
1. Own Python loader — explicit `_LANG.python.loader`, else the bare `loader`
2. Manifest format default — `[_LANG.python.loaders][format]`, else the bare `[_LOADERS][format]` map
3. Built-in format default (csv, parquet, nc, …)
4. Error

At every own-language rung the explicit `_LANG.python` binding wins over the bare
one. A binding that is **present** for the running language — bare *or* explicit
`_LANG.python` — is **fail-loud** (spec-v3.6): if it fails to resolve it is an
error, and if it resolves and then raises the error propagates — never a silent
fall-through to a different loader/fetcher. The ladder falls through only to skip
rungs that are **absent** (another language's `_LANG.<other>` binding, or no own
loader). A manifest meant for more than one language uses explicit
`[<ds>._LANG.<lang>]` bindings (absent, and so correctly skipped, in the others).

### Language-implicit (bare) bindings

For a single-language project the `[<ds>._LANG.<lang>]` wrapper is needless
ceremony. A dataset may instead carry a **bare** `fetcher`/`loader` directly, and
a top-level `[_LOADERS]` table may carry a bare `format → binding` map — all read
as bindings in the running tool's **own language** (here, Python):

```toml
[_LOADERS]                                # language-implicit format → loader defaults
csv = "myproject.io:read_csv"
nc  = "myproject.io:read_nc"

[temperature]
uri    = "https://example.com/temperature.csv"
format = "csv"
loader = "myproject.loaders:load_temperature"   # bare per-dataset loader

[derived]
format  = "nc"
fetcher = "myproject.build:derived"             # bare per-dataset fetcher (no uri)

[model_output]                                  # bare, language-agnostic shell fetcher
format = "nc"
shell  = "make model_output OUTPUT=$download_path"   # same command for every tool
```

The bare `shell` field is the **canonical, language-agnostic** shell fetcher (the
same command for every tool — not a `_LANG` tag); the legacy
`[<ds>._LANG.shell].fetcher` is still read and preserved as the fallback. Bare
bindings are kept **bare** on write (never promoted into `_LANG.python`), so a
hand-authored single-language manifest round-trips unchanged.

A full, runnable example manifest — bare loaders/fetchers, a parameterized loader,
the bare `shell` fetcher, storage selectors — lives in the spec repo:
[`examples/datasets.toml`](https://github.com/perrette/datamanifest.toml/blob/main/examples/datasets.toml).

**Cross-language fetch (rung 3).** The rare case: a dataset whose only fetcher is
defined in another language (e.g. `[<ds>._LANG.julia].fetcher`), with no native
Python fetcher, no `_LANG.shell` fetcher, and no `uri`. Python materializes it by
invoking the **local Julia `DataManifest` environment** directly —
`julia --project=<env> -e 'using DataManifest; download_dataset(Database("<datasets.toml>"), "<name>")'` —
which writes the bytes into the shared store; Python then reads them from disk
(load never crosses languages, only bytes do). The Julia env is discovered by
walking up from the manifest directory (or `$JULIA_PROJECT`) for a `Project.toml`
whose `[deps]` lists `DataManifest`, and the rung is gated on `julia` being on
`PATH`. When the toolchain is absent the rung **logs a warning and skips**, and
the ladder advances to the `uri` download. Cross-language fetch applies to fetched
datasets only (never `@cached` produced datasets); it is **on by default** and
probe-gated (a no-op unless a foreign fetcher and a usable Julia env are both
present). Toggle it per file with `delegate = false`, or per run with the
`--delegate` / `--no-delegate` flags on `datamanifest download`.

### Parameterized bindings

A binding (a `fetcher`, a `loader`, or an entry in the `[_LANG.python.loaders]`
map) may be a `{ ref, args, kwargs }` table instead of a plain string, so one
entry-point can be reused across datasets that differ only in arguments (example
from the spec's [`examples/datasets.toml`](https://github.com/perrette/datamanifest.toml/blob/main/examples/datasets.toml)):

```toml
[esm_5x5._LANG.python.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]                                     # positional, in order
kwargs = { grid = "5x5", skip_models = ["CESM.*"] }    # keyword

[esm_10x10._LANG.python.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]
kwargs = { grid = "10x10" }
```

String values in `args` and `kwargs` undergo `$var` substitution before the
call. Available variables: `$download_path` (fetcher), `$path` (loader),
`$key`, `$version`, `$doi`, `$format`, `$branch`, `$uri`, `$project_root`.

The two forms are interchangeable at **every** binding site — explicit
`[<ds>._LANG.python]` `fetcher`/`loader`, the language-implicit bare
`fetcher`/`loader`, and the project-wide `[_LANG.python.loaders]` / bare
`[_LOADERS]` defaults. (The `shell` field is a separate command-template string,
not a `module:function` binding, so it is always a string, never a table.) A
bare string `"module:function"` is the alias for `{ ref = "module:function" }`
and makes the conventional call (a loader gets the dataset path; a fetcher the
standard context). Canonical writing: a binding with no `args`/`kwargs` is
written as the **string**, one that carries them as the **table**.

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim on every read→write cycle; Python never modifies them. Unknown structural tables (any `_*` key that Python does not recognise) are similarly passed through.

### Migration

```bash
datamanifest migrate datamanifest.toml            # upgrade in place
datamanifest migrate datamanifest.toml --dry-run  # preview the changes
```

`migrate` upgrades an older manifest to the current format **without moving any
data**. It:

- modernizes the storage settings (writes the two folder fields at their
  repo-local defaults, drops retired keys) and any inline language bindings;
- **finds data you already have**: it looks in the old default locations on disk
  (and well-known shared folders like `~/.cache/Datasets`) and records each file's
  real location in the [state file](#state-file-datamanifest-statetoml), so your
  existing downloads keep working while new ones follow the clean defaults. If one
  location holds most of your data, it offers to point `datasets_dir` there for
  this machine; if a file turns up in two places, it asks which to use
  (`--no-input` picks automatically).

## Python adaptations

The Python port uses the same manifest format as `DataManifest.jl`. The `_LANG` namespace is the preferred form; legacy flat fields are still accepted for backwards compatibility.

**Supported bare forms** (language-implicit / language-agnostic, spec-v3.4/v3.5 — not legacy):

- **`fetcher`** / **`loader`** (per dataset) — bare bindings read as Python; a string `"pkg.mod:func"` or a `{ ref, args, kwargs }` table. Equivalent to `[<ds>._LANG.python].fetcher/.loader` but without the wrapper; an explicit `_LANG.python` binding overrides the bare one.
- **`shell`** (per dataset) — the canonical, language-agnostic command-template fetcher.
- **`[_LOADERS]`** — manifest-wide bare `format → binding` map; the language-implicit counterpart of `[_LANG.python.loaders]`.

**Legacy fields** (still accepted on read; only these are deprecated):

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`) resolved via `importlib`. The callable receives keyword arguments `(download_path, project_root, entry, uri, key, version, doi, format, branch, requires_paths)`. No inline code execution (`exec`/`eval`) anywhere. Equivalent to `[<ds>._LANG.python].fetcher`.
- **`[<ds>._LANG.shell].fetcher`** — the legacy shell fetcher; read as the fallback for the canonical bare `shell`.
- **`python_includes=`** — list of directory paths prepended to `sys.path` during ref resolution (obsolete; the project root is auto-added).

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

Already using Pooch? `datamanifest import pooch registry.txt --base-url URL --cache-dir "$(python -c 'import pooch; print(pooch.os_cache("yourpkg"))')"` turns a registry into manifest entries and **adopts the files Pooch already downloaded in place** (checksum-verified), so switching over costs no re-downloads.

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
