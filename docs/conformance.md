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
| `lang-read` | ✅ | Parse the `_LANG` namespace on read; apply the load ladder. |
| `lang-write` | ✅ | Regenerate `_LANG.python`; preserve foreign `_LANG.*` and unknown `_*` tables verbatim (lossless round-trip). |
| `shell-fetch` | ✅ | Run the `_LANG.shell.fetcher` command template in the fetch ladder. |
| `storage` | ✅ | Folder variables (`$data`/`$cache`/`$repo` + user-defined), `$`-selectors, `[_STORAGE]` with per-host overrides, content prefixes + scope, `DATAMANIFEST_DIR`, bare roots over `platformdirs`. |
| `binding-args` | ✅ | The `{ ref, args, kwargs }` table binding form with `$var` substitution. |
| `byte-identity` | ✅ | Canonical lexicographic key ordering (this implementation is the reference). |
| `cache-produce` | ✅ | `@cached` produce-or-load: parameter-hash keying, optional recipe `version`, `config.toml`/`metadata.toml` sidecars, `cached/` content prefix + project scope. |
| `inspect` | ✅ | The `cached.toml` index and the `datamanifest list` maintenance surface (filter + `--delete`/`--move`, dry-run by default; never an automatic collector). |
| `sync` | ✅ | Cross-machine `push`/`pull` over rsync+ssh, addressed by machine-independent id (`name`/`alias`/`doi`, or `cachetype[/version]/hash`); remote root resolved best-effort from the remote env (`source ~/.bashrc`) then `[_STORAGE._HOST]`/default; writes no manifest; idempotent. |
| `delegation` | ⬜ not yet | Run a fetcher defined in another language (peer-CLI / cross-language fetch). |

## Conformance tests

`tests/test_conformance.py` downloads the pinned shared fixture tarball, verifies every
file against a recorded per-file SHA-256 hash (`tests/conformance_pin.toml`), and runs only
the fixtures whose declared capabilities are a subset of those implemented above, skipping
the rest with a reason.
