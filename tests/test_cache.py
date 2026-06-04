"""Tests for the Layer 1b produce-or-load cache layer (Phase 1).

Offline only: a ``tmp_path`` cache root (via ``DATAMANIFEST_CACHE_DIR``) and the
trivial ``txt`` / ``json`` formats.
"""

import json
import os
import sys

import pytest

from datamanifest.cache import (
    cached,
    config_is_valid,
    key_table_from_kwargs,
    param_hash,
    read_config,
    read_metadata,
)
from datamanifest.cache._sidecars import config_key_table


# ----- param_hash: reference vector + type rules -----------------------------

def test_param_hash_reference_vector():
    # Normative cross-language reference vector (SCHEMA "Parameter-hash keying").
    h = param_hash({"grid": "5x5", "skip_models": ["CESM.*", "FGOALS.*"]})
    assert h == "83425a30d111562d46c1fce9de7618ea7f1f54e1be72e086cba0ac63c6f2ce9b"


def test_param_hash_key_order_insensitive():
    a = param_hash({"grid": "5x5", "skip_models": ["CESM.*", "FGOALS.*"]})
    b = param_hash({"skip_models": ["CESM.*", "FGOALS.*"], "grid": "5x5"})
    assert a == b


def test_param_hash_accepts_str_int_bool_nested():
    # strings / ints / bools / arrays / objects-of-those are allowed.
    param_hash({"s": "x", "i": 3, "b": True, "arr": [1, 2], "obj": {"k": "v"}})


def test_param_hash_accepts_finite_float():
    # finite floats are valid hash inputs and hash deterministically.
    assert param_hash({"x": 1.5}) == param_hash({"x": 1.5})
    # a float and an int that render differently are distinct keys.
    assert param_hash({"x": 1.0}) != param_hash({"x": 1})
    param_hash({"nested": {"a": [1, 2.0]}})  # finite floats anywhere are fine


def test_param_hash_rejects_nonfinite_float():
    import math

    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            param_hash({"x": bad})
    with pytest.raises(ValueError):
        param_hash({"nested": [1, math.inf]})


def test_param_hash_rejects_none():
    with pytest.raises(ValueError):
        param_hash({"x": None})
    with pytest.raises(ValueError):
        param_hash({"nested": [1, None]})


# ----- key_table_from_kwargs -------------------------------------------------

def test_key_table_drops_underscore_prefixed():
    table = key_table_from_kwargs(
        {"grid": "5x5", "_parallel": True, "_verbose": False, "n": 3}
    )
    assert table == {"grid": "5x5", "n": 3}


# ----- @cached round-trip ----------------------------------------------------

@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setenv("DATAMANIFEST_CACHE_DIR", str(root))
    # Isolate Phase-2 side effects of a produce: the depot usage log and the
    # cwd-fallback cached.toml a bare @cached (no project_root) registers into.
    monkeypatch.setenv("DATAMANIFEST_USAGE_LOG", str(tmp_path / "usage.toml"))
    monkeypatch.chdir(tmp_path)
    return root


@pytest.fixture
def scope_base(cache_root):
    """The spec-v3 project-scoped base a bare ``@cached`` (no ``project_root``)
    materializes under: ``<cache>/cached/<project-id>/``. The project-id of a
    bare produce is the path-hash of the cwd (here ``tmp_path``, chdir'd into by
    the ``cache_root`` fixture)."""
    from datamanifest.store.locations import project_id

    return cache_root / "cached" / project_id("")


def test_cached_rejects_positional_args(cache_root):
    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    with pytest.raises(TypeError):
        produce("positional")


def test_cached_round_trip_miss_then_hit(cache_root, scope_base):
    calls = {"n": 0}

    @cached(cachetype="greeting", format="txt")
    def produce(*, name):
        calls["n"] += 1
        return f"hello {name}"

    # First call: miss -> function runs, artifact + sidecars written.
    result1 = produce(name="world")
    assert result1 == "hello world"
    assert calls["n"] == 1

    h = param_hash({"name": "world"})
    artifact_dir = scope_base / "greeting" / h
    assert (artifact_dir / "data.txt").read_text() == "hello world"
    assert (artifact_dir / "config.toml").exists()
    assert (artifact_dir / "metadata.toml").exists()
    assert (artifact_dir / ".complete").exists()

    # config.toml: key table + [_META] with cachetype/hash; recomputes valid.
    config = read_config(str(artifact_dir))
    assert config_key_table(config) == {"name": "world"}
    assert config["_META"]["cachetype"] == "greeting"
    assert config["_META"]["hash"] == h
    assert config["_META"]["schema"] == 1
    assert config_is_valid(str(artifact_dir))

    # metadata.toml stamped once.
    meta_before = read_metadata(str(artifact_dir))
    assert meta_before["_META"]["schema"] == 1
    assert meta_before["tool"].startswith("datamanifestpy ")
    meta_mtime = (artifact_dir / "metadata.toml").stat().st_mtime_ns

    # Second call: hit -> function body NOT re-run, loaded value returned.
    result2 = produce(name="world")
    assert result2 == "hello world"
    assert calls["n"] == 1  # body did not run again

    # metadata.toml not re-stamped on a hit (write-if-absent).
    assert (artifact_dir / "metadata.toml").stat().st_mtime_ns == meta_mtime
    meta_after = read_metadata(str(artifact_dir))
    assert meta_after == meta_before


def test_cached_distinct_params_distinct_artifacts(cache_root, scope_base):
    @cached(cachetype="g", format="txt")
    def produce(*, name):
        return f"hi {name}"

    produce(name="a")
    produce(name="b")
    assert (scope_base / "g" / param_hash({"name": "a"}) / "data.txt").exists()
    assert (scope_base / "g" / param_hash({"name": "b"}) / "data.txt").exists()


def test_cached_underscore_kwargs_excluded_from_hash(cache_root, scope_base):
    seen = {}

    @cached(cachetype="g", format="json")
    def produce(*, name, _debug=False):
        seen["debug"] = _debug
        return {"name": name}

    produce(name="x", _debug=True)
    # The hash key ignores _debug; the artifact lands at hash({"name":"x"}).
    h = param_hash({"name": "x"})
    artifact = scope_base / "g" / h / "data.json"
    assert artifact.exists()
    with open(artifact) as fh:
        assert json.load(fh) == {"name": "x"}
    assert seen["debug"] is True  # but the body still saw the runtime knob


def test_cached_escape_hatch_forces_recompute(cache_root, scope_base):
    calls = {"n": 0}

    @cached(cachetype="g", format="txt")
    def produce(*, name):
        calls["n"] += 1
        return f"{name}-{calls['n']}"

    first = produce(name="z")
    assert first == "z-1"
    # cached=False forces recompute even though a valid hit exists.
    again = produce(name="z", cached=False)
    assert again == "z-2"
    assert calls["n"] == 2
    # The re-materialized artifact reflects the recompute.
    h = param_hash({"name": "z"})
    assert (scope_base / "g" / h / "data.txt").read_text() == "z-2"


def test_cached_key_selector_narrows_table(cache_root, scope_base):
    @cached(cachetype="g", format="txt", key=["name"])
    def produce(*, name, region):
        return f"{name}/{region}"

    produce(name="n", region="r1")
    # Only `name` is hashed; the artifact dir keys on {"name": "n"} alone.
    h = param_hash({"name": "n"})
    artifact_dir = scope_base / "g" / h
    assert (artifact_dir / "data.txt").exists()
    assert config_key_table(read_config(str(artifact_dir))) == {"name": "n"}


# ----- spec-v3 path composition: cached/ prefix + project-id scope -----------

def test_cached_produce_lands_under_cached_project_scope(cache_root, scope_base):
    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    produce(name="v")
    h = param_hash({"name": "v"})
    # <cache>/cached/<project-id>/t/<hash>/data.txt
    assert (scope_base / "t" / h / "data.txt").read_text() == "v"
    # the cached/ prefix and the project-id scope are real path segments
    assert scope_base.parent.name == "cached"
    assert scope_base.parent.parent == cache_root


# ----- default format: pickle ------------------------------------------------

def test_cached_default_format_is_pickle(cache_root, scope_base):
    """A format-less @cached self-saves with pickle, so any picklable value
    (here a bare int — the original failing case) round-trips."""
    calls = {"n": 0}

    @cached(cachetype="memory")
    def produce(*, name):
        calls["n"] += 1
        return {"n": 42, "items": [1, 2, 3], "label": name}

    first = produce(name="x")
    assert first == {"n": 42, "items": [1, 2, 3], "label": "x"}
    assert calls["n"] == 1

    h = param_hash({"name": "x"})
    artifact_dir = scope_base / "memory" / h
    # The serialized value is a pickle next to the sidecars.
    assert (artifact_dir / "data.pickle").exists()
    config = read_config(str(artifact_dir))
    assert config["_META"]["cachetype"] == "memory"

    # Second call hits the cache and unpickles the value (body not re-run).
    second = produce(name="x")
    assert second == first
    assert calls["n"] == 1


def test_cached_bare_value_round_trips(cache_root, scope_base):
    @cached(cachetype="memory")
    def produce():
        return 42

    assert produce() == 42
    assert produce() == 42  # reload via pickle


# ----- default cachetype = fully-qualified importable name -------------------

def test_default_cachetype_is_qualified_name():
    """With no explicit cachetype, it defaults to the function's module.qualname."""
    @cached(format="txt")
    def produce(*, x):
        return x

    ct = produce.recipe.cachetype
    assert ct.endswith(".produce")
    assert produce.__module__ in ct  # the module path is the prefix


def test_explicit_cachetype_overrides_default():
    @cached(cachetype="my.semantic.name", format="txt")
    def produce(*, x):
        return x

    assert produce.recipe.cachetype == "my.semantic.name"


def test_bare_cached_decorator_uses_all_defaults():
    @cached  # no parentheses
    def produce(*, x):
        return x

    assert produce.recipe.format == "pickle"
    assert produce.recipe.cachetype.endswith(".produce")


def test_main_with_spec_resolves_to_module_name(monkeypatch):
    """A __main__ function launched via `python -m pkg.mod` resolves to
    pkg.mod.<qualname> via __main__.__spec__.name."""
    import types

    from datamanifest.cache._decorator import _resolve_cachetype

    def fn(*, x):
        return x
    fn.__module__ = "__main__"
    fn.__qualname__ = "produce"

    fake_main = types.SimpleNamespace(__spec__=types.SimpleNamespace(name="pkg.mod"))
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    assert _resolve_cachetype(fn, None) == "pkg.mod.produce"


def test_main_without_spec_requires_explicit_cachetype(monkeypatch):
    """A __main__ function with no __spec__ (loose script / REPL / notebook) has
    no importable identity, so an explicit cachetype is required."""
    import types

    from datamanifest.cache._decorator import _resolve_cachetype

    def fn(*, x):
        return x
    fn.__module__ = "__main__"
    fn.__qualname__ = "produce"

    fake_main = types.SimpleNamespace(__spec__=None)
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    with pytest.raises(ValueError, match="explicit cachetype"):
        _resolve_cachetype(fn, None)
    # An explicit cachetype sidesteps the requirement.
    assert _resolve_cachetype(fn, "calibration") == "calibration"


# ----- decoration-time recipe registry + conflict detection ------------------

# A module-level @cached participates in the in-process registry; locals do not.
@cached(cachetype="_module_level_fixture_ct", format="txt")
def _module_level_recipe(*, x):
    return x


def test_registered_recipes_lists_module_level():
    from datamanifest.cache import registered_recipes

    refs = [r.ref for r in registered_recipes()]
    assert any(r.endswith(":_module_level_recipe") for r in refs)
    assert _module_level_recipe.recipe.cachetype == "_module_level_fixture_ct"


def test_local_cached_has_recipe_but_is_unregistered():
    from datamanifest.cache import registered_recipes

    @cached(cachetype="local_only_ct", format="txt")
    def produce(*, x):
        return x

    # .recipe is attached for introspection, but a local fn is not registered
    # (and so is exempt from the conflict check).
    assert produce.recipe.cachetype == "local_only_ct"
    assert produce.recipe not in registered_recipes()


@pytest.fixture
def clean_registry():
    """Snapshot/restore the process-global recipe registry around a test."""
    from datamanifest.cache import _decorator as d

    recipes, owners = dict(d._RECIPES), dict(d._CACHETYPE_OWNERS)
    yield
    d._RECIPES.clear(); d._RECIPES.update(recipes)
    d._CACHETYPE_OWNERS.clear(); d._CACHETYPE_OWNERS.update(owners)


def _decorate_as(module, qualname, *, cachetype, version=""):
    """Decorate a synthetic module-level function (given module/qualname) so the
    registry/conflict path treats it as non-local."""
    def raw(*, x):
        return x
    raw.__module__ = module
    raw.__qualname__ = qualname
    return cached(cachetype=cachetype, version=version, format="txt")(raw)


def test_cachetype_conflict_raises_for_distinct_functions(clean_registry):
    from datamanifest.cache import CacheTypeConflict

    _decorate_as("pkga.mod", "produce", cachetype="shared_ct")
    with pytest.raises(CacheTypeConflict):
        _decorate_as("pkgb.mod", "produce", cachetype="shared_ct")


def test_cachetype_conflict_allows_different_version(clean_registry):
    # Same cachetype, different version, two functions — valid, must coexist.
    _decorate_as("pkga.mod", "calib_v1", cachetype="calibration", version="1")
    _decorate_as("pkgb.mod", "calib_v2", cachetype="calibration", version="2")


def test_cachetype_redecoration_same_ref_is_not_a_conflict(clean_registry):
    _decorate_as("pkga.mod", "produce", cachetype="ct_x")
    # Same ref (module:qualname) re-decorating overwrites, not a conflict.
    _decorate_as("pkga.mod", "produce", cachetype="ct_x")


# ----- hit requires the data file on disk ------------------------------------

def test_cached_recomputes_when_data_file_absent(cache_root, scope_base):
    """A complete, hash-valid artifact whose data file for *this* format is
    missing is not a hit — the recipe recomputes instead of failing to read.
    Guards the collision case where two recipes share a cachetype + hash."""
    calls = {"n": 0}

    @cached(cachetype="g", format="txt")
    def produce(*, name):
        calls["n"] += 1
        return f"v{calls['n']}"

    assert produce(name="a") == "v1"
    h = param_hash({"name": "a"})
    artifact = scope_base / "g" / h
    assert (artifact / "data.txt").exists()

    # Drop the data file but leave the .complete marker + valid config.toml.
    (artifact / "data.txt").unlink()
    assert (artifact / ".complete").exists()
    assert config_is_valid(str(artifact))

    # Not a hit (data absent) -> recompute, no FileNotFoundError.
    assert produce(name="a") == "v2"
    assert calls["n"] == 2
    assert (artifact / "data.txt").read_text() == "v2"


# ----- registry self-heals on hit --------------------------------------------

def test_cached_hit_reregisters_when_index_deleted(cache_root, scope_base):
    """Deleting cached.toml by hand does not lose the registration: the next
    cache hit re-adds the entry, so the index rebuilds itself by re-running."""
    from datamanifest.cache import CachedIndex

    @cached(cachetype="g", format="txt")
    def produce(*, name):
        return f"hi {name}"

    produce(name="a")
    index_path = os.path.join(os.getcwd(), "cached.toml")
    assert CachedIndex.read(index_path).scoped_keys()

    # Delete the index by hand; the artifact itself stays on disk.
    os.remove(index_path)
    h = param_hash({"name": "a"})
    assert (scope_base / "g" / h / "data.txt").exists()

    # A hit (artifact present + valid) self-heals the registry.
    assert produce(name="a") == "hi a"
    cts = {r["cachetype"] for r in CachedIndex.read(index_path).recipe_records()}
    assert "g" in cts


def test_cached_hit_does_not_rewrite_index_when_present(cache_root, scope_base):
    """A hit whose entry is already registered does not rewrite the index."""
    @cached(cachetype="g", format="txt")
    def produce(*, name):
        return name

    produce(name="a")
    index_path = os.path.join(os.getcwd(), "cached.toml")
    mtime = os.stat(index_path).st_mtime_ns

    produce(name="a")  # hit; entry already present -> no rewrite
    assert os.stat(index_path).st_mtime_ns == mtime


# ----- spec-v3 scope field ---------------------------------------------------

def test_cached_registers_scope_and_params(cache_root, scope_base):
    """The cached.toml recipe records the project ``scope`` and each variation's
    ``params`` (the kwargs it was produced with)."""
    from datamanifest.cache import CachedIndex
    from datamanifest.store.locations import project_id

    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    produce(name="v")
    index = CachedIndex.read(os.path.join(os.getcwd(), "cached.toml"))
    rec = {r["cachetype"]: r for r in index.recipe_records()}["t"]
    assert rec["scope"] == project_id("")
    assert list(rec["instances"].values()) == [{"name": "v"}]


def test_cached_discovers_project_root_for_scope(cache_root, tmp_path, monkeypatch):
    """When no project_root is passed, it is discovered by walking up for a
    pyproject.toml, so the scope resolves to ``[project].name`` instead of a
    path hash."""
    from datamanifest.cache import CachedIndex

    proj = tmp_path / "proj"
    sub = proj / "deep" / "nested"
    sub.mkdir(parents=True)
    (proj / "pyproject.toml").write_text('[project]\nname = "myproj"\n')
    monkeypatch.chdir(sub)

    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    produce(name="v")
    h = param_hash({"name": "v"})
    # Scope segment in the on-disk path is the discovered project name.
    assert (cache_root / "cached" / "myproj" / "t" / h / "data.txt").exists()
    # cached.toml lands at the discovered project root and records scope=myproj.
    index = CachedIndex.read(str(proj / "cached.toml"))
    assert {r["scope"] for r in index.recipe_records()} == {"myproj"}


def test_cached_scope_override_param(cache_root, scope_base):
    """An explicit @cached(scope=...) drives BOTH the on-disk path and the
    recorded entry — they cannot diverge (the highest-priority scope)."""
    from datamanifest.cache import CachedIndex

    @cached(cachetype="t", format="txt", scope="shared")
    def produce(*, name):
        return name

    produce(name="v")
    h = param_hash({"name": "v"})
    # Path lands under the explicit scope.
    assert (cache_root / "cached" / "shared" / "t" / h / "data.txt").exists()
    # Entry records the SAME scope (path and entry agree).
    index = CachedIndex.read(os.path.join(os.getcwd(), "cached.toml"))
    assert {r["scope"] for r in index.recipe_records()} == {"shared"}
    assert index.has_instance(scope="shared", cachetype="t", version="", hash=h)


def test_cached_scope_env_override_path_and_entry_agree(cache_root, scope_base,
                                                        monkeypatch):
    """A DATAMANIFEST_SCOPE_CACHED override reaches the entry too (the bug:
    previously only the path honored it, the entry kept the project id)."""
    from datamanifest.cache import CachedIndex

    monkeypatch.setenv("DATAMANIFEST_SCOPE_CACHED", "envscope")

    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    produce(name="v")
    h = param_hash({"name": "v"})
    assert (cache_root / "cached" / "envscope" / "t" / h / "data.txt").exists()
    index = CachedIndex.read(os.path.join(os.getcwd(), "cached.toml"))
    # The recorded scope matches the path's scope — reachability stays consistent.
    assert index.has_instance(scope="envscope", cachetype="t", version="", hash=h)


# ----- spec-v3 recipe version ------------------------------------------------

def test_cached_version_adds_path_segment_not_in_hash(cache_root, scope_base):
    @cached(cachetype="t", format="txt", version="v3")
    def produce(*, name):
        return name

    produce(name="w")
    # version does not change the param hash ...
    h = param_hash({"name": "w"})
    # ... it inserts a <cachetype>/<version>/<hash> segment.
    artifact_dir = scope_base / "t" / "v3" / h
    assert (artifact_dir / "data.txt").read_text() == "w"
    # version is recorded in config.toml's [_META] (never in the key table).
    config = read_config(str(artifact_dir))
    assert config["_META"]["version"] == "v3"
    assert config_key_table(config) == {"name": "w"}
    assert config_is_valid(str(artifact_dir))


def test_cached_version_same_hash_as_unversioned(cache_root, scope_base):
    @cached(cachetype="t", format="txt")
    def plain(*, name):
        return name

    @cached(cachetype="t", format="txt", version="v9")
    def versioned(*, name):
        return name

    plain(name="k")
    versioned(name="k")
    h = param_hash({"name": "k"})
    # Same kwargs hash to the same <hash> with or without a version; the version
    # only differs as a path segment.
    assert (scope_base / "t" / h / "data.txt").exists()
    assert (scope_base / "t" / "v9" / h / "data.txt").exists()


# ----- spec-v3 explicit per-call cache_dir bypass ----------------------------

def test_cached_cache_dir_is_verbatim(cache_root, tmp_path):
    @cached(cachetype="t", format="txt", version="v2")
    def produce(*, name):
        return name

    explicit = tmp_path / "explicit"
    produce(name="q", cache_dir=str(explicit))
    h = param_hash({"name": "q"})
    # <cache_dir>/<cachetype>/[<version>/]<hash> — no cached/ prefix, no scope.
    assert (explicit / "t" / "v2" / h / "data.txt").read_text() == "q"
    # nothing landed under the composed $cache root.
    assert not (cache_root / "cached").exists()


# ----- invalidation: a stale/corrupt sidecar is not a hit --------------------

def test_cached_recomputes_on_invalid_config(cache_root, scope_base):
    import tomli_w

    calls = {"n": 0}

    @cached(cachetype="g", format="txt")
    def produce(*, name):
        calls["n"] += 1
        return f"{name}-{calls['n']}"

    produce(name="q")
    assert calls["n"] == 1
    h = param_hash({"name": "q"})
    artifact_dir = scope_base / "g" / h
    assert config_is_valid(str(artifact_dir))

    # Tamper the recorded hash so config_is_valid() no longer matches the key
    # table (a stale artifact from a different code/branch, or a corrupt write).
    config = read_config(str(artifact_dir))
    config["_META"]["hash"] = "0" * 64
    with open(artifact_dir / "config.toml", "wb") as f:
        tomli_w.dump(config, f)
    assert not config_is_valid(str(artifact_dir))

    # The invalid sidecar must NOT count as a hit: the body re-runs and the
    # artifact is rewritten with a valid sidecar.
    again = produce(name="q")
    assert calls["n"] == 2
    assert again == "q-2"
    assert config_is_valid(str(artifact_dir))


# ----- real-format round-trips (writer <-> loader ladder), optional deps -----

def test_cached_round_trip_csv(cache_root, scope_base):
    pd = pytest.importorskip("pandas")
    from pandas.testing import assert_frame_equal

    @cached(cachetype="frame", format="csv")
    def produce(*, n):
        return pd.DataFrame({"a": list(range(n)), "b": ["x"] * n})

    df_miss = produce(n=3)          # miss: returns the produced frame, writes CSV
    df_hit = produce(n=3)           # hit: loads via pandas.read_csv(comment="#")
    assert_frame_equal(df_miss, df_hit)
    assert (scope_base / "frame" / param_hash({"n": 3}) / "data.csv").exists()


def test_cached_round_trip_nc(cache_root, scope_base):
    xr = pytest.importorskip("xarray")
    pytest.importorskip("netCDF4")
    import numpy as np

    @cached(cachetype="grid", format="nc")
    def produce(*, n):
        return xr.Dataset({"t": ("x", np.arange(n, dtype="float64"))})

    produce(n=4)                    # miss: writes NetCDF
    ds_hit = produce(n=4)           # hit: xarray.open_dataset
    try:
        assert list(ds_hit["t"].values) == [0.0, 1.0, 2.0, 3.0]
    finally:
        ds_hit.close()
    assert (scope_base / "grid" / param_hash({"n": 4}) / "data.nc").exists()
