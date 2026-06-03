"""Tests for the Layer 1b produce-or-load cache layer (Phase 1).

Offline only: a ``tmp_path`` cache root (via ``DATAMANIFEST_CACHE_DIR``) and the
trivial ``txt`` / ``json`` formats.
"""

import json
import os

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
