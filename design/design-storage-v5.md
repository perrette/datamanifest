# Design note: storage v5 — global defaults, scoped config, and the transfer surface

Status: **agreed design** (settled 2026-06-10); **phase 1 implemented** in the
Python port (defaults, `$project`, `.datamanifest/`, scoped config files +
ladder, `config` command, `migrate` changes, `POOL_DEFAULTS`) — spec repo +
Julia alignment still pending. Phases 2–4 not implemented. Supersedes the
spec-v4 repo-local defaults; builds on the state file (see `design-state-file.md`,
whose read-first resolution is what makes these changes safe). Cross-language:
the parts marked **spec-normative** below must go to the spec repo and be
mirrored (or consciously skipped) by `DataManifest.jl`.

## 1. Default locations: global keyed store for datasets, per-project cache

The spec-v4 defaults resolve repo-local (`datasets/`, `cached/`). v5 reverses
that deliberately — the repo holds only the manifest and `.datamanifest/`:

```
datasets_dir  = "$user_data_dir/datamanifest/shared/datasets"
datacache_dir = "$user_cache_dir/datamanifest/projects/$project/cached"
```

- **Datasets are shared, keyed, with no project segment.** A dataset key
  (`host/path[#version]`) is a globally unique content identity, so one shared
  store dedups across projects by construction, and no project name appears in
  the path — the project-name-derivation problem does not arise. The store
  coincides with the default read-pool location, so the pool self-populates.
- **Produced cache is per-project.** `cachetype[@version]/hash` is not globally
  unique (two projects can both have a `utils.load@v1`) and artifacts carry no
  content checksum, so they are namespaced under a new predefined **`$project`**
  symbol — default: the basename of the project root, overridable on the
  ordinary ladder (a committed `project = "..."` is shared intent, not a leak).
  Renames are safe: the state file keeps finding old artifacts at their recorded
  locations; new writes go under the new name; leftovers are `clean` material.
- **The trailing kind segments (`datasets` / `cached`) are load-bearing**, not
  cosmetic: `$user_data_dir` and `$user_cache_dir` are platform-dependent and
  often merged onto one folder (e.g. both pointed at `$scratch`); the full paths
  stay disjoint because `shared/datasets` and `projects/$project/cached` differ
  all the way down. The `shared` / `projects/` dichotomy also means no project
  name can ever collide with the shared store.
- A globally-stored object is **syncable**: `push`/`pull` refuse repo-local
  objects, so the new defaults make both kinds transferable out of the box.

**Garbage collection** for the shared store: datasets are re-downloadable, so an
explicit age/LRU `clean` over the store suffices. A refcount/registry file
(each project registering itself per dataset) was considered and **rejected**:
deleted or moved projects never deregister, so counts only grow and a GC could
never trust them — cost without payoff.

**Back-compat** (the "follow the de-facto layout, never relocate" principle):
- `POOL_DEFAULTS` gains `$repo/datasets` (so pre-existing repo-local data in
  unconfigured projects is still found/adopted, never re-downloaded) and the new
  shared store, keeping the legacy locations:
  `("$repo/datasets", "$user_data_dir/datamanifest/shared/datasets",
  "$user_data_dir/datamanifest/datasets", "~/.cache/Datasets")`.
- Manifests migrated from v3 already pin `datasets_dir = "datasets"` explicitly
  and keep their repo-local behavior unchanged.
- Going forward, `migrate` **stops writing explicit defaults** into
  `[_STORAGE]` — a written-out default would permanently shadow the user's
  machine-wide preference (see the ladder below).

## 2. Scoped configuration: two new files, one `config` command

Folder directives are inherently per-machine, but today the only persistent
homes are the committed manifest (which leaks per-user absolute paths to
collaborators — `migrate` currently writes `_HOST` entries there) and ad-hoc
env vars. Directives must **not** move into the state file: it is rewritten
wholesale on every download/produce (hand edits would be clobbered) and its
contract is *regenerable, safe to delete* — a directive is the one thing that
isn't. Instead, two config files, git-config style:

- **`.datamanifest/config.toml`** — per-checkout, git-ignored. The state file
  moves in beside it: `.datamanifest-state.toml` → **`.datamanifest/state.toml`**
  (reader accepts the legacy path; first write relocates). The rule:
  **`datamanifest.toml` is the only committed file; `.datamanifest/` is entirely
  git-ignored** (one ignore entry). Unlike DVC's committed `.dvc/config`,
  committed config already has a home here — the manifest's `[_STORAGE]`.
- **`~/.config/datamanifest/config.toml`** (`$XDG_CONFIG_HOME`) — user-global.

Both files are `[_STORAGE]`-shaped TOML (folder fields, `$symbols`, `*_pools`,
`project`, `default_remote`), **including `_HOST` sections** — home directories
and checkouts commonly live on filesystems shared across cluster nodes, so even
"personal" config needs per-host scoping.

The resolution ladder (first match wins; more specific scope wins, git-style):

```
1. DATAMANIFEST_<NAME> environment variable
2. .datamanifest/config.toml        (checkout: _HOST glob, then base)
3. manifest [_STORAGE._HOST.<glob>]  (committed, shared infrastructure)
4. manifest [_STORAGE] base          (committed project intent)
5. ~/.config/datamanifest/config.toml (user: _HOST glob, then base)
6. built-in defaults
```

The CLI surface is a git-style **`config`** command absorbing `storage
show/set/unset` (which stays as a deprecated alias), with scope flags:

| Scope | Writes to |
|---|---|
| `--local` **(default)** | `.datamanifest/config.toml` |
| `--global` | `~/.config/datamanifest/config.toml` |
| `--project` | manifest `[_STORAGE]` base |
| `--host GLOB` | manifest `[_STORAGE._HOST.<glob>]` |

The `--local` default is the structural fix for the leak: personal by default,
shared deliberately. `migrate`'s interactive "send downloads there?" writes to
local config instead of the manifest.

## 3. The transfer surface

Principle (from the CLI review): every datamanifest transfer is
**store ↔ location** — one end must be the project store, because that end
supplies the object selection, the machine-independent address (`rel`), and the
checksums. A transfer with no store end is plain rsync; one with two store ends
is `normalize` (§3.3). Hence no new `sync SRC DST` command: the verbs `push` /
`pull` carry the direction, and only the non-store operand needs a grammar.

### 3.1 Generalized push/pull operand

| Operand | Meaning |
|---|---|
| `PATH` (no colon) | local folder, keyed layout — `push` = raw export, `pull` = adopt-by-copy |
| `HOST:` | the remote machine's store (today's behavior) |
| `HOST:PATH` | explicit folder on an ssh host |
| `NAME:` | git remote → that checkout's project store (§3.2) |

- rsync colon rule: a colon means remote. Today's bare `push ID host` form gets
  a deprecation warning (it would otherwise collide with a relative path).
- The repo-local refusal **lifts for explicit-path targets** — re-attaching
  `rel` is only ill-defined for the store-resolved `HOST:` form.
- **`pull` records the received object in the state file** (previously it
  landed as an orphan adopted on access; as the adopt-by-copy path it should
  record directly).
- `default_remote` (a config field, any scope) names the target used by bare
  `push` / `pull`; it can hold any operand form, including a git remote name.

### 3.2 Git remotes as transfer targets

A git remote whose URL is ssh-like and points at a **checked-out** repo (no
bare repos) is a **pure project reference**: `origin:` only — no `origin:path`
form (explicit paths are `HOST:PATH`'s job). Its value is the one datum no
other layer has: the peer **checkout path**, hence access to the peer project's
own files. Semantics mirror the local read/write split:

- **pull** reads the remote checkout's `.datamanifest/state.toml` — recorded
  *resolved absolute locations*, so nothing needs resolving at all.
- **push** resolves the directive ladder **in the remote context** (the
  checkout's `.datamanifest/config.toml` + manifest, evaluated on the remote
  host) — the remote's `$user_data_dir` / `$project` are never guessed locally.
  Preferred mechanism: run `datamanifest` on the remote over ssh (the
  scriptable `where` form); fallback: read the files over ssh and evaluate
  locally with the best-effort env probe (today's probe, but fed real config).

Git-remote names take precedence over ssh hosts on collision; `git:` / `ssh:`
prefixes are reserved as explicit disambiguators. An https remote (the typical
GitHub `origin`) is rejected as a data target with a clear error (pushing data
via the Git-LFS batch API is a separate future track — see
`design-remote-protocols.md`). No new registry: git's remote table is the
registry.

### 3.3 `normalize` — bytes follow the directive

`datamanifest normalize [TERM... filters] [--dry-run] [--copy]` re-homes every
selected tracked object whose recorded location differs from the
directive-derived path (`$datasets_dir/$key`,
`$datacache_dir/<cachetype>[/<version>]/<hash>`), then repoints the state file:

| Bytes found | Action |
|---|---|
| at the derived path | no-op |
| in a read pool | **copy** (pools are shared — never drained) |
| anywhere else | **move** |
| user-managed / `skip_download` / `lazy_access` | skipped, reported |

`--copy` forces copy everywhere; checksums are verified on the way when
declared; the move/copy lands (staging sibling + atomic rename) before any
record changes — nothing is deleted first. `normalize` never downloads
(`download` is the verb for missing data). The pairing: **`refresh` makes the
state file follow the bytes; `normalize` makes the bytes follow the
directive.** A matching `list` filter, **`--out-of-place`** (recorded ≠
derived), previews the selection. It is deliberately distinct from `--outside`,
which mismatches in both directions: pools count as conformant for `--outside`
(but pool copies *are* out-of-place), and user-managed exact-path data is
"outside" (but *in place* — its recorded location equals its own directive).

### 3.4 `export` — a self-describing bundle (lowest priority)

`export DEST [filters]`: copy the selected datasets to `DEST/<key>` (keyed
layout), verify checksums during the copy, and write a manifest copy into
`DEST` with `datasets_dir = "."`. The result is simultaneously a **read pool**
(consumers add it to `datasets_pools`) and a **standalone datamanifest
project** (`cd` + `verify` / `list` / `path` work). Because export is read-only
on the source, it *includes* user-managed and `skip_download` datasets — the
manually-downloaded data a fresh clone cannot re-obtain is exactly what is
worth bundling. No byte-level `import` verb is needed (consume a bundle via
pools or as a project), so the catalog `import` (pooch/csv/urls/intake/dvc)
keeps its name. An `--adopt` variant (repoint the state file to the copies) is
deferred.

## 4. Spec-normative vs tooling

**Spec-normative** (spec repo + Julia): the two default folder expressions, the
`$project` predefined symbol, the config-file ladder rungs (file locations,
`[_STORAGE]` shape, precedence order), the new `POOL_DEFAULTS`, and the
`.datamanifest/` layout (`state.toml` path + legacy-name reading).
**Tooling** (per-implementation, conventions shared informally): the `config`
command, the push/pull operand grammar and git-remote targets, `normalize`,
`export`, `default_remote`.

## 5. Phases

1. **Defaults + config plumbing** (the spec-level chunk): new defaults,
   `$project`, `.datamanifest/` (state-file move), the two config files and
   ladder rungs, `config` command with `--local` default, `migrate` stops
   writing `_HOST`/explicit defaults into the manifest, `POOL_DEFAULTS` update.
   Spec repo + Julia alignment ride with this.
2. **Operand generalization**: colon rule + bare-host deprecation, local-path
   targets, repo-local refusal lifted for explicit paths, `pull` records,
   `normalize` + `list --out-of-place`.
3. **Git-remote targets**: `NAME:` resolution, remote-side ladder evaluation,
   `default_remote`.
4. **`export`** bundle verb.
