"""Default loaders by format. Each loader is a callable `path -> value`.
Optional packages are imported at call-time with a pip-install hint on failure.
"""

import importlib
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
