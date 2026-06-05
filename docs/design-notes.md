# Design notes

Design decisions and recent deviations from the normative `datamanifest.toml`
spec, written from the Python implementation but intended to be **reformulated
cross-language** by the spec. Everything below is **implemented** in this package
and needs reconciling into the spec.

## Produced-dataset identity and conflict detection

A produced artifact lives at
`<datacache_dir>/<cachetype>/[<version>/]<hash>`, where `datacache_dir` is the
single folder field that locates the cache (see *Storage: two explicit folder
fields* below). The path segments below do the disambiguation:

### 1. `cachetype` + parameter `hash` — disambiguation

- **`cachetype` gains a default**: the producing function's fully-qualified,
  *importable* name. In Python that is `module.qualname` (e.g.
  `mypkg.analysis.produce`); cross-language, it is the implementation's canonical
  fully-qualified callable name. The explicit `cachetype=` remains as an override
  (a stable hand-chosen name, or to deliberately group several functions).
- **Rationale**: the worst outcome is silently *mixing* unrelated caches, so the
  default must be unique per function. A consequence — accepted and to be
  **documented prominently** — is that renaming/moving the function (or changing
  package structure) changes its cachetype and so orphans prior artifacts. That
  is the correct default (lost code context ⇒ a conscious decision to re-pin
  `cachetype=` or recompute); `version=` remains the tool for *deliberate*
  busting.
- **Auto and explicit cachetypes share one namespace** — an explicit
  `cachetype="mypkg.analysis.produce"` and the auto-derived value are the same
  identity.
- **Top-level / script execution** (the function's importable identity is not
  well-defined): a tool must **not guess**.
  - Python reference resolution: a function whose `__module__` is `__main__` is
    resolved via the launch's recorded module identity — `__main__.__spec__.name`,
    which Python sets for `python -m pkg.mod` (→ `pkg.mod`) but leaves `None` for a
    loose script (`python path/to/mod.py`), `python -c`, the REPL, and notebooks.
  - So: `python -m pkg.mod` → cachetype `pkg.mod.func` (and this *matches*
    `import pkg.mod`, so the `-m` run and the imported use share the cache); a
    loose script / `-c` / REPL / notebook → **require an explicit `cachetype=`**,
    error otherwise.
  - General principle for the spec: when the producing function has no stable
    importable identity in the host language, the implementation must require an
    explicit `cachetype` rather than synthesize an ambiguous one. This mirrors the
    well-known pickle constraint (objects defined in `__main__` cannot be
    referenced by a portable qualified name).

### 2. Conflict detection (load-time, in-process)

- At decoration / registration time, each producing function is indexed by its
  `(cachetype, version)` pair.
- If **two distinct functions** claim the **same `(cachetype, version)`** while
  *simultaneously live in one process*, raise immediately (name both).
- The key is the **pair**: the same `cachetype` with *different* `version`s is a
  valid, tolerated case (e.g. `calibration` v1 and v2 of two functions active at
  once).
- The guard is intentionally **same-process / same-time**. Two equally-named
  functions used at *different* times (separate runs) simply share the slot, which
  is permitted — a user may engineer that, or make their modules into packages /
  set explicit cachetypes to keep them apart. There is no static cross-process
  check, and none is wanted: "at the same time" is exactly the boundary.

### 3. `cached.toml` records every variation (schema 2, nested)

A recipe called with different parameters produces several artifacts (one per
parameter `hash`). The index must record **all** of them, or the unrecorded ones
read as orphans and risk deletion. Schema 2 is therefore **nested**:

```toml
[_META]
schema = 2

[[produced]]                       # one per (cachetype, version)
cachetype = "mypkg.mod.produce"
ref = "mypkg.mod:produce"
format = "txt"

  [[produced.instances]]           # one per produced variation
  hash = "4413…"
  [produced.instances.params]      # the key table that produced it (omitted if empty)
  grid = "5x5"
```

- The recipe is keyed by `(cachetype, version)` (an array-of-tables, so the
  dotted cachetype needs no key-quoting); each instance records its parameter
  `hash` and the `params`. Registering **accumulates** instances rather than
  overwriting, so reachability (`cachetype, version, hash`) spans them all.
- Recipe-level metadata (`ref`/`format`) is refreshed on each register, so
  `ref` tracks the producing function across a refactor (no invalidation, since it
  is not in the hash). The `ref`-refresh also happens on a cache *hit* when it
  drifted; an absent variation is re-registered on hit (self-healing).
- Schema 1 (a flat table per registry *name*, single `hash`, no params) is still
  **read** — each becomes a one-instance recipe — but always rewritten as schema 2.

### 4. Centralized storage config applies to `@cached`

The storage backend (the two folder fields `datasets_dir`/`datacache_dir` and any
custom `[_STORAGE]` symbols, plus `_HOST` per-machine overrides) is defined once in
the manifest's `[_STORAGE]` table and must apply to **both** fetched datasets and
produced artifacts. `@cached` therefore loads `[_STORAGE]` from the nearest
discovered manifest (the same upward walk it uses for `project_root`) when no
`storage_config` is passed — a plain TOML read, no `Database`/fetch layer, so the
cache layer stays Database-free. Without this, produced artifacts would resolve
`datacache_dir` from only env vars + its default and diverge from where the
manifest puts fetched data (e.g. a cluster scratch partition). Env vars still
override at the top; an explicit `storage_config=` wins over the manifest.

## Storage: two explicit folder fields (spec-v4)

Spec-v4 **removed scope** — and with it content prefixes, the `$data`/`$cache`
selectors, the `[_STORAGE].default` selector, and the project-id / app-name path
segments — in favor of **two explicit folder fields** in `[_STORAGE]`:
`datasets_dir` (default `"datasets"`) and `datacache_dir` (default `"cached"`).
Both default to relative paths, so they are **local by default** (resolved against
the project root) and the folder you set *is* the location — no derived name, and
adding a `pyproject.toml` moves nothing. A dataset's `storage_path` (default
`$datasets_dir/$key`) replaces the old `store`/`local_path` fields. To centralize or
share across clones, point the two fields at a machine directory
(`$user_data_dir/…` / `$user_cache_dir/…`).

## Recent deviations already shipped (to reconcile)

- **Default serialization format is `pickle`.** A format-less `@cached`
  self-saves with pickle (`data.pickle`), so a bare return value round-trips; an
  explicit `format=` overrides. (Spec previously required a format.)
- **`project_root` auto-discovery.** When `project_root` is not given, it is
  discovered by walking up from the working directory for a `datasets.toml` /
  `pyproject.toml`, so `[_STORAGE]` (and thus the repo-local `datacache_dir`)
  resolves against that root rather than the bare call-time directory.
- **Cache hit self-heals the registry.** If a produced artifact is present and
  valid but its `cached.toml` entry is missing (index deleted by hand, or never
  written), a hit re-registers it — the index rebuilds itself by re-running.
  (Spec previously said a hit re-registers nothing.) `metadata.toml` is still
  never re-stamped on a hit; an already-correct entry is not rewritten.
- **A hit requires the data file for the requested format on disk.** A complete,
  hash-valid artifact whose data file for *this* format is absent recomputes
  instead of failing — so two recipes sharing a `cachetype` and hashing to the
  same key (different formats) coexist rather than crash.

### CLI (non-normative — each tool's own concern)

- `datamanifest list` default view is a styled, grouped, terminal-width one-line
  layout of fetched datasets and the produced artifacts this project's
  `cached.toml` roots, with clickable OSC-8 `file://` locations. **Filters narrow
  only and never change the output style**; `--bare`/`--names` selects a plain
  name list, `--fields` the tab-separated machine table. `--all` adds orphans.
  Reachability for `--orphan` keys on `(cachetype, version, hash)`.
- A bare `datamanifest` (no subcommand) prints the command list and the
  `-h/--help` hint instead of erroring.
