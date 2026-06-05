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


def test_pool_copy_with_wrong_checksum_is_not_adopted(tmp_path, caplog):
    import logging

    content = b"pooled,data\n"
    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(content)
    wrong = hashlib.sha256(b"different").hexdigest()

    db = _project(tmp_path, pool_dir=str(pool), sha256=wrong)
    # The declared sha256 doesn't match the pooled file → not adopted, but a
    # warning surfaces that the file IS present (e.g. a stale manifest checksum).
    with caplog.at_level(logging.WARNING):
        assert resolve_from_pools(db, db.datasets["a"]) == ""
    assert any("does not match" in r.message for r in caplog.records)


def test_pool_copy_with_matching_checksum_is_adopted(tmp_path):
    content = b"pooled,data\n"
    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(content)
    good = hashlib.sha256(content).hexdigest()

    db = _project(tmp_path, pool_dir=str(pool), sha256=good)
    assert resolve_from_pools(db, db.datasets["a"]) == \
        str(pool / "example.com" / "a.csv")


# ----- produced-artifact pools (datacache_pools, opt-in) ---------------------

def test_datacache_pools_opt_in_empty_by_default():
    # No de-facto shared compute cache: undefined → no pools.
    assert L.datacache_pools(storage_config={}, env={}) == []
    assert L.datacache_pools(
        storage_config={"datacache_pools": ["/shared/cache"]}, env={}) == ["/shared/cache"]


def test_datacache_pools_compose_with_host():
    cfg = {"_HOST": {"hpc*": {"datacache_pools": ["/scratch/shared"]}}}
    assert L.datacache_pools(storage_config=cfg, env={}, host="hpc01") == ["/scratch/shared"]
    assert L.datacache_pools(storage_config=cfg, env={}, host="laptop") == []


def test_cached_reuses_pooled_artifact(tmp_path, monkeypatch):
    """A @cached call whose artifact already exists in a datacache pool loads it
    in place instead of recomputing."""
    from datamanifest.cache import cached, param_hash
    from datamanifest.cache._sidecars import write_config

    monkeypatch.setenv("DATAMANIFEST_USAGE_LOG", str(tmp_path / "usage.toml"))
    monkeypatch.setenv("DATAMANIFEST_DATACACHE_DIR", str(tmp_path / "cache"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "datamanifest.toml").write_text("[_META]\nschema = 1\n")

    pool = tmp_path / "pool"
    h = param_hash({"x": 1})
    art = pool / "ct" / h
    art.mkdir(parents=True)
    (art / "data.txt").write_text("from-pool")
    write_config(str(art), "ct", h, {"x": 1})
    (art / ".complete").write_text("")

    calls = {"n": 0}

    @cached(cachetype="ct", format="txt", project_root=str(proj),
            storage_config={"datacache_pools": [str(pool)]})
    def f(*, x=1):
        calls["n"] += 1
        return "computed"

    assert f(x=1) == "from-pool"          # loaded from the pool …
    assert calls["n"] == 0                # … not recomputed


def test_refresh_scan_adopts_pool_dataset(tmp_path):
    """`refresh --scan` records a pool-present-but-not-local dataset in the state
    file (checksum-gated, no download)."""
    from datamanifest.cache import CachedIndex
    from datamanifest.cli import _refresh_scan_pools

    pool = tmp_path / "pool"
    (pool / "example.com").mkdir(parents=True)
    (pool / "example.com" / "a.csv").write_bytes(b"x\n")

    db = _project(tmp_path, pool_dir=str(pool))
    # Dry run records nothing.
    _refresh_scan_pools(db, dry_run=True)
    assert not (tmp_path / ".datamanifest-state.toml").exists()
    # Apply adopts it.
    _refresh_scan_pools(db, dry_run=False)
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of("example.com/a.csv")


def test_pool_reuses_extracted_dataset(tmp_path):
    """An extract=true dataset is reused from a pool that holds the EXTRACTED
    directory (<pool>/<extract_path>), not just the archive."""
    from datamanifest.config import get_extract_path

    pool = tmp_path / "pool"
    extract_key = get_extract_path("host.com/data/archive.zip")
    (pool / extract_key).mkdir(parents=True)
    (pool / extract_key / "inner.txt").write_text("x")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        "[_META]\nschema = 1\n"
        f'[_STORAGE]\ndatasets_dir = "datasets"\ndatasets_pools = ["{pool}"]\n'
        '[a]\nuri = "https://host.com/data/archive.zip"\nextract = true\n'
    )
    db = Database(datasets_toml=str(toml))
    entry = db.datasets["a"]
    assert entry.extract                                  # it's an extract dataset
    assert resolve_from_pools(db, entry) == str(pool / extract_key)

    got = download_dataset(db, "a")                       # reused, not downloaded
    assert os.path.abspath(got) == str(pool / extract_key)
    assert not (tmp_path / "datasets").exists()


# ----- explicit per-invocation override --------------------------------------

def test_resolve_pool_exprs_dedupes_and_expands(tmp_path):
    out = L.resolve_pool_exprs(["/a", "/a", str(tmp_path / "x")], project_root="/p")
    assert out == ["/a", str(tmp_path / "x")]                # deduped, absolute


def test_resolve_from_pools_override_beats_config(tmp_path):
    alt = tmp_path / "alt"
    (alt / "example.com").mkdir(parents=True)
    (alt / "example.com" / "a.csv").write_bytes(b"x\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        "[_META]\nschema = 1\n"
        '[_STORAGE]\ndatasets_dir = "datasets"\ndatasets_pools = []\n'  # config: disabled
        '[a]\nuri = "https://example.com/a.csv"\n'
    )
    db = Database(datasets_toml=str(toml))
    # Config pools are empty → no hit; an explicit override finds it.
    assert resolve_from_pools(db, db.datasets["a"]) == ""
    assert resolve_from_pools(db, db.datasets["a"], pools=[str(alt)]) == \
        str(alt / "example.com" / "a.csv")
    # An explicit empty override also yields nothing.
    assert resolve_from_pools(db, db.datasets["a"], pools=[]) == ""


def test_refresh_scan_pool_override(tmp_path):
    """`refresh --scan --datasets-pools <dir>` adopts from the override even when
    the configured pools are empty/disabled."""
    from datamanifest.cache import CachedIndex
    from datamanifest.cli import _refresh_scan_pools

    alt = tmp_path / "alt"
    (alt / "example.com").mkdir(parents=True)
    (alt / "example.com" / "a.csv").write_bytes(b"x\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        "[_META]\nschema = 1\n"
        '[_STORAGE]\ndatasets_dir = "datasets"\ndatasets_pools = []\n'
        '[a]\nuri = "https://example.com/a.csv"\n'
    )
    db = Database(datasets_toml=str(toml))
    state = tmp_path / ".datamanifest-state.toml"

    _refresh_scan_pools(db, dry_run=False)                       # config disabled
    assert not state.exists() or CachedIndex.read(state).dataset_path_of("example.com/a.csv") == ""

    _refresh_scan_pools(db, dry_run=False, datasets_pools=[str(alt)])
    assert CachedIndex.read(state).dataset_path_of("example.com/a.csv")
