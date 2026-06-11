# Storage model

How `datamanifest` decides **where data lives** on disk. This is a property of the
manifest format — the same model is consumed by the CLI, the Python API, and any
peer-language implementation — not a CLI feature. The `datamanifest config`
command edits it; everything else reads it.

Storage is **two folder fields**:

- `datasets_dir` — where fetched datasets go. Default:
  `$user_data_dir/datamanifest/shared/datasets`, a machine-wide **shared keyed
  store**. A dataset key (`host/path[#version]`) is globally unique, so one
  shared store deduplicates downloads across all your projects, with no project
  name in the path.
- `datacache_dir` — where the produced `@cached` cache goes. Default:
  `$user_cache_dir/datamanifest/projects/$project/cached`, a **per-project**
  cache (`cachetype/hash` is not globally unique, so each project gets its own
  segment — `$project` defaults to the checkout's folder name).

The repository itself holds only the manifest and the git-ignored
`.datamanifest/` directory. The layout under each folder is flat — the folder
you set **is** the location:

- fetched dataset → `<datasets_dir>/<key>`
- produced artifact → `<datacache_dir>/<cachetype>/[<version>/]<hash>`

## Scoped configuration

Folder directives are inherently **per-machine**, so they have per-machine homes
besides the committed manifest — two `[_STORAGE]`-shaped TOML config files,
git-config style:

- `.datamanifest/config.toml` — per-checkout, git-ignored. Personal settings
  for this clone (the `.datamanifest/` directory ignores itself via its own
  `.gitignore`).
- `~/.config/datamanifest/config.toml` (`$XDG_CONFIG_HOME`) — user-global
  settings for every project on the machine.

Both files take the same fields as the manifest's `[_STORAGE]` (folder fields,
`$symbols`, `*_pools`, `project`, `default_remote`), including `[_HOST."<glob>"]`
sections — home directories and checkouts often live on filesystems shared
across cluster nodes, so even personal config can need per-host scoping.

Edit any scope with `datamanifest config set` (see the resolution ladder below
for what wins):

```console
$ datamanifest config set datasets_dir /data/store          # this checkout (default)
$ datamanifest config set datasets_dir /data/store --global # this user, all projects
$ datamanifest config set datacache_dir '$repo/cached' --project  # committed intent
$ datamanifest config set scratch /scratch/$USER --host 'login*'  # committed, per-host
$ datamanifest config show                                   # resolved + raw rules
```

The `--local` default is deliberate: configuration is **personal by default,
shared deliberately** — a per-user absolute path never leaks into the committed
manifest by accident.

## Path expressions

A path expression (a config value or a dataset's `storage_path`) may
interpolate `$`-symbols and `~`. The available symbols:

**Machine directories (predefined):**

- `$user_data_dir` — the OS user *data* dir, straight from `platformdirs`, **bare**
  (no app name): `~/.local/share` on Linux, etc.
- `$user_cache_dir` — the OS user *cache* dir, bare: `~/.cache` on Linux, etc.
- `$repo` — the project root (the manifest's directory).
- `$project` — the project name (default: the basename of the project root).
  Override it like any field — a committed `project = "..."` in the manifest is
  shared intent. Renames are safe: the state file keeps finding old artifacts
  at their recorded locations; new writes go under the new name.

**Storage fields & your own symbols:**

- `$datasets_dir`, `$datacache_dir` — the two resolved storage fields.
- `$<name>` — any bare key you define on any scope (e.g. `$scratch`).
  Host-specific overrides go in `[_HOST."<glob>"]` sections.

**Environment:**

- `$NAME` — any name not defined above falls back to the `NAME` **environment
  variable** (`$USER`, `$HOME`, …). A configured symbol of the same name takes
  precedence over the environment variable.
- `~` — expands to the home directory.

**Dataset-specific:**

- `$key` — a dataset's storage key (`host/path[#version]`). Only meaningful inside
  a dataset's `storage_path` (whose default is `$datasets_dir/$key`).

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

## Resolution ladder

For any field/symbol *name*, first match wins (more specific scope wins,
git-style):

1. `DATAMANIFEST_<NAME>` environment variable.
2. `.datamanifest/config.toml` — checkout scope (`_HOST` glob, then base).
3. Manifest `[_STORAGE._HOST.<glob>]` — committed, shared infrastructure.
4. Manifest `[_STORAGE]` base — committed project intent.
5. `~/.config/datamanifest/config.toml` — user scope (`_HOST` glob, then base).
6. Built-in defaults.

The only two env vars of note are `DATAMANIFEST_DATASETS_DIR` and
`DATAMANIFEST_DATACACHE_DIR`.

## Per-dataset override

A dataset may set `storage_path` — a path expression, default
`$datasets_dir/$key`. A `storage_path` that contains `$key` is a tool-managed
keyed location; an exact path with no `$key` is a **user-managed** location used
verbatim that maintenance never touches.

## Keeping data inside the repository

The defaults are machine-global. If you prefer the previous repo-local layout
(visible `./datasets/` and `./cached/` folders), it's one committed edit:

```toml
[_STORAGE]
datasets_dir  = "datasets"
datacache_dir = "cached"
```

Note that repo-local objects cannot be `push`/`pull`ed between machines — the
sync surface needs a machine-global location to re-attach objects under.

## Read pools — don't re-download what you already have

If a dataset (or a `@cached` result) already exists somewhere else on your
machine — say another project downloaded it — datamanifest can **reuse that copy
in place** instead of fetching it again. It checks a few **read pools** (extra
read-only folders); on a match it records the location and uses it, while new
downloads still go to your own `datasets_dir`.

- **Datasets** are looked up by default in the repo-local `./datasets/` layout
  (so pre-existing repo-local data is found, never re-downloaded), the shared
  store (which therefore self-populates as a pool), and the well-known legacy
  folders (`$user_data_dir/datamanifest/datasets`, `~/.cache/Datasets`). A
  found copy is **checksum-verified** before it's trusted.
- **`@cached` results** are not shared by default (opt-in) — there's no standard
  shared location for them, and they carry no content checksum.

Point the tool at your own shared folders with
`datamanifest config set datasets_pools <dir> …`; see what's reusable with
`where --scan` (report) or pull it all in with `refresh --scan` (adopt). Pools
can differ per machine, and an empty list turns them off.

## The state file

Inside the project's private directory, the tool keeps a small git-ignored
**state file**, `.datamanifest/state.toml`, recording *where each object ended
up on this machine* (and its checksum) — so it never loses track of your data
and never re-downloads something it can already find. It's local and
disposable: delete it and it rebuilds itself as you use your data. The legacy
sibling names (`.datamanifest-state.toml`, `cached.toml`) are still read; the
first write relocates them. `list --dirty`, `refresh`, and the maintenance
actions all operate on it; the full design is in
[design-state-file.md](https://github.com/perrette/datamanifest/blob/main/design/design-state-file.md).

Linked `git worktree`s share the main checkout's state file (spec-v5.1). A
worktree starts without the git-ignored `.datamanifest/` directory; when the
project directory has no state file of its own and sits inside a linked
worktree (`git worktree add`), lookups fall through to the corresponding
directory in the main checkout — reads consult its inventory and writes update
it, so all worktrees of a repository maintain one shared inventory. A state
file present in the worktree itself always takes precedence (create one there
to opt a worktree out). The main checkout is resolved by asking the `git`
executable; when `git` is not installed, the main repository is bare, or the
directory is not inside a worktree, lookups stay local.
