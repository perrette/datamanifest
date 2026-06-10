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

# Two recipes for the index round-trip (schema 4: cachetype-keyed, instances a
# hash→storage_path map; params live in each artifact's config.toml, not here).
_FIX_RECIPES = [
    {
        "cachetype": "esm_20c_anomaly",
        "hash": "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b",
        "storage_path": "cached/esm_20c_anomaly/83425a30",
        "ref": "lgmpre.data:load_20c_esm_anomaly",
        "format": "nc",
    },
    {
        "cachetype": "esm_lgm_anomaly",
        "hash": "40384c4db019d383728340a2d8c5db9c96eb41db0231e93dd944091ca180633c",
        "storage_path": "cached/esm_lgm_anomaly/40384c4d",
        "ref": "lgmpre.data:load_lgm_esm_anomaly",
        "format": "nc",
    },
]


def _write_fixture_index(path):
    index = CachedIndex(path=str(path))
    for r in _FIX_RECIPES:
        index.register(
            cachetype=r["cachetype"], hash=r["hash"],
            storage_path=r["storage_path"], ref=r["ref"], format=r["format"],
        )
    return index.write()


def test_cached_index_round_trip_recipes(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    back = CachedIndex.read(p)
    recs = {r["cachetype"]: r for r in back.recipe_records()}
    assert set(recs) == {"esm_20c_anomaly", "esm_lgm_anomaly"}
    assert recs["esm_20c_anomaly"]["ref"] == "lgmpre.data:load_20c_esm_anomaly"
    # instances map hash → recorded storage_path (full artifact dir).
    assert recs["esm_20c_anomaly"]["instances"] == {
        _FIX_RECIPES[0]["hash"]: _FIX_RECIPES[0]["storage_path"]
    }
    # reachable_keys() is (cachetype, version, hash) — no scope (spec-v4).
    assert back.reachable_keys() == {
        ("esm_20c_anomaly", "", _FIX_RECIPES[0]["hash"]),
        ("esm_lgm_anomaly", "", _FIX_RECIPES[1]["hash"]),
    }


def test_cached_index_write_is_canonical(tmp_path):
    p = _write_fixture_index(tmp_path / "cached.toml")
    text = open(p).read()
    # [_META] first, then schema-4 recipe tables keyed/sorted by cachetype.
    assert text.index("[_META]") < text.index("esm_20c_anomaly")
    assert text.index("esm_20c_anomaly") < text.index("esm_lgm_anomaly")
    # Idempotent: a second write of the read-back index is byte-identical.
    CachedIndex.read(p).write(tmp_path / "again.toml")
    assert (tmp_path / "again.toml").read_text() == text


def test_cached_index_accumulates_variations(tmp_path):
    # Registering the same recipe with different hashes accumulates instances —
    # it does not overwrite (the core fix).
    index = CachedIndex(path=str(tmp_path / "cached.toml"))
    index.register(cachetype="c", hash="h1", storage_path="cached/c/h1", ref="m:f")
    index.register(cachetype="c", hash="h2", storage_path="cached/c/h2", ref="m:f")
    recs = index.recipe_records()
    assert len(recs) == 1
    assert recs[0]["instances"] == {"h1": "cached/c/h1", "h2": "cached/c/h2"}
    assert index.reachable_keys() == {("c", "", "h1"), ("c", "", "h2")}


def test_schema5_datacache_namespace_version_in_key_and_hash_to_path(tmp_path):
    """schema-5 keys produced recipes under the ``datacache`` namespace as
    ["<cachetype>@<version>"] (bare when unversioned) and maps each instance
    hash → its full artifact dir (no params in the index; no recipe-level
    storage_path)."""
    p = tmp_path / ".datamanifest" / "state.toml"
    index = CachedIndex(path=str(p))
    index.register(cachetype="mypkg.mod.run", version="v3", hash="83b2",
                   storage_path="cached/mypkg.mod.run/v3/83b2",
                   ref="mypkg.mod:run", format="pickle")
    index.register(cachetype="plain", hash="44de", storage_path="cached/plain/44de",
                   ref="m:plain")
    index.write()
    text = p.read_text()

    assert 'schema = 5' in text
    assert '[datacache."mypkg.mod.run@v3"]' in text             # version after @
    assert '[datacache."mypkg.mod.run@v3".instances]' in text   # hash→path map
    assert '83b2 = "cached/mypkg.mod.run/v3/83b2"' in text      # full artifact dir
    assert 'params' not in text and 'grid' not in text         # params not in index
    assert '[datacache.plain]' in text                          # unversioned ⇒ bare key
    assert '[datacache.plain.instances]' in text

    # Round-trips identically.
    again = tmp_path / "again.toml"
    CachedIndex.read(p).write(again)
    assert again.read_text() == text


def test_entries_with_storage_paths_survive_roundtrip(tmp_path):
    """Guard against a future format change silently wiping recorded entries:
    several recipes (versioned + unversioned, each with per-instance paths) must
    round-trip read → write → read losslessly and byte-stably."""
    p = tmp_path / ".datamanifest" / "state.toml"
    idx = CachedIndex(path=str(p))
    idx.register(cachetype="a.b.run", version="v1", hash="h1",
                 ref="a.b:run", format="pickle", storage_path="cached/a.b.run/v1/h1")
    idx.register(cachetype="a.b.run", version="v2", hash="h2",
                 ref="a.b:run", format="pickle", storage_path="/scratch/v2/h2")
    idx.register(cachetype="plain", hash="h3", ref="m:plain",
                 storage_path="cached/plain/h3")
    idx.write()
    text = p.read_text()

    back = CachedIndex.read(p)
    assert back.recipes == idx.recipes                       # nothing dropped/altered
    assert back.instance_path_of(cachetype="a.b.run", version="v2", hash="h2") == "/scratch/v2/h2"
    assert back.instance_path_of(cachetype="plain", version="", hash="h3") == "cached/plain/h3"
    # A second write is byte-identical (stable; no drift).
    again = tmp_path / "again.toml"
    back.write(again)
    assert again.read_text() == text


def test_pinned_schema4_fixture_preserves_paths(tmp_path):
    """A hand-pinned canonical schema-4 file must read back with every instance's
    path intact. If a future reader/writer change breaks this, the test fails —
    forcing a schema bump + migration rather than a silent wipe."""
    p = tmp_path / "cached.toml"
    p.write_text(
        '[_META]\nschema = 4\n\n'
        '[greet]\nref = "m:greet"\nformat = "txt"\n\n'
        '[greet.instances]\naa = "cached/greet/aa"\n\n'
        '["a.b.run@v2"]\nref = "a.b:run"\nformat = "pickle"\n\n'
        '["a.b.run@v2".instances]\nbb = "/scratch/v2/bb"\n'
    )
    idx = CachedIndex.read(p)
    assert idx.instance_path_of(cachetype="greet", version="", hash="aa") == "cached/greet/aa"
    assert idx.instance_path_of(cachetype="a.b.run", version="v2", hash="bb") == "/scratch/v2/bb"
    assert idx.reachable_keys() == {("greet", "", "aa"), ("a.b.run", "v2", "bb")}


def test_legacy_schema3_migrates_to_schema5(tmp_path):
    """A legacy schema-3 file (recipe storage_path + params-body) reads so each
    instance inherits <recipe storage_path>/<hash>, params are dropped, and a
    rewrite emits schema 5 under the datacache namespace."""
    p = tmp_path / "cached.toml"
    p.write_text(
        '[_META]\nschema = 3\n\n'
        '["a.b.run@v2"]\nref = "a.b:run"\nformat = "pickle"\n'
        'storage_path = "/scratch/v2"\n\n'
        '["a.b.run@v2".instances.bb]\nn = 2\n'
    )
    idx = CachedIndex.read(p)
    assert idx.instance_path_of(cachetype="a.b.run", version="v2", hash="bb") == "/scratch/v2/bb"
    written = idx.write()            # raw read().write() preserves the path
    text = open(written).read()      # ... relocated to .datamanifest/state.toml
    assert not p.exists()            # the legacy file is removed
    assert "schema = 5" in text and 'bb = "/scratch/v2/bb"' in text
    assert '[datacache."a.b.run@v2"]' in text
    assert "params" not in text and "n = 2" not in text


def test_read_or_empty_migrates_legacy_filename(tmp_path):
    """Reading a legacy ``cached.toml`` via read_or_empty keeps resolving, and
    the next write relocates the inventory to the canonical
    ``.datamanifest/state.toml`` (removing the legacy file)."""
    legacy = tmp_path / "cached.toml"
    legacy.write_text(
        '[_META]\nschema = 4\n\n[greet]\nref = "m:greet"\nformat = "txt"\n\n'
        '[greet.instances]\naa = "cached/greet/aa"\n'
    )
    idx = CachedIndex.read_or_empty(str(tmp_path))     # finds the legacy file
    assert idx.instance_path_of(cachetype="greet", version="", hash="aa") == "cached/greet/aa"
    written = idx.write()
    assert written == str(tmp_path / ".datamanifest" / "state.toml")
    assert "schema = 5" in open(written).read()
    assert not legacy.exists()           # first write relocates


def test_dead_instanceless_recipe_is_dropped_on_read(tmp_path):
    """A residual recipe with no instances (e.g. left by an older shape) roots
    nothing and is dropped on read, so it self-cleans on the next write —
    without touching the real, populated entries."""
    p = tmp_path / "cached.toml"
    p.write_text(
        '[_META]\nschema = 4\n\n'
        '[memory2]\nformat = ""\nref = ""\n\n'
        '[memory2.instances]\n\n'
        '["memory2@2"]\nref = "m:p"\nformat = "pickle"\n\n'
        '["memory2@2".instances]\nh = "/c/ho/memory2/h"\n'
    )
    idx = CachedIndex.read(p)
    assert ("memory2", "") not in idx.recipes               # dead empty entry gone
    assert ("memory2", "2") in idx.recipes                  # real entry kept
    assert idx.instance_path_of(cachetype="memory2", version="2", hash="h") == "/c/ho/memory2/h"
    written = idx.write()
    assert "[memory2]\n" not in open(written).read()        # self-cleaned on rewrite


# ----- datasets namespace (fetched-dataset inventory) ------------------------

def test_datasets_namespace_round_trip(tmp_path):
    """The state file records fetched datasets (key → storage_path + sha256) in a
    ``datasets`` namespace, alongside produced recipes, and round-trips them."""
    p = tmp_path / ".datamanifest-state.toml"
    idx = CachedIndex(path=str(p))
    idx.register(cachetype="ct", hash="h1", storage_path="cached/ct/h1", ref="m:f")
    idx.register_dataset(key="example.com/a.csv",
                         storage_path="datasets/example.com/a.csv", sha256="abc123")
    idx.register_dataset(key="example.com/big.zip",
                         storage_path="datasets/example.com/big.zip")   # skip_checksum
    written = idx.write()
    text = open(written).read()

    # Both namespaces present; datacache sorts before datasets.
    assert '[datacache.ct]' in text
    assert '[datasets."example.com/a.csv"]' in text
    assert text.index("[datacache") < text.index("[datasets")
    # A skip_checksum dataset records only its location (no sha256 line).
    assert 'sha256 = "abc123"' in text
    assert text.count("sha256") == 1

    back = CachedIndex.read(p)
    assert back.dataset_path_of("example.com/a.csv") == "datasets/example.com/a.csv"
    assert back.dataset_sha256_of("example.com/a.csv") == "abc123"
    assert back.dataset_path_of("example.com/big.zip") == "datasets/example.com/big.zip"
    assert back.dataset_sha256_of("example.com/big.zip") == ""
    assert back.recipes == idx.recipes                      # produced side intact
    # Idempotent rewrite.
    again = tmp_path / "again.toml"
    back.write(again)
    assert again.read_text() == text


def test_register_dataset_is_additive(tmp_path):
    """register_dataset overwrites with a non-empty value but leaves an existing
    one untouched on an empty argument (a skip_checksum re-record keeps the sha);
    set_dataset_path repoints, remove_dataset prunes."""
    idx = CachedIndex(path=str(tmp_path / ".datamanifest-state.toml"))
    idx.register_dataset(key="k", storage_path="datasets/k", sha256="sha1")
    # Re-record without a checksum (skip_checksum): keeps the recorded sha + path.
    idx.register_dataset(key="k")
    assert idx.dataset_sha256_of("k") == "sha1"
    assert idx.dataset_path_of("k") == "datasets/k"
    # A relocation repoints; a new checksum overwrites.
    idx.register_dataset(key="k", storage_path="/moved/k", sha256="sha2")
    assert idx.dataset_path_of("k") == "/moved/k"
    assert idx.dataset_sha256_of("k") == "sha2"

    assert idx.set_dataset_path("k", "/again/k") is True
    assert idx.set_dataset_path("absent", "/x") is False
    assert idx.dataset_path_of("k") == "/again/k"

    assert [r["key"] for r in idx.dataset_records()] == ["k"]
    assert idx.remove_dataset("k") is True
    assert idx.remove_dataset("k") is False
    assert idx.has_dataset("k") is False


def test_set_and_remove_instance(tmp_path):
    """set_instance_path repoints a recorded variation (after a move);
    remove_instance prunes it (after a delete), dropping the now-empty recipe."""
    idx = CachedIndex(path=str(tmp_path / "cached.toml"))
    idx.register(cachetype="ct", hash="h1", storage_path="cached/ct/h1", ref="m:f")
    idx.register(cachetype="ct", hash="h2", storage_path="cached/ct/h2", ref="m:f")

    # repoint h1; a missing instance returns False and changes nothing.
    assert idx.set_instance_path(cachetype="ct", version="", hash="h1",
                                 storage_path="/moved/h1") is True
    assert idx.instance_path_of(cachetype="ct", version="", hash="h1") == "/moved/h1"
    assert idx.set_instance_path(cachetype="ct", version="", hash="nope",
                                 storage_path="/x") is False

    # remove h1, then h2 — the recipe disappears once empty.
    assert idx.remove_instance(cachetype="ct", version="", hash="h1") is True
    assert ("ct", "") in idx.recipes
    assert idx.remove_instance(cachetype="ct", version="", hash="h2") is True
    assert ("ct", "") not in idx.recipes
    assert idx.remove_instance(cachetype="ct", version="", hash="h2") is False


def test_cachetype_with_at_sign_is_rejected(tmp_path):
    """'@' is reserved as the version separator — a cachetype can't contain it."""
    import pytest
    index = CachedIndex(path=str(tmp_path / "cached.toml"))
    with pytest.raises(ValueError, match="@"):
        index.register(cachetype="blabla@v2", hash="a1", ref="m:b")


def test_cached_storage_path_replaces_cachetype_dir(tmp_path):
    """@cached(storage_path=P) puts artifacts at P/[version]/hash (no cachetype
    subfolder) and records P; cache_dir keeps the <cachetype> subfolder."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "datasets.toml").write_text("[_META]\nschema = 1\n")

    @cached(cachetype="ct", format="txt", project_root=str(proj),
            storage_path=str(tmp_path / "out"))
    def f(*, x=1):
        return str(x)

    f(x=1)
    # Artifact directly under storage_path (no "ct" segment).
    hits = [r for r, _, fs in os.walk(tmp_path / "out") if "data.txt" in fs]
    assert len(hits) == 1 and "/ct/" not in hits[0]
    idx = CachedIndex.read(proj / ".datamanifest" / "state.toml")
    h = next(iter(idx.recipes[("ct", "")]["instances"]))
    rec = idx.instance_path_of(cachetype="ct", version="", hash=h)
    # Recorded location = the full artifact dir (absolute, outside the repo).
    assert rec == os.path.join(str(tmp_path / "out"), h)


def test_cached_versioned_storage_path_includes_version(tmp_path):
    """A versioned recipe records the version in the instance path, and the
    artifact is <datacache_dir>/<cachetype>/<version>/<hash>."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "datasets.toml").write_text("[_META]\nschema = 1\n")

    @cached(cachetype="ct", version="v3", format="txt", project_root=str(proj))
    def f(*, x=1):
        return str(x)

    f(x=1)
    idx = CachedIndex.read(proj / ".datamanifest" / "state.toml")
    h = next(iter(idx.recipes[("ct", "v3")]["instances"]))
    rec = idx.instance_path_of(cachetype="ct", version="v3", hash=h)
    # The default datacache_dir is machine-global (per-project, $project =
    # the checkout basename), so the recorded path is absolute.
    from datamanifest.store import locations
    cache_root = locations.datacache_dir(project_root=str(proj))
    assert rec == os.path.join(cache_root, "ct", "v3", h)    # version in the path
    hits = [r for r, _, fs in os.walk(cache_root) if "data.txt" in fs]
    assert len(hits) == 1
    assert hits[0].endswith(os.path.join("cached", "ct", "v3", os.path.basename(hits[0])))


def test_cached_hit_prefers_recorded_storage_path(tmp_path, monkeypatch):
    """On a hit, the recorded storage_path wins over the machine-derived path: a
    later call after datacache_dir changed still finds the artifact where it was
    first written, instead of recomputing at the new default."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "datasets.toml").write_text("[_META]\nschema = 1\n")
    calls = {"n": 0}

    @cached(cachetype="ct", format="txt", project_root=str(proj))
    def f(*, x=1):
        calls["n"] += 1
        return str(x)

    monkeypatch.setenv("DATAMANIFEST_DATACACHE_DIR", str(tmp_path / "A"))
    assert f(x=1) == "1"
    assert calls["n"] == 1                                    # produced under A

    # datacache_dir now points elsewhere; the derived path would miss, but the
    # recorded storage_path (under A) is tried first → hit, no recompute.
    monkeypatch.setenv("DATAMANIFEST_DATACACHE_DIR", str(tmp_path / "B"))
    assert f(x=1) == "1"
    assert calls["n"] == 1
    assert not (tmp_path / "B").exists()                     # nothing written at the new default


def test_schema2_is_read_and_rewritten_as_schema5(tmp_path):
    """A legacy schema-2 ([[produced]]) file is read and rewritten as schema 5.
    Schema 2 recorded no location, so each instance's path is "" (derived on
    next access)."""
    p = tmp_path / "cached.toml"
    p.write_text(
        '[_META]\nschema = 2\n\n'
        '[[produced]]\ncachetype = "c"\nref = "m:f"\nformat = "txt"\n'
        '[[produced.instances]]\nhash = "h1"\n[produced.instances.params]\nn = 1\n'
    )
    index = CachedIndex.read(p)
    assert index.reachable_keys() == {("c", "", "h1")}
    assert index.recipes[("c", "")]["instances"] == {"h1": ""}
    written = index.write()
    text = open(written).read()
    assert 'schema = 5' in text and '[[produced]]' not in text
    assert '[datacache.c.instances]' in text and 'h1 = ""' in text


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
    index_path = proj / ".datamanifest" / "state.toml"
    assert index_path.is_file()
    index = CachedIndex.read(str(index_path))
    recs = {r["cachetype"]: r for r in index.recipe_records()}
    assert "greet" in recs
    rec = recs["greet"]
    # ref = "<module>:<qualname>" (qualname includes the enclosing scope).
    assert ":" in rec["ref"]
    assert rec["ref"].split(":", 1)[1].endswith("make_greeting")
    # one instance, recording the full artifact dir it was written to (absolute
    # here, since cache_root is outside the project root).
    artifact_hash = next(iter(rec["instances"]))
    assert rec["instances"][artifact_hash] == str(cache_root / "greet" / artifact_hash)

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
    index_path = proj / ".datamanifest" / "state.toml"
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
