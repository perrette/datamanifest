# Using it from your code

Where the [CLI](cli.md) *manages* a project's data, the in-code API *consumes*
it: your analysis code resolves and loads what the manifest declares, and never
edits it. This page is the narrative guide; the complete list of functions and
classes is in the [Python API reference](python-api.md). The Julia tabs show
the equivalent calls in [DataManifest.jl](https://awi-esc.github.io/DataManifest.jl/),
which reads the same manifest.

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")

    df = db.load_dataset("co2")          # download on first use, then load
                                         # (pandas/xarray/… per format)
    path = db.get_dataset_path("co2")    # just the on-disk path
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")

    df = load_dataset(db, "co2")         # download on first use, then load
    path = get_dataset_path(db, "co2")   # just the on-disk path
    ```

`load_dataset` downloads on first use, verifies the checksum, then returns the
loaded object using the backend for the dataset's format (install the matching
[extra](installation.md)). `get_dataset_path` stops at the on-disk path, for
when you want to open the file yourself.

## Caching computed results

Cache an expensive computation, keyed by its keyword arguments:

=== "Python"

    ```python
    from datamanifest.cache import cached

    @cached
    def load_anomaly(*, grid="5x5"):
        ...        # expensive; returns e.g. an xarray.Dataset
        return ds

    ds = load_anomaly(grid="5x5")                # first call: computes and stores
    ds = load_anomaly(grid="5x5")                # later calls: loads and returns
    ds = load_anomaly(grid="5x5", cached=False)  # force recompute
    ```

=== "Julia"

    ```julia
    using DataManifest

    @cached key=(a -> (; a.grid,)) function load_anomaly(; grid::String = "5x5")
        # … expensive computation …
        return ds
    end

    ds = load_anomaly(grid="5x5")                # first call: computes and stores
    ds = load_anomaly(grid="5x5")                # later calls: loads and returns
    ds = load_anomaly(grid="5x5", cached=false)  # run the body, no disk I/O
    ```

    Julia's `@cached` takes the cache key explicitly (`key=` maps the keyword
    arguments to the parameters that identify the result) and saves with the
    stdlib `Serialization` (`jls`) by default — see the
    [Julia caching page](https://awi-esc.github.io/DataManifest.jl/caching/).

Each distinct keyword combination is stored separately. The result is saved
with `pickle` by default; pass `format="nc"`/`"csv"`/… to pick a serialization,
and `version="v2"` to invalidate when the function's *logic* changes.
`datamanifest list` shows cached results grouped by function with their
parameters; `datamanifest list --orphan --delete` cleans up.

The `@cached` cache shares the same storage and bookkeeping as fetched data — it
lands under [`datacache_dir`](storage.md) (default:
`$user_cache_dir/datamanifest/projects/$project/cached`) and shows up in `list`
alongside your datasets. The [design notes](https://github.com/perrette/datamanifest/blob/main/design/design-notes.md) cover how an
artifact's identity (`cachetype`, `version`, parameter hash) is derived.

## The `Database` object, and the module-level shortcuts

The recommended style is to load the database once and call its methods:

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")

    df = db.load_dataset("co2")
    path = db.get_dataset_path("co2")
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")

    df = load_dataset(db, "co2")
    path = get_dataset_path(db, "co2")
    ```

This is explicit about which project's manifest the code uses, lets several
databases coexist in one program, and pins the configuration: a `Database`
takes its [configuration snapshot](configuration.md) — config files,
environment, host — once, when it is created.

The module-level functions are shortcuts over a **default database**. On first
use they locate the project's manifest — walking up from the working directory
for the canonical `datamanifest.toml` or one of the alternate names
(`DataManifest.toml`, `datasets.toml`, `Datasets.toml`); `DATAMANIFEST_TOML`
overrides — build the default `Database` from it, and keep it for the rest of
the process — the manifest is read once, not on every call. A no-argument
`Database()` runs the same discovery, so you can hold an explicit `db` without
naming the file. Every `datamanifest.X(...)` is the method `X` on that default
database, or on the database you pass explicitly — `add` included, which
registers **and downloads** either way:

=== "Python"

    ```python
    datamanifest.download_dataset("co2")            # the auto-discovered default
    datamanifest.download_dataset("co2", db=mydb)   # a specific database
    ```

=== "Julia"

    ```julia
    download_dataset("co2")          # the active project's manifest
    download_dataset(mydb, "co2")    # a specific database
    ```

The rest of the surface — registering and deleting datasets, downloading in
bulk, validating loader bindings — is in the
[Python API reference](python-api.md); the
[design notes](https://github.com/perrette/datamanifest/blob/main/design/design-notes.md)
cover the rationale.

## A file-less database (no manifest)

For library code that wants checksummed downloads into a folder it controls —
an OS-appropriate data dir, say — a **file-less database** skips the manifest
entirely: no `datamanifest.toml`, no state file, nothing written but the data.
The folder accepts the same `$`-symbols as the [storage model](storage.md), and
the database's methods do everything the module-level functions do:

=== "Python"

    ```python
    from datamanifest import Database

    db = Database(datasets_folder="$user_data_dir/mylib", persist=False)
    db.add("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
    path = db.get_dataset_path("co2")   # → ~/.local/share/mylib/gml.noaa.gov/…/co2_annmean_mlo.csv
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = Database(datasets_folder=raw"$user_data_dir/mylib", persist=false)   # raw"": keep Julia
                                                                              # from interpolating $
    add(db, "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"; name="co2")
    path = get_dataset_path(db, "co2")  # → ~/.local/share/mylib/gml.noaa.gov/…/co2_annmean_mlo.csv
    ```
