# CLI reference

```
datamanifest COMMAND [OPTIONS]
```

Every command also documents itself: `datamanifest COMMAND -h`. The
[use cases](use-cases.md) page shows the common workflows by example; this page
is the full per-command reference. The [storage model](storage.md) the commands
operate on has its own page.

A bare `datamanifest` (no subcommand) prints the command list.

## Set up and add data

### `init [--folder PATH] [--force]`

Create a fresh `datamanifest.toml` in the current directory (or `--folder PATH`).
`--force` overwrites an existing one.

### `add URI|DOI [--name N] [--pick GLOB] [--split] [--no-download] [--extract] [--lazy] [--overwrite]`

Register and (by default) download a dataset. `--name` sets the entry name,
`--extract` unpacks archives after download, `--no-download` registers only,
`--overwrite` replaces an existing duplicate entry.

Two independent special forms:

1. **Zenodo** — a DOI / record URL bundles the record's files into one `uris=`
   dataset (plain HTTPS; declare-only). `--pick GLOB` filters files (repeatable),
   `--split` makes one dataset per file instead, `--name` becomes a name *prefix*.
2. **`--lazy`** — register an **object-store** URI (`s3://`, `gs://`, …) for lazy
   access *instead of* downloading: it sets `lazy_access` (a language-neutral
   marker) and a built-in Python fsspec loader, so `load()` opens it in place.

The two are unrelated (Zenodo serves HTTPS files; `--lazy` is for object stores).

### `import {pooch|csv|urls|intake|dvc} SOURCE [--base-url URL] [--cache-dir DIR] [--overwrite] [--dry-run]`

Bulk-import datasets from another tool's catalog:

- **pooch** — a registry file (`filename [algo:]hash [url]`); `--base-url` supplies
  pooch's `base_url` for lines without an explicit URL.
- **csv** — a `name,url,sha256` file.
- **urls** — a plain URL list.
- **intake** — a `catalog.yml` (each single-file `urlpath` source; needs the
  `[yaml]` extra).
- **dvc** — `.dvc` / `dvc.lock` files (uri from an import-url dep or the default
  remote's content-addressed path; `.dvc/cache` adopted by md5; `[yaml]` extra).

With `--cache-dir` already-downloaded files are **adopted in place,
checksum-verified — no re-download**. See
[adding-datasets.md](adding-datasets.md) for the full per-source detail.

## Inspect

### `list [SEARCH ...] [filters] [output style] [--delete ... | --move DEST ... | --push SSH_HOST ... | --pull SSH_HOST ...]`

List fetched datasets and the cached artifacts this project's state file roots,
each with its state↔disk status. Free-text `SEARCH` terms match
(case-insensitive substring) against each object's key fields; all terms must
match unless `--any`; `--invert` selects non-matches.

Filters (narrow the selection; never change the output style):

- `--cached` / `--datasets` — only produced artifacts / only fetched datasets.
- `--present` / `--missing` — only present / missing datasets (plain name list).
- `--all` — also show orphans and other projects' artifacts.
- `--orphan` — only unreferenced produced artifacts (no state-file root).
- `--dirty` — only objects whose state-file record disagrees with disk
  (`missing` / `relocated` / `untracked`).
- `--outside` — only tracked objects stored outside `datasets_dir` /
  `datacache_dir` and the read pools.
- `--hash PREFIX ...` — produced artifacts by hash prefix(es).
- `--format FMT` — only objects in this serialization format.
- `--older-than AGE` — only objects last accessed more than AGE ago
  (e.g. `7d`, `36h`, `3600`).

Output style: the default is a styled, grouped, one-line-per-object view with
clickable `file://` locations; `--bare`/`--names` prints a plain name list
(scriptable); `--fields FIELD ...` a tab-separated machine table.

Actions — each action flag applies the matching standalone command to the
filtered selection, **forwarding the rest of the line to that command's own
options** (the `list` selection replaces its `ID`). Put the filters first, then
the action flag and its options. The selection applies directly (`--dry-run`
previews):

- `--delete [--dry-run] [--prune]` — delete the selected objects' bytes
  (artifacts **and** fetched datasets); `--prune` also drops a dataset's
  manifest entry. (Same options as the standalone `delete`; `--batch` is
  irrelevant here — the selection is already explicit — and is ignored.)
- `--move DEST [--dry-run]` — move them under DEST and repoint their state
  records (the manifest is not edited). The tail starts with `DEST`.
- `--push SSH_HOST [--dry-run]` / `--pull SSH_HOST [--dry-run]` — bulk
  cross-machine sync of the selection (rsync over ssh). The tail starts with
  `SSH_HOST`.

```
datamanifest list --cached --orphan --delete --dry-run --prune
datamanifest list --datasets --older-than 30d --move /archive --dry-run
datamanifest list --outside --push user@hpc
datamanifest list --datasets --pull user@hpc --dry-run
```

Maintenance never touches **user-managed data** — a `skip_download` entry, or a
fixed `storage_path` with no `$key` — which the tool didn't place.

### `show NAME`

Print full entry detail in TOML style.

### `path NAME`

Print the resolved on-disk path (composable in shell:
`python analysis.py --data "$(datamanifest path foo)"`).

### `where [--manifest|--state-file|--datasets-dir|--datacache-dir] [--scan]`

Show the active manifest, state file, and the `datasets_dir` / `datacache_dir`
resolved for this host with their read pools folded in; notes how many tracked
objects live outside those folders (`list --outside` to inspect). A single
selector flag prints just that one bare path (scriptable). `--scan` probes the
read pools for datasets present there but not local — the report twin of
`refresh --scan`.

## Fetch and verify

### `download [NAME ...] [--all] [--overwrite] [--delegate|--no-delegate]`

Download specific datasets or `--all` of them; `--overwrite` re-downloads.
`--no-delegate` disables the cross-language fetch rung for the run (`--delegate`
forces it on); see [language-bindings.md](language-bindings.md#cross-language-fetch).

### `verify [NAME ...]`

Re-check checksums in each dataset's declared algorithm (default: all present
datasets); exits nonzero on any mismatch.

### `update-checksums [NAME ...] [--dry-run]`

Recompute stored checksums from what's on disk (e.g. after regenerating data).

## Maintain

### `refresh [--scan] [--dry-run]`

Reconcile the git-ignored state file (`.datamanifest/state.toml`) with disk:
repoint records whose bytes moved, drop records whose bytes are gone, adopt
present-but-untracked datasets. No downloads, no file moves, no bytes touched —
so it applies by default; `--dry-run` previews, and `list --dirty` shows what
would change first.

`--scan` also probes the read pools (including the well-known legacy locations)
and adopts datasets present there but not local yet (checksum-gated; no
downloads or copies) — the active twin of `where --scan`.

### `delete ID [--dry-run] [--batch] [--prune]`

Delete a stored object's **bytes** and prune its state-file record. By default
the manifest entry stays (the recipe survives, so it can be re-fetched);
`--prune` also drops the dataset's manifest entry (≡ `remove`; no effect on
cached artifacts, which have no entry). Protected (user-managed / skip_download /
lazy_access) data is skipped. The object is addressed by its machine-independent
id: a fetched dataset by name/alias/doi, a produced artifact by
`cachetype[/version]/hash` (full or an unambiguous hash prefix). An ambiguous id
errors unless `--batch` (act on all matches).

### `move ID DEST [--dry-run] [--batch]`

Move a stored object's **bytes** under DEST and repoint its state record; the
manifest is not edited (a later re-fetch still follows `datasets_dir`). Same
addressing as `delete`.

### `remove NAME [--keep-cache]`

Delete a dataset's **manifest entry**; `--keep-cache` preserves its files on
disk.

## Sync between machines

### `push ID SSH_HOST [--dry-run] [--batch]` / `pull ID SSH_HOST [--dry-run] [--batch]`

Transfer a single stored object to / from an SSH host (rsync over ssh), same
addressing as `delete`. `--dry-run` reports the selection (id, kind, paths,
size) and transfers nothing. For bulk transfers, filter with `list` and use its
`--push` / `--pull` actions.

The `TARGET` operand follows rsync's colon rule (a colon means remote):

| Operand | Meaning |
|---|---|
| `HOST:` | the remote machine's **store** (its folders resolved remotely) |
| `HOST:PATH` | an explicit folder on an ssh host |
| `NAME:` | a **git remote**'s checkout — that project's own store (see below) |
| `PATH` (no colon) | a local folder, keyed layout — `push` = raw export, `pull` = adopt-by-copy |

A git-remote name takes precedence over an ssh host on collision; the reserved
`git:NAME` / `ssh:HOST[:PATH]` prefixes disambiguate explicitly. The
historical bare-host form (`push ID host`, no colon) still works with a
deprecation warning — write `host:`. Omitting `TARGET` entirely uses the
configured `default_remote` (a config field on any scope, holding any operand
form — including a git remote name).

**Git remotes as targets.** A git remote whose URL is ssh-like and points at a
*checked-out* repo (no bare repos, no https) is a pure project reference: its
value is the peer **checkout path**. `pull` reads the peer's
`.datamanifest/state.toml` — recorded resolved locations, nothing to resolve;
`push` resolves the directive ladder **in the remote context** (preferably by
running `datamanifest where` there over ssh; else by reading the peer's config
files over ssh and evaluating locally, fed the remote env). An https remote
(the typical GitHub `origin`) is rejected as a data target with a clear error.
No new registry: git's remote table is the registry.

- An SSH target (`user@host:`) is both the transport and the host identity —
  no remote registry.
- For the store form, the receiver's folders are resolved best-effort from the
  remote's own environment (the tool probes `DATAMANIFEST_*` via
  `ssh <host> 'source ~/.bashrc; env'`), then the manifest's
  `[_STORAGE._HOST]` rules for that host, then the default. An explicit
  `HOST:PATH` / `PATH` folder is used outright.
- A **local / `$repo`-relative object is not syncable to a store target**. The
  default folders are machine-global, so objects are syncable out of the box;
  an explicit-path target lifts the refusal even for repo-local objects.
- Sync **writes no manifest**. A `pull` records the received object in the
  state file; a pushed object lands in the receiving store as an orphan
  (present, unreferenced) and is immediately usable. Transfers are idempotent
  (a no-op when the target already holds the object complete).
- A folder produced by `push ID PATH` is itself a **read pool** — consumers
  can add it to `datasets_pools`.

### `normalize [TERM...] [--dry-run] [--copy]`

Make **the bytes follow the directive** (the pairing of `refresh`, which makes
the state file follow the bytes): every selected tracked object whose bytes
are not at the directive-derived path (`$datasets_dir/$key`,
`$datacache_dir/<cachetype>[/<version>]/<hash>`) is re-homed there and its
state-file record repointed.

| Bytes found | Action |
|---|---|
| at the derived path | no-op |
| in a read pool | **copy** (pools are shared — never drained) |
| anywhere else | **move** |
| user-managed / `skip_download` / `lazy_access` | skipped, reported |

`--copy` forces copy everywhere. Declared checksums are verified on the way;
the copy/move lands (staging sibling + atomic rename) before any record
changes. `normalize` never downloads (`download` is the verb for missing
data). Preview the selection with `list --out-of-place` (recorded ≠ derived) —
deliberately distinct from `--outside`: a read-pool copy is conformant for
`--outside` but *is* out-of-place, while user-managed exact-path data is
"outside" but in place.

## Configure storage

### `config [show]` / `config set FIELD VALUE... [--local|--global|--project|--host GLOB]` / `config unset FIELD [...]`

Show or edit the scoped storage configuration. `set`/`unset` write to the
checkout's git-ignored `.datamanifest/config.toml` by default (`--local`) —
personal by default, shared deliberately: `--project` edits the committed
manifest's `[_STORAGE]` base, `--host GLOB` its per-host table (with
`--local`/`--global`, the `_HOST` section of that file instead), `--global` the
user-wide `~/.config/datamanifest/config.toml`. `FIELD` is
`datasets_dir`/`datacache_dir`, `project`, `default_remote`, a user `$symbol`,
or a `datasets_pools`/`datacache_pools` list (several values, or none for an
explicit empty list). `show` (the default) prints the config resolved for this
host plus every scope's raw rules.

```bash
datamanifest config set datacache_dir "/scratch/$USER/cache"               # this checkout
datamanifest config set datasets_dir /pool --global                        # this user
datamanifest config set datacache_dir "$user_cache_dir/myproj" --project   # committed default
datamanifest config set datasets_dir /fast/data --host "login*.hpc.edu"    # committed, per-host
datamanifest config                   # show resolved config + raw rules
```

`datamanifest storage` is a deprecated alias of `config` (`--all-hosts` maps to
`--project`).

### `export DEST [TERM...] [--dry-run]`

Copy the selected datasets to `DEST/<key>` (keyed layout), verify declared
checksums during the copy, and write a manifest copy into `DEST` pinning
`datasets_dir = "."`. The result is simultaneously:

- a **read pool** — consumers add it to `datasets_pools`;
- a **standalone datamanifest project** — `cd` into it and `verify` / `list` /
  `path` work as-is.

Export is read-only on the source, so it *includes* user-managed and
`skip_download` datasets — the manually-obtained data a fresh clone cannot
re-download is exactly what is worth bundling (`lazy_access` entries have no
local bytes and are skipped). There is no byte-level `import` verb: consume a
bundle via pools or as a project.

## Manifest tools

### `format [FILE] [-i]`

Rewrite a manifest in canonical form (stable key ordering, cross-tool
byte-identical output). Reads stdin by default; `-i` rewrites FILE in place.

### `migrate FILE [--dry-run] [--no-input]`

Upgrade an older manifest to the current format **without moving any data**:

- modernizes the storage settings (drops retired keys, carries `local_path` →
  `storage_path`; no folder defaults are written out — they would shadow your
  machine-wide config) and any inline language bindings;
- **finds data you already have** — it looks in the old default locations on
  disk (and the read pools) and records each file's real location in the state
  file, so existing downloads keep working while new ones follow the
  configured directive. If one location holds most of your data, it offers to
  point `datasets_dir` there (written to the git-ignored
  `.datamanifest/config.toml`, never the committed manifest); if a file turns
  up in two places, it asks which to use (`--no-input` picks automatically).

`migrate`, `refresh --scan` and `where --scan` also accept `--datasets-pools` /
`--datacache-pools` to override the read pools for a single run (no values =
none).

## Storage model

Where data lives on disk — the two `[_STORAGE]` folder fields, `$`-symbols and
path expressions, the resolution ladder, per-dataset `storage_path`, read pools,
and the state file — is a property of the **manifest format**, consumed by the
CLI, the Python API, and peer-language tools alike. It has its own reference:
**[storage.md](storage.md)**. The [`storage`](#configure-storage) command above
edits it.
