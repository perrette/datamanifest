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

## Resolution ladders

### Fetch ladder (per dataset)

1. Own `[<ds>._LANG.python].fetcher` entry-point (resolved via `importlib`, no inline exec)
2. Own `[<ds>._LANG.shell].fetcher` shell template
3. Plain `uri` HTTP/git/rsync/local download
4. Error — no source configured

Delegation to a peer CLI is **not yet implemented**.

### Load ladder (per dataset)

1. Own `[<ds>._LANG.python].loader` entry-point
2. Manifest `[_LANG.python.loaders][format]` default for this manifest
3. Built-in format default (csv, parquet, nc, json, yaml, toml, zip, tar)
4. Error

### Foreign-language passthrough

Python reads and writes every `_LANG.<other>` subtree (e.g. `_LANG.julia`) verbatim —
it never parses, validates, or modifies them. The same applies to any unknown `_*`
top-level table. This guarantees lossless round-trips of multi-language manifests.

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
bindings only; foreign keys are left verbatim).

## Cross-reference

| Concern | Julia — `DataManifest.jl` | Python — `datamanifest` | Schema spec |
|---|---|---|---|
| Implementation | [awi-esc/DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) | [perrette/datamanifest](https://github.com/perrette/datamanifest) | — |
| Schema version | v1 | v1 (v0 accepted on read) | [SCHEMA.md](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md) |
| Language bindings | `[_LANG.julia]` subtrees | `[_LANG.python]` subtrees | [Extensions](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#extensions) |
| Code hook style | `julia=` (inline code) | `python=` / `callable=` (entry-point ref, no inline exec) | [Extensions](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#extensions) |
| Common fields | `Databases.jl` `DatasetEntry` | `database.py` `DatasetEntry` | [Common fields](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#common-fields) |
