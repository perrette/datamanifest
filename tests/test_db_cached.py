"""Database-scoped caching (cache bundles).

The design's verification checklist, Python flavor:

- ``db.cached``: artifacts under the db's ``datacache_dir``, keyed with the
  db's ``project``; ``lock_stale_age`` from the db's frozen snapshot.
- In-memory db: the caller's cwd stays clean (no ``.datamanifest/`` outside the
  db's roots); state files appear under the roots; round-trip load works;
  inspection over the bundle works.
- Bare ``@cached`` in a manifest project: behavior identical to before (same
  paths, same state file) — now resolved over the default database.
- Bare form in a directory with no manifest: still works (ambient fallback);
  the default database's "no manifest" RuntimeError never leaks into caching.
- Addressing unchanged: RFC 8785 key -> SHA-256, ``cachetype/[version/]hash``
  layout (the normative cross-language reference vector).

Offline only: ``file://`` URIs and tmp dirs.
"""

import os
from pathlib import Path

import pytest

import datamanifest.database as dbmod
from datamanifest import storage
from datamanifest.cache import CachedIndex, cached, enumerate_artifacts, param_hash
from datamanifest.database import Database, get_default_database
from datamanifest.store import materialize as materialize_mod


@pytest.fixture(autouse=True)
def _fresh_default_db():
    """Isolate the process-wide default-database singleton per test."""
    saved = dbmod._default_db
    dbmod._default_db = None
    yield
    dbmod._default_db = saved


# ----- db.cached: context from the database ----------------------------------

def test_db_cached_uses_db_datacache_project_and_lock_age(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database(
        persist=False,
        storage_config={"project": "mylib", "lock_stale_age": 7},
    )

    captured = {}
    real = materialize_mod.materialize

    def spy(target, write_fn, **kw):
        captured["stale_age"] = kw.get("stale_age")
        return real(target, write_fn, **kw)

    monkeypatch.setattr(materialize_mod, "materialize", spy)

    @db.cached(cachetype="mylib.mask", format="json")
    def mask(*, grid):
        return {"grid": grid}

    assert mask(grid="5x5") == {"grid": "5x5"}

    # Artifact under the db's resolved datacache_dir, whose default is
    # namespaced by the db's project name (storage_config={"project": ...}).
    cache_root = Path(storage.datacache_dir(storage_config=db.storage_config))
    assert str(cache_root).endswith(
        os.path.join("datamanifest", "projects", "mylib", "cached"))
    h = param_hash({"grid": "5x5"})
    assert (cache_root / "mylib.mask" / h / "data.json").is_file()
    # lock_stale_age threaded from the db's frozen snapshot into materialize.
    assert captured["stale_age"] == 7.0


def test_db_cached_addressing_matches_reference_vector(tmp_path):
    # Cross-language bullet: the RFC 8785 -> SHA-256 hash and the
    # cachetype/[version/]hash layout are unchanged under db.cached, so bundles
    # stay shareable across the Python and Julia tools.
    bundle = tmp_path / "bundle"
    db = Database(
        persist=False,
        storage_config={"project": "x", "datacache_dir": str(bundle)},
    )

    @db.cached(cachetype="ref.t", format="json", version="v2")
    def produce(*, grid, skip_models):
        return {"grid": grid}

    produce(grid="5x5", skip_models=["CESM.*", "FGOALS.*"])
    # The SCHEMA "Parameter-hash keying" normative reference vector.
    h = "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b"
    assert (bundle / "ref.t" / "v2" / h / "data.json").is_file()


# ----- the persist=false rule (in-memory databases) ---------------------------

def test_in_memory_db_bundle_keeps_cwd_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    libdata = tmp_path / "lib" / "datasets"
    libcache = tmp_path / "lib" / "cache"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")

    db = Database(
        persist=False,
        datasets_folder=str(libdata),
        storage_config={"project": "mylib", "datacache_dir": str(libcache)},
    )

    # --- fetched side: bytes + dataset inventory under the datasets root.
    name, entry = db.add(uri=f"file://{src}/a.csv")
    path = db.get_dataset_path(name)
    assert os.path.isfile(path)
    assert os.path.abspath(path).startswith(str(libdata) + os.sep)
    ds_state = libdata / ".datamanifest" / "state.toml"
    assert ds_state.is_file()
    idx = CachedIndex.read(str(ds_state))
    recorded = idx.dataset_path_of(entry.key)
    assert os.path.isabs(recorded)                       # no project root: absolute
    assert os.path.abspath(recorded) == os.path.abspath(path)
    assert idx.dataset_sha256_of(entry.key) == entry.sha256

    # --- produced side: artifact + inventory under the datacache root.
    calls = {"n": 0}

    @db.cached(cachetype="mylib.mask", format="json")
    def mask(*, grid):
        calls["n"] += 1
        return {"grid": grid}

    first = mask(grid="5x5")
    again = mask(grid="5x5")
    assert first == again == {"grid": "5x5"}
    assert calls["n"] == 1                               # round-trip load (a hit)
    h = param_hash({"grid": "5x5"})
    assert (libcache / "mylib.mask" / h / "data.json").is_file()
    cache_state = libcache / ".datamanifest" / "state.toml"
    assert cache_state.is_file()
    cidx = CachedIndex.read(str(cache_state))
    assert cidx.has_instance(cachetype="mylib.mask", version="", hash=h)

    # --- inspection over the bundle works.
    objs = list(enumerate_artifacts(str(libcache)))
    assert [o.key for o in objs] == [f"mylib.mask/{h}"]

    # --- the caller's cwd never gains a .datamanifest/ from an in-memory db.
    assert not (tmp_path / ".datamanifest").exists()


def test_in_memory_db_default_roots_leave_cwd_clean(tmp_path, monkeypatch):
    # No explicit folders at all: everything (bytes, artifacts, both state
    # files) goes to the machine-wide defaults, nothing into the cwd.
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")
    db = Database(persist=False, storage_config={"project": "mylib"})
    db.add(uri=f"file://{src}/a.csv")

    @db.cached(cachetype="mylib.t2", format="txt")
    def produce(*, a):
        return f"v{a}"

    assert produce(a=1) == "v1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src"]
    # State files under the roots the db resolves: the shared dataset store and
    # the per-project cache (both under the isolated per-test XDG home).
    ds_root = storage.datasets_dir(storage_config=db.storage_config)
    cache_root = storage.datacache_dir(storage_config=db.storage_config)
    assert os.path.isfile(os.path.join(ds_root, ".datamanifest", "state.toml"))
    assert os.path.isfile(os.path.join(cache_root, ".datamanifest", "state.toml"))


# ----- the bare form: default database / ambient fallback ---------------------

def test_bare_cached_in_manifest_project_unchanged(tmp_path, monkeypatch):
    # In a manifest project the bare form now resolves over the default
    # database — which anchors at the same project, so paths and state file
    # are identical to the previous ambient behavior.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "datamanifest.toml").write_text(
        '[_STORAGE]\ndatacache_dir = "cached"\n')

    @cached(cachetype="proj.t", format="txt")
    def produce(*, name):
        return f"hi {name}"

    assert produce(name="x") == "hi x"
    h = param_hash({"name": "x"})
    assert (tmp_path / "cached" / "proj.t" / h / "data.txt").is_file()
    state = tmp_path / ".datamanifest" / "state.toml"
    assert state.is_file()
    idx = CachedIndex.read(str(state))
    assert idx.has_instance(cachetype="proj.t", version="", hash=h)
    # Recorded portably (relative to the project root), as before.
    sp = idx.instance_path_of(cachetype="proj.t", version="", hash=h)
    assert sp and not os.path.isabs(sp)


def test_bare_cached_without_manifest_ambient_fallback(tmp_path, monkeypatch):
    # No manifest anywhere up the tree: the default database is not
    # constructible (RuntimeError) — that must not leak into caching, which
    # falls back to the ambient derivation.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError):
        get_default_database()

    calls = {"n": 0}

    @cached(cachetype="nomanifest.t", format="txt")
    def produce(*, a):
        calls["n"] += 1
        return f"v{a}"

    assert produce(a=1) == "v1"
    assert produce(a=1) == "v1"
    assert calls["n"] == 1
    # Ambient layout: the per-project default cache keyed by the cwd basename.
    h = param_hash({"a": 1})
    root = (Path(os.environ["XDG_CACHE_HOME"]) / "datamanifest" / "projects"
            / tmp_path.name / "cached")
    assert (root / "nomanifest.t" / h / "data.txt").is_file()


def test_database_storage_config_kwarg_overrides_manifest(tmp_path):
    # The constructor's storage_config dict is the manifest-layer override —
    # it wins over the manifest's own [_STORAGE] keys and is never written back.
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_STORAGE]\nproject = "committed"\n')
    db = Database(datasets_toml=str(toml), storage_config={"project": "override"})
    assert storage.config_value(
        "project", storage_config=db.storage_config) == "override"
    assert db.extra.get("_STORAGE", {}).get("project") == "committed"
