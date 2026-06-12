# Python API reference

A hand-curated reference of the public `datamanifest` surface. For a guided
tour with worked examples, read [Using it from code](api.md) first.

Most functions exist in two equivalent forms: as a method on a
[`Database`](#the-database-class) (`db.load_dataset(...)`) and as a
module-level shortcut (`datamanifest.load_dataset(...)`). The module-level
form takes an extra keyword-only `db=None` argument and operates on the
[default database](#resolve_db-get_default_database) when it is omitted.

## Everyday functions

These are the calls most scripts need. Each is shown with its `Database`
method signature; the module-level twin accepts the same arguments plus
`db=`.

### `add`

```python
db.add(uri="", name="", skip_download=False, **kwargs) -> (name, entry)
```

Register a dataset in the manifest **and download it** in one step — the same
path the CLI's `add` command uses. Pass `skip_download=True` to register
only. Entries that are never fetched (`lazy_access=True`, or a
`skip_download=true` entry field) are registered and left in place. Extra
keyword arguments become [`DatasetEntry`](#datasetentry) fields. See
[Adding datasets](adding-datasets.md) for what `uri` can be.

### `load_dataset`

```python
db.load_dataset(name, loader=None, **kwargs)
```

Download a dataset (if not already present) and load it into memory,
returning the loaded value. `loader` is a callable (`path -> value`), a
loader name declared in the manifest, or a built-in format name (`csv`,
`parquet`, `nc`, `json`, `yaml`, `toml`, ...); when omitted, the loader is
resolved from the entry's bindings and `format`. See
[Using it from code](api.md).

### `get_dataset_path`

```python
db.get_dataset_path(name, extract=None, ...) -> str
```

Resolve a dataset's on-disk path without downloading anything. The location
follows the entry's `storage_path` expression (default
`$datasets_dir/$key`); `skip_download` and `lazy_access` entries return
their `uri` directly. How paths are resolved is described in the
[storage model](storage.md).

### `download_dataset` / `download_datasets`

```python
db.download_dataset(name, extract=None, overwrite=False) -> str
db.download_datasets(names=None, **kwargs)
```

Fetch one dataset (returning its local path) or several — all of them when
`names` is `None`. Dependencies declared via `requires=` are downloaded
first; an existing verified copy (including one found in a
[read pool](storage.md#read-pools)) is reused instead of re-downloaded.
`overwrite=True` forces a fresh fetch.

### `register_dataset`

```python
db.register_dataset(uri="", name="", overwrite=False, persist=True,
                    check_duplicate=True, uris=None, **kwargs) -> (name, entry)
```

Add an entry to the manifest **without downloading**. When `name` is empty
it is derived from the URI. Registering an identical entry twice is a no-op;
a conflicting one raises unless `overwrite=True`. With `persist=True` (the
default) the manifest file is rewritten immediately. Extra keyword arguments
become [`DatasetEntry`](#datasetentry) fields.

### `delete_dataset`

```python
db.delete_dataset(name, keep_cache=False, persist=True)
```

Remove a dataset entry from the manifest and, unless `keep_cache=True`,
delete its downloaded files and state record as well.

## Default database resolution

### `resolve_db` / `get_default_database`

```python
datamanifest.resolve_db(db=None) -> Database
datamanifest.get_default_database() -> Database
```

`resolve_db` returns `db` if given, else the process-wide default database —
which `get_default_database` creates lazily on first use and then reuses.
The default manifest is located from the `DATAMANIFEST_TOML` (or legacy
`DATASETS_TOML`) environment variable, else by walking up from the current
directory looking for `datamanifest.toml` > `DataManifest.toml` >
`datasets.toml` > `Datasets.toml` (a directory with `pyproject.toml` also
counts as project root). `get_default_database` raises `RuntimeError` when
no manifest can be found. See [Configuration](configuration.md).

## The `Database` class

```python
Database(datasets_toml="", datasets_folder="", persist=True,
         skip_checksum=False, skip_checksum_folders=False, datasets=None,
         storage_config=None)
```

The in-memory registry of dataset entries, tied to a manifest file.

- `datasets_toml` — path to the manifest. When empty and `persist=True`,
  the default manifest is discovered as above. The file, if it exists, is
  read on construction.
- `datasets_folder` — overrides the `datasets_dir` storage variable (where
  downloads land) for this database. Default: resolved from the
  [scoped configuration](configuration.md).
- `persist` — when `True` (default), registry changes (`add`,
  `register_dataset`, `delete_dataset`) rewrite the manifest file.
  `persist=False` gives a file-less, in-memory database (see
  [the guide](api.md#a-file-less-database-no-manifest)).
- `skip_checksum` / `skip_checksum_folders` — disable checksum verification
  globally / for directory datasets.
- `datasets` — an initial `{name: DatasetEntry}` mapping.
- `storage_config` — an optional `[_STORAGE]`-shaped dict applied as the
  manifest layer of the database's [scoped configuration](configuration.md):
  its keys (`project`, `datacache_dir`, `datasets_dir`, `lock_stale_age`, …)
  override the manifest's own `[_STORAGE]` values but sit below the checkout
  config and the `DATAMANIFEST_*` environment variables. Runtime-only — never
  written back to the manifest. The main use is an in-memory library database
  naming its [cache bundle](api.md#library-cache-bundles-database-scoped-caching):
  `Database(persist=False, storage_config={"project": "mylib"})`.

Useful methods beyond the everyday functions above:

```python
db[name]                          # -> DatasetEntry (also matches aliases)
db.write(datasets_toml)           # serialize the registry to a TOML file
db.get_project_root()             # project root derived from datasets_toml
db.register_datasets(datasets, persist=True)   # bulk-register a dict or a .toml path
```

## `DatasetEntry`

A dataclass holding one dataset's declaration — the Python form of one
manifest table (see the [manifest format](manifest-format.md)). All fields
are keyword arguments to the constructor, and to `add` /
`register_dataset`. The main ones:

- `uri` / `uris` — where the data comes from (one source, or a batch).
- `key` — the storage key (derived from the URI when not set); the default
  on-disk location is `$datasets_dir/$key`.
- `checksum` — expected content digest as `"<algo>:<hex>"` (a bare hex value
  is read as sha256); computed on first download when empty.
- `version`, `branch`, `doi`, `aliases`, `description` — metadata; `branch`
  selects the branch for git sources.
- `storage_path` — per-dataset location override, a
  [path expression](storage.md#path-expressions).
- `extract` / `format` — unpack a `zip` / `tar` / `tar.gz` archive after
  download; `format` also drives the default loader.
- `skip_download` — the URI is an existing local file; nothing is fetched.
- `lazy_access` — the URI is opened in place by the loader (e.g. via
  fsspec), never downloaded.
- `skip_checksum` — exempt this entry from verification.
- `fetcher`, `shell`, `loader`, `requires` — custom download / load hooks
  and dependencies; see [Adding datasets](adding-datasets.md).

Convenience accessors: `entry.hash_algo`, `entry.hash_value` and the
back-compat `entry.sha256` view of `checksum`.

## Loader validation

```python
datamanifest.validate_loader(db, name)   # resolve one named loader, raise if it can't
datamanifest.validate_loaders(db)        # eagerly resolve every registered loader
```

Loaders declared in the manifest (`[_LOADERS]`, `[_LANG.python.loaders]`)
are `"pkg.mod:func"` entry-point references resolved lazily at load time;
these helpers force resolution early so a broken reference fails fast.

## Caching computed results

### `datamanifest.cache.cached`

```python
from datamanifest.cache import cached

@cached                       # bare, all defaults
@cached(cachetype=..., format=..., key=..., version=..., ...)
```

Produce-or-load decorator for a function returning a cacheable value. The
wrapped function is **keyword-only**: its keyword arguments are hashed into
a parameter hash that identifies the artifact, stored under
`<datacache_dir>/<cachetype>/[<version>/]<hash>`. On a hit the artifact is
loaded and returned; on a miss the function runs and its result is
serialized there. The narrative walk-through is in
[Using it from code](api.md#caching-computed-results); where the cache
lives is part of the [storage model](storage.md).

Main options:

- `cachetype` — namespace for the artifact (first path component under the
  cache folder). Defaults to the function's fully-qualified importable name
  (`module.qualname`); an explicit value is required for functions without
  one (REPL, notebook, loose script).
- `format` — serialization format (e.g. `"txt"`, `"json"`, `"nc"`); drives
  the default writer and matching loader.
- `key` — selector narrowing the hash-affecting parameters: a callable
  `kwargs -> table` or a sequence of parameter names.
- `version` — recipe version. Becomes a path segment and is recorded in the
  sidecars, but never enters the parameter hash; bump it to isolate
  artifacts across recipe revisions.
- `storage_path` — explicit parent directory for the hash dirs, used
  verbatim instead of `<datacache_dir>/<cachetype>[/<version>]`.

The decorated function gains two per-call escape hatches: `cached=False`
(force a recompute) and `cache_dir="..."` (explicit cache directory for this
call).

The module-level form resolves its cache context over the
[default database](#resolve_db-get_default_database) when a manifest is
discoverable (which anchors at the same project, so paths are unchanged), and
falls back to the ambient working-directory derivation when none is — caching
works in projects without a manifest.

### `Database.cached`

```python
@db.cached(cachetype=None, format=None, key=None, basename="", version="",
           storage_path="", cached_toml="", name="")
```

The same produce-or-load decorator, bound to a specific
[`Database`](#the-database-class): the cache context comes from the
database's frozen configuration instead of the working directory — artifacts
land under *its* `datacache_dir` (keyed by *its* `project`), locks use *its*
`lock_stale_age`, and produced artifacts register in *its* state file (for an
in-memory database, under the `datacache_dir` root itself). The context is
read at call time. Accepts the same options as `datamanifest.cache.cached`
except `project_root` / `storage_config` / `context` — those are exactly what
the database supplies. See
[library cache bundles](api.md#library-cache-bundles-database-scoped-caching).

Advanced, for code building its own context instead of a `Database`:
`datamanifest.cache.CacheContext(project_root="", storage_config=None,
state_file="")` is the plain value a `Database` hands down into the cache
layer (pass it, or a zero-arg callable returning one, as `cached(...,
context=)`); `datamanifest.cache.set_default_context_provider(provider)`
registers the callable the bare form uses to resolve the default database's
context (installed by the fetch layer at import time).
