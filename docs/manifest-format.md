# Manifest format (`datamanifest.toml`)

This page describes the structure of the manifest file — the fields you write by
hand or that the CLI writes for you. It is a practical reference for this Python
implementation. The format is shared across languages and defined normatively in
its own specification: the
[`datamanifest.toml` spec](https://perrette.github.io/datamanifest.toml). For the
cross-tool details — which features this implementation supports and which spec
version it targets — see [Conformance](conformance.md).

The canonical filename is `datamanifest.toml`; `datasets.toml` and
`Datasets.toml` are also recognized when walking up from the working directory.

## Top-level layout

A manifest is a TOML document containing:

- **one table per dataset**, keyed by the dataset name — the common case;
- **`[_LANG.python.loaders]`** — an optional `format → loader` map of project-wide
  default loaders (see [Language bindings](language-bindings.md));
- **`[_STORAGE]`** — optional configuration of where data lives on disk (see
  [Storage model](storage.md));
- **`[_META]`** — optional schema marker (`schema = 1`).

Keys beginning with `_` are **structural** — they are not datasets. Unknown `_*`
tables (and any foreign-language `_LANG.*` subtrees) are preserved verbatim on
write, so a manifest shared with another tool round-trips losslessly.

A minimal manifest is just dataset tables:

```toml
[co2]
uri    = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
sha256 = "…"
format = "csv"
```

## Dataset fields

Every field is optional. Types are TOML types; defaults are the empty
string / empty list / `false` unless noted.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `uri` | string | `""` | Single source URI: HTTP(S), `git`/`*.git`, `ssh`/`sshfs`/`rsync`, object store (`s3://`, `gs://`, …), or `file://`. Mutually exclusive with `uris`. |
| `uris` | array of string | `[]` | Several source URIs materialized into one dataset folder. Mutually exclusive with `uri`. |
| `sha256` | string | `""` | Expected SHA-256 of the download. Auto-filled on first successful fetch and verified at fetch time. |
| `format` | string | `""` | Format hint used to pick a loader (`csv`, `nc`, `parquet`, `json`, `zip`, `tar.gz`, …). Inferred from the URI when absent. |
| `version` | string | `""` | Dataset version; participates in the storage key, so multiple versions coexist on disk. |
| `branch` | string | `""` | For git sources: the branch or tag to clone. |
| `doi` | string | `""` | DOI of the dataset; also usable as a lookup/search key. |
| `extract` | bool | `false` | After download, unpack the archive (`zip` / `tar` / `tar.gz`) and use the extracted directory as the dataset path. |
| `aliases` | array of string | `[]` | Alternative names the dataset can be looked up by. |
| `description` | string | `""` | Human-readable description. |
| `requires` | array of string | `[]` | Names of datasets fetched before this one; defines a dependency graph resolved in topological order. |
| `skip_download` | bool | `false` | Treat the dataset as externally provided — return the `uri`/path and attempt no download. |
| `skip_checksum` | bool | `false` | Disable checksum verification for this dataset. |
| `storage_path` | string | `""` | Per-dataset location override (a path expression; default `$datasets_dir/$key`). See [Storage model](storage.md). |
| `delegate` | bool | `true` | Allow cross-language fetch delegation to a peer `datamanifest` CLI. |

`host`, `path`, and `scheme` are **derived** from the URI; the tools omit them on
write.

## Language bindings

By default a dataset is downloaded from its `uri` and loaded by the built-in
loader for its `format`. Both steps can be overridden with `module:function`
references (never inline code):

- **bare `fetcher`** — a Python `module:function` called to produce the bytes
  instead of (or in addition to) downloading the `uri`;
- **bare `loader`** — a Python `module:function` called to load the dataset into
  memory, overriding the format default;
- **bare `shell`** — a command template run to fetch the data (with `$download_path`,
  `$uri`, `$key`, … substitutions); language-agnostic, no loader.

```toml
[regridded]
format  = "nc"
fetcher = "mypkg.build:regrid"      # produce the bytes in Python
loader  = "mypkg.io:open_regridded" # override the default .nc loader
```

A binding may instead be a `{ ref, args, kwargs }` table so one function is reused
across datasets that differ only in arguments, with `$var` substitution in string
values:

```toml
[esm_5x5.loader]
ref    = "mypkg.io:load_esm"
args   = ["$path"]
kwargs = { grid = "5x5" }
```

Bindings can also be written per-language under `[<dataset>._LANG.<lang>]`, and
project-wide format defaults under `[_LANG.python.loaders]`. The bare forms above
are the common single-language case; the
[Language bindings](language-bindings.md) page covers the full `_LANG` namespace,
the loaders map, and the fetch/load resolution ladders.

## Storage

Where data lives is configured in `[_STORAGE]` (two folder fields,
`datasets_dir` and `datacache_dir`, both repo-local by default) and per dataset
via `storage_path`. This is its own topic — see the
[Storage model](storage.md) page.

## A full example

The manifest below declares four real datasets fetched from PANGAEA, a CMIP6
output over SSH, and a GitHub release archive:

```toml
[CMIP6_lgm_tos]
uri    = "ssh://albedo1.dmawi.de:/albedo/work/projects/p_forclima/preproc_data_esmvaltool/LGM/recipe_cmip6_lgm_tos_20241114_151009/preproc/lgm/tos_CLIM"
sha256 = "7f28454b5c399226f923be60393ecbb2983c15538f9733864e5ce0f9f4b83601"

[herzschuh2023]
uri     = "https://doi.pangaea.de/10.1594/PANGAEA.930512?format=zip"
format  = "zip"
sha256  = "4e40e43ac0f1ddea125cb5314eee46e332aacbcb18aff7efbf59f1d8b1d84a13"
doi     = "10.1594/PANGAEA.930512"
extract = true

[jonkers2024]
uri    = "https://download.pangaea.de/dataset/962852/files/LGM_foraminifera_assemblages_20240110.csv"
sha256 = "35ee6e8b94dc355973276da609fb18846ee1156d0fa848c1a9b221edd3715513"
doi    = "10.1594/PANGAEA.962852"

["jesstierney/lgmDA"]
uri     = "https://github.com/jesstierney/lgmDA/archive/refs/tags/v2.1.zip"
sha256  = "da5f85235baf7f858f1b52ed73405f5d4ed28a8f6da92e16070f86b724d8bb25"
extract = true
```

## The normative specification

This page is a practical summary, not the contract. The authoritative,
cross-language definition — the full field list, the resolution ladders, the
preservation rules, and the conformance fixtures — lives in the
[`datamanifest.toml` specification](https://perrette.github.io/datamanifest.toml).
