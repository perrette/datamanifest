# Quickstart

After [installing](installation.md), declare your first dataset, download it,
and use it. Each step below comes in tabs — pick your client once and the
whole site follows.

## Declare a dataset

=== "CLI"

    ```bash
    datamanifest init                  # create datamanifest.toml here
    datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
    datamanifest list                  # what's tracked, and where it lives
    ```

    `add` writes the manifest entry and downloads the file right away
    (pass `--no-download` to only declare it).

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")
    db.add("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
    ```

    `add` writes the manifest entry and downloads the file right away
    (pass `skip_download=True` to only declare it).

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")
    add(db, "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"; name="co2")
    ```

    `add` writes the manifest entry and downloads the file right away
    (pass `skip_download=true` to only declare it).

=== "Manifest"

    ```toml
    [co2]
    checksum = "sha256:0058b3788040b5c27b2b5c1dd6d26226b7e4deef85e34c153e64806c37df7c75"
    uri = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
    ```

The step above declared the Mauna Loa CO₂ record in the *manifest* — a plain
TOML file you can read and edit by hand (the "Manifest" tab shows the entry).
The `checksum` is a content hash of the file; every later download or
verification is checked against it, so everyone gets byte-identical data.

All clients follow the same filename rule: new manifests are created as the
canonical `datamanifest.toml`, and an existing `DataManifest.toml`,
`datasets.toml` or `Datasets.toml` is discovered too, in that order.

**Commit the manifest** — it's the recipe (what to fetch and how). It is
the only file to commit: the private `.datamanifest/` directory, which records
*where* each file landed on this machine, writes its own `.gitignore` and stays
out of git. Data lives in a machine-wide shared store by default —
shared across your projects, deduplicated by dataset key — and the produced
cache in a per-project folder under your OS cache dir; point either elsewhere
with the [storage model](storage.md).

## Download

A collaborator clones the repo and materializes everything the manifest
declares — files already present and matching their checksum are skipped:

=== "CLI"

    ```bash
    datamanifest download
    ```

=== "Python"

    ```python
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")
    db.download_datasets()
    ```

=== "Julia"

    ```julia
    using DataManifest

    db = read_dataset("datamanifest.toml")
    download_datasets(db)
    ```

## Get a path

Resolve a dataset's on-disk location, for a script or any tool that wants a
file path:

=== "CLI"

    ```bash
    datamanifest path co2
    ```

=== "Python"

    ```python
    path = db.get_dataset_path("co2")
    ```

=== "Julia"

    ```julia
    path = get_dataset_path(db, "co2")
    ```

## Load it

Going one step further than the path, `load_dataset` returns the data as an
in-memory object (the CLI stops at the path — loading is a library concern):

=== "Python"

    ```python
    df = db.load_dataset("co2")     # download on first use, then load
                                    # (pandas/xarray/… per format)
    ```

=== "Julia"

    ```julia
    tbl = load_dataset(db, "co2")   # needs a loader declared for the dataset or
                                    # its format, e.g. csv = "CSV:read"
    ```

    How loaders are declared in the manifest is covered in
    [language bindings](language-bindings.md).

## The CLI / library split

The tooling separates managing data from consuming it:

- the **CLI manages** the project's data — set it up, share it, maintain it;
- the **libraries consume** it — your analysis code resolves and loads what
  the manifest declares, and never edits it.

So you set things up once on the command line, then your scripts just ask for
data by name.

That is the whole loop: declare, download, consume. From here:

- [Using it from your code](api.md) — `load_dataset`, the `@cached` decorator,
  and the file-less `Database`.
- [CLI reference](cli.md) — every command and flag.
- [Storage model](storage.md) — where data lives and how to centralize it.
- [Configuration](configuration.md) — the config scopes and every setting.
- [Adding datasets](adding-datasets.md) / [importing](importing.md) — Zenodo
  DOIs, object stores, and other tools' catalogs.
