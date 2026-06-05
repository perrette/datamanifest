"""Behavioral tests for the state-aware ``datamanifest list`` surface:
dirty-state enumeration, ``--dirty`` filtering, ``--refresh`` reconciliation, and
unified ``--delete`` / ``--move`` over fetched datasets (with protection).

Driven through the CLI composition-root functions directly (fast, precise) with
a real manifest-backed project and a local ``file://`` dataset.
"""

import os
import shutil
import types

from datamanifest.cache import CachedIndex
from datamanifest.cli import _enumerate_objects, _filter_objects, _maintain, _refresh
from datamanifest.database import Database
from datamanifest.pipelines import download_dataset


def _args(**kw):
    base = dict(
        search=[], hash=None, invert=False, any=False, cached=False,
        datasets=False, format=None, present=False, missing=False, orphan=False,
        dirty=False, all=False, older_than=None, dry_run=False, move=None,
        delete=False,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _project(tmp_path, *, skip_download=False, user_managed=False):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")
    body = ["[_META]\nschema = 1\n", '[_STORAGE]\ndatasets_dir = "datasets"\n']
    if skip_download:
        # skip_download: the URI *is* the local file (no scheme).
        body.append(f'[a]\nuri = "{src / "a.csv"}"\nskip_download = true\n')
    elif user_managed:
        body.append(f'[a]\nuri = "file://{src / "a.csv"}"\n'
                    f'storage_path = "{src / "a.csv"}"\n')
    else:
        body.append(f'[a]\nuri = "file://{src / "a.csv"}"\n')
    (tmp_path / "datamanifest.toml").write_text("\n".join(body))
    return Database(datasets_toml=str(tmp_path / "datamanifest.toml"))


def _dataset_obj(db):
    return next(o for o in _enumerate_objects(db) if o.kind == "datasets")


def test_clean_after_download(tmp_path):
    db = _project(tmp_path)
    download_dataset(db, "a")
    obj = _dataset_obj(db)
    assert obj.present and obj.dirty == ""


def test_relocated_is_flagged_and_refreshed(tmp_path):
    db = _project(tmp_path)
    path = download_dataset(db, "a")          # recorded == derived (clean)
    key = db.datasets["a"].key

    # Point the recorded location at a stale path while the bytes stay at the
    # derived location → "relocated" (state record disagrees with disk).
    state = tmp_path / ".datamanifest-state.toml"
    idx = CachedIndex.read(state)
    idx.set_dataset_path(key, str(tmp_path / "stale" / "a.csv"))
    idx.write()

    obj = _dataset_obj(db)
    assert obj.dirty == "relocated"
    assert obj.location == os.path.abspath(path)         # found where bytes are
    assert _filter_objects(_enumerate_objects(db), _args(dirty=True))  # surfaced

    # refresh repoints the recorded location to the actual (derived) one.
    _refresh(_enumerate_objects(db), db, dry_run=False)
    assert _dataset_obj(db).dirty == ""
    idx2 = CachedIndex.read(state)
    assert os.path.abspath(os.path.join(tmp_path, idx2.dataset_path_of(key))) \
        == os.path.abspath(path)


def test_missing_is_flagged_and_dropped_by_refresh(tmp_path):
    db = _project(tmp_path)
    path = download_dataset(db, "a")
    key = db.datasets["a"].key
    os.remove(path)                       # bytes gone; state still records them

    obj = _dataset_obj(db)
    assert obj.dirty == "missing" and not obj.present

    _refresh(_enumerate_objects(db), db, dry_run=False)
    # The stale entry is dropped from the state file.
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of(key) == ""


def test_untracked_dataset_is_adopted_by_refresh(tmp_path):
    db = _project(tmp_path)
    download_dataset(db, "a")
    key = db.datasets["a"].key

    # Present on disk but not recorded (e.g. fetched before the state file
    # existed): drop the entry, keep the bytes.
    state = tmp_path / ".datamanifest-state.toml"
    idx = CachedIndex.read(state)
    idx.remove_dataset(key)
    idx.write()

    obj = _dataset_obj(db)
    assert obj.present and obj.dirty == "untracked"

    # refresh adopts it (records its location); no re-download.
    _refresh(_enumerate_objects(db), db, dry_run=False)
    assert _dataset_obj(db).dirty == ""
    assert CachedIndex.read(state).dataset_path_of(key)      # recorded again


def test_dataset_delete_removes_bytes_and_entry(tmp_path):
    db = _project(tmp_path)
    path = download_dataset(db, "a")
    key = db.datasets["a"].key

    _maintain(_enumerate_objects(db), _args(delete=True), db)
    assert not os.path.exists(path)                       # bytes gone
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of(key) == ""                 # entry pruned
    # Manifest untouched — the dataset is still declared.
    assert "[a]" in (tmp_path / "datamanifest.toml").read_text()


def test_dataset_move_repoints_state_not_manifest(tmp_path):
    db = _project(tmp_path)
    path = download_dataset(db, "a")
    key = db.datasets["a"].key
    before = (tmp_path / "datamanifest.toml").read_text()
    dest = tmp_path / "archive"

    _maintain(_enumerate_objects(db), _args(move=str(dest)), db)
    moved = dest / key
    assert moved.exists() and not os.path.exists(path)    # bytes relocated
    # State repointed at the new home; manifest unchanged.
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert os.path.abspath(os.path.join(tmp_path, idx.dataset_path_of(key))) \
        == os.path.abspath(str(moved))
    assert (tmp_path / "datamanifest.toml").read_text() == before


def test_remove_prunes_state_record(tmp_path):
    """`remove` (delete entry + bytes) also drops the dataset's state record, so
    no stale entry lingers in the state file."""
    from datamanifest.database import delete_dataset

    db = _project(tmp_path)
    download_dataset(db, "a")
    key = db.datasets["a"].key
    state = tmp_path / ".datamanifest-state.toml"
    assert CachedIndex.read(state).dataset_path_of(key)        # recorded after download

    delete_dataset(db, "a")
    assert CachedIndex.read(state).dataset_path_of(key) == ""  # pruned


def test_remove_keep_cache_keeps_state_record(tmp_path):
    """`remove --keep-cache` keeps the bytes, so it keeps the state record too."""
    from datamanifest.database import delete_dataset

    db = _project(tmp_path)
    download_dataset(db, "a")
    key = db.datasets["a"].key
    delete_dataset(db, "a", keep_cache=True)
    state = tmp_path / ".datamanifest-state.toml"
    assert CachedIndex.read(state).dataset_path_of(key)        # still recorded


def test_skip_download_dataset_is_protected(tmp_path):
    db = _project(tmp_path, skip_download=True)
    download_dataset(db, "a")            # skip_download: the URI file is the data
    src_file = db.datasets["a"].uri.replace("file://", "")
    _maintain(_enumerate_objects(db), _args(delete=True), db)
    assert os.path.exists(src_file)      # never deleted (protected)


def test_user_managed_dataset_is_protected(tmp_path):
    db = _project(tmp_path, user_managed=True)
    path = download_dataset(db, "a")
    _maintain(_enumerate_objects(db), _args(delete=True), db)
    assert os.path.exists(path)          # exact user path never touched
