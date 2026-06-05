# Conformance & the shared manifest format

`datamanifest` reads and writes a manifest format that is **shared across languages** —
the same `datasets.toml` can be consumed by sibling implementations (today the Julia
[`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)) without conflict, because
each tool reads the common fields plus its own `_LANG`-namespaced bindings and preserves
the rest verbatim. The format is defined in its own repository,
[`perrette/datamanifest.toml`](https://github.com/perrette/datamanifest.toml).

You do **not** need this page to use `datamanifest` — the [README](../README.md) covers
everyday usage. This file is for anyone who needs the cross-tool details: which features
this Python implementation supports, and which version of the shared format it targets.

A deeper, Python-specific walkthrough of the CLI and language bindings lives in
[`cli.md`](cli.md) and [`language-bindings.md`](language-bindings.md).

## Targeted format version

This release targets **spec-v4** of the shared format.

## Implemented capabilities

| Capability | Status | Notes |
|---|---|---|
| `lang-read` | ✅ | Parse the `_LANG` namespace and the language-implicit ("bare") forms — a per-dataset `fetcher`/`loader` and the top-level `[_LOADERS]` format→binding map, read as Python (spec-v3.4) — then apply the fetch/load ladders. Explicit `_LANG.python` wins over the bare counterpart; a binding present for the running language (bare or explicit) that fails is an error — fail-loud, no silent fall-through (spec-v3.6). |
| `lang-write` | ✅ | Regenerate `_LANG.python` from explicit bindings only; keep bare `fetcher`/`loader`/`shell` and `[_LOADERS]` bare (never promoted); preserve foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Run the dataset's bare `shell` command template (spec-v3.5 canonical, language-agnostic) in the fetch ladder, else the legacy `_LANG.shell.fetcher` fallback. |
| `storage` | ✅ | Two folder fields in `[_STORAGE]` — `datasets_dir` (default `"datasets"`) and `datacache_dir` (default `"cached"`), both repo-local by default. Path expressions interpolate `$user_data_dir`/`$user_cache_dir` (bare `platformdirs`, no app name), `$repo` (project root), `$datasets_dir`/`$datacache_dir`, `$key`, user-defined `[_STORAGE]` symbols, `$USER`/env vars, `~`. Per-host overrides in `[_STORAGE._HOST."<glob>"]`; per-dataset `storage_path` override. Resolution ladder per name: `DATAMANIFEST_<NAME>` env → `[_STORAGE._HOST.<glob>]` → `[_STORAGE]` → default; the two env vars of note are `DATAMANIFEST_DATASETS_DIR`/`DATAMANIFEST_DATACACHE_DIR`. |
| `binding-args` | ✅ | The `{ ref, args, kwargs }` table binding form with `$var` substitution. |
| `byte-identity` | ✅ | Canonical lexicographic key ordering (this implementation is the reference). |
| `cache-produce` | ✅ | `@cached` produce-or-load: parameter-hash keying, optional recipe `version`, `config.toml`/`metadata.toml` sidecars; artifacts land at `<datacache_dir>/<cachetype>/[<version>/]<hash>`. |
| `inspect` | ✅ | The `cached.toml` index and the `datamanifest list` maintenance surface (filter + `--delete`/`--move`, dry-run by default; never an automatic collector). |
| `sync` | ✅ | Cross-machine `push`/`pull` over rsync+ssh, addressed by machine-independent id (a fetched dataset's `key`, or a produced artifact's `cachetype[/version]/hash`); the object lands under the receiver's own `datasets_dir`/`datacache_dir`, resolved best-effort from the remote env (`source ~/.bashrc`) then `[_STORAGE._HOST]`/default; writes no manifest; idempotent. A `$repo`-relative object is not syncable, so the default repo-local folders must be pointed at a machine-global location to sync. |
| `delegation` | ✅ | Cross-language fetch (rung 3): runs a foreign-language fetcher by invoking the local Julia `DataManifest` env (`julia --project=… -e 'using DataManifest; download_dataset(Database("…"), "…")'`) when present, materializing into the shared store; logs a warning and falls through to `uri` when the Julia env is absent. Fetched-only; on by default and probe-gated; the `delegate` field / `--delegate` / `--no-delegate` toggles it. |

## Conformance tests

`tests/test_conformance.py` downloads the pinned shared fixture tarball, verifies every
file against a recorded per-file SHA-256 hash (`tests/conformance_pin.toml`), and runs only
the fixtures whose declared capabilities are a subset of those implemented above, skipping
the rest with a reason.
