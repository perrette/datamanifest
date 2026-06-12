# Moving data

Data tracked by a manifest is not pinned to one folder. This page covers
moving it — to another folder, another disk, another machine — and how the
tool keeps track. Data movement is a CLI job; the Python and Julia libraries
see the result through the same manifest and state file.

## The mental model

Two files divide the work:

- the committed manifest (`datamanifest.toml`) says **what** each dataset is
  and how to obtain it;
- the git-ignored, per-machine [state file](storage.md#the-state-file)
  (`.datamanifest/state.toml`) says **where the bytes are on this machine**.

Moving bytes is therefore always safe, as long as the state file is told the
new location — the commands below do that as part of the move — or re-derives
it afterwards (`refresh`). Declared checksums make every move verifiable:
`datamanifest verify` re-checks the data wherever it lands. The state file is
never synced between machines; each machine keeps its own.

## On one machine

### Move one object

`move` relocates a single stored object — a fetched dataset addressed by
name (or alias / DOI), a produced artifact by `cachetype[/version]/hash` or
an unambiguous hash prefix — under a destination folder, and repoints its
state-file record:

```bash
datamanifest move co2 /data/archive --dry-run    # preview
datamanifest move co2 /data/archive
```

A dataset lands at `DEST/<key>`, an artifact at
`DEST/<cachetype>/[<version>/]<hash>`. The manifest is not edited, so a later
re-fetch still follows `datasets_dir`. User-managed data — an exact
`storage_path` without `$key`, a `skip_download` or `lazy_access` entry — is
never touched. Reference: [`move`](cli.md#move-id-dest-dry-run-batch).

### Change the folder and make the bytes follow

To re-home everything at once, change the directive, then `normalize`:

```bash
datamanifest config set datasets_dir /big-disk/data
datamanifest normalize --dry-run
datamanifest normalize
```

`normalize` moves every tracked object whose bytes are not at the
directive-derived path to that path and repoints its record. Copies found in
a read pool are copied rather than moved (pools are shared, never drained),
declared checksums are verified before any record changes, and it never
downloads. Reference: [`normalize`](cli.md#normalize-term-filters-dry-run-copy).

### After a manual move: refresh

The same in the opposite order — you moved the bytes yourself (`mv`, `rsync`)
and the records lag behind:

```bash
mv /old/store /big-disk/data                       # the bytes moved by hand
datamanifest config set datasets_dir /big-disk/data
datamanifest refresh                               # the records catch up
```

`refresh` reconciles the state file with disk: it repoints records whose
bytes turn up at the directive-derived path (or in a read pool), drops
records whose bytes are gone, and adopts present-but-untracked datasets — no
downloads, no file moves. The two commands are a pair: `refresh` makes the
state file follow the bytes, `normalize` makes the bytes follow the
directive. `list --dirty` previews what `refresh` would reconcile.
Reference: [`refresh`](cli.md#refresh-scan-dry-run).

### No move at all: read pools

When the data already exists elsewhere on the machine — another project's
store, a shared group folder — nothing needs to move. List the location as a
[read pool](storage.md#read-pools) and adopt the copies in place:

```bash
datamanifest config set datasets_pools /shared/data
datamanifest where --scan      # report what the pools could supply
datamanifest refresh --scan    # adopt: record the pooled locations (checksum-gated, no copies)
```

## Across machines

### Push and pull one object

`push` / `pull` transfer one stored object between machines — rsync over
ssh, or a plain byte copy for a local target — with the same addressing as
`move`:

```bash
datamanifest push co2 user@hpc:                  # to the remote's own store
datamanifest pull esm_anomaly/83425a3 user@hpc:  # a produced artifact, by hash prefix
datamanifest push co2 user@hpc: --dry-run        # preview resolved paths + size
```

The `TARGET` follows rsync's colon rule — a colon means remote:

| Target | Meaning |
|---|---|
| `HOST:` | the remote machine's own store: its `datasets_dir` / `datacache_dir`, resolved in the remote's context (its environment, its `_HOST` rules) |
| `HOST:PATH` | an explicit folder on an ssh host |
| `NAME:` | a git remote's checkout — `pull` reads the peer's state file, `push` resolves the peer's own directives |
| `PATH` | a local folder in keyed layout — `push` = raw export, `pull` = adopt-by-copy |

Omitting `TARGET` uses the configured `default_remote`
(see [Configuration](configuration.md)). Transfers are idempotent — rsync
skips bytes the target already holds — and bytes-only: no manifest is
written. A `pull` records each received object in the local state file; a
`push` leaves the remote state file alone — the receiving side picks the
object up at its derived path on first access, or with its own `refresh`.
Objects stored inside the repository cannot sync to a store target (`HOST:`),
only to an explicit path. The full operand grammar and the git-remote form:
[CLI reference → Sync between machines](cli.md#sync-between-machines).

### Bulk transfers

`list` filters select; the `--push` / `--pull` action flags transfer the
selection, forwarding the rest of the line to the action (`TARGET` first):

```bash
datamanifest list --datasets --push user@hpc: --dry-run
datamanifest list --cached --older-than 30d --push user@hpc:
datamanifest list co2 temperature --pull user@hpc:
```

(The same pattern drives bulk local moves: `list ... --move DEST`.)

### Export a bundle

`export` copies the selected datasets into one self-contained folder: the
bytes in keyed layout (`DEST/<key>`), checksums verified during the copy,
plus a manifest copy pinning `datasets_dir = "."`. The bundle is both a read
pool (consumers add it to `datasets_pools`) and a standalone datamanifest
project — handy for a USB drive, an air-gapped machine, or archiving. It
includes user-managed and `skip_download` data (exactly what a fresh clone
cannot re-download). Reference: [`export`](cli.md#export-dest-term-filters-dry-run).

```bash
datamanifest export /media/usb/bundle
```

### A shared filesystem (HPC)

On a cluster with a shared filesystem there is often nothing to transfer:
point `datasets_dir` at the shared store, or list it as a read pool. A
committed `[_STORAGE._HOST."<glob>"]` table gives each host its own folders
from one manifest (see [Configuration](configuration.md#the-scopes)):

```toml
[_STORAGE._HOST."login*.hpc.org"]
datasets_dir = "/work/shared/datasets"
```

### What does not move

The state file stays per-machine — never push, pull, or commit it; each
machine derives and maintains its own. And nothing about a produced
artifact's identity is machine-specific: it is addressed by
`cachetype/[version/]hash`, so a pulled artifact resolves on the receiving
machine exactly as if it had been computed there.

The Julia package (DataManifest.jl) does not implement `push`/`pull`; use the
CLI alongside it — both read the same manifest and the same stores.
