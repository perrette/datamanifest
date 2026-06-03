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
| `download [NAME ...] [--all] [--overwrite] [--delegate\|--no-delegate]` | Download specific datasets or all of them; `--no-delegate` disables the cross-language fetch rung for the run |
| `path NAME` | Print the resolved on-disk path (composable in shell) |
| `add URI [--name N] [--no-download] [--extract] [--delegate\|--no-delegate]` | Register and (by default) download a dataset |
| `remove NAME [--keep-cache]` | Delete an entry, optionally preserving cached files |
| `show NAME` | Print full entry detail in TOML style |
| `verify [NAME ...]` | Re-check sha256 checksums; exits nonzero on any mismatch |
| `update-checksums [NAME ...] [--dry-run]` | Recompute stored checksums from what's on disk |
| `init [--folder PATH] [--force]` | Create a fresh `datasets.toml` in the current directory |
| `where` | Print active `datasets_toml` and `datasets_folder` paths |
| `migrate FILE` | Update an older manifest in place (move legacy flat fields into `_LANG`; rewrite bare `store = "x"` to `$`-selectors) |
| `push ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **to** an SSH host (rsync over ssh), addressed by id (`name`/`alias`/`doi`, or `cachetype[/version]/hash`) |
| `pull ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **from** an SSH host (rsync over ssh), same addressing |

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

# Move a stored object between machines (rsync over ssh; no re-download/recompute)
datamanifest push foo user@hpc --dry-run            # preview: resolved paths + size
datamanifest push foo user@hpc                      # push the dataset `foo` to the host
datamanifest pull esm_anomaly/83425a3 user@hpc      # pull a produced artifact by hash prefix
datamanifest list --kind cached --push user@hpc     # bulk: push the filtered set
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
| Portable storage model (folder variables, `$`-selectors, `[_STORAGE]` with per-host overrides, `platformdirs` roots) | yes |
| Parameterized bindings (`{ ref, args, kwargs }` + `$var` substitution) | yes |
| Safe concurrent materialization (`.tmp` → atomic publish → `.complete` marker) | yes |
| Verify-once integrity (checksum only at fetch; `.complete` entry skips re-hash) | yes |
| Canonical key ordering (stable, cross-tool byte-identical output) | yes |
| Produce-or-load cache (`@cached`: parameter-hash keying, optional `version=`, `config.toml`/`metadata.toml` sidecars) | yes |
| `cached.toml` index + `datamanifest list` inspect/maintenance (`--orphan`, `--delete`, `--move`) | yes |
| Cross-machine sync (`push`/`pull` a stored object over rsync+ssh; writes no manifest; idempotent) | yes |

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

## Cross-machine sync

Move a stored object between machines instead of re-downloading or recomputing it. Every
object has a machine-independent address — a fetched dataset by `name`/`alias`/`doi`, a
produced artifact by `cachetype[/version]/hash` — so only the physical root differs per host:

```bash
datamanifest push foo user@hpc             # copy dataset `foo` to the host (rsync over ssh)
datamanifest pull esm_anomaly/83425a3 hpc  # pull a produced artifact by hash prefix
datamanifest push foo user@hpc --dry-run   # preview resolved paths + size, transfer nothing
datamanifest list --kind cached --push user@hpc   # bulk: push a filtered selection
```

- **Transport is rsync over SSH**, and the SSH target (`user@host`) is both the transport and
  the host identity — no remote registry.
- **The remote store root** is resolved best-effort from the remote's own environment (the
  tool probes `DATAMANIFEST_*` via `ssh <host> 'source ~/.bashrc; env'`), then the manifest's
  `[_STORAGE._HOST]` rules for that host, then the shared default. `$repo` (project-relative)
  is not syncable.
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
`--delegate` / `--no-delegate` flags on `datamanifest download` / `add`.

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
datamanifest migrate datasets.toml
```

Updates a manifest in place through all outstanding steps:

- **Legacy inline-code fields:** promotes per-dataset `python=`/`callable=` into `[<ds>._LANG.python].fetcher` and adds the `[_META]` header. Bare `fetcher`/`loader` and the `[_LOADERS]` map are **supported** language-implicit forms and are left bare. Foreign keys are left verbatim.
- **Shell fetcher:** demotes a legacy `[<ds>._LANG.shell].fetcher` into the canonical bare `shell` field (dropping the emptied `_LANG.shell` block); an existing bare `shell` is left as-is.
- **Storage selectors:** rewrites bare `store = "x"` entries to `store = "$x"` (`"data"`/`""` are elided, leaving the project default). `[_STORAGE]` folder *definitions* (bare keys like `data = "…"`) are left untouched.

Reading an older manifest without migrating still works for most operations, but a manifest with bare `store` values will error on resolution. A one-time deprecation warning is logged for the inline-code legacy fields.

## Python adaptations

The Python port uses the same manifest format as `DataManifest.jl`. The `_LANG` namespace is the preferred form; legacy flat fields are still accepted for backwards compatibility.

**Supported bare forms** (language-implicit / language-agnostic, spec-v3.4/v3.5 — not legacy):

- **`fetcher`** / **`loader`** (per dataset) — bare bindings read as Python; a string `"pkg.mod:func"` or a `{ ref, args, kwargs }` table. Equivalent to `[<ds>._LANG.python].fetcher/.loader` but without the wrapper; an explicit `_LANG.python` binding overrides the bare one.
- **`shell`** (per dataset) — the canonical, language-agnostic command-template fetcher.
- **`[_LOADERS]`** — manifest-wide bare `format → binding` map; the language-implicit counterpart of `[_LANG.python.loaders]`.

**Legacy fields** (still accepted on read; only these are deprecated):

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`) resolved via `importlib`. The callable receives keyword arguments `(download_path, project_root, entry, uri, key, version, doi, format, branch, requires_paths)`. No inline code execution (`exec`/`eval`) anywhere. `datamanifest migrate` promotes these into `[<ds>._LANG.python].fetcher`.
- **`[<ds>._LANG.shell].fetcher`** — the legacy shell fetcher; `migrate` demotes it to the canonical bare `shell`.
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

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
