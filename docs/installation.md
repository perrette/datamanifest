# Installation

The CLI and the Python library ship in one PyPI package, `datamanifestpy`;
Julia support is the separate package
[DataManifest.jl](https://github.com/awi-esc/DataManifest.jl).

=== "CLI"

    ```bash
    pipx install datamanifestpy   # the `datamanifest` command, in its own environment
    # or
    pip install datamanifestpy
    ```

    `pipx` keeps the command isolated from your project environments; `pip`
    installs the CLI and the Python library into the current environment.

=== "Python"

    ```bash
    pip install datamanifestpy
    ```

    This also puts the `datamanifest` command on your path.

=== "Julia"

    ```julia
    using Pkg
    Pkg.add("DataManifest")
    ```

## Optional loader backends (Python)

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
the format you ask it to load. In Julia, loaders are ordinary packages (e.g.
`CSV.jl`) that you `Pkg.add` yourself and reference from the manifest — see
[language bindings](language-bindings.md).

## Requirements

- CLI and Python library: Python ≥ 3.10; core dependencies (`httpx`, `tqdm`,
  `tomli_w`, `platformdirs`) install automatically.
- Julia package: Julia ≥ 1.10.

Once installed, head to the [quickstart](quickstart.md).
