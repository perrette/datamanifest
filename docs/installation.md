# Installation

```bash
pip install datamanifestpy
```

The core package fetches, verifies, extracts and tracks your datasets and caches
your computed results. The data *loaders* (turning a file into a
`pandas`/`xarray`/… object) are optional, so you only install the backends you
actually use:

```bash
pip install "datamanifestpy[csv]"       # pandas CSV
pip install "datamanifestpy[parquet]"   # pandas + pyarrow
pip install "datamanifestpy[nc]"        # xarray + netcdf4
pip install "datamanifestpy[yaml]"      # pyyaml (intake / DVC import)
pip install "datamanifestpy[fsspec]"    # object-store access (s3://, gs://, …)
pip install "datamanifestpy[all]"       # all of the above
```

Without an extra, `datamanifest` still downloads, verifies and tracks data and
hands you the on-disk path — `load_dataset` only needs the matching backend for
the format you ask it to load.

## Requirements

- Python ≥ 3.10.
- Core dependencies (`httpx`, `tqdm`, `tomli_w`, `platformdirs`) install
  automatically.

Once installed, head to the [quickstart](quickstart.md).
