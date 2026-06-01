try:
    from ._version import __version__
except ImportError:
    __version__ = "unknown"

from .database import (
    Database,
    DatasetEntry,
    delete_dataset as _delete_dataset_db,
    get_default_database,
    validate_loader,
    validate_loaders,
)
from .pipelines import (
    _module_add as add,
    _module_delete_dataset as delete_dataset,
    _module_download_dataset as download_dataset,
    _module_download_datasets as download_datasets,
    _module_get_dataset_path as get_dataset_path,
    _module_load_dataset as load_dataset,
    _module_register_dataset as register_dataset,
)

__all__ = [
    "__version__",
    "Database",
    "DatasetEntry",
    "add",
    "delete_dataset",
    "download_dataset",
    "download_datasets",
    "get_dataset_path",
    "get_default_database",
    "load_dataset",
    "register_dataset",
    "validate_loader",
    "validate_loaders",
]
