# Using it from your code

Where the [CLI](cli.md) *manages* a project's data, the Python API *consumes*
it: your analysis code resolves and loads what the manifest declares, and never
edits it.

```python
import datamanifest

df = datamanifest.load_dataset("co2")          # download on first use, then load
                                               # (pandas/xarray/… per format)
path = datamanifest.get_dataset_path("co2")    # just the on-disk path
```

`load_dataset` downloads on first use, verifies the checksum, then returns the
loaded object using the backend for the dataset's format (install the matching
[extra](installation.md)). `get_dataset_path` stops at the on-disk path, for
when you want to open the file yourself.

## Caching computed results

Cache an expensive computation, keyed by its keyword arguments:

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

Each distinct keyword combination is stored separately. The result is saved
with `pickle` by default; pass `format="nc"`/`"csv"`/… to pick a serialization,
and `version="v2"` to invalidate when the function's *logic* changes.
`datamanifest list` shows cached results grouped by function with their
parameters; `datamanifest list --orphan --delete` cleans up.

The `@cached` cache shares the same storage and bookkeeping as fetched data — it
lands under `datacache_dir` (`./cached/` by default) and shows up in `list`
alongside your datasets. The [design notes](design-notes.md) cover how an
artifact's identity (`cachetype`, `version`, parameter hash) is derived.

## Finding the manifest, and targeting a specific database

The module-level functions find the project's manifest automatically (walking
up from the working directory; `DATAMANIFEST_TOML` overrides). To use a specific
database instead, either call the function with `db=`
(`datamanifest.download_dataset("co2", db=mydb)`) or use the database's own
methods (`mydb.download_dataset("co2")`). Every `datamanifest.X(...)` is just
`resolve_db(db).X(...)` — the method on `db`, or on the default database when
`db` is None. See the docstrings (`help(datamanifest)`) and the
[design notes](design-notes.md).

## A file-less database (no manifest)

For library code that wants checksummed downloads into a folder it controls —
an OS-appropriate data dir, say — a **file-less database** skips the manifest
entirely: no `datamanifest.toml`, no state file, nothing written but the data.
The folder accepts the same `$`-symbols as the [storage model](storage.md), and
the database's methods do everything the module-level functions do:

```python
from datamanifest import Database

db = Database(datasets_folder="$user_data_dir/mylib", persist=False)
db.add("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
path = db.download_dataset("co2")   # → ~/.local/share/mylib/gml.noaa.gov/…/co2_annmean_mlo.csv
```
