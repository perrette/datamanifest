# Use cases

A tour of day-to-day use. Most of it is CLI commands; where the Python or
Julia API has an equivalent, it is shown in a tab. Each section links to the
reference page with the full flag set; the [CLI reference](cli.md) has them
all in one place.

## Manage datasets

=== "CLI"

    ```bash
    datamanifest add https://host/path/file.nc                     # a direct URL
    datamanifest add 10.5281/zenodo.1234567 --pick "*.csv"         # a Zenodo record's files
    datamanifest add "https://github.com/u/repo/archive/v2.1.zip" --extract
    datamanifest add s3://bucket/key.zarr --lazy                   # open in place, no download

    datamanifest list                       # one styled line each, clickable locations
    datamanifest show co2                   # full entry detail
    datamanifest remove old_entry           # drop an entry

    datamanifest verify                     # re-check all checksums (e.g. before submission)
    datamanifest update-checksums           # recompute them after regenerating data

    python analysis.py --data "$(datamanifest path co2)"   # composable in shell
    ```

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")

    db.add("https://host/path/file.nc")     # register in the manifest + download
    db.download_datasets()                  # fetch everything not yet present

    data = db.load_dataset("co2")           # download if needed, then load
    path = db.get_dataset_path("co2")       # just the on-disk path
    db.delete_dataset("old_entry")          # drop an entry
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")

    add(db, "https://host/path/file.nc")    # register in the manifest + download
    download_datasets(db)                   # fetch everything not yet present

    data = load_dataset(db, "co2")          # download if needed, then load
    path = get_dataset_path(db, "co2")      # just the on-disk path
    delete_dataset(db, "old_entry")         # drop an entry
    ```

The CLI-only commands (`list`, `show`, `verify`, `update-checksums`, …) manage
the project's data; the libraries consume it from code (see the
[CLI / library split](quickstart.md#the-cli-library-split)).

A concrete run — continuing from the [quickstart](quickstart.md)'s CO₂ record,
add the HadCRUT5 global temperature series next to it:

```console
$ datamanifest add "https://www.metoffice.gov.uk/hadobs/hadcrut5/data/HadCRUT.5.0.2.0/analysis/diagnostics/HadCRUT.5.0.2.0.analysis.summary_series.global.annual.csv" --name temperature
$ datamanifest list
Datasets
● co2          csv         3.1 KiB  …webdata/ccgg/trends/co2/co2_annmean_mlo.csv
● temperature  csv         6.9 KiB  …0.analysis.summary_series.global.annual.csv

Cached
◆ myproj.load_anomaly  pickle  2×  768 B
    40384c4db019  grid=10x10                                         386 B
    50f04896d3ee  grid=5x5                                           382 B
```

`temperature` loads from code just like `co2` —
`db.load_dataset("temperature")` — and the **Cached** group lists the
`load_anomaly(grid=…)` results from the [`@cached` example](api.md), grouped by
function with their parameters.

## Repair: reassociate data on disk

The tool records where every file actually lives in a small git-ignored
[state file](storage.md#the-state-file) (`.datamanifest/state.toml`), so moving
data around by hand is recoverable — `refresh` reconciles the records with disk, and `--scan`
discovers copies elsewhere on the machine (e.g. downloaded by another project)
and adopts them, checksum-verified, instead of re-downloading:

```bash
datamanifest list --dirty       # preview: records that disagree with disk
datamanifest refresh            # repoint moved files, drop deleted, adopt untracked
datamanifest refresh --scan     # also discover & adopt copies found elsewhere
datamanifest refresh --scan --datasets-pools ~/other-project/datasets /shared/data \
                            --datacache-pools /shared/cache   # extend the scan to extra folders
```

`refresh` only edits local state — never your data, never the manifest. To act
on the bytes themselves, filter with `list` and apply an action flag. Each flag
runs the matching standalone command (`delete` / `move` / `push` / `pull`) over
the selection and **forwards the rest of the line to that command's own
options** — filters first, then the action flag and its tail (`--dry-run`
previews):

```bash
datamanifest list --cached --orphan --delete                 # clean up orphaned cached artifacts
datamanifest list --older-than 30d --delete --dry-run        # preview; --dry-run goes to delete
datamanifest list --datasets stale --delete --prune          # also drop the manifest entry
datamanifest list --older-than 90d --move /archive --dry-run # DEST then options
```

## Put data where you want it

Storage is two folders — `datasets_dir` (fetched data, default: a machine-wide
shared store deduplicated by dataset key) and `datacache_dir` (`@cached`
results, default: a per-project folder under your OS cache dir).
`datamanifest config` edits them at the scope you want — this checkout (the
default, git-ignored), this user, or the committed project:

```bash
datamanifest config set datasets_dir "/scratch/$USER/data"                 # this checkout
datamanifest config set datacache_dir "$user_cache_dir/myproj" --project   # committed default
datamanifest config                                                        # show resolved config
```

`config set` writes plain TOML files that can also be edited by hand; every
implementation (including DataManifest.jl) reads the same files — see
[Configuration](configuration.md).

Pointing the folders at a machine directory (instead of the repo) shares data
across clones and projects. Path expressions, per-host rules, per-dataset
overrides and read pools (extra read-only folders searched for existing copies
before downloading): the [storage model](storage.md).

## Sync between machines

Move a stored object between machines instead of re-downloading or recomputing
it. Objects are addressed machine-independently — a dataset by name, a cached
artifact by `function/hash` — and land in the receiver's own folders:

```bash
datamanifest push foo user@hpc             # copy dataset `foo` to the host (rsync over ssh)
datamanifest pull esm_anomaly/83425a3 hpc  # pull a cached artifact by hash prefix
datamanifest push foo user@hpc --dry-run   # preview resolved paths + size
datamanifest list --cached --push user@hpc # bulk: push a filtered selection
```

Sync is bytes-only and idempotent; it needs the data folders to be
machine-global (not repo-local) on both ends. Details:
[CLI reference → Sync between machines](cli.md#sync-between-machines); the
[Moving data](moving-data.md) guide covers the whole topic, local moves
included.

## One manifest, several languages

A dataset can carry per-language bindings under `_LANG` — a *fetcher* (how to
retrieve the data) and a *loader* (how to turn the file into an in-memory
object). Each implementation runs its own bindings and preserves the others
verbatim, so one manifest serves a mixed Python/Julia project:

```toml
[mydata]
uri = "https://example.com/mydata.csv"

[mydata._LANG.python]
loader = "mypkg.load:load_mydata"      # how Python loads it

[mydata._LANG.julia]
loader = "MyPkg:load_mydata"           # Julia's binding; Python never touches it
```

A single-language project can write bare `fetcher` / `loader` / `shell` fields
instead of `_LANG` tables, and `[_LOADERS]` maps formats to project-wide
loaders. How a binding is resolved, parameterized bindings
(`{ ref, args, kwargs }`), and fetching through another language's toolchain:
[language bindings](language-bindings.md).
