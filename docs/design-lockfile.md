# Design note: the lock file — one inventory for fetched **and** produced data

Status: proposed (for the spec to formalize). Supersedes nothing yet; additive.

## The split: spec vs lock

Two files, two jobs — the classic **manifest ↔ lockfile** separation:

- **`datasets.toml` — the spec.** *What* to track and *how* to obtain it: a
  dataset's `uri`/`fetcher`/`shell`, a `@cached` function's code. Hand-authored,
  **git-committed**, the source of intent. **Unchanged by this proposal.**
- **`cached.toml` — the lock.** *Where* each object actually landed, frozen: a
  per-object inventory of resolved on-disk locations (+ a fetched dataset's
  checksum). Tool-maintained, **git-ignored** (it says nothing about *how* to
  re-obtain a resource, so it doesn't help a fresh clone — unlike a true
  manifest-lock). A clearer name (e.g. `*.lock`) is deferred; see *Deferred*.

Today the lock already records **produced** artifacts per instance (schema 4:
`hash → on-disk location`, params in each artifact's `config.toml`). This note
extends it to record **fetched datasets** the same way, so the lock is one
inventory of everything materialized — fetched or produced.

The guiding invariant is unchanged: the lock is **read-only inventory**. It
records *where things are* and is consulted to *find* an existing object; it
never directs a *write*. Writes always follow the current directive
(`datasets_dir` / `datacache_dir` / `@cached(storage_path=)`) — the gold
standard. This already holds for produced data and applies verbatim to fetched.

## File layout (schema 5)

Two top-level namespaces — `datasets` (fetched) and `datacache` (produced) —
parallel to the two storage fields, so the two kinds never collide and each is
greppable on its own:

```toml
[_META]
schema = 5

# --- fetched datasets: key → resolved location (+ checksum) ---
[datasets."example.com/foo.nc"]
storage_path = "datasets/example.com/foo.nc"   # systematically recorded
sha256 = "abc123…"                              # actual; omitted when skip_checksum

[datasets."example.com/big.zip"]
storage_path = "datasets/example.com/big.zip"
# no sha256: this dataset has skip_checksum = true

# --- produced artifacts: cachetype[@version] → instances{hash → location} ---
[datacache."mypkg.run@v3"]
ref = "mypkg.run:run"
format = "pickle"

[datacache."mypkg.run@v3".instances]
"83b2…" = "cached/mypkg.run/v3/83b2…"
```

- **Fetched key** is the dataset's existing storage **key** (`host/path[#version]`)
  — its identity; it's already what `datasets.toml` derives. No new id.
- **Produced key** keeps schema 4's `cachetype[@version]` (with `@` reserved as
  the version separator) and its `instances` hash→location map. This is just the
  current cached-recipe table moved under the `datacache` namespace.
- `storage_path` is the **full on-disk location** (relative to the manifest dir
  when under the repo, else absolute — the existing portable convention).

## Behavior

- **Record `storage_path` systematically.** Every fetched dataset's resolved
  location is written to the lock — on **download** *and* on **read-resolution**
  — exactly as a produced artifact's is. This is the main gain: one place that
  knows where every object lives, so `list`, cleanup, sync and move/relocation
  work uniformly across fetched and produced. (`list` already enumerates both
  kinds and already surfaces produced artifacts at their recorded location;
  fetched datasets join that path.)
- **Record `sha256` on download, honoring `skip_checksum`.** When a dataset is
  downloaded and `skip_checksum` is not set, store the computed `sha256` in its
  lock entry. With `skip_checksum = true`, omit it (so a very large dataset
  needn't be hashed even once). The lock's `sha256` is the **actual** checksum of
  what's on disk.
- **Gold standard preserved.** The recorded location is consulted to *find* an
  existing dataset (a hit), never to direct a fetch; a (re)download follows the
  current `datasets_dir` directive.
- **Self-heal / consistency.** Same lifecycle as produced: a missing/stale lock
  entry is (re)written on the next access; a `--move`/`--delete` repoints/prunes
  it. No behavior in `datasets.toml` changes.

## Scope of this change (additive, low-risk)

- **`datasets.toml` is untouched.** Its `sha256` (expected/contract) and any
  per-dataset `storage_path` (a *directive*) stay where they are and keep their
  current meaning. The lock records the **resolved** location and **actual**
  checksum alongside — duplication is fine; the lock is derived/disposable.
- No field is *moved* out of `datasets.toml` yet. That tighter spec/lock split
  (expected-vs-actual checksum, directive-vs-resolved location) is **deferred**.

## Deferred (explicitly out of scope here)

- **Rename** the lock file from `cached.toml` to something that reflects "frozen
  resolved state for both kinds" (e.g. `datamanifest.lock`); update `.gitignore`
  guidance.
- **sha256 / skip_checksum rework** in `datasets.toml`: how the *expected*
  checksum (spec) and the *actual* checksum (lock) relate, and re-verification
  policy.
- **Moving** `storage_path` (resolved) and `sha256` (actual) fully out of
  `datasets.toml` into the lock, leaving the spec as pure recipe.
- **Platform-dependent defaults** docs (how to point `datasets_dir`/
  `datacache_dir` at `$user_data_dir`/`$user_cache_dir` per host).

## Migration

`_META.schema` 4 → 5 (a shape change: produced recipes move under the
`datacache` namespace, fetched datasets appear under `datasets`). The reader
migrates 1–4 forward: schema-4 recipes become `datacache.*`; older shapes as
already handled. Fetched-dataset entries simply accrue as datasets are accessed.
