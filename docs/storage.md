# Storage model

How `datamanifest` decides **where data lives** on disk. This is a property of the
manifest format — the same model is consumed by the CLI, the Python API, and any
peer-language implementation — not a CLI feature. The `datamanifest storage`
command edits it; everything else reads it.

Storage is **two folder fields** set in `[_STORAGE]`:

- `datasets_dir` (default `"datasets"`) — where fetched datasets go.
- `datacache_dir` (default `"cached"`) — where the produced `@cached` cache goes.

Both default to **relative** paths, so they resolve against the project root
(the manifest's directory, `$repo`) and you get a visible local `./datasets/`
and `./cached/` with zero config. The layout is flat — the folder you set **is**
the location:

- fetched dataset → `<datasets_dir>/<key>`
- produced artifact → `<datacache_dir>/<cachetype>/[<version>/]<hash>`

## Path expressions

A path expression (a `[_STORAGE]` value or a dataset's `storage_path`) may
interpolate `$`-symbols and `~`. The available symbols:

**Machine directories (predefined):**

- `$user_data_dir` — the OS user *data* dir, straight from `platformdirs`, **bare**
  (no app name): `~/.local/share` on Linux, etc.
- `$user_cache_dir` — the OS user *cache* dir, bare: `~/.cache` on Linux, etc.
- `$repo` — the project root (the manifest's directory).

**Storage fields & your own symbols:**

- `$datasets_dir`, `$datacache_dir` — the two resolved storage fields.
- `$<name>` — any bare key you define under `[_STORAGE]` (e.g. `$scratch`).
  Host-specific overrides go in `[_STORAGE._HOST."<glob>"]`.

**Environment:**

- `$NAME` — any name not defined above falls back to the `NAME` **environment
  variable** (`$USER`, `$HOME`, …). A `[_STORAGE]` symbol of the same name takes
  precedence over the environment variable.
- `~` — expands to the home directory.

**Dataset-specific:**

- `$key` — a dataset's storage key (`host/path[#version]`). Only meaningful inside
  a dataset's `storage_path` (whose default is `$datasets_dir/$key`).

```toml
[_STORAGE]
datasets_dir  = "datasets"               # default: repo-local ./datasets/
datacache_dir = "$user_cache_dir/myproj" # produced artifacts on the machine cache dir
scratch       = "/tmp/$USER/scratch"     # user-defined symbol

[_STORAGE._HOST."login*.hpc.edu"]
scratch = "/scratch/$USER"               # host-specific override of a user symbol

[bigsim]                                 # → datasets/bigsim  (default storage_path)
uri = "https://example.com/bigsim.nc"

[hpc_output]                             # per-dataset override (a path expression)
storage_path = "$scratch/results/$key"
format = "nc"
```

## Resolution ladder

For any field/symbol *name*, first match wins:

1. `DATAMANIFEST_<NAME>` environment variable.
2. First `[_STORAGE._HOST.<glob>].<name>` whose glob matches the hostname.
3. `[_STORAGE].<name>` base value.
4. The predefined symbol or field default.

The only two env vars of note are `DATAMANIFEST_DATASETS_DIR` and
`DATAMANIFEST_DATACACHE_DIR`.

## Per-dataset override

A dataset may set `storage_path` — a path expression, default
`$datasets_dir/$key`. A `storage_path` that contains `$key` is a tool-managed
keyed location; an exact path with no `$key` is a **user-managed** location used
verbatim that maintenance never touches.

## Centralizing / sharing

Because the default folders are repo-local, point both fields at a machine
directory to share data across clones or projects — one explicit edit:

```toml
[_STORAGE]
datasets_dir  = "$user_data_dir/myproj"
datacache_dir = "$user_cache_dir/myproj"
```

## Read pools — don't re-download what you already have

If a dataset (or a `@cached` result) already exists somewhere else on your
machine — say another project downloaded it — datamanifest can **reuse that copy
in place** instead of fetching it again. It checks a few **read pools** (extra
read-only folders); on a match it records the location and uses it, while new
downloads still go to your own `datasets_dir`.

- **Datasets** are looked up in well-known shared folders by default (e.g.
  `~/.cache/Datasets`), and a found copy is **checksum-verified** before it's
  trusted.
- **`@cached` results** are not shared by default (opt-in) — there's no standard
  shared location for them, and they carry no content checksum.

Point the tool at your own shared folders with
`datamanifest storage set datasets_pools <dir> …`; see what's reusable with
`where --scan` (report) or pull it all in with `refresh --scan` (adopt). Pools
can differ per machine, and an empty list turns them off.

## The state file

Next to the manifest, the tool keeps a small git-ignored **state file**,
`.datamanifest-state.toml`, recording *where each object ended up on this
machine* (and its checksum) — so it never loses track of your data and never
re-downloads something it can already find. It's local and disposable: delete it
and it rebuilds itself as you use your data. `list --dirty`, `refresh`, and the
maintenance actions all operate on it; the full design is in
[design-state-file.md](design-state-file.md).
