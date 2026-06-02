# The `datamanifest.toml` schema

`datamanifest` (Python) and [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)
(Julia) read and write the *same* TOML manifest. Because the common fields are identical and
each tool ignores the other's extension keys, a single `datasets.toml` can be consumed by
either implementation. The normative spec lives in its own repository so neither
implementation owns it.

The current schema version is **v1**, which introduces the `_LANG` namespace for
language-specific bindings. Schema v0 (flat `python=`, `[_LOADERS]`, etc.) is still
accepted on read for backwards compatibility.

See: https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md

## Schema v1 structure

A v1 manifest uses `_`-prefixed top-level tables for structural metadata. Only tables
without a leading `_` are datasets.

```toml
[_META]
schema = 1                          # schema version; absent → v0

[_LANG.python.loaders]              # manifest-wide format→loader defaults (Python)
csv = "mypkg.loaders:load_csv"

[_LANG.julia.loaders]               # Julia's equivalent; Python preserves this verbatim
csv = "MyPkg.load_csv"

[mydata]                            # dataset entry (no leading _)
uri     = "https://example.com/mydata.csv"
sha256  = "abc123..."
format  = "csv"

[mydata._LANG.python]               # Python-specific bindings for this dataset
fetcher = "mypkg.fetch:fetch_mydata"
loader  = "mypkg.load:load_mydata"

[mydata._LANG.julia]                # Julia's bindings; Python passes through verbatim
fetcher = "MyPkg.fetch_mydata"
```

## Storage model (`store` + `[_STORAGE]`)

Each dataset entry has an optional `store` field (default: `"data"`). The available
stores are `data`, `cache`, and `repo`; `mount` is parsed and preserved but not yet
activated.

> **Behavior change from earlier releases.** Prior releases stored datasets under
> `$XDG_CACHE_HOME/Datasets`. As of spec-v1.1, the default `data` store resolves
> to `platformdirs.user_data_dir("datamanifest")/Datasets` and `cache` to
> `platformdirs.user_cache_dir("datamanifest")/Datasets`.

The optional `[_STORAGE]` table overrides the root directories per store, host, or
profile:

```toml
[_STORAGE]
data  = "~/data/Datasets"
cache = "~/.cache/Datasets"
repo  = "datasets"               # relative → resolved against <project_root>

[_STORAGE._HOST."login*.hpc.edu"]
data  = "/scratch/$USER/Datasets"

[_STORAGE._PROFILE.cluster]
data  = "/work/proj/Datasets"
```

`~` and `$VAR` are expanded in all path values. `[_STORAGE]` is preserved verbatim
in every round-trip; Python never strips it.

**Per-store precedence** (highest to lowest):

1. `DATAMANIFEST_<STORE>_DIR` environment variable.
2. `[_STORAGE._PROFILE.<name>].<store>` — activated by `DATAMANIFEST_PROFILE`.
3. First matching `[_STORAGE._HOST.<glob>].<store>`.
4. `[_STORAGE].<store>` base value.
5. `platformdirs` default (`data`/`cache`) or `<project_root>/datasets` (`repo`).

**Read resolution** searches the stores in the order `repo → data → cache`; the
first root where `<root>/<key>` exists with a `.complete` marker wins. Falls back
to the write path for that entry's `store` if none exist.

## Parameterized bindings (`{ ref, args, kwargs }`)

Python `fetcher` and `loader` values may be a `{ ref, args, kwargs }` table instead
of a bare string, allowing the same entry-point to serve multiple datasets:

```toml
[esm_5x5._LANG.python.loader]
ref    = "mypkg.load:esm"
kwargs = { grid = "5x5" }

[esm_10x10._LANG.python.loader]
ref    = "mypkg.load:esm"
kwargs = { grid = "10x10" }
```

String values inside `args` and `kwargs` are substituted before the call.
Available `$var` names: `$download_path` (fetcher path), `$path` (loader path),
plus `$key`, `$version`, `$doi`, `$format`, `$branch`, `$uri`, `$project_root`.

A bare-string binding keeps the original convention (`ref(download_path=…, …)`)
and is unchanged.

## Resolution ladders

### Fetch ladder (per dataset)

1. Own `[<ds>._LANG.python].fetcher` entry-point (resolved via `importlib`; bare string or `{ ref, args, kwargs }` table)
2. Own `[<ds>._LANG.shell].fetcher` shell template
3. Plain `uri` HTTP/git/rsync/local download
4. Error — no source configured

Delegation to a peer CLI is **not yet implemented**.

### Load ladder (per dataset)

1. Own `[<ds>._LANG.python].loader` entry-point (bare string or `{ ref, args, kwargs }` table)
2. Manifest `[_LANG.python.loaders][format]` default for this manifest
3. Built-in format default (csv, parquet, nc, json, yaml, toml, zip, tar)
4. Error

### Foreign-language passthrough

Python reads and writes every `_LANG.<other>` subtree (e.g. `_LANG.julia`) verbatim —
it never parses, validates, or modifies them. The same applies to any unknown `_*`
top-level table. This guarantees lossless round-trips of multi-language manifests.

## Canonical key ordering / byte-identity

This Python implementation is the **normative reference** for canonical key ordering.
All dict keys are sorted by Unicode code point at every nesting level (top-level
tables, within-entry fields, and keys inside inline `{ }` tables) before writing —
with no `_META`/`_LOADERS`-first special case. Output is therefore byte-identical to
Julia's `TOML.print(sorted=true)`.

The helper `_sort_recursive(obj)` in `datamanifest.database` performs this sort
and is used by both `Database.write()` and the conformance round-trip test.

## v0 compatibility

Legacy flat fields are still accepted on read:

| v0 field | v1 equivalent |
|---|---|
| `python=` / `callable=` (per dataset) | `[<ds>._LANG.python] fetcher =` |
| `loader=` (per dataset) | `[<ds>._LANG.python] loader =` |
| `shell=` (per dataset) | `[<ds>._LANG.shell] fetcher =` |
| `[_LOADERS]` format→ref map | `[_LANG.python.loaders]` |
| `python_includes=` | — (project root auto-added to `sys.path`) |

Use `datamanifest migrate <file>` to rewrite a v0 manifest to v1 in-place (Python
bindings and flat `shell=` fields; foreign keys are left verbatim).

## Cross-reference

| Concern | Julia — `DataManifest.jl` | Python — `datamanifest` | Schema spec |
|---|---|---|---|
| Implementation | [awi-esc/DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) | [perrette/datamanifest](https://github.com/perrette/datamanifest) | — |
| Schema version | v1 | v1 (v0 accepted on read) | [SCHEMA.md](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md) |
| Language bindings | `[_LANG.julia]` subtrees | `[_LANG.python]` subtrees | [Extensions](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#extensions) |
| Code hook style | `julia=` (inline code) | `python=` / `callable=` (entry-point ref, no inline exec) | [Extensions](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#extensions) |
| Common fields | `Databases.jl` `DatasetEntry` | `database.py` `DatasetEntry` | [Common fields](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#common-fields) |
