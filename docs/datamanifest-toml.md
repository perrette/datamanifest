# `datamanifest` — Python implementation notes

This page documents how the **Python** `datamanifest` package behaves. The
`datamanifest.toml` *format* it reads and writes is not defined here: the
normative, language-independent spec lives in its own repository so neither
implementation owns it, and [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)
(Julia) reads and writes the same files.

## Conformance

This package conforms to **schema v1** (`_META.schema = 1`) against spec tag
**`spec-v3`**. The two version axes are independent: `_META.schema` is the
data-model version (bumped only on breaking structural change), and the spec tag
tracks prose/fixture evolution. Pinning the tag here is what lets this package and
`DataManifest.jl` move at their own pace while sharing one normative format.

**Pinned spec:** https://github.com/perrette/datamanifest.toml/blob/spec-v3/SCHEMA.md

> **Note:** The conformance fixture tarball still points at `spec-v1.1`. Re-pinning
> the fixture suite to the `spec-v3` git tag requires a network fetch and is a manual
> post-merge step; the offline test suite runs against the already-downloaded fixtures.

The whole *contract* two implementations must agree on — top-level layout, common
fields, `_LANG` bindings and `$var` substitution, the fetch/load ladders, the
storage model (folder variables, `$`-selectors, path-expression interpolation,
`repo→data→cache` read order, `.complete` markers), and canonical byte-identity
ordering — is normative *in the spec*, not restated here. When this page and the
spec disagree, the spec wins.

The spec is capability-based: a tool declares which named capabilities it supports
and runs only the fixtures tagged for them. This package's status:

| Capability | Status | Notes |
|---|---|---|
| `lang-read` | ✅ | Parses `[<ds>._LANG.python]` / `[_LANG.python.loaders]`; applies the load ladder. |
| `lang-write` | ✅ | Regenerates `_LANG.python`, preserves foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Executes the `[<ds>._LANG.shell].fetcher` command template (`expand_shell_template`). |
| `storage` | ✅ | spec-v3 storage model: **bare roots** (`$data`/`$cache` resolve to `platformdirs` dirs without `/Datasets` suffix; `$repo` = project root); content composed as `<root>/datasets/<key>` (fetch) or `<root>/cached/<project-id>/<cachetype>/[<version>/]<hash>` (produced); `DATAMANIFEST_DIR` application base; folder variables (`$data`, `$cache`, `$repo` built-in; user-defined via `[_STORAGE]`), `$folder[/subpath]` selectors, `[_STORAGE].default` project default, path-expression interpolation, env-var/`_HOST` precedence ladder (no `_PROFILE` rung), `repo→data→cache` built-in probe order under `datasets/` prefix, atomic publish + `.complete` markers. Bare (non-`$`) `store` values are rejected with a migration hint. |
| `byte-identity` | ✅ | Canonical lexicographic key ordering; this package is the **normative reference** (`sort_recursive` in the `datamanifest.store` substrate). |
| `binding-args` | ✅ | Executes the `{ ref, args, kwargs }` table form with `$var` substitution (`_substitute_vars`). |
| `cache-produce` | ✅ | Produce-or-load: the `@cached` decorator with canonical-JSON→SHA-256 param-hash keying, optional `version=` segment (path + `config.toml` entry, not in hash), `config.toml`/`metadata.toml` sidecars; spec-v3 artifact path `<cache>/cached/<project-id>/<cachetype>/[<version>/]<hash>`. See [Produce-or-load cache layer](#produce-or-load-cache-layer). |
| `inspect` | ✅ | The `cached.toml` produced-dataset index and `datamanifest list` maintenance surface: `--kind`/`--scope`/`--orphan`/`--older-than`/`--format`/`--fields` filters + `--delete`/`--move` actions (dry run by default; `--yes` to apply). `last-access` updated on read (best-effort advisory). |
| `sync` | ❌ | Cross-machine push/pull of cached artifacts is not yet implemented (deferred to a separate follow-up). |
| `delegation` | ❌ | Peer-CLI delegation (fetch-ladder rung 3) is not implemented; the ladder skips straight to `uri` download. No `datamanifest fetch` subcommand yet. |

### What differs / is added on top

Behavior in this package beyond — or looser than — the normative spec:

- **v0 read compatibility.** A file with no `[_META]` is read leniently as schema v0
  (flat `python=`, `[_LOADERS]`, …). The spec marks these forms deprecated; this
  package keeps reading them. See [v0 → v1](#v0--v1-read-compatibility-and-migration).
- **`datamanifest migrate`** — opt-in v0→v1→v2 rewrite for Python bindings and `store`
  selectors. Explicitly **non-normative** in the spec (migration is each tool's own
  concern). Running `datamanifest migrate` rewrites bare `store = "x"` entries to
  `store = "$x"` (v1.1 → v2) in addition to the existing v0 → v1 Python-binding
  rewrite.
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

### Produce-or-load cache layer

The produce-or-load feature is an **in-repo layer** (no `[cache]` extra). The code is
organised in three layers with a one-way import arrow — the substrate is consumed by
both feature layers, and neither feature layer imports the other:

```
datamanifest.store   (Layer 0 substrate)  — location resolution, safe-materialize, loaders, canonical sort
        ▲ consumes                 ▲ consumes
datamanifest.database/pipelines    datamanifest.cache
   (Layer 1a: fetch)                  (Layer 1b: @cached) — imports store only, never the fetch layer
```

`@cached` (`from datamanifest.cache import cached`) wraps a **keyword-only** producing
function: its keyword arguments (minus `_`-prefixed runtime knobs) form the key table,
which is hashed (canonical JSON → SHA-256) into a `<hash>` key. The result is materialized
once under `$cache` at `<cache>/cached/<project-id>/<cachetype>/[<version>/]<hash>/`
beside a `config.toml` (re-hashable key table + `[_META]`) and a write-if-absent
`metadata.toml` (provenance + an `[origin].cached_toml` back-pointer); subsequent calls
load and return it. A `cached=False` call argument forces a recompute.

An optional `version=` string (e.g. `@cached(cachetype="t", version="v2")`) inserts a
path segment before `<hash>` and is recorded in `config.toml` and `cached.toml`. It is
**not** part of the param hash, so changing only `version=` produces a distinct artifact
path without invalidating the hash for callers with other versions. A per-call `cache_dir=`
argument bypasses folder/prefix/scope and uses the supplied directory verbatim.

Each produce registers the artifact in a sibling **`cached.toml`** (gitignored per-machine
state by default). **`datamanifest list`** is the maintenance command: `--kind cached`
selects produced artifacts; `--orphan` flags those with no `cached.toml` root reference;
`--older-than AGE` filters by last-access time. `--delete` / `--move DIR` act on the
selected set (dry run by default; `--yes` to apply). The `list` command is the composition
root — the only place that imports both the fetch and cache layers. It never touches
`$data`/`$repo`, and identifies produced artifacts by their `config.toml` sidecar (so
fetched `$cache` datasets are never selected by `--kind cached`).

### Canonical serialization

This Python implementation is the **normative reference** for canonical key
ordering: `Database.write()` sorts every dict key by Unicode code point at every
nesting level, with no
`_META`/`_LOADERS`-first special case. The sort lives in the Layer 0 substrate
(`sort_recursive` in `datamanifest.store`); `Database.write()`, the `cached.toml`
index, and `datamanifest format` all share that single normative
implementation. Output is byte-identical to Julia's
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
`format`). `list` doubles as the inspect/maintenance command via `--delete`/`--move`
flags. See the [README](../README.md) or `datamanifest <command> --help`.

## Cross-reference

| Concern | Julia — `DataManifest.jl` | Python — `datamanifest` | Schema spec |
|---|---|---|---|
| Implementation | [awi-esc/DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) | [perrette/datamanifest](https://github.com/perrette/datamanifest) | — |
| Schema version | v1 | v1 (v0 accepted on read) | [SCHEMA.md @ spec-v2](https://github.com/perrette/datamanifest.toml/blob/spec-v2/SCHEMA.md) |
| Language bindings | `[_LANG.julia]` subtrees | `[_LANG.python]` subtrees | [§ Language bindings](https://github.com/perrette/datamanifest.toml/blob/spec-v2/SCHEMA.md) |
| Common fields | `Databases.jl` `DatasetEntry` | `database.py` `DatasetEntry` | [§ Common fields](https://github.com/perrette/datamanifest.toml/blob/spec-v2/SCHEMA.md) |
