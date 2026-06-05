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
| `list [SEARCH ...] [--cached\|--datasets] [--present\|--missing\|--all] [--orphan] [--dirty] [--hash P ...] [--older-than AGE] [--format F] [--fields ...] [--delete\|--move DIR] [--dry-run]` | List datasets and cached artifacts, with their state↔disk status; with `--delete`/`--move` becomes the maintenance command. The filtered selection applies directly (`--dry-run` previews); `--delete`/`--move` act on both artifacts and fetched datasets (protected data is skipped) |
| `refresh [--dry-run]` | Reconcile the state file (`.datamanifest-state.toml`) with disk: relocate stale records, drop missing ones, adopt present-but-untracked datasets. Edits only local state — no downloads/moves — so it applies by default; `--dry-run` previews (use `list --dirty` to see what would change) |
| `download [NAME ...] [--all] [--overwrite] [--delegate\|--no-delegate]` | Download specific datasets or all of them; `--no-delegate` disables the cross-language fetch rung for the run |
| `path NAME` | Print the resolved on-disk path (composable in shell) |
| `add URI [--name N] [--no-download] [--extract] [--delegate\|--no-delegate]` | Register and (by default) download a dataset |
| `remove NAME [--keep-cache]` | Delete an entry, optionally preserving cached files |
| `show NAME` | Print full entry detail in TOML style |
| `verify [NAME ...]` | Re-check sha256 checksums; exits nonzero on any mismatch |
| `update-checksums [NAME ...] [--dry-run]` | Recompute stored checksums from what's on disk |
| `init [--folder PATH] [--force]` | Create a fresh `datasets.toml` in the current directory |
| `where` | Print active `datasets_toml` and `datasets_folder` paths |
| `migrate FILE [--dry-run]` | Reshape a spec-v3 manifest's `[_STORAGE]` to the spec-v4 two-field model: write `datasets_dir`/`datacache_dir` at their defaults, drop the retired keys, carry `local_path` → `storage_path`. Moves no bytes |
| `push ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **to** an SSH host (rsync over ssh), addressed by id (a dataset's `key`, or `cachetype[/version]/hash`) |
| `pull ID SSH_HOST [--dry-run] [--batch]` | Transfer a stored object **from** an SSH host (rsync over ssh), same addressing |

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

`datamanifest.toml` is the **spec** — *what* to track and *how* to obtain it,
hand-authored and git-committed. Next to it the tool maintains a **state file**,
`.datamanifest-state.toml` — a per-object inventory of *where* each object
actually landed **on this machine**: every fetched dataset's resolved
`storage_path` (+ its actual `sha256`, unless `skip_checksum`) and every
`@cached` artifact's location, under two namespaces:

```toml
[_META]
schema = 5

[datacache."mypkg.run@v3"]
ref = "mypkg.run:run"
format = "pickle"
[datacache."mypkg.run@v3".instances]
"83b2…" = "cached/mypkg.run/v3/83b2…"

[datasets."example.com/a.csv"]
storage_path = "datasets/example.com/a.csv"
sha256 = "abc123…"
```

It is **git-ignored, regenerable local state** (it says nothing about *how* to
re-obtain anything), so add `.datamanifest-state.toml` to your `.gitignore`. The
state file is **read-first**: resolving a dataset checks its recorded location
before any derivation, so a moved object is found where it really lives. It is
maintained non-destructively — active access self-heals it (registers, relocates;
never deletes), and `list` surfaces a **dirty** marker (`missing` / `relocated` /
`untracked`) when a record disagrees with disk. `datamanifest refresh` reconciles
it in bulk (relocate stale records, drop missing ones, adopt untracked datasets;
no downloads or moves — edits only local state, so it applies by default); `list
--delete` / `--move` act on the bytes and update the record (the spec is never
edited). Writes always follow the current directive — the recorded location only
helps *find* existing bytes, never directs a write.

The previous filename `cached.toml` (produced artifacts only) is still read and
is rewritten to `.datamanifest-state.toml` on the next write.

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
datamanifest migrate datasets.toml            # rewrite in place
datamanifest migrate datasets.toml --dry-run  # preview the changes
```

`migrate` reshapes a spec-v3 manifest's `[_STORAGE]` to the spec-v4 two-field
model. It:

- writes `[_STORAGE].datasets_dir`/`datacache_dir` at their **defaults**
  (repo-local `./datasets/`, `./cached/`) and drops the retired
  `default`/`scope`/`store`/`_SCOPE`/`_PREFIX` keys (a `_HOST` table and
  user-defined symbols are preserved);
- carries each dataset's explicit `local_path` over to `storage_path` losslessly,
  and surfaces any dataset that used a retired `store` selector for manual attention.

It **moves no bytes**. The v4 defaults are repo-local, so if your data lives
elsewhere edit `datasets_dir`/`datacache_dir` (or a dataset's `storage_path`) —
see the [Storage model](#storage-model) for `$`-symbols and platform-dependent
(`$user_data_dir`/`$user_cache_dir`) defaults.

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

## Acknowledgments

`datamanifest` is a Python port of [`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written by the same author (Mahé Perrette). The Python port was implemented with assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
