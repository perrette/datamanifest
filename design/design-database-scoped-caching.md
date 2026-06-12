# Design: database-scoped caching (cache bundles)

Status: agreed 2026-06-12 (maintainer + assistant), to be implemented in
datamanifest (Python) and DataManifest.jl together. Target releases:
Python v0.19.0, Julia 0.33.0. Spec impact: one non-normative paragraph.

## Motivation

The fetch layer has an explicit context object: the `Database` — several can
coexist, a library can hold an in-memory one, and since the frozen-config work
it carries the entire resolved configuration (a snapshot of the ladder,
including `datacache_dir`, `project`, `lock_stale_age`, the project root and
hence the state-file location). The cache layer (`@cached` / `cached`) is the
last *ambient* holdout: each call re-derives its context from the working
directory / active project, and a library that wants its own cache bundle must
say so at every call site (`cache_dir=`), scattering the bundle decision.

This design binds caching to the Database, collapsing the two
context-discovery mechanisms into one.

## The mechanism

1. **Python**: `Database.cached(...)` — a decorator factory method.
   `@db.cached(key=..., cachetype=..., ...)` accepts exactly the options of
   `datamanifest.cache.cached` and resolves the cache context from the
   database's frozen `ScopedConfig`: `datacache_dir`, `$project`, the
   state-file location, `lock_stale_age`. Implementation keeps the layering:
   the cache module does not import the database — the Database hands a plain
   context (its snapshot + roots) down into the existing cache machinery.
2. **Julia**: the `@cached` macro gains a `db=` option (an expression, e.g. a
   `const LIBDB = Database(...)`, evaluated at call time so definition order
   does not matter). Context derivation goes through `storage_layers(db)`
   (the frozen `ConfigSnapshot`).
3. **The bare forms stay and unify**: module-level `cached` (Python) and bare
   `@cached` (Julia) resolve over the **default database** when one is
   discoverable — which anchors at the same ambient project, so behavior in a
   normal project is unchanged — and **fall back to today's ambient
   derivation when no manifest is discoverable** (caching must keep working
   in projects without a manifest; do not let the default-database "no
   manifest found" error leak into `@cached`).

## The `persist=false` rule (in-memory databases)

An in-memory database (no `datasets_toml`; constructed with `persist=false` /
no discoverable manifest) must not write a project state file — today it
scribbles `.datamanifest/state.toml` into whatever directory it is anchored
to (typically the caller's cwd/project), which is wrong for library use.

New rule: **an in-memory database keeps each inventory under the storage root
it describes** —

- produced-artifact records under the resolved `datacache_dir`
  (`<datacache_root>/.datamanifest/state.toml`, via the existing
  `locate_state(base)` machinery anchored at the root instead of a project);
- dataset records under the `datasets_folder` likewise.

Nothing is written outside directories the database explicitly owns.
Resolution works even with no state file (paths derive from key /
`cachetype/[version/]hash`); the per-root state keeps `inspect_store`,
`list`, and maintenance working for the bundle.

## What a library does (the target ergonomics)

```python
# mylib/_data.py
from datamanifest import Database
_DB = Database(datasets_folder="$user_data_dir/mylib",  # fetched bytes
               storage_config={"project": "mylib"},      # names the cache bundle
               persist=False)

@_DB.cached(key=["grid"])
def landmask(*, grid):
    ...
```

Fetched data goes to the folder the library owns (or the shared store);
produced artifacts land under `…/projects/mylib/cached`; both inventories
live under those roots; the host project's `.datamanifest/` is never touched.
(Exact `Database` kwargs above to be confirmed against the constructors at
implementation time — `storage_config` may be the manifest-layer dict or a
dedicated kwarg; verify.)

## Collisions and sharing

Collision/identity checks remain **per database** (one project's inventory).
Two databases share artifacts exactly when they resolve the same
`datacache_dir`; the tools make no cross-project claims about caches that
happen to share a directory — deliberately. One docs sentence states this.

## Out of scope

- No new `CacheStore` object: the Database already aggregates the needed
  context, and the library scenario always has one.
- No spec schema change: construction surface is per-language and
  non-normative (like `Database` itself). Add one paragraph to SCHEMA.md's
  cache-layer section: a produced dataset's cache context MAY be scoped to a
  manifest/database context; an in-memory context keeps its inventories under
  the storage roots themselves.

## Verification checklist (per tool)

- `db.cached` / `@cached db=`: artifacts under the db's `datacache_dir`,
  keyed with the db's `project`; `lock_stale_age` from the db's snapshot.
- In-memory db: caller's cwd stays clean (no `.datamanifest/` created
  outside the db's roots); state files appear under the roots; round-trip
  load works; `inspect_store` over the bundle works.
- Bare `@cached`/`cached` in a manifest project: behavior identical to today
  (same paths, same state file).
- Bare form in a directory with no manifest: still works (ambient fallback).
- Cross-language: hash/addressing unchanged (RFC 8785 key → SHA-256;
  `cachetype/[version/]hash` layout), so bundles remain shareable across the
  two tools when their roots coincide.
