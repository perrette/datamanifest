# `datamanifest` — Python implementation notes

This page documents how the **Python** `datamanifest` package behaves. The
`datamanifest.toml` *format* it reads and writes is not defined here: the
normative, language-independent spec lives in its own repository so neither
implementation owns it, and [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)
(Julia) reads and writes the same files.

## Conformance

This package conforms to **schema v1** (`_META.schema = 1`) against spec tag
**`spec-v1.1`**. The two version axes are independent: `_META.schema` is the
data-model version (bumped only on breaking structural change), and the spec tag
tracks prose/fixture evolution. Pinning the tag here is what lets this package and
`DataManifest.jl` move at their own pace while sharing one normative format.

**Pinned spec:** https://github.com/perrette/datamanifest.toml/blob/spec-v1.1/SCHEMA.md

The whole *contract* two implementations must agree on — top-level layout, common
fields, `_LANG` bindings and `$var` substitution, the fetch/load ladders, the
storage model (default roots, `DATAMANIFEST_<STORE>_DIR` env vars, `repo→data→cache`
read order, `.complete` markers), and canonical byte-identity ordering — is
normative *in the spec*, not restated here. When this page and the spec disagree,
the spec wins.

The spec is capability-based: a tool declares which named capabilities it supports
and runs only the fixtures tagged for them. This package's status:

| Capability | Status | Notes |
|---|---|---|
| `lang-read` | ✅ | Parses `[<ds>._LANG.python]` / `[_LANG.python.loaders]`; applies the load ladder. |
| `lang-write` | ✅ | Regenerates `_LANG.python`, preserves foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Executes the `[<ds>._LANG.shell].fetcher` command template (`expand_shell_template`). |
| `storage` | ✅ | Honors `store` + `[_STORAGE]`; `platformdirs` roots, env-var/`_HOST`/`_PROFILE` precedence, `repo→data→cache` read order, atomic publish + `.complete` markers. |
| `byte-identity` | ✅ | Canonical lexicographic key ordering; this package is the **normative reference** (`_sort_recursive`). |
| `binding-args` | ✅ | Executes the `{ ref, args, kwargs }` table form with `$var` substitution (`_substitute_vars`). |
| `delegation` | ❌ | Peer-CLI delegation (fetch-ladder rung 3) is not implemented; the ladder skips straight to `uri` download. No `datamanifest fetch` subcommand yet. |
| `mount` | ◐ | The `mount` store value is parsed and preserved verbatim, but not activated (the spec leaves its mechanics unspecified in v1.1, so this is intentional). |

### What differs / is added on top

Behavior in this package beyond — or looser than — the normative spec:

- **v0 read compatibility.** A file with no `[_META]` is read leniently as schema v0
  (flat `python=`, `[_LOADERS]`, …). The spec marks these forms deprecated; this
  package keeps reading them. See [v0 → v1](#v0--v1-read-compatibility-and-migration).
- **`datamanifest migrate`** — opt-in v0→v1 rewrite for Python bindings. Explicitly
  **non-normative** in the spec (migration is each tool's own concern).
- **`datamanifest update-checksums`** — recompute stored `sha256` from disk. A local
  convenience, not part of the spec's CLI surface.
- **Legacy read-only location probe.** When a dataset isn't in any configured store,
  this package also probes the pre-v1.1 default `~/.cache/Datasets` (read-only, never
  written, one-time warning). A back-compat affordance on top of the spec's read
  resolution, suppressed once `DATAMANIFEST_DATA_DIR` is set.
- **Built-in loader set.** The spec only requires "the tool's built-in default loader
  for `<format>`"; the concrete Python format→library map is
  [documented below](#built-in-default-loaders).

## Python-specific behavior

### Reference resolution (`importlib`)

A `module:function` binding (e.g. `mypkg.fetch:fetch_mydata`) is resolved with
`importlib` — never executed as inline code. The manifest's directory (the project
root) is placed on `sys.path` so a local module alongside `datasets.toml` is
importable without installation. The v0 `python_includes=` field is therefore
obsolete and ignored.

### Built-in default loaders

When a dataset's `format` has no per-dataset `loader` and no
`[_LANG.python.loaders]` entry, Python falls back to a built-in loader. The
format → implementation map (in `datamanifest/default_loaders.py`):

| `format` | Loader | Dependency |
|---|---|---|
| `csv` | `pandas.read_csv(comment="#")` | pandas |
| `parquet` | `pandas.read_parquet` | pandas |
| `nc` | `xarray.open_dataset` | xarray + netcdf4 |
| `dimstack` | `xarray.open_dataset` (no Python `DimStack` type; `xarray.Dataset` is the equivalent) | xarray + netcdf4 |
| `json` | `json.load` | stdlib |
| `toml` | `tomllib.load` | stdlib (`tomli` on 3.10) |
| `yaml` / `yml` | `yaml.safe_load` | pyyaml |
| `md` / `txt` | `open().read()` | stdlib |
| `zip` / `tar` / `tar.gz` | archive extraction loaders | stdlib |

Each third-party dependency is imported lazily, so the package installs without
pandas/xarray/pyyaml and only errors (with an install hint) when such a loader is
actually invoked.

### Canonical serialization

This Python implementation is the **normative reference** for canonical key
ordering: `Database.write()` sorts every dict key by Unicode code point at every
nesting level (top-level tables, within-entry fields, and inside inline `{ }`
tables) via `_sort_recursive()` in `datamanifest.database`, with no
`_META`/`_LOADERS`-first special case. Output is byte-identical to Julia's
`TOML.print(sorted=true)`. The same helper backs `datamanifest format`, which
peer tools pipe their output through to obtain byte-identical files.

### v0 → v1 read compatibility and migration

Legacy flat fields are accepted on read and mapped to their v1 equivalents:

| v0 field | v1 equivalent |
|---|---|
| `python=` / `callable=` (per dataset) | `[<ds>._LANG.python] fetcher =` |
| `loader=` (per dataset) | `[<ds>._LANG.python] loader =` |
| `shell=` (per dataset) | `[<ds>._LANG.shell] fetcher =` |
| `[_LOADERS]` format→ref map | `[_LANG.python.loaders]` |
| `python_includes=` | — (project root auto-added to `sys.path`) |

`datamanifest migrate <file>` rewrites a v0 manifest to v1 in-place (Python
bindings and flat `shell=` fields; foreign `_LANG.*` keys are left verbatim).
Migration is a Python-only convenience — the Julia tool reads v0 but does not
rewrite it.

### CLI

The package ships a `datamanifest` CLI (`list`, `download`, `path`, `add`,
`remove`, `show`, `verify`, `update-checksums`, `init`, `where`, `migrate`,
`format`). See the [README](../README.md) or `datamanifest <command> --help`.

## Cross-reference

| Concern | Julia — `DataManifest.jl` | Python — `datamanifest` | Schema spec |
|---|---|---|---|
| Implementation | [awi-esc/DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) | [perrette/datamanifest](https://github.com/perrette/datamanifest) | — |
| Schema version | v1 | v1 (v0 accepted on read) | [SCHEMA.md @ spec-v1.1](https://github.com/perrette/datamanifest.toml/blob/spec-v1.1/SCHEMA.md) |
| Language bindings | `[_LANG.julia]` subtrees | `[_LANG.python]` subtrees | [§ Language bindings](https://github.com/perrette/datamanifest.toml/blob/spec-v1.1/SCHEMA.md) |
| Common fields | `Databases.jl` `DatasetEntry` | `database.py` `DatasetEntry` | [§ Common fields](https://github.com/perrette/datamanifest.toml/blob/spec-v1.1/SCHEMA.md) |
