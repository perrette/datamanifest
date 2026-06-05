"""Read pools — extra read-only locations probed for an already-present dataset
(``[_STORAGE].datasets_pools``), reused in place instead of re-downloading."""

import hashlib
import os

from datamanifest.database import Database, resolve_from_pools
from datamanifest.pipelines import download_dataset
from datamanifest.store import locations as L


# ----- pool list resolution (config + _HOST + defaults) ----------------------

def test_pools_default_to_well_known_when_undefined():
    pools = L.datasets_pools(storage_config={}, env={})
    assert any(p.endswith(os.path.join(".cache", "Datasets")) for p in pools)
    assert any(p.endswith(os.path.join("datamanifest", "datasets")) for p in pools)


def test_pools_explicit_list_replaces_defaults():
    pools = L.datasets_pools(storage_config={"datasets_pools": ["/a", "/b"]}, env={})
    assert pools == ["/a", "/b"]


def test_pools_empty_list_disables():
    assert L.datasets_pools(storage_config={"datasets_pools": []}, env={}) == []


def test_pools_compose_with_host():
    cfg = {"datasets_pools": ["/base"],
           "_HOST": {"myhost": {"datasets_pools": ["/hostpool"]}}}
    assert L.datasets_pools(storage_config=cfg, env={}, host="myhost") == ["/hostpool"]
    assert L.datasets_pools(storage_config=cfg, env={}, host="other.example") == ["/base"]


def test_pools_env_override():
    pools = L.datasets_pools(
        storage_config={"datasets_pools": ["/base"]},
        env={"DATAMANIFEST_DATASETS_POOLS": os.pathsep.join(["/e1", "/e2"])},
    )
    assert pools == ["/e1", "/e2"]


# ----- reuse from a pool on download -----------------------------------------

def _project(tmp_path, *, pool_dir, sha256=None):
    body = [
        "[_META]\nschema = 1\n",
        f'[_STORAGE]\ndatasets_dir = "datasets"\ndatasets_pools = ["{pool_dir}"]\n',
        '[a]\nuri = "https://example.com/a.csv"\n'
        + (f'sha256 = "{sha256}"\n' if sha256 else ""),
    ]
    (tmp_path / "datamanifest.toml").write_text("\n".join(body))
    return Database(datasets_toml=str(tmp_path / "datamanifest.toml"))


def test_download_reuses_pooled_copy_in_place(tmp_path):
    content = b"pooled,data\n"
    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(content)

    db = _project(tmp_path, pool_dir=str(pool))
    path = download_dataset(db, "a")

    # Reused from the pool in place — no ./datasets copy, no download.
    assert os.path.abspath(path) == str(pool / "example.com" / "a.csv")
    assert not (tmp_path / "datasets").exists()
    # Recorded in the state file.
    from datamanifest.cache import CachedIndex
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of("example.com/a.csv")


def test_pool_copy_with_wrong_checksum_is_not_adopted(tmp_path):
    content = b"pooled,data\n"
    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(content)
    wrong = hashlib.sha256(b"different").hexdigest()

    db = _project(tmp_path, pool_dir=str(pool), sha256=wrong)
    # The declared sha256 doesn't match the pooled file → not adopted.
    assert resolve_from_pools(db, db.datasets["a"]) == ""


def test_pool_copy_with_matching_checksum_is_adopted(tmp_path):
    content = b"pooled,data\n"
    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(content)
    good = hashlib.sha256(content).hexdigest()

    db = _project(tmp_path, pool_dir=str(pool), sha256=good)
    assert resolve_from_pools(db, db.datasets["a"]) == \
        str(pool / "example.com" / "a.csv")
