# Design note: the state file — one inventory for fetched **and** produced data

Status: proposed (for the spec to formalize). Supersedes nothing yet; additive.

## The split: spec vs state

Two files, two jobs — like a manifest and its lockfile, except ours is the
**spec** plus a **git-ignored local state file** (resolved per machine, not a
committed reproducibility lock):

- **`datamanifest.toml` — the spec.** *What* to track and *how* to obtain it: a
  dataset's `uri`/`fetcher`/`shell`, a `@cached` function's code. Hand-authored,
  **git-committed**, the source of intent. (Default name; `datasets.toml` /
  `Datasets.toml` stay recognized legacy aliases.) **Unchanged by this proposal.**
- **`.datamanifest-state.toml` — the state file.** *Where* each object actually
  landed on this machine: a per-object inventory of resolved on-disk locations
  (+ a fetched dataset's checksum). Tool-maintained, **git-ignored** (it says
  nothing about *how* to re-obtain a resource, so it doesn't help a fresh clone —
  it is regenerable local state). Hidden dotfile: it exists for the `datamanifest`
  CLI to read, not for hand-editing.

Today the state file already records **produced** artifacts per instance
(schema 4: `hash → on-disk location`, params in each artifact's `config.toml`).
This note extends it to record **fetched datasets** the same way, so it is one
inventory of everything materialized — fetched or produced.

The guiding invariant: the state file is **read-only inventory**. It records
*where things are* and is consulted to *find* an existing object; it never
directs a *write*. Writes always follow the current directive (`datasets_dir` /
`datacache_dir` / `@cached(storage_path=)`) — the gold standard. This already
holds for produced data and applies verbatim to fetched.

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
  — its identity; it's already what the spec derives. No new id.
- **Produced key** keeps schema 4's `cachetype[@version]` (with `@` reserved as
  the version separator) and its `instances` hash→location map. This is just the
  current produced-recipe table moved under the `datacache` namespace.
- `storage_path` is the **full on-disk location** (relative to the manifest dir
  when under the repo, else absolute — the existing portable convention).

## Behavior

- **Record `storage_path` systematically.** Every fetched dataset's resolved
  location is written to the state file — on **download** *and* on
  **read-resolution** — exactly as a produced artifact's is. This is the main
  gain: one place that knows where every object lives, so `list`, cleanup, sync
  and move/relocation work uniformly across fetched and produced.
- **Record `sha256` on download, honoring `skip_checksum`.** When a dataset is
  downloaded and `skip_checksum` is not set, store the computed `sha256` in its
  entry. With `skip_checksum = true`, omit it (so a very large dataset needn't be
  hashed even once). This `sha256` is the **actual** checksum of what's on disk.
- **Gold standard preserved.** The recorded location is consulted to *find* an
  existing object, never to direct a fetch/produce; a (re)materialization follows
  the current directive.
- No behavior in `datamanifest.toml` changes.

## Read-resolution: the state file is checked first

Resolving where an object lives (`get_dataset_path` / `download_dataset` /
`resolve_existing_path` for fetched; the `@cached` hit path for produced)
**checks the state file's recorded `storage_path` first** — if the bytes are
actually there (and, for a dataset, checksum-valid when recorded), that is a hit.
Only then does it fall back to the machine-derived/directive path, and only then
(for a dataset) download. This is a pure "are the bytes already here?"
short-circuit, *ahead of* any derivation rule — including for a user-managed
exact `storage_path` (an earlier run may have recorded a different location).

Produced data already works this way; this generalizes it to fetched data. The
state file thus becomes the resolver's fast path and the single "where is it"
truth: a moved object is found at its new home with no re-derive / re-download.

## Source of truth: non-destructive updates + a dirty state

The state file is a **first-order source of truth** for *where objects are*, and
is maintained **non-destructively**, git-style. Two code paths touch it and are
never conflated:

- **Active resolution** (`get_dataset_path`/`download`/`@cached`) **self-heals
  additively**: *relocated* → refresh the recorded location to where the bytes
  actually are; *untracked* → register; *missing* → (re)materialize at the
  current directive (download/produce) and record. It **never deletes** — and
  because any access that consults the state file and finds nothing *proceeds to
  download/produce*, it always lands in the relocate/register path, so it cannot
  leave a stale record. The relocate-refresh is the **only** automatic mutation.
- **`list` (passive)** only **labels** state — never mutates.

**Dirty states** (state file ↔ disk), git-style: `clean` / `missing` (recorded
bytes gone) / `relocated` (recorded `L`, bytes at derived `D`) / `untracked`
(bytes present, no entry — *orphan* for produced) / `modified` (recorded
`sha256` ≠ actual; datasets; deferred with the sha rework). `list` shows a dirty
visual signal (a red/`✗` marker beside the existing `⚑custom`) and a **`--dirty`**
filter.

**Actions** (explicit, on the selected objects):
- **`--refresh`** — fix the **state file only** (no downloads, no file moves):
  refresh *relocated* entries to their actual location and **drop** *stale /
  missing* entries. A pure state↔disk reconcile. (Untracked artifacts are picked
  up by active access, not here.)
- **`--delete`** — remove the selected objects' **bytes and** their entries
  (works across a filtered set). The only byte-removing action.

Removal is therefore **explicit-only** — `--refresh`/`--delete` are user-invoked;
passive `list` and active resolution never delete.

**Concurrency.** Every write **re-reads the state file, merges** (additive union;
last-writer-wins per object), then writes via temp-file + **atomic rename** — so
parallel `@cached`/`download`s can't clobber each other. Additive-only updates
make the merge conflict-free.

**Garbage vs dirty.** A *malformed* entry rooting nothing (e.g. instance-less
residue from a format change) is corruption, not a tracked-but-missing object —
**cleaned silently** on read.

Still deferred: the *modified* state in full (expected-vs-actual `sha256` /
`skip_checksum` rework) and multiple recorded locations (one object synced to
two places).

## Unified maintenance surface (the payoff)

Once a fetched dataset has a recorded location, the same maintenance operations
that apply to produced artifacts apply to it — one surface for everything
materialized:

- **Filter / list.** Already spans both kinds (free-text `search`, `--datasets`/
  `--cached`, `--format`, `--older-than`, `--present`/`--missing`, …). Unchanged.
- **`--delete`.** Extends to fetched datasets: remove the on-disk bytes **and
  prune the dataset's entry**. (Today delete is `cached`-only.)
- **`--move DEST`.** Extends to fetched datasets: relocate the bytes **and
  repoint the recorded `storage_path`** — exactly as for a produced artifact.
  `datamanifest.toml` is *not* edited; only the resolved location moves (a
  subsequent re-fetch still follows the `datasets_dir` directive — gold standard).

**Protections (unchanged rule).** Maintenance never touches data the user owns:
a dataset whose `storage_path` is a **user-managed exact path** (no `$key`) or
that is **`skip_download`** (the URI *is* the file) is reported as *skipped*,
never moved or deleted — the same guard already used for deletion today,
generalized. Tool-managed (keyed) fetched datasets under `datasets_dir` are
fair game, like produced artifacts.

## Scope of this change (additive, low-risk)

- **`datamanifest.toml` is untouched.** Its `sha256` (expected/contract) and any
  per-dataset `storage_path` (a *directive*) stay where they are and keep their
  current meaning. The state file records the **resolved** location and **actual**
  checksum alongside — duplication is fine; the state file is derived/disposable.
- No field is *moved* out of the spec yet. That tighter spec/state split
  (expected-vs-actual checksum, directive-vs-resolved location) is **deferred**.

## Deferred (explicitly out of scope here)

- **sha256 / skip_checksum rework** in the spec: how the *expected* checksum
  (spec) and the *actual* checksum (state) relate, and re-verification policy.
- **Moving** `storage_path` (resolved) and `sha256` (actual) fully out of the
  spec into the state file, leaving the spec as pure recipe.
- **Multiple recorded locations** for one object (synced to two places).
- **Platform-dependent defaults** docs (how to point `datasets_dir`/
  `datacache_dir` at `$user_data_dir`/`$user_cache_dir` per host).

## Migration

`_META.schema` 4 → 5 (a shape change: produced recipes move under the
`datacache` namespace, fetched datasets appear under `datasets`). The reader
migrates 1–4 forward: schema-4 recipes become `datacache.*`; older shapes as
already handled. Fetched-dataset entries simply accrue as datasets are accessed.
The file itself is renamed `cached.toml` → `.datamanifest-state.toml`
(git-ignored).

### Guiding principle: follow the user's de-facto layout, never relocate

A migration (or any default change across versions) should **preserve where the
user's data already lives** and adjust the *defaults* to keep matching that
de-facto choice — never move bytes to fit a new default. Ideally a tool could
read the recorded locations (the state file's per-object `storage_path`s), strip
the known structural suffix (`/<key>` for a dataset, `/<cachetype>[/<version>]/
<hash>` for an artifact), and infer the common `datasets_dir`/`datacache_dir`
*pattern* the user is implicitly following — writing that compact, editable
pattern back, with per-object overrides only for genuine outliers.

In practice this is only worthwhile once the state file is populated; at a
cold v3→v4 upgrade there's not enough recorded information to infer reliably, so
the current `migrate` deliberately stays minimal (write v4 defaults, carry an
explicit `local_path`, surface anything it can't translate) and leaves the user
to set the folder fields. The inference idea is recorded here as the intended
*direction* — not an implemented feature.
