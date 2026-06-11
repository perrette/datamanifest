# Configuration

This page lists every configuration variable, the places a value can be set
(its *scopes*), and the rule that decides which value wins. What the storage
settings *mean* is covered in the [storage model](storage.md); this page shows
the whole configuration system at a glance.

The guiding principle: the manifest (`datamanifest.toml`) is committed and
shared with collaborators, so it carries only project-wide intent. Anything
machine-specific — where data lands on *this* computer, personal preferences —
belongs in git-ignored config files, so it never leaks into the repository.

## The scopes

A value can be set in five places. From the most specific to the most general:

| Scope | Where | Shared? | Typical use |
|---|---|---|---|
| Environment variable | `DATAMANIFEST_<NAME>` (upper-cased variable name) | per process | one-off overrides, CI, tests |
| Checkout config | `<project>/.datamanifest/config.toml` (git-ignored) | this clone only | per-machine choices for one project |
| Manifest | `[_STORAGE]` table in `datamanifest.toml` (committed) | every collaborator | project-wide intent |
| User config | `~/.config/datamanifest/config.toml` (`$XDG_CONFIG_HOME`) | every project of this user | machine-wide preferences |
| Built-in default | — | — | what you get without any configuration |

Two refinements:

- **Host-specific values.** Each of the three file scopes accepts a
  `[_HOST."<glob>"]` sub-table whose values apply only on matching hostnames
  (`*` and `?` wildcards). Within a file, a `_HOST` match beats the base
  value. This is how one committed manifest serves a laptop and an HPC
  cluster at once.
- **Git worktrees.** A linked `git worktree` starts without the git-ignored
  `.datamanifest/` directory, so when a worktree has no checkout config of
  its own, the main checkout's file is read instead. A config file created in
  the worktree itself takes precedence.

## How a value is resolved

For a variable `name`, the first match wins, top to bottom:

1. the `DATAMANIFEST_<NAME>` environment variable;
2. the checkout config (`_HOST` match first, then the base value);
3. the manifest's `[_STORAGE._HOST."<glob>"]`, then `[_STORAGE]`;
4. the user config (`_HOST` match first, then base);
5. the built-in default.

The whole ladder — environment and hostname included — is captured **once,
when a `Database` is created** (and again whenever it reads a manifest). The
result is a frozen snapshot, so every variable has one well-defined value for
the `Database`'s lifetime; editing a config file or the environment afterwards
does not silently retarget an existing session. To pick up changes, create a
new `Database` (or re-read the manifest). Command-line invocations build a
fresh `Database` on every run, so edits always apply to the next command.

## Editing configuration

Edit the TOML files directly, or use `datamanifest config` (see the
[CLI reference](cli.md#configure-storage)):

```bash
datamanifest config show                       # resolved values + each scope's raw rules
datamanifest config set datasets_dir /data/store               # checkout config (the default scope)
datamanifest config set datasets_dir /scratch/data --global    # user config
datamanifest config set datacache_dir cached --project         # committed [_STORAGE]
datamanifest config set datasets_dir /work/data --host 'login*.hpc.org'
datamanifest config unset datasets_dir         # remove from a scope
```

`set` writes to the checkout config by default — configuration is personal by
default, shared deliberately (`--project` / `--host`). A bare `--host GLOB`
targets the manifest's `[_STORAGE._HOST."<glob>"]`; combined with `--local` or
`--global` it targets that file's `[_HOST]` section instead. `set` stores
native TOML types for the typed variables: `canonical` becomes a boolean,
`lock_stale_age` a number; everything else is stored as a string.

## The variables

| Variable | Type | Default | What it does |
|---|---|---|---|
| `datasets_dir` | path expression | `$user_data_dir/datamanifest/shared/datasets` | Where fetched datasets are stored. See [storage model](storage.md). |
| `datacache_dir` | path expression | `$user_cache_dir/datamanifest/projects/$project/cached` | Where [`@cached`](api.md#caching-computed-results) results are stored. |
| `datasets_pools` | list of path expressions | built-in list | Extra read-only places probed for already-present datasets before downloading. See [read pools](storage.md#read-pools). |
| `datacache_pools` | list of path expressions | none | Same, for `@cached` results. |
| `project` | name | basename of the project root | The project's name — the `$project` symbol, which namespaces the default cache folder. |
| `default_remote` | transfer target | unset | The target `push`/`pull` use when none is given on the command line. Takes any [target form](cli.md#sync-between-machines): `HOST:` (a remote store), `HOST:PATH`, a git remote name, or a local path. Used verbatim — no `$`-symbol interpolation. |
| `canonical` | boolean | `false` | Whether manifest writes must go through `datamanifest format` to produce the cross-tool reference form. This tool's writer already emits that form, so the setting does not change its output; it is consumed by peer tools such as [DataManifest.jl](https://github.com/perrette/DataManifest.jl), which pipe their writes through `datamanifest format` when it is set. |
| `lock_stale_age` | seconds | `30` | How old a materialization lock may grow (its holder refreshes it every `lock_stale_age / 2` as a heartbeat) before a waiting process may reclaim it. A non-positive or unparsable value falls back to the default. |

The environment-variable form is always `DATAMANIFEST_` + the upper-cased
name: `DATAMANIFEST_DATASETS_DIR`, `DATAMANIFEST_DEFAULT_REMOTE`,
`DATAMANIFEST_LOCK_STALE_AGE`, … List-valued variables use the platform path
separator in their environment form
(`DATAMANIFEST_DATASETS_POOLS="/a:/b"` on Linux).

Beyond these, **any other bare key** in `[_STORAGE]` or a config file defines
a **user symbol**: `scratch = "/scratch/$USER"` makes `$scratch` available
inside [path expressions](storage.md#path-expressions), host-composable via
`_HOST` like everything else. Tools ignore keys they do not understand, so the
same files can carry fields used by only one tool.

## Examples

A checkout config that keeps one project's data inside the repository:

```toml
# <project>/.datamanifest/config.toml  (git-ignored)
datasets_dir = "datasets"
datacache_dir = "cached"
```

A committed manifest that names the project and routes data to a shared
filesystem on the cluster only:

```toml
# datamanifest.toml
[_STORAGE]
project = "lgm-recons"

[_STORAGE._HOST."*.hpc.org"]
datasets_dir = "/work/shared/datasets"
```

A user config that relocates every project's cache to a big disk and sets a
default transfer target:

```toml
# ~/.config/datamanifest/config.toml
datacache_dir = "/data/$USER/datamanifest/$project/cached"
default_remote = "user@hpc:"
```
