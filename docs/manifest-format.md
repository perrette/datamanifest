# Manifest format (`datamanifest.toml`)

The manifest is a TOML file that declares the datasets a project depends on:
where each one comes from, what its content should be, and how to load it. This
page is a practical reference for the Python implementation. The format itself
is shared across languages and defined normatively in the
[`datamanifest.toml` specification](https://perrette.github.io/datamanifest.toml);
see [Conformance](conformance.md) for which parts this implementation supports.

The canonical filename is `datamanifest.toml`; `DataManifest.toml`,
`datasets.toml` and `Datasets.toml` are also recognized, in that order, when
walking up from the working directory.

## A minimal entry

A manifest holds **one table per dataset**, keyed by the dataset name. The
smallest useful entry is a source and a checksum:

```toml
[co2]
uri      = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
checksum = "sha256:…"
```

- `uri` says where the bytes come from.
- `checksum` is a **content digest** — `<algo>:<hex>`, e.g. `sha256:…` or
  `md5:…` — that pins what the bytes should be. You rarely write it by hand:
  when it is empty, the digest is computed (as `sha256:`) on the first
  successful download and written back; on later fetches the download is
  verified against it, in the algorithm it names. A bare hex value with no
  `algo:` prefix is read as `sha256`.

## Common fields

Every field is optional. Types are TOML types; defaults are the empty
string / empty list / `false` unless noted.

| Field | Type | Meaning |
|---|---|---|
| `uri` | string | Single source URI: HTTP(S), `git`/`*.git`, `ssh`/`sshfs`/`rsync`, object store (`s3://`, `gs://`, …), or `file://`. Mutually exclusive with `uris`. |
| `uris` | array of string | Several source URIs materialized into one dataset folder. Mutually exclusive with `uri`. |
| `checksum` | string | Expected content digest as `<algo>:<hex>` (see above). |
| `format` | string | Format hint used to pick a loader (`csv`, `nc`, `parquet`, `json`, `zip`, `tar.gz`, …). Inferred from the URI when absent. |
| `extract` | bool | After download, unpack the archive (`zip` / `tar` / `tar.gz`) and use the extracted directory as the dataset path. |
| `version` | string | Dataset version; participates in the storage key, so multiple versions coexist on disk. |
| `branch` | string | For git sources: the branch or tag to clone. |
| `doi` | string | DOI of the dataset; also usable as a lookup/search key. |
| `aliases` | array of string | Alternative names the dataset can be looked up by. |
| `description` | string | Human-readable description. |
| `requires` | array of string | Names of datasets fetched before this one; defines a dependency graph resolved in topological order. |

`host`, `path`, and `scheme` are **derived** from the URI; the tools omit them
on write.

## Fetchers and loaders

By default a dataset is downloaded from its `uri` and loaded by the built-in
loader for its `format`. Both steps can be overridden:

- a **fetcher** is a function or command that produces the dataset's bytes,
  instead of (or in addition to) downloading the `uri`;
- a **loader** is a function that reads the dataset from disk into memory,
  overriding the format default.

A fetcher or loader is declared as a **binding** — a `module:function`
reference (called a **ref**), never inline code. The common single-language
fields are written directly on the dataset table:

```toml
[regridded]
format  = "nc"
fetcher = "mypkg.build:regrid"      # produce the bytes in Python
loader  = "mypkg.io:open_regridded" # override the default .nc loader
```

A `shell` field instead names a command template run to fetch the data (with
`$download_path`, `$uri`, `$key`, … substitutions); it is language-agnostic
and has no loader counterpart.

A binding may also be a `{ ref, args, kwargs }` table, so one function is
reused across datasets that differ only in arguments, with `$var` substitution
in string values:

```toml
[esm_5x5.loader]
ref    = "mypkg.io:load_esm"
args   = ["$path"]
kwargs = { grid = "5x5" }
```

Bindings can also be written per language under `[<dataset>._LANG.<lang>]`,
with project-wide format defaults under `[_LANG.python.loaders]`. The
[Language bindings](language-bindings.md) page covers the full picture: the
`_LANG` namespace, the loaders maps, and the order in which fetchers and
loaders are resolved.

## Structural tables

Keys beginning with `_` are **structural** — they are not datasets. Unknown
`_*` tables (and any foreign-language `_LANG.*` subtrees) are preserved
verbatim on write, so a manifest shared with another tool round-trips
losslessly. The defined structural tables are:

- **`[_META]`** — schema marker (`schema = 1`).
- **`[_STORAGE]`** — where data lives on disk: two folder fields
  (`datasets_dir` for fetched datasets, `datacache_dir` for the produced
  cache — machine-global by default), read pools, and reusable `$`-symbols.
  This is its own topic — see the [Storage model](storage.md) page.
- **`[_LANG]`** — per-language configuration; in particular
  `[_LANG.python.loaders]`, a project-wide `format → loader` map (see
  [Language bindings](language-bindings.md)).
- **`[_LOADERS]`** — the language-implicit counterpart of
  `[_LANG.python.loaders]`: a bare `format → binding` map read as the running
  tool's own language.

## Rare fields

| Field | Type | Meaning |
|---|---|---|
| `skip_download` | bool | Treat the dataset as externally provided — return the `uri`/path as-is, attempt no download, and never let maintenance touch it. |
| `skip_checksum` | bool | Disable checksum verification for this dataset. |
| `lazy_access` | bool | **Lazy access**: open the `uri` in place (typically an object store) instead of downloading a local copy — no local bytes, no checksum. Requires a loader; in Python, `datamanifest add --lazy` sets it up with the built-in fsspec loader. |
| `storage_path` | string | Per-dataset location override (a path expression; default `$datasets_dir/$key`). See [Storage model](storage.md). |
| `key` | string | The dataset's **storage key** — its relative path under the datasets folder. Derived from the URI's host and path (plus `version`) when absent. |
| `delegate` | bool | Allow **delegation** — cross-language fetch through a peer `datamanifest` implementation (see [Language bindings](language-bindings.md#cross-language-fetch)). Defaults to `true`; only `delegate = false` is written. |
| `sha256` | string | Legacy alias for `checksum`: a bare SHA-256 hex, read as `checksum = "sha256:<hex>"` and written back as `checksum` on the next save. |

## How the file is written

Manifests are written in a canonical form: structural `_*` tables first at the
top level, then the datasets — both alphabetical, with keys sorted at every
nesting level. `datamanifest format [FILE]` re-serializes any manifest into
that canonical byte form without changing its content, so different tools can
emit byte-identical files.

## A full example

The manifest below declares four real datasets fetched from PANGAEA, a CMIP6
output over SSH, and a GitHub release archive:

```toml
[CMIP6_lgm_tos]
uri      = "ssh://albedo1.dmawi.de:/albedo/work/projects/p_forclima/preproc_data_esmvaltool/LGM/recipe_cmip6_lgm_tos_20241114_151009/preproc/lgm/tos_CLIM"
checksum = "sha256:7f28454b5c399226f923be60393ecbb2983c15538f9733864e5ce0f9f4b83601"

[herzschuh2023]
uri      = "https://doi.pangaea.de/10.1594/PANGAEA.930512?format=zip"
format   = "zip"
checksum = "sha256:4e40e43ac0f1ddea125cb5314eee46e332aacbcb18aff7efbf59f1d8b1d84a13"
doi      = "10.1594/PANGAEA.930512"
extract  = true

[jonkers2024]
uri      = "https://download.pangaea.de/dataset/962852/files/LGM_foraminifera_assemblages_20240110.csv"
checksum = "sha256:35ee6e8b94dc355973276da609fb18846ee1156d0fa848c1a9b221edd3715513"
doi      = "10.1594/PANGAEA.962852"

["jesstierney/lgmDA"]
uri      = "https://github.com/jesstierney/lgmDA/archive/refs/tags/v2.1.zip"
checksum = "sha256:da5f85235baf7f858f1b52ed73405f5d4ed28a8f6da92e16070f86b724d8bb25"
extract  = true
```

## The normative specification

This page is a practical summary, not the contract. The authoritative,
cross-language definition — the full field list, the resolution order, the
preservation rules, and the conformance fixtures — is the
[`datamanifest.toml` specification](https://perrette.github.io/datamanifest.toml).
