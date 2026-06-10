# Quickstart

After [installing](installation.md), declare your first dataset and load it.

```bash
datamanifest init                  # create datamanifest.toml here
datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
datamanifest list                  # what's tracked, and where it lives
datamanifest path co2              # resolve the on-disk path (for a script)
datamanifest config                # where data goes on this host; `config set` to change
```

The `add` above downloaded the Mauna Loa CO₂ record and wrote one entry to
`datamanifest.toml` — a plain TOML file you can read and edit by hand:

```toml
[co2]
checksum = "sha256:0058b3788040b5c27b2b5c1dd6d26226b7e4deef85e34c153e64806c37df7c75"
uri = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
```

**Commit `datamanifest.toml`** — it's the recipe (what to fetch and how). It is
the only file to commit: the private `.datamanifest/` directory (which records
*where* each file landed on this machine) ignores itself from git. A
collaborator clones the repo and runs `datamanifest download` to materialize
everything. Data lives in a machine-wide shared store by default — shared
across your projects, deduplicated by dataset key — and the produced cache in a
per-project folder under your OS cache dir; point either elsewhere with the
[storage model](storage.md).

## The CLI / API split

The split is the thing to keep in mind:

- the **CLI manages** the project's data — set it up, share it, maintain it;
- the **API consumes** it — your analysis code resolves and loads what the
  manifest declares, and never edits it.

So you set things up once on the command line, then your scripts just ask for
data by name.

## Load it from your code

```python
import datamanifest

df = datamanifest.load_dataset("co2")          # download on first use, then load
                                               # (pandas/xarray/… per format)
path = datamanifest.get_dataset_path("co2")    # just the on-disk path
```

That's the whole loop: declare on the CLI, consume from code. From here:

- [Using it from your code](api.md) — `load_dataset`, the `@cached` decorator,
  and the file-less `Database`.
- [CLI reference](cli.md) — every command and flag.
- [Storage model](storage.md) — where data lives and how to centralize it.
- [Adding datasets](adding-datasets.md) / [importing](importing.md) — Zenodo
  DOIs, object stores, and other tools' catalogs.
