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

A deeper, Python-specific walkthrough of the format and behaviours lives in
[`datamanifest-toml.md`](datamanifest-toml.md).

## Targeted format version

This release targets **spec-v3** of the shared format.

## Implemented capabilities

| Capability | Status | Notes |
|---|---|---|
| `lang-read` | ✅ | Parse the `_LANG` namespace and the language-implicit ("bare") forms — a per-dataset `fetcher`/`loader` and the top-level `[_LOADERS]` format→binding map, read as Python (spec-v3.4) — then apply the fetch/load ladders. Explicit `_LANG.python` wins over the bare counterpart; a binding present for the running language (bare or explicit) that fails is an error — fail-loud, no silent fall-through (spec-v3.6). |
| `lang-write` | ✅ | Regenerate `_LANG.python` from explicit bindings only; keep bare `fetcher`/`loader`/`shell` and `[_LOADERS]` bare (never promoted); preserve foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Run the dataset's bare `shell` command template (spec-v3.5 canonical, language-agnostic) in the fetch ladder, else the legacy `_LANG.shell.fetcher` fallback. |
| `storage` | ✅ | Folder variables (`$data`/`$cache`/`$repo` + user-defined), `$`-selectors, `[_STORAGE]` with per-host overrides, content prefixes + scope, `DATAMANIFEST_DIR`, bare roots over `platformdirs`. |
| `binding-args` | ✅ | The `{ ref, args, kwargs }` table binding form with `$var` substitution. |
| `byte-identity` | ✅ | Canonical lexicographic key ordering (this implementation is the reference). |
| `cache-produce` | ✅ | `@cached` produce-or-load: parameter-hash keying, optional recipe `version`, `config.toml`/`metadata.toml` sidecars, `cached/` content prefix + project scope. |
| `inspect` | ✅ | The `cached.toml` index and the `datamanifest list` maintenance surface (filter + `--delete`/`--move`, dry-run by default; never an automatic collector). |
| `sync` | ✅ | Cross-machine `push`/`pull` over rsync+ssh, addressed by machine-independent id (`name`/`alias`/`doi`, or `cachetype[/version]/hash`); remote root resolved best-effort from the remote env (`source ~/.bashrc`) then `[_STORAGE._HOST]`/default; writes no manifest; idempotent. |
| `delegation` | ✅ | Cross-language fetch (rung 3): runs a foreign-language fetcher by invoking the local Julia `DataManifest` env (`julia --project=… -e 'using DataManifest; download_dataset(Database("…"), "…")'`) when present, materializing into the shared store; falls through to `uri` otherwise. Fetched-only; on by default and probe-gated; the `delegate` field / `--delegate` / `--no-delegate` toggles it. |

## Conformance tests

`tests/test_conformance.py` downloads the pinned shared fixture tarball, verifies every
file against a recorded per-file SHA-256 hash (`tests/conformance_pin.toml`), and runs only
the fixtures whose declared capabilities are a subset of those implemented above, skipping
the rest with a reason.
