"""Tests for the cache layer's index + inspection maintenance (spec-v4).

Covers the ``cached.toml`` index round-trip, ``@cached`` produce registration,
and the ``cache/_inspect.py`` object enumeration / delete / move that backs the
``datamanifest list`` maintenance surface (which replaced the old automatic GC).

Offline only: a ``tmp_path`` cache root (via ``DATAMANIFEST_DATACACHE_DIR``), a
``tmp_path`` usage log (via ``DATAMANIFEST_USAGE_LOG``), and the trivial ``txt``
format.
"""

import os
import time

import pytest

from datamanifest.cache import (
    CachedIndex,
    cached,
    delete_object,
    enumerate_artifacts,
    find_produced_artifacts,
    move_object,
    param_hash,
    read_metadata,
    read_usage,
)
from datamanifest.cache._inspect import CacheObject
from datamanifest.cache._sidecars import write_config
from datamanifest.cache._usage import iso_from_mtime, last_access


# ----- fixtures --------------------------------------------------------------

@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """The spec-v4 ``datacache_dir`` root. A produced artifact lands directly
    under it as ``<cachetype>/[<version>/]<hash>`` — no ``cached/`` prefix and no
    scope segment."""
    root = tmp_path / "cache"
    monkeypatch.setenv("DATAMANIFEST_DATACACHE_DIR", str(root))
    return root


@pytest.fixture
def usage_log(tmp_path, monkeypatch):
    log = tmp_path / "usage.toml"
    monkeypatch.setenv("DATAMANIFEST_USAGE_LOG", str(log))
    return log


# ----- CachedIndex round-trip (spec fixture schema) --------------------------

# Two recipes for the index round-trip (schema 2: nested by cachetype, with
# per-variation instances carrying hash + params). spec-v4 dropped the recipe
# scope/store fields.
_FIX_RECIPES = [
    {
        "cachetype": "esm_20c_anomaly",
        "hash": "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b",
        "params": {"grid": "5x5"},
        "ref": "lgmpre.data:load_20c_esm_anomaly",
        "format": "nc",
    },
    {
        "cachetype": "esm_lgm_anomaly",
        "hash": "40384c4db019d383728340a2d8c5db9c96eb41db0231e93dd944091ca180633c",
        "params": {},
        "ref": "lgmpre.data:load_lgm_esm_anomaly",
        "format": "nc",
    },
]


def _write_fixture_index(path):
    index = CachedIndex(path=str(path))
    for r in _FIX_RECIPES:
        index.register(
            cachetype=r["cachetype"], hash=r["hash"], params=r["params"],
            ref=r["ref"], format=r["format"],
        )
    return index.write()


def test_cached_index_round_trip_recipes(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    back = CachedIndex.read(p)
    recs = {r["cachetype"]: r for r in back.recipe_records()}
    assert set(recs) == {"esm_20c_anomaly", "esm_lgm_anomaly"}
    assert recs["esm_20c_anomaly"]["ref"] == "lgmpre.data:load_20c_esm_anomaly"
    assert recs["esm_20c_anomaly"]["instances"] == {
        _FIX_RECIPES[0]["hash"]: {"grid": "5x5"}
    }
    # reachable_keys() is (cachetype, version, hash) — no scope (spec-v4).
    assert back.reachable_keys() == {
        ("esm_20c_anomaly", "", _FIX_RECIPES[0]["hash"]),
        ("esm_lgm_anomaly", "", _FIX_RECIPES[1]["hash"]),
    }


def test_cached_index_write_is_canonical(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    text = (tmp_path / "cached.toml").read_text()
    # [_META] first, then [[produced]] recipe tables sorted by cachetype.
    assert text.index("[_META]") < text.index("[[produced]]")
    assert text.index("esm_20c_anomaly") < text.index("esm_lgm_anomaly")
    # Idempotent: a second write of the read-back index is byte-identical.
    CachedIndex.read(p).write(tmp_path / "again.toml")
    assert (tmp_path / "again.toml").read_text() == text


def test_cached_index_accumulates_variations(tmp_path):
    # Registering the same recipe with different params accumulates instances —
    # it does not overwrite (the core fix).
    index = CachedIndex(path=str(tmp_path / "cached.toml"))
    index.register(cachetype="c", hash="h1", params={"n": 1}, ref="m:f")
    index.register(cachetype="c", hash="h2", params={"n": 2}, ref="m:f")
    recs = index.recipe_records()
    assert len(recs) == 1
    assert recs[0]["instances"] == {"h1": {"n": 1}, "h2": {"n": 2}}
    assert index.reachable_keys() == {("c", "", "h1"), ("c", "", "h2")}


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

    # Registered in the sibling cached.toml under the cachetype.
    index_path = proj / "cached.toml"
    assert index_path.is_file()
    index = CachedIndex.read(str(index_path))
    recs = {r["cachetype"]: r for r in index.recipe_records()}
    assert "greet" in recs
    rec = recs["greet"]
    # ref = "<module>:<qualname>" (qualname includes the enclosing scope).
    assert ":" in rec["ref"]
    assert rec["ref"].split(":", 1)[1].endswith("make_greeting")
    # one instance, recording the params it was produced with.
    assert list(rec["instances"].values()) == [{"who": "x"}]
    artifact_hash = next(iter(rec["instances"]))

    # Artifact metadata back-points at that index (audit only). spec-v4 layout:
    # <datacache_dir>/<cachetype>/<hash> — no cached/ prefix, no scope.
    artifact = cache_root / "greet" / artifact_hash
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
    _rec = {r["cachetype"]: r for r in CachedIndex.read(str(index_path)).recipe_records()}["greet"]
    artifact_hash = next(iter(_rec["instances"]))
    metadata_path = cache_root / "greet" / artifact_hash / "metadata.toml"
    first_index = index_path.read_bytes()
    first_meta_mtime = metadata_path.stat().st_mtime

    time.sleep(0.01)
    make_greeting(who="x")  # hit
    assert calls["n"] == 1  # no recompute
    assert index_path.read_bytes() == first_index  # no re-register / churn
    assert metadata_path.stat().st_mtime == first_meta_mtime  # no re-stamp


def test_metadata_records_provenance(cache_root, usage_log, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    @cached(cachetype="t", format="txt", project_root=str(proj))
    def produce(*, name):
        return name

    produce(name="v")
    artifact = cache_root / "t" / param_hash({"name": "v"})
    md = read_metadata(str(artifact))
    assert md["_META"]["schema"] == 1
    assert md["tool"].startswith("datamanifestpy ")
    # `created` is stamped once, as an RFC-3339 UTC instant.
    assert md["created"].endswith("Z") and "T" in md["created"]


# ----- last-access stamp (best-effort, advisory) -----------------------------

def test_last_access_is_read_only_stat(tmp_path):
    d = tmp_path / "artifact"
    d.mkdir()
    # Reports *some* access stamp, derived from stat (non-empty RFC-3339).
    assert last_access(str(d))
    # It is purely read-derived: calling it never mutates the artifact's times.
    before = (d.stat().st_atime, d.stat().st_mtime)
    last_access(str(d))
    after = (d.stat().st_atime, d.stat().st_mtime)
    assert before == after
    # iso_from_mtime is the independent modification stamp.
    assert iso_from_mtime(str(d))


def test_last_access_missing_path_is_empty(tmp_path):
    assert last_access(str(tmp_path / "nope")) == ""


# ----- inspect: enumerate produced artifacts ---------------------------------

def _produce_artifact(cache_root, cachetype, key_table, *, version=""):
    """Materialize a minimal produced artifact directly under the datacache_dir
    root: ``<cachetype>/[<version>/]<hash>`` (spec-v4: no cached/ prefix)."""
    from datamanifest.cache import param_hash

    h = param_hash(key_table)
    parts = [cachetype] + ([version] if version else []) + [h]
    directory = cache_root.joinpath(*parts)
    directory.mkdir(parents=True)
    (directory / "data.txt").write_text("payload")
    write_config(str(directory), cachetype, h, key_table, version=version)
    (directory / ".complete").write_text("")
    return directory, f"{cachetype}/{h}"


def test_find_produced_artifacts_skips_non_config_dirs(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    prod_dir, key = _produce_artifact(cache_root, "kept", {"g": "5x5"})
    # A fetched-style dir (no config.toml) under the cache root must be skipped.
    fetched = cache_root / "somehost.org" / "file.nc"
    fetched.mkdir(parents=True)
    (fetched / "payload.bin").write_text("bytes")

    found = dict(find_produced_artifacts(str(cache_root)))
    assert found == {str(prod_dir): key}


def test_enumerate_artifacts_fields(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    prod_dir, key = _produce_artifact(cache_root, "mytype", {"g": "5x5"}, version="v3")

    objs = list(enumerate_artifacts(str(cache_root)))
    assert len(objs) == 1
    obj = objs[0]
    assert obj.kind == "cached"
    assert obj.key == key
    assert obj.cachetype == "mytype"
    assert obj.version == "v3"
    assert obj.format == "txt"          # data.txt
    assert obj.size > 0
    assert obj.created
    assert obj.last_access
    assert obj.referenced is None       # the composition root resolves this
    assert os.path.abspath(str(prod_dir)) == obj.location


# ----- inspect: delete / move are explicit and produced-only -----------------

def test_delete_object_removes_artifact_and_markers(cache_root):
    cache_root.mkdir(parents=True, exist_ok=True)
    prod_dir, _key = _produce_artifact(cache_root, "t", {"g": "5x5"})
    (obj,) = list(enumerate_artifacts(str(cache_root)))

    delete_object(obj)
    assert not os.path.exists(str(prod_dir))
    assert not os.path.exists(str(prod_dir) + ".complete")


def test_delete_object_refuses_non_cached(tmp_path):
    fake = CacheObject(kind="datasets", location=str(tmp_path / "data" / "x"))
    with pytest.raises(ValueError):
        delete_object(fake)


def test_move_object_preserves_key_path(cache_root, tmp_path):
    cache_root.mkdir(parents=True, exist_ok=True)
    prod_dir, _key = _produce_artifact(cache_root, "t", {"g": "5x5"}, version="v2")
    (obj,) = list(enumerate_artifacts(str(cache_root)))

    dest_root = tmp_path / "elsewhere"
    new = move_object(obj, str(dest_root))
    # <dest>/<cachetype>/<version>/<hash>/data.txt
    assert new == os.path.join(str(dest_root), "t", "v2", obj.hash)
    assert os.path.isfile(os.path.join(new, "data.txt"))
    assert not os.path.exists(str(prod_dir))
