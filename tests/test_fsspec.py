"""Object-store download via fsspec (``s3://`` / ``gs://`` / ``az://`` …).

The fetch primitive is exercised against fsspec's in-memory filesystem (no cloud /
network / backend needed); the scheme dispatch is checked with a stubbed primitive
so it needs no fsspec backend at all.
"""

import pytest

from datamanifest.database import init_dataset_entry
from datamanifest.pipelines import _FSSPEC_SCHEMES, _fetch_into_path, _fsspec_download


def test_fsspec_schemes_cover_the_common_object_stores():
    for s in ("s3", "gs", "gcs", "az", "abfs"):
        assert s in _FSSPEC_SCHEMES
    # HTTP is NOT routed through fsspec — it keeps its own httpx path.
    assert "http" not in _FSSPEC_SCHEMES and "https" not in _FSSPEC_SCHEMES


def test_fsspec_download_single_file(tmp_path):
    fsspec = pytest.importorskip("fsspec")
    fsspec.filesystem("memory").pipe_file("/d/x.bin", b"hello")

    dest = tmp_path / "x.bin"
    _fsspec_download("memory://d/x.bin", str(dest))
    assert dest.read_bytes() == b"hello"


def test_fsspec_download_directory_tree(tmp_path):
    """A prefix / store (e.g. a zarr) is mirrored recursively."""
    fsspec = pytest.importorskip("fsspec")
    mem = fsspec.filesystem("memory")
    mem.pipe_file("/store/a.txt", b"a")
    mem.pipe_file("/store/sub/b.txt", b"b")

    dest = tmp_path / "store"
    _fsspec_download("memory://store", str(dest))
    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "sub" / "b.txt").read_bytes() == b"b"


def test_fetch_routes_object_store_scheme_to_fsspec(tmp_path, monkeypatch):
    """An ``s3://`` URI reaches `_fsspec_download` through the scheme dispatch
    (stubbed, so no fsspec backend is required)."""
    import datamanifest.pipelines as P

    seen = {}

    def fake(uri, download_path, *, overwrite=False):
        seen["uri"] = uri
        with open(download_path, "w") as f:
            f.write("ok")

    monkeypatch.setattr(P, "_fsspec_download", fake)
    entry = init_dataset_entry(uri="s3://bucket/path/data.nc")
    _fetch_into_path(entry, str(tmp_path / "out.nc"))
    assert seen["uri"] == "s3://bucket/path/data.nc"


def test_fsspec_missing_dependency_gives_actionable_error(tmp_path, monkeypatch):
    """Without fsspec installed, the error names the extra to install."""
    import builtins

    real_import = builtins.__import__

    def no_fsspec(name, *a, **k):
        if name == "fsspec" or name.startswith("fsspec."):
            raise ImportError("no fsspec")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fsspec)
    with pytest.raises(ValueError, match=r"datamanifest\[fsspec\]"):
        _fsspec_download("s3://bucket/key", str(tmp_path / "x"))


# ----- on-the-fly loader (add --on-the-fly) ----------------------------------

def test_fsspec_loader_parses_json_remotely(tmp_path):
    fsspec = pytest.importorskip("fsspec")
    import json

    from datamanifest.store.loaders import fsspec_loader
    fsspec.filesystem("memory").pipe_file("/b/cfg.json", json.dumps({"k": 42}).encode())
    assert fsspec_loader("memory://b/cfg.json") == {"k": 42}


def test_fsspec_loader_unknown_format_returns_lazy_handle(tmp_path):
    fsspec = pytest.importorskip("fsspec")

    from datamanifest.store.loaders import fsspec_loader
    fsspec.filesystem("memory").pipe_file("/b/blob.bin", b"raw")
    handle = fsspec_loader("memory://b/blob.bin")   # no reader for .bin → OpenFile
    with handle as fh:
        assert fh.read() == b"raw"


def test_lazy_access_registers_and_loads_in_place(tmp_path):
    fsspec = pytest.importorskip("fsspec")
    import json

    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset
    from datamanifest.store.loaders import FSSPEC_LOADER_REF

    fsspec.filesystem("memory").pipe_file("/b/cfg.json", json.dumps({"v": 1}).encode())
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    db = Database(datasets_toml=str(toml))

    name, entry = db.register_dataset(
        "memory://b/cfg.json", lazy_access=True, lang_python_loader=FSSPEC_LOADER_REF)
    # lazy_access is its own marker — NOT skip_download.
    assert entry.lazy_access and not entry.skip_download
    assert entry.lang_python_loader == FSSPEC_LOADER_REF
    # No download, no state record (nothing local), and the loader streams it.
    assert load_dataset(db, name) == {"v": 1}
    assert not (tmp_path / ".datamanifest-state.toml").exists()


def test_add_lazy_cli_wiring(tmp_path, monkeypatch, capsys):
    import types

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    from datamanifest import cli
    from datamanifest.store.loaders import FSSPEC_LOADER_REF

    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    monkeypatch.setenv("DATAMANIFEST_TOML", str(toml))

    args = types.SimpleNamespace(
        uri="s3://bucket/store.zarr", name=None, extract=False,
        lazy=True, overwrite=False, no_download=False,
    )
    cli._cmd_add(args)
    assert "lazy" in capsys.readouterr().out.lower()

    with open(toml, "rb") as f:
        data = tomllib.load(f)
    entry = data["bucket/store"]
    # Only lazy_access is written — no skip_download double-marker.
    assert entry["lazy_access"] is True
    assert "skip_download" not in entry
    assert entry["uri"] == "s3://bucket/store.zarr"
    assert entry["_LANG"]["python"]["loader"] == FSSPEC_LOADER_REF
