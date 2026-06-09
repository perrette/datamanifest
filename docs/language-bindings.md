# Per-language bindings (`_LANG`)

Language-specific bindings live in a dedicated `_LANG` namespace, so a single
manifest can serve multiple language implementations without conflicts. The
[use cases page](use-cases.md#one-manifest-several-languages) shows the short
version; this page is the full behaviour: the fetch/load ladders, the bare
(language-implicit) forms, parameterized bindings, cross-language fetch, and
the legacy fields still accepted on read.

```toml
[mydata._LANG.python]
fetcher = "mypkg.fetch:download_mydata"   # entry-point ref; resolved via importlib
loader  = "mypkg.load:load_mydata"

[_LANG.python.loaders]                    # project-wide format → loader defaults
csv = "pandas.io.parsers:read_csv"        # string form (a bare module:function ref)
nc  = { ref = "myclimate.loaders:load_nc", kwargs = { decode_times = false } }  # table form

[mydata._LANG.julia]
fetcher = "MyPkg.fetch_mydata"            # preserved verbatim; Python never touches it
```

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim on
every read→write cycle; Python never modifies them. Unknown structural tables
(any `_*` key Python does not recognise) are similarly passed through.

## The ladders

**Fetch ladder** (per dataset, in order):

1. Own Python fetcher — explicit `_LANG.python.fetcher`, else the bare
   `fetcher`, else legacy `python=`
2. Bare `shell` command template (else legacy `_LANG.shell.fetcher`)
3. Cross-language fetch — run a fetcher defined in another language
4. Plain `uri` download
5. Error — no source available

**Load ladder** (per dataset, in order):

1. Own Python loader — explicit `_LANG.python.loader`, else the bare `loader`
2. Manifest format default — `[_LANG.python.loaders][format]`, else the bare
   `[_LOADERS][format]` map
3. Built-in format default (csv, parquet, nc, …)
4. Error

At every own-language rung the explicit `_LANG.python` binding wins over the
bare one. A binding that is **present** for the running language — bare *or*
explicit `_LANG.python` — is **fail-loud**: if it fails to resolve it is an
error, and if it resolves and then raises the error propagates — never a silent
fall-through to a different loader/fetcher. The ladder falls through only to
skip rungs that are **absent** (another language's `_LANG.<other>` binding, or
no own loader). A manifest meant for more than one language uses explicit
`[<ds>._LANG.<lang>]` bindings (absent, and so correctly skipped, in the
others).

## Language-implicit (bare) bindings

For a single-language project the `[<ds>._LANG.<lang>]` wrapper is needless
ceremony. A dataset may instead carry a **bare** `fetcher`/`loader` directly,
and a top-level `[_LOADERS]` table may carry a bare `format → binding` map — all
read as bindings in the running tool's **own language** (here, Python):

```toml
[_LOADERS]                                # language-implicit format → loader defaults
csv = "myproject.io:read_csv"
nc  = "myproject.io:read_nc"

[temperature]
uri    = "https://example.com/temperature.csv"
format = "csv"
loader = "myproject.loaders:load_temperature"   # bare per-dataset loader

[derived]
format  = "nc"
fetcher = "myproject.build:derived"             # bare per-dataset fetcher (no uri)

[model_output]                                  # bare, language-agnostic shell fetcher
format = "nc"
shell  = "make model_output OUTPUT=$download_path"   # same command for every tool
```

The bare `shell` field is the **canonical, language-agnostic** shell fetcher
(the same command for every tool — not a `_LANG` tag); the legacy
`[<ds>._LANG.shell].fetcher` is still read and preserved as the fallback. Bare
bindings are kept **bare** on write (never promoted into `_LANG.python`), so a
hand-authored single-language manifest round-trips unchanged.

A full, runnable example manifest — bare loaders/fetchers, a parameterized
loader, the bare `shell` fetcher — lives in the spec's
[examples](https://perrette.github.io/datamanifest.toml).

## Parameterized bindings

A binding (a `fetcher`, a `loader`, or an entry in the `[_LANG.python.loaders]`
map) may be a `{ ref, args, kwargs }` table instead of a plain string, so one
entry-point can be reused across datasets that differ only in arguments:

```toml
[esm_5x5._LANG.python.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]                                     # positional, in order
kwargs = { grid = "5x5", skip_models = ["CESM.*"] }    # keyword

[esm_10x10._LANG.python.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]
kwargs = { grid = "10x10" }
```

String values in `args` and `kwargs` undergo `$var` substitution before the
call. Available variables: `$download_path` (fetcher), `$path` (loader),
`$key`, `$version`, `$doi`, `$format`, `$branch`, `$uri`, `$project_root`.

The two forms are interchangeable at **every** binding site — explicit
`[<ds>._LANG.python]` `fetcher`/`loader`, the language-implicit bare
`fetcher`/`loader`, and the project-wide `[_LANG.python.loaders]` / bare
`[_LOADERS]` defaults. (The `shell` field is a separate command-template
string, not a `module:function` binding, so it is always a string, never a
table.) A bare string `"module:function"` is the alias for
`{ ref = "module:function" }` and makes the conventional call (a loader gets
the dataset path; a fetcher the standard context). Canonical writing: a binding
with no `args`/`kwargs` is written as the **string**, one that carries them as
the **table**.

## Cross-language fetch

The rare case: a dataset whose only fetcher is defined in another language
(e.g. `[<ds>._LANG.julia].fetcher`), with no native Python fetcher, no shell
fetcher, and no `uri`. Python materializes it by invoking the **local Julia
`DataManifest` environment** directly —
`julia --project=<env> -e 'using DataManifest; download_dataset(Database("<datasets.toml>"), "<name>")'`
— which writes the bytes into the shared store; Python then reads them from
disk (load never crosses languages, only bytes do).

The Julia env is discovered by walking up from the manifest directory (or
`$JULIA_PROJECT`) for a `Project.toml` whose `[deps]` lists `DataManifest`, and
the rung is gated on `julia` being on `PATH`. When the toolchain is absent the
rung **logs a warning and skips**, and the ladder advances to the `uri`
download. Cross-language fetch applies to fetched datasets only (never
`@cached` produced datasets); it is **on by default** and probe-gated (a no-op
unless a foreign fetcher and a usable Julia env are both present). Toggle it
per file with `delegate = false`, or per run with the `--delegate` /
`--no-delegate` flags on `datamanifest download`.

## Legacy fields

Still accepted on read; only these are deprecated:

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`)
  resolved via `importlib`. The callable receives keyword arguments
  `(download_path, project_root, entry, uri, key, version, doi, format, branch,
  requires_paths)`. No inline code execution (`exec`/`eval`) anywhere.
  Equivalent to `[<ds>._LANG.python].fetcher`.
- **`[<ds>._LANG.shell].fetcher`** — the legacy shell fetcher; read as the
  fallback for the canonical bare `shell`.
- **`python_includes=`** — list of directory paths prepended to `sys.path`
  during ref resolution (obsolete; the project root is auto-added).

A single manifest can be consumed by several tools: each reads the common
fields and ignores the other's extension keys. See
[conformance.md](conformance.md) for the shared manifest format and what this
implementation supports.
