# Language bindings

A **fetcher** is a function or command that produces a dataset's bytes,
instead of (or in addition to) downloading its `uri`. A **loader** is a
function that reads the dataset from disk into memory, overriding the default
loader for its `format`. Both are declared in the manifest as **bindings**: a
binding names a function by a `module:function` reference (a **ref**, resolved
via `importlib`) — never inline code — and may optionally carry arguments.

The same manifest drives both tools: the Python `datamanifest` package and
Julia's [DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) read one
file, each running only the bindings written for its own language. This page
describes how the Python tool reads bindings; the Julia-side rules and API are
in the
[Julia language-bindings notes](https://github.com/awi-esc/DataManifest.jl/blob/main/docs/language-bindings.md).

This page covers the binding forms from the common case to the advanced ones:
bare bindings for a single-language project, parameterized bindings,
the per-language `_LANG` namespace for manifests shared across languages, the
order in which fetchers and loaders are resolved, cross-language fetch, and
the legacy fields accepted on read. The
[use cases page](use-cases.md#one-manifest-several-languages) shows the short
version.

## Bare bindings (single-language projects)

In a project used from one language, bindings are written directly on the
dataset table — a bare `fetcher` and/or `loader` — and project-wide
`format → loader` defaults go in a top-level `[_LOADERS]` table. "Bare" means
**language-implicit**: the reading tool interprets these as bindings in its
own language (here, Python).

```toml
[_LOADERS]                                # format → loader defaults
csv = "myproject.io:read_csv"
nc  = "myproject.io:read_nc"

[temperature]
uri    = "https://example.com/temperature.csv"
format = "csv"
loader = "myproject.loaders:load_temperature"   # per-dataset loader

[derived]
format  = "nc"
fetcher = "myproject.build:derived"             # per-dataset fetcher (no uri)

[model_output]                                  # language-agnostic shell fetcher
format = "nc"
shell  = "make model_output OUTPUT=$download_path"   # same command for every tool
```

The `shell` field is a separate, **language-agnostic** fetcher: a command
template run as a subprocess, the same command for every tool. It is a fetcher
only — a subprocess cannot return an in-memory object, so there is no shell
loader.

Bare bindings are kept bare on write (never rewritten into the `_LANG`
namespace below), so a hand-authored single-language manifest round-trips
unchanged.

A full, runnable example manifest — bare loaders/fetchers, a parameterized
loader, a `shell` fetcher — lives in the spec's
[examples](https://github.com/perrette/datamanifest.toml/blob/main/examples/datasets.toml).

## Parameterized bindings

A binding may be a `{ ref, args, kwargs }` table instead of a plain string, so
one function is reused across datasets that differ only in arguments:

```toml
[esm_5x5.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]                                     # positional, in order
kwargs = { grid = "5x5", skip_models = ["CESM.*"] }    # keyword

[esm_10x10.loader]
ref    = "myclimate.loaders:load_esm"
args   = ["$path"]
kwargs = { grid = "10x10" }
```

String values in `args` and `kwargs` undergo `$var` substitution before the
call. Available variables: `$download_path` (fetcher), `$path` (loader),
`$key`, `$version`, `$doi`, `$format`, `$branch`, `$uri`, `$project_root`.

The two forms are interchangeable at every binding site — per-dataset
`fetcher`/`loader` (bare or under `_LANG`), and every entry of the
project-wide loaders maps. A bare string `"module:function"` is an alias for
`{ ref = "module:function" }` and makes the conventional call (a loader gets
the dataset path; a fetcher the standard context); with `args`/`kwargs` the
call is explicit — `ref(*args, **kwargs)`, nothing auto-injected. On write, a
binding with no `args`/`kwargs` is the string form, one that carries them is
the table form. (The `shell` field is a command-template string, not a
`module:function` binding, so it is always a string.)

## Per-language bindings (`_LANG`)

A manifest read from more than one language writes its bindings per language,
under a dedicated `_LANG` namespace, so each tool sees only its own:

```toml
[mydata._LANG.python]
fetcher = "mypkg.fetch:download_mydata"
loader  = "mypkg.load:load_mydata"

[mydata._LANG.julia]
fetcher = "MyPkg:fetch_mydata"            # preserved verbatim; Python never touches it

[_LANG.python.loaders]                    # project-wide format → loader defaults
csv = "pandas.io.parsers:read_csv"
nc  = { ref = "myclimate.loaders:load_nc", kwargs = { decode_times = false } }
```

- `[<dataset>._LANG.<lang>]` holds the per-dataset `fetcher` / `loader` for
  language `<lang>` (each a binding in either form).
- `[_LANG.python.loaders]` is the per-language counterpart of `[_LOADERS]`:
  project-wide `format → loader` defaults for Python.

Each language has its own ref flavor; a ref is only ever resolved by the tool
whose `_LANG` subtree it sits in:

=== "Python"

    A ref names an importable function, `"package.module:function"`, resolved
    via `importlib`. The project root is added to `sys.path`, so a module file
    next to the manifest works as well as an installed package.

=== "Julia"

    A ref is `"Module:function"`, resolved by `using Module` followed by a
    function lookup — no `eval` of manifest content. The project root is added
    to the load path, so a module file next to the manifest works as well as a
    package dependency.

Foreign `_LANG.<other>` subtrees (e.g. `_LANG.julia`) are preserved verbatim
on every read→write cycle; Python never modifies them. Unknown structural
tables (any `_*` key Python does not recognize) are likewise passed through.

## Resolution order

At runtime the tool collapses these declarations to one effective fetcher and
one effective loader per dataset, trying each rung in order and using the
first that applies.

**Fetch order** (per dataset):

1. Own Python fetcher — explicit `_LANG.python.fetcher`, else the bare
   `fetcher`, else the legacy `python=`
2. The `shell` command template (else the legacy `_LANG.shell.fetcher`)
3. Cross-language fetch — run a fetcher defined in another language (see
   below)
4. Plain `uri` download
5. Error — no source available

**Load order** (per dataset):

1. Own Python loader — explicit `_LANG.python.loader`, else the bare `loader`
2. Manifest format default — `[_LANG.python.loaders][format]`, else
   `[_LOADERS][format]`
3. Built-in format default (csv, parquet, nc, …)
4. Error

At every own-language rung the explicit `_LANG.python` binding wins over the
bare one. A binding that is **present** for the running language — bare *or*
explicit `_LANG.python` — fails loud: if it does not resolve, that is an
error, and if it resolves and then raises, the error propagates — never a
silent fall-through to a different loader or fetcher. The ladder only skips
rungs that are **absent** (another language's `_LANG.<other>` binding, or no
own loader). A manifest meant for more than one language uses explicit
`[<ds>._LANG.<lang>]` bindings, which are absent — and so correctly skipped —
in the other languages.

## Cross-language fetch

The rare case: a dataset whose only fetcher is defined in another language,
with no own-language fetcher, no shell fetcher, and no `uri`. Running a
foreign fetcher on a dataset's behalf is called **delegation**. Loading never
crosses languages; only bytes on disk do — the delegating tool reads from the
shared store what the peer wrote there.

=== "Python"

    Python materializes a dataset whose only fetcher is
    `[<ds>._LANG.julia].fetcher` by invoking the local Julia `DataManifest`
    environment directly —
    `julia --project=<env> -e 'using DataManifest; download_dataset(Database("<manifest path>"), "<name>")'`.
    The Julia environment is discovered at `$JULIA_PROJECT`, else by walking
    up from the manifest directory for a `Project.toml` whose `[deps]` lists
    `DataManifest`; the rung also requires `julia` on `PATH`. When the
    toolchain is absent or the invocation fails, the rung logs a warning and
    the ladder advances to the `uri` download.

=== "Julia"

    DataManifest.jl delegates the opposite direction: a dataset whose only
    fetcher is `[<ds>._LANG.python].fetcher` is fetched by running
    `datamanifest download <name>` — the Python CLI, when it is on `PATH` —
    with `DATAMANIFEST_TOML` pointing at the same manifest. When the CLI is
    absent, disabled, or fails, the fetch ends in the ordinary "no fetcher"
    error. See the
    [Julia language-bindings notes](https://github.com/awi-esc/DataManifest.jl/blob/main/docs/language-bindings.md).

Cross-language fetch applies to fetched
datasets only (never `@cached` produced datasets); it is on by default and a
no-op unless a foreign fetcher and a usable peer toolchain are both
present. Turn it off per dataset with `delegate = false` in the manifest, or
per run with the `--delegate` / `--no-delegate` flags on
`datamanifest download`.

## Legacy fields

Accepted on read; deprecated:

- **`python=`** (or **`callable=`**) — entry-point reference (`"pkg.mod:func"`)
  equivalent to `[<ds>._LANG.python].fetcher`. The callable receives keyword
  arguments `(download_path, project_root, entry, uri, key, version, doi,
  format, branch, requires_paths)`.
- **`[<ds>._LANG.shell].fetcher`** — read as a fallback for the canonical bare
  `shell` field.
- **`python_includes=`** — list of directory paths prepended to `sys.path`
  during ref resolution (obsolete; the project root is added automatically).

A single manifest can be consumed by several tools: each reads the common
fields and ignores the others' extension keys. The Julia-side API —
`load_dataset`, `download_dataset`, and the rest of DataManifest.jl — is
documented in the [Julia API reference](julia-api.md) on this site. See
[conformance.md](conformance.md) for the shared manifest format and what this
implementation supports.
