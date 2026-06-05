"""Default loaders by format. Each loader is a callable `path -> value`.
Optional packages are imported at call-time with a pip-install hint on failure.
"""

import importlib
import os
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def default_loader(format: str):
    """Return a `path -> value` loader for the given format string."""
    f = format.strip().lower()
    if not f:
        raise ValueError(
            "No loader provided and dataset format is empty. "
            "Pass a loader function, e.g. loader=lambda path: open(path).read()."
        )
    if f == "json":
        return _json_loader
    if f in ("yaml", "yml"):
        return _yaml_loader
    if f == "toml":
        return _toml_loader
    if f in ("md", "txt"):
        return _text_io_loader
    if f in ("pickle", "pkl"):
        return _pickle_loader
    if f == "csv":
        return _csv_loader
    if f == "parquet":
        return _parquet_loader
    if f == "nc":
        return _nc_loader
    if f == "dimstack":
        return _dimstack_loader
    if f == "zip":
        return _zip_loader
    if f == "tar":
        return _tar_loader
    if f == "tar.gz":
        return _tar_gz_loader
    raise ValueError(
        f'No default loader for format "{format}". '
        "Pass a loader function or register a named loader in [_LOADERS]."
    )


def fsspec_loader(path):
    """Open a remote (fsspec) dataset *path* lazily, **without downloading it** —
    the built-in loader behind ``add --on-the-fly`` (paired with ``skip_download``,
    so *path* is the original ``s3://`` / ``gs://`` / … URI).

    Dispatches by the URI's extension to the reader that streams it in place:
    ``zarr`` / ``nc`` → xarray, ``csv`` / ``parquet`` → pandas (all read fsspec
    URLs directly). For ``json`` / ``yaml`` / ``toml`` the bytes are opened through
    fsspec and parsed; any other format returns an ``fsspec`` *OpenFile* — a lazy
    handle the caller uses as a context manager. Needs fsspec plus the backend for
    the scheme (``s3fs`` / ``gcsfs`` / ``adlfs`` …) and the format's reader.
    """
    clean = path.split("?")[0].rstrip("/")
    fmt = "tar.gz" if clean.endswith(".tar.gz") \
        else clean.rsplit(".", 1)[-1].lower() if "." in os.path.basename(clean) else ""

    def _need(mod, extra):
        try:
            return importlib.import_module(mod)
        except ImportError:
            raise ImportError(
                f"on-the-fly loading of {fmt or 'this URI'} needs {extra}.")

    if fmt == "zarr":
        return _need("xarray", "xarray + zarr + the fsspec backend").open_zarr(path)
    if fmt in ("nc", "nc4", "netcdf", "h5", "hdf5", "dimstack"):
        return _need("xarray", "xarray + the fsspec backend").open_dataset(path)
    if fmt == "csv":
        return _need("pandas", "pandas + the fsspec backend").read_csv(path, comment="#")
    if fmt == "parquet":
        return _need("pandas", "pandas + pyarrow + the fsspec backend").read_parquet(path)

    fsspec = _need("fsspec", "fsspec + the backend (s3fs / gcsfs / …)")
    if fmt == "json":
        import json
        with fsspec.open(path, "rt") as fh:
            return json.load(fh)
    if fmt in ("yaml", "yml"):
        yaml = _need("yaml", "pyyaml")
        with fsspec.open(path, "rt") as fh:
            return yaml.safe_load(fh)
    if fmt == "toml":
        with fsspec.open(path, "rb") as fh:
            return tomllib.load(fh)
    # Unknown / binary / text: hand back a lazy fsspec handle (a context manager).
    return fsspec.open(path)


# The portable ref for the built-in fsspec loader, wired by `add --on-the-fly`.
FSSPEC_LOADER_REF = "datamanifest.store.loaders:fsspec_loader"


def _json_loader(path):
    import json
    with open(path) as fh:
        return json.load(fh)


def _yaml_loader(path):
    try:
        yaml = importlib.import_module("yaml")
    except ImportError:
        raise ImportError(
            "For YAML default loader, install pyyaml: pip install pyyaml"
        )
    with open(path) as fh:
        return yaml.safe_load(fh)


def _toml_loader(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _text_io_loader(path):
    return open(path).read()


def _pickle_loader(path):
    import pickle
    with open(path, "rb") as fh:
        return pickle.load(fh)


def _csv_loader(path):
    try:
        pandas = importlib.import_module("pandas")
    except ImportError:
        raise ImportError(
            "For CSV default loader, install pandas: pip install pandas"
        )
    return pandas.read_csv(path, comment="#")


def _parquet_loader(path):
    try:
        pandas = importlib.import_module("pandas")
    except ImportError:
        raise ImportError(
            "For Parquet default loader, install pandas and pyarrow: pip install pandas pyarrow"
        )
    return pandas.read_parquet(path)


def _nc_loader(path):
    try:
        xarray = importlib.import_module("xarray")
    except ImportError:
        raise ImportError(
            "For NetCDF default loader, install xarray and netcdf4: pip install xarray netcdf4"
        )
    return xarray.open_dataset(path)


def _dimstack_loader(path):
    # In Python there is no DimStack type; xarray.Dataset is the equivalent.
    return _nc_loader(path)


def _zip_loader(path):
    import tempfile
    import zipfile

    dest = tempfile.mkdtemp(prefix="datamanifest_zip_")
    with zipfile.ZipFile(path) as zf:
        zf.extractall(dest)
    return dest


def _tar_loader(path):
    import tarfile
    import tempfile

    dest = tempfile.mkdtemp(prefix="datamanifest_tar_")
    with tarfile.open(path) as tf:
        tf.extractall(dest)
    return dest


def _tar_gz_loader(path):
    import tarfile
    import tempfile

    dest = tempfile.mkdtemp(prefix="datamanifest_tar_gz_")
    with tarfile.open(path, "r:gz") as tf:
        tf.extractall(dest)
    return dest
