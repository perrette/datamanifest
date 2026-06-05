"""The fetched-dataset side of the state file (.datamanifest-state.toml).

A download records the dataset's resolved ``storage_path`` and actual ``sha256``
(unless checksums are skipped) into the state file sitting next to the manifest,
and read-first resolution then finds a relocated dataset where it really lives.
Offline: all sources are local ``file://`` URIs.
"""

import os
import shutil

from datamanifest.cache import CachedIndex
from datamanifest.database import Database, resolve_existing_path
from datamanifest.pipelines import download_dataset


def _project(tmp_path, *, skip_checksum=False):
    """A manifest-backed project under *tmp_path*: a ``datamanifest.toml`` whose
    datasets_dir is the repo-local default, and one ``file://`` dataset."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        "[_META]\nschema = 1\n\n"
        "[_STORAGE]\ndatasets_dir = \"datasets\"\n\n"
        f"[a]\nuri = \"file://{src / 'a.csv'}\"\n"
        + ("skip_checksum = true\n" if skip_checksum else "")
    )
    # persist=True (the default, as the CLI builds it) keeps datasets_toml set, so
    # the state file resolves as its sibling.
    db = Database(datasets_toml=str(toml))
    return db, toml


def _state(tmp_path):
    return tmp_path / ".datamanifest-state.toml"


def test_download_records_storage_path_and_sha256(tmp_path):
    db, _ = _project(tmp_path)
    path = download_dataset(db, "a")
    assert os.path.isfile(path)

    assert _state(tmp_path).is_file()
    key = db.datasets["a"].key
    idx = CachedIndex.read(_state(tmp_path))
    # storage_path recorded portably (relative to the manifest dir) and resolves
    # to where the bytes actually are.
    sp = idx.dataset_path_of(key)
    assert sp and not os.path.isabs(sp)                  # portable, repo-relative
    assert os.path.abspath(os.path.join(tmp_path, sp)) == os.path.abspath(path)
    # actual sha256 recorded (matches the entry's verified checksum).
    assert idx.dataset_sha256_of(key) == db.datasets["a"].sha256
    assert idx.dataset_sha256_of(key)


def test_skip_checksum_records_path_but_no_sha(tmp_path):
    db, _ = _project(tmp_path, skip_checksum=True)
    download_dataset(db, "a")
    key = db.datasets["a"].key
    idx = CachedIndex.read(_state(tmp_path))
    assert idx.dataset_path_of(key)               # location still recorded
    assert idx.dataset_sha256_of(key) == ""       # but no checksum (skip_checksum)


def test_read_first_resolution_finds_relocated_dataset(tmp_path):
    db, _ = _project(tmp_path)
    original = download_dataset(db, "a")

    # Relocate the bytes out of the derived datasets_dir and repoint the state
    # file's recorded storage_path at the new home (what a --move would do).
    key = db.datasets["a"].key
    moved = tmp_path / "elsewhere" / "a.csv"
    moved.parent.mkdir()
    shutil.move(original, str(moved))
    idx = CachedIndex.read(_state(tmp_path))
    idx.set_dataset_path(key, str(moved))
    idx.write()

    # resolve_existing_path consults the recorded location first → finds it at the
    # new home, not the (now-empty) derived path.
    resolved = resolve_existing_path(db, db.datasets["a"])
    assert os.path.abspath(resolved) == os.path.abspath(str(moved))

    # A re-download with the bytes present is a hit at the recorded location
    # (no re-fetch to the derived datasets_dir location).
    from datamanifest.database import get_dataset_path

    derived = get_dataset_path(
        db.datasets["a"], db.datasets_folder,
        project_root=db.get_project_root(), storage_config=db.storage_config,
    )
    again = download_dataset(db, "a")
    assert os.path.abspath(again) == os.path.abspath(str(moved))
    assert not os.path.exists(derived)        # nothing re-fetched at the derived path


def test_no_state_file_written_without_a_manifest(tmp_path, monkeypatch):
    """A manifest-less Database records nothing — the state file is defined
    relative to a manifest, so there is no sibling to write."""
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"x\n")
    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.register_dataset(f"file://{src / 'a.csv'}", name="a", persist=False)
    download_dataset(db, "a")
    assert not (tmp_path / ".datamanifest-state.toml").exists()
