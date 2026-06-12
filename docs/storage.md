# Storage model

How `datamanifest` decides where data lives on disk. This is a property of the
manifest format — the same model is shared by the CLI, the Python API, and
peer-language implementations such as
[DataManifest.jl](https://github.com/awi-esc/DataManifest.jl).

The repository itself holds only the manifest (`datamanifest.toml`) and the
git-ignored `.datamanifest/` directory. The data lives elsewhere, governed by
two folder settings.

## Where data goes by default

Storage is two folder settings:

- `datasets_dir` — where fetched datasets go. Default:
  `$user_data_dir/datamanifest/shared/datasets` (under `~/.local/share` on
  Linux). A dataset's **key** — `host/path[#version]`, derived from its URI —
  is globally unique, so one machine-wide store deduplicates downloads across
  all your projects.
- `datacache_dir` — where the results of
  [`@cached` functions](api.md#caching-computed-results) go. Default:
  `$user_cache_dir/datamanifest/projects/$project/cached` (under `~/.cache` on
  Linux). A cached result is identified by `cachetype/hash`, which is *not*
  globally unique across projects, so each project gets its own segment —
  `$project` defaults to the checkout's folder name.

The layout under each folder is flat — the folder you set **is** the location:

- fetched dataset → `<datasets_dir>/<key>`
- cached result → `<datacache_dir>/<cachetype>/[<version>/]<hash>`

### Names on disk

A dataset's `version` is part of its key, and hence of the on-disk name:
`version = "v2.1"` turns `<datasets_dir>/<key>` into
`<datasets_dir>/<key>#v2.1`, so several versions coexist. For
`extract = true` datasets, the extracted folder is named after the archive
with the archive extension stripped (`.zip`, `.tar`, `.tar.gz`); when there
is no recognizable extension to strip (e.g. a versioned name ending in
`#v2.1`), a `.d` suffix is appended instead.

To see where a dataset resolves on this machine:

=== "CLI"

    ```bash
    datamanifest path co2        # print the resolved on-disk path
    ```

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")
    db.get_dataset_path("co2")   # print the resolved on-disk path
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")
    get_dataset_path(db, "co2")   # print the resolved on-disk path
    ```

## Changing the folders

Set either folder with `datamanifest config set`:

```console
$ datamanifest config set datasets_dir /data/store           # this checkout (default)
$ datamanifest config set datasets_dir /data/store --global  # this user, all projects
$ datamanifest config set datacache_dir '$repo/cached' --project   # committed to the manifest
$ datamanifest config set datasets_dir /work/data --host 'login*'  # committed, per-host
$ datamanifest config show                                    # resolved values + raw rules
```

A value can be set per checkout (the default — the git-ignored
`.datamanifest/config.toml`), in the committed manifest's `[_STORAGE]` table
(`--project`, shared with collaborators), per host (`--host GLOB`), or
user-wide (`--global`, in `~/.config/datamanifest/config.toml`). `config set`
is a CLI convenience over plain TOML files — you can also edit them by hand,
and peer implementations such as DataManifest.jl read the same files. An
environment variable `DATAMANIFEST_DATASETS_DIR` / `DATAMANIFEST_DATACACHE_DIR`
overrides them all for one process. The full set of scopes, the resolution
order, and every variable are documented in [Configuration](configuration.md).

### Keeping data inside the repository

If you prefer visible `./datasets/` and `./cached/` folders inside the
project, it is one committed edit:

```toml
[_STORAGE]
datasets_dir  = "datasets"
datacache_dir = "cached"
```

Relative paths are anchored at the project root (the manifest's directory).
Note that repo-local objects cannot be `push`/`pull`ed between machines — the
[sync commands](cli.md#sync-between-machines) need a machine-global location to
re-attach objects under.

## Path expressions

A **path expression** (a folder setting, or a dataset's `storage_path`) may
interpolate `$`-symbols (`$NAME` or `${NAME}`) and `~`. The available symbols:

**Machine directories (predefined):**

- `$user_data_dir` — the OS user *data* directory, straight from
  [`platformdirs`](https://pypi.org/project/platformdirs/), bare (no app
  name): `~/.local/share` on Linux.
- `$user_cache_dir` — the OS user *cache* directory, bare: `~/.cache` on Linux.
- `$repo` — the project root (the manifest's directory).
- `$project` — the project name (default: the basename of the project root).
  Override it like any setting — a committed `project = "..."` in the manifest
  is shared intent. Renames are safe: the [state file](#the-state-file) keeps
  finding old artifacts at their recorded locations; new writes go under the
  new name.

**Storage settings and your own symbols:**

- `$datasets_dir`, `$datacache_dir` — the two resolved folder settings.
- `$<name>` — any other bare key you define in `[_STORAGE]` or a config file
  (e.g. `scratch = "/scratch/$USER"` makes `$scratch` available).
  Host-specific values go in `[_HOST."<glob>"]` sections.

**Environment:**

- `$NAME` — a name not defined above falls back to the `NAME` environment
  variable (`$USER`, `$HOME`, …); if that is unset too, the token is left
  verbatim. A configured symbol of the same name takes precedence over the
  environment variable.
- `~` — expands to the home directory.

**Dataset-specific:**

- `$key` — the dataset's storage key (`host/path[#version]`). Only meaningful
  inside a dataset's `storage_path` (whose default is `$datasets_dir/$key`).

```toml
[_STORAGE]
datacache_dir = "$user_cache_dir/myproj" # committed project intent
scratch       = "/tmp/$USER/scratch"     # user-defined symbol

[_STORAGE._HOST."login*.hpc.edu"]
scratch = "/scratch/$USER"               # host-specific override of a user symbol

[bigsim]                                 # → <datasets_dir>/<key>  (default storage_path)
uri = "https://example.com/bigsim.nc"

[hpc_output]                             # per-dataset override (a path expression)
storage_path = "$scratch/results/$key"
format = "nc"
```

## Per-dataset override

A dataset may set `storage_path` — a path expression whose default is
`$datasets_dir/$key`. Setting it relocates one dataset without changing
anything else. It comes in two flavors:

- a `storage_path` that contains `$key` is a **tool-managed** keyed
  location — maintenance commands may move or delete it like any stored
  object;
- an exact path with no `$key` is a **user-managed** location: it is used
  verbatim, maintenance commands (`delete`, `move`) never touch it, and the
  [read pools](#read-pools) are not probed for it.

The expression may use `$datasets_dir`, `$key` and the other
[`$`-symbols](#path-expressions). A relative result is resolved against the
project root (the manifest's directory) — convenient for committing small
data files alongside the code. An absolute path is used as-is — handy for a
NAS, an external drive, or a scratch volume.

The rest of the pipeline is unchanged:

- **Cache hit.** If the file is already at the resolved path, it is returned
  without ever consulting the URI — exactly the behavior of a file committed
  to the repository.
- **Cache miss.** If the file is missing, the normal download from `uri` runs
  and the result lands at the resolved path — so a fetched dataset can be
  redirected into the repository instead of the shared store.
- **Checksum.** A declared `checksum` is still verified against the file at
  the resolved path.

```toml
[in_repo_dataset]
uri = "https://example.com/dataset.csv"   # source
storage_path = "data/dataset.csv"         # relative → resolved against the project root
checksum = "sha256:..."                   # still verified
```

For sources that cannot be fetched automatically (bot protection,
click-through agreements, manual logins), pair the entry with
`skip_download = true`: it makes the user-managed nature explicit and
prevents any download attempt. `skip_download = true` without a
`storage_path` makes the dataset's local path the `uri` value, returned
verbatim — useful when the URI already *is* a local path. With an explicit
`storage_path`, the path is resolved like any other; both tools follow the
same rule.

## Read pools

If a dataset (or a `@cached` result) already exists somewhere else on your
machine — say another project downloaded it — `datamanifest` can reuse that
copy in place instead of fetching it again. A **read pool** is an extra
read-only folder probed for an existing copy before downloading; on a match
the location is recorded in the state file and used as-is, while new downloads
still go to your own `datasets_dir`. Pools are never written to.

- **Datasets** (`datasets_pools`). The built-in pools are `$repo/datasets`
  (the repo-local layout), the shared store itself
  (`$user_data_dir/datamanifest/shared/datasets`, so the default store doubles
  as a pool), `$user_data_dir/datamanifest/datasets`, and `~/.cache/Datasets`.
  A copy found in a pool is verified against the dataset's declared checksum
  before it is adopted; a mismatch is reported and skipped.
- **`@cached` results** (`datacache_pools`). Off unless configured — there is
  no standard shared location for computed results, and they carry no content
  checksum (they are identified by `cachetype`/`version`/`hash` and validated
  via their `config.toml` sidecar).

Point the tool at your own shared folders with
`datamanifest config set datasets_pools <dir> …` — setting an explicit value
replaces the built-in list, and an empty list (`config set datasets_pools`
with no values) turns pools off. Pools can differ per machine. See what is
reusable with `datamanifest where --scan` (report only) or pull it all in with
`datamanifest refresh --scan` (adopt).

## The state file

The tool keeps a small git-ignored **state file**,
`.datamanifest/state.toml`, recording where each object — fetched or produced —
actually landed on this machine, plus each fetched dataset's checksum. The
manifest says *what* to track and *how* to obtain it; the state file says
*where it is right now*, so the tool never loses track of your data and never
re-downloads something it can already find.

The state file is an inventory, not a directive: it is only read to *find*
objects, never to decide where new writes go (writes always follow the current
`datasets_dir` / `datacache_dir` / `storage_path` settings). It is local and
disposable — delete it and it rebuilds itself as you use your data. When
`.datamanifest/state.toml` is absent, the sibling files
`.datamanifest-state.toml` and `cached.toml` in the project root are read as
fallbacks, and the next write relocates the inventory to
`.datamanifest/state.toml`. The full design is in
[design-state-file.md](https://github.com/perrette/datamanifest/blob/main/design/design-state-file.md).

Linked `git worktree`s share the main checkout's state file. A worktree starts
without the git-ignored `.datamanifest/` directory; when the project directory
has no state file of its own and sits inside a linked worktree (`git worktree
add`), lookups fall through to the corresponding directory in the main
checkout — reads consult its inventory and writes update it, so all worktrees
of a repository maintain one shared inventory. A state file present in the
worktree itself always takes precedence (create one there to opt a worktree
out). The main checkout is resolved by asking the `git` executable; when `git`
is not installed, the main repository is bare, or the directory is not inside
a worktree, lookups stay local.

## Maintenance

The [maintenance commands](cli.md#maintain) operate on the state file and the
stored bytes:

- `datamanifest list --dirty` — show objects whose state-file record disagrees
  with what is on disk.
- `datamanifest refresh` — make the state file follow the bytes: re-point
  stale records to where the data actually is, drop records whose bytes are
  gone, adopt present-but-untracked data. No bytes are touched. `--dry-run`
  previews; `--scan` also probes the read pools.
- `datamanifest normalize` — the other direction: make the bytes follow the
  current settings, re-homing objects whose location disagrees with the
  resolved directives.
- `datamanifest where` — print the resolved manifest, state-file, and folder
  paths; `--scan` reports what the read pools could supply.
- `datamanifest delete ID` / `datamanifest move ID DEST` — remove or relocate
  a single stored object by id.

How these fit together when relocating data — within a machine or across
machines — is walked through in [Moving data](moving-data.md).
