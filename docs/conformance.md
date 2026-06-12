# Conformance & the shared manifest format

`datamanifest` reads and writes a manifest format that is **shared across languages** —
the same `datamanifest.toml` can be consumed by sibling implementations (the Julia
[`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)) without conflict, because
each tool reads the common fields plus its own `_LANG`-namespaced bindings and preserves
the rest verbatim. The format is defined in its own specification,
[`datamanifest.toml`](manifest-spec.md) (mirrored on this site). For a
practical summary of the fields, see the [manifest format](manifest-format.md)
page.

You do **not** need this page to use `datamanifest` — the
[quickstart](quickstart.md) and [use cases](use-cases.md) cover everyday usage. This file is for anyone who needs the cross-tool details: which features
this Python implementation supports, and which version of the shared format it targets.

A deeper, Python-specific walkthrough of the CLI and language bindings lives in
[`cli.md`](cli.md) and [`language-bindings.md`](language-bindings.md).

## Implemented capabilities

| Capability | Status | Notes |
|---|---|---|
| `lang-read` | ✅ | Parse the `_LANG` namespace and the language-implicit ("bare") forms — a per-dataset `fetcher`/`loader` and the top-level `[_LOADERS]` format→binding map, read as Python — then apply the fetch/load ladders. Explicit `_LANG.python` wins over the bare counterpart; a binding present for the running language (bare or explicit) that fails is an error — fail-loud, no silent fall-through. |
| `lang-write` | ✅ | Regenerate `_LANG.python` from explicit bindings only; keep bare `fetcher`/`loader`/`shell` and `[_LOADERS]` bare (never promoted); preserve foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Run the dataset's bare `shell` command template (the canonical, language-agnostic form) in the fetch ladder; the older `_LANG.shell.fetcher` form is read as a fallback. |
| `storage` | ✅ | Two folder fields — `datasets_dir` (default: the machine-wide shared store `$user_data_dir/datamanifest/shared/datasets`) and `datacache_dir` (default: the per-project `$user_cache_dir/datamanifest/projects/$project/cached`). Path expressions interpolate `$user_data_dir`/`$user_cache_dir` (bare `platformdirs`, no app name), `$repo` (project root), `$project` (project name; default the root's basename), `$datasets_dir`/`$datacache_dir`, `$key`, user-defined symbols, `$USER`/env vars, `~`. Per-host overrides in `[_HOST."<glob>"]` sections; per-dataset `storage_path` override. Resolution ladder per name: `DATAMANIFEST_<NAME>` env → `.datamanifest/config.toml` (host, base) → manifest `[_STORAGE._HOST.<glob>]` → `[_STORAGE]` base → `~/.config/datamanifest/config.toml` (host, base) → default; the two env vars of note are `DATAMANIFEST_DATASETS_DIR`/`DATAMANIFEST_DATACACHE_DIR`. |
| `binding-args` | ✅ | The `{ ref, args, kwargs }` table binding form with `$var` substitution. |
| `byte-identity` | ✅ | Canonical serialization (this implementation is the reference): structural `_*` tables first at the top level, then the datasets, every key sorted by Unicode code point at every nesting level — `datamanifest format` exposes it for peer tools. |
| `cache-produce` | ✅ | `@cached` produce-or-load: parameter-hash keying, optional recipe `version`, `config.toml`/`metadata.toml` sidecars; artifacts land at `<datacache_dir>/<cachetype>/[<version>/]<hash>`. |
| `inspect` | ✅ | The `cached.toml` index and the `datamanifest list` maintenance surface (filter + `--delete`/`--move`, dry-run by default; never an automatic collector). |
| `sync` | ✅ | Cross-machine `push`/`pull` over rsync+ssh, addressed by machine-independent id (a fetched dataset's `key`, or a produced artifact's `cachetype[/version]/hash`); the object lands under the receiver's own `datasets_dir`/`datacache_dir`, resolved best-effort from the remote env (`source ~/.bashrc`) then `[_STORAGE._HOST]`/default; writes no manifest; idempotent. A `$repo`-relative object is not syncable; the machine-global defaults are syncable out of the box. |
| `delegation` | ✅ | Cross-language fetch (rung 3): runs a foreign-language fetcher by invoking the local Julia `DataManifest` env (`julia --project=… -e 'using DataManifest; download_dataset(Database("…"), "…")'`) when present, materializing into the shared store; logs a warning and falls through to `uri` when the Julia env is absent. Fetched-only; on by default and probe-gated; the `delegate` field / `--delegate` / `--no-delegate` toggles it. |

## Conformance tests and the pinned spec version

The conformance claim is anchored in `tests/conformance_pin.toml`, which pins a
tag of the shared specification repository (currently `spec-v5.6`) and records
a SHA-256 hash for every fixture file of that tag. The pin is advanced
deliberately, when this implementation is verified against the newer fixtures,
not automatically on every spec release.

`tests/test_conformance.py` downloads the pinned fixture tarball, verifies every
file against its recorded hash, and runs only the fixtures whose declared
capabilities are a subset of those implemented above, skipping the rest with a
reason.

## Julia implementation

[DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) anchors its
conformance claim the same way: its `test/conformance_pin.toml` pins the same
spec tag (currently `spec-v5.6`) with the same fixture mechanism — per-file
content hashes, fixtures filtered by declared capabilities. The one capability
it does not implement is `sync` (cross-machine `push`/`pull`); the Python CLI
covers it.
