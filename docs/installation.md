# Installation

The CLI and the Python library ship in one PyPI package, `datamanifestpy`;
Julia support is the separate package
[DataManifest.jl](https://github.com/awi-esc/DataManifest.jl).

=== "CLI"

    ```bash
    pipx install datamanifestpy
    ```

=== "Python"

    ```bash
    pip install datamanifestpy
    ```

=== "Julia"

    ```julia
    using Pkg
    Pkg.add("DataManifest")
    ```

The sections below cover each client in full.

## The command-line client

```bash
pipx install datamanifestpy   # the `datamanifest` command, in its own environment
# or
pip install datamanifestpy
```

`pipx` keeps the command isolated from your project environments; `pip`
installs the CLI (and the Python library) into the current environment.

Requirements: Python ≥ 3.10. The core dependencies (`httpx`, `tqdm`,
`tomli_w`, `platformdirs`) install automatically. Nothing else is needed to
fetch, verify, extract and track datasets from the shell — the optional
extras below only matter when you load data from Python.

## Python library

```bash
pip install datamanifestpy
```

This installs the library and also puts the `datamanifest` command on your
path. Requirements: Python ≥ 3.10.

The core package fetches, verifies, extracts and tracks your datasets and
caches your computed results. The data *loaders* (turning a file into a
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
hands you the on-disk path — `load_dataset` only needs the matching backend
for the format you ask it to load.

## Julia package

```julia
using Pkg
Pkg.add("DataManifest")
```

Requirements: Julia ≥ 1.10.

In Julia, data loaders are ordinary packages (e.g. `CSV.jl`) that you
`Pkg.add` yourself and reference from the manifest — see
[language bindings](language-bindings.md). The
[Julia API reference](julia-api.md) is mirrored on this site; the Julia
walkthrough lives in the
[DataManifest.jl repository](https://github.com/awi-esc/DataManifest.jl/blob/main/docs/doc.md).

Once installed, head to the [quickstart](quickstart.md).
