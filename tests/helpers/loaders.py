"""Helper callables for loader-registry / python-hook tests (Item 11)."""


def my_loader(path):
    """A trivial named loader: returns a tag plus the path it was given."""
    return ("loaded", path)


def param_loader(path, *, grid=None, scale=1):
    """A parameterized loader: records the path + kwargs it was called with.

    Used to verify the ``{ ref, args, kwargs }`` table form of a manifest-wide
    ``[_LANG.python.loaders]`` default, including ``$var`` substitution.
    """
    param_loader.last_call = {"path": path, "grid": grid, "scale": scale}
    return ("param", path, grid, scale)


def my_downloader(download_path, **kwargs):
    """A download-phase python hook: writes a sentinel file at download_path.

    Records the kwargs it was called with on the module so tests can assert the
    hook ran with the expected keyword names.
    """
    with open(download_path, "w") as f:
        f.write("hook ran")
    my_downloader.last_call = {"download_path": download_path, **kwargs}
