"""Tests for Phase 2 of the cache layer: cached.toml index, usage log, GC.

Offline only: a ``tmp_path`` cache root (via ``DATAMANIFEST_CACHE_DIR``), a
``tmp_path`` usage log (via ``DATAMANIFEST_USAGE_LOG``), and the trivial ``txt``
format.
"""

import os
import time

import pytest

from datamanifest.cache import (
    CachedIndex,
    cached,
    collect,
    read_metadata,
    read_usage,
)
from datamanifest.cache._gc import find_produced_artifacts
from datamanifest.cache._sidecars import write_config


# ----- fixtures --------------------------------------------------------------

@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setenv("DATAMANIFEST_CACHE_DIR", str(root))
    return root


@pytest.fixture
def usage_log(tmp_path, monkeypatch):
    log = tmp_path / "usage.toml"
    monkeypatch.setenv("DATAMANIFEST_USAGE_LOG", str(log))
    return log


# ----- CachedIndex round-trip (spec fixture schema) --------------------------

# The two entries of the spec fixture
# (datamanifest.toml/tests/fixtures/cached_index.toml).
_FIX_ENTRIES = {
    "load_20c_esm_anomaly": {
        "cachetype": "esm_20c_anomaly",
        "hash": "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b",
        "ref": "lgmpre.data:load_20c_esm_anomaly",
        "format": "nc",
        "store": "$cache",
    },
    "load_lgm_esm_anomaly": {
        "cachetype": "esm_lgm_anomaly",
        "hash": "40384c4db019d383728340a2d8c5db9c96eb41db0231e93dd944091ca180633c",
        "ref": "lgmpre.data:load_lgm_esm_anomaly",
        "format": "nc",
        "store": "$cache",
    },
}


def _write_fixture_index(path):
    index = CachedIndex(path=str(path))
    for name, e in _FIX_ENTRIES.items():
        index.register(
            name,
            cachetype=e["cachetype"], hash=e["hash"],
            ref=e["ref"], format=e["format"], store=e["store"],
        )
    return index.write()


def test_cached_index_round_trip_entries(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    back = CachedIndex.read(p)
    assert back.entries == _FIX_ENTRIES
    assert back.keys() == {
        "esm_20c_anomaly/" + _FIX_ENTRIES["load_20c_esm_anomaly"]["hash"],
        "esm_lgm_anomaly/" + _FIX_ENTRIES["load_lgm_esm_anomaly"]["hash"],
    }


def test_cached_index_write_is_canonical(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    text = (tmp_path / "cached.toml").read_text()
    # [_META] first (uppercase '_' sorts before lowercase names), then the two
    # tables in name order; within a table the five fields are sorted.
    assert text.index("[_META]") < text.index("[load_20c_esm_anomaly]")
    assert text.index("[load_20c_esm_anomaly]") < text.index("[load_lgm_esm_anomaly]")
    block = text.split("[load_20c_esm_anomaly]", 1)[1].split("[load_lgm", 1)[0]
    keys = [ln.split("=")[0].strip() for ln in block.splitlines() if "=" in ln]
    assert keys == sorted(keys) == ["cachetype", "format", "hash", "ref", "store"]
    # Idempotent: a second write of the read-back index is byte-identical.
    again = CachedIndex.read(p).write(tmp_path / "again.toml")
    assert (tmp_path / "again.toml").read_text() == text


# ----- @cached produce registers in a sibling cached.toml --------------------

def test_cached_produce_registers_and_back_points(cache_root, usage_log, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    calls = {"n": 0}

    @cached(cachetype="greet", format="txt", project_root=str(proj))
    def make_greeting(*, who="world"):
        calls["n"] += 1
        return f"hello {who}"

    make_greeting(who="x")
    assert calls["n"] == 1

    # Registered in the sibling cached.toml under the function name.
    index_path = proj / "cached.toml"
    assert index_path.is_file()
    index = CachedIndex.read(str(index_path))
    assert "make_greeting" in index.entries
    entry = index.entries["make_greeting"]
    assert entry["cachetype"] == "greet"
    # ref = "<module>:<qualname>" (qualname includes the enclosing scope).
    assert ":" in entry["ref"]
    assert entry["ref"].split(":", 1)[1].endswith("make_greeting")
    assert entry["store"] == "$cache"

    # Artifact metadata back-points at that index (audit only).
    artifact = cache_root / "greet" / entry["hash"]
    md = read_metadata(str(artifact))
    assert md["origin"]["cached_toml"] == os.path.abspath(str(index_path))

    # The index path is recorded in the depot usage log.
    assert os.path.abspath(str(index_path)) in read_usage()


def test_cached_hit_does_not_duplicate_or_restamp(cache_root, usage_log, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    calls = {"n": 0}

    @cached(cachetype="greet", format="txt", project_root=str(proj))
    def make_greeting(*, who="world"):
        calls["n"] += 1
        return f"hello {who}"

    make_greeting(who="x")
    index_path = proj / "cached.toml"
    metadata_path = cache_root / "greet" / CachedIndex.read(
        str(index_path)
    ).entries["make_greeting"]["hash"] / "metadata.toml"
    first_index = index_path.read_bytes()
    first_meta_mtime = metadata_path.stat().st_mtime

    time.sleep(0.01)
    make_greeting(who="x")  # hit
    assert calls["n"] == 1  # no recompute
    assert index_path.read_bytes() == first_index  # no re-register / churn
    assert metadata_path.stat().st_mtime == first_meta_mtime  # no re-stamp


# ----- GC collector ----------------------------------------------------------

def _produce_artifact(cache_root, cachetype, key_table, *, name=None):
    """Materialize a minimal produced artifact (config.toml-bearing dir)."""
    from datamanifest.cache import param_hash

    h = param_hash(key_table)
    directory = cache_root / cachetype / h
    directory.mkdir(parents=True)
    (directory / "data.txt").write_text("v")
    write_config(str(directory), cachetype, h, key_table)
    # mark complete so it looks like a real published artifact
    (directory / ".complete").write_text("")
    return str(directory), f"{cachetype}/{h}"


def test_gc_collects_orphan_keeps_referenced(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    kept_dir, kept_key = _produce_artifact(cache_root, "kept", {"g": "5x5"})
    orph_dir, orph_key = _produce_artifact(cache_root, "orphan", {"g": "10x10"})

    # only the kept key is rooted; grace 0 so age always exceeds it
    collected = collect(str(cache_root), {kept_key}, grace_seconds=0)
    collected_keys = {c.key for c in collected}
    assert collected_keys == {orph_key}
    assert not os.path.exists(orph_dir)
    assert os.path.exists(kept_dir)


def test_gc_grace_protects_young_artifact(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    orph_dir, orph_key = _produce_artifact(cache_root, "young", {"g": "1"})

    # grace is huge; the just-created artifact is younger -> kept even unrooted
    collected = collect(str(cache_root), set(), grace_seconds=10_000)
    assert collected == []
    assert os.path.exists(orph_dir)


def test_gc_ignores_fetched_cache_dir_without_config(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    # A fetched $cache dataset: a directory under $cache with NO config.toml.
    fetched = cache_root / "somehost.org" / "file.nc"
    fetched.mkdir(parents=True)
    (fetched / "payload.bin").write_text("bytes")

    # It is not even enumerated as a produced artifact ...
    assert list(find_produced_artifacts(str(cache_root))) == []
    # ... and a full collect (with no live keys, grace 0) never touches it.
    collect(str(cache_root), set(), grace_seconds=0)
    assert fetched.is_dir()


def test_gc_never_walks_data_or_repo(tmp_path, cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    # A produced-looking artifact under a $data/$repo root must never be seen:
    # collect() is only ever handed the resolved $cache folder.
    data_root = tmp_path / "data"
    data_root.mkdir()
    prod_dir, _key = _produce_artifact(data_root, "underdata", {"g": "x"})

    collect(str(cache_root), set(), grace_seconds=0)
    assert os.path.exists(prod_dir)  # untouched — never under cache_root


def test_gc_dry_run_reports_without_deleting(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    orph_dir, orph_key = _produce_artifact(cache_root, "orphan", {"g": "z"})

    candidates = collect(str(cache_root), set(), grace_seconds=0, dry_run=True)
    assert [c.key for c in candidates] == [orph_key]
    assert all(not c.collected for c in candidates)
    assert os.path.exists(orph_dir)  # nothing deleted
