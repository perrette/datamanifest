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


def test_param_hash_rejects_float():
    with pytest.raises(ValueError):
        param_hash({"x": 1.5})
    with pytest.raises(ValueError):
        param_hash({"nested": {"a": [1, 2.0]}})


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


def test_cached_rejects_positional_args(cache_root):
    @cached(cachetype="t", format="txt")
    def produce(*, name):
        return name

    with pytest.raises(TypeError):
        produce("positional")


def test_cached_round_trip_miss_then_hit(cache_root):
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
    artifact_dir = cache_root / "greeting" / h
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


def test_cached_distinct_params_distinct_artifacts(cache_root):
    @cached(cachetype="g", format="txt")
    def produce(*, name):
        return f"hi {name}"

    produce(name="a")
    produce(name="b")
    assert (cache_root / "g" / param_hash({"name": "a"}) / "data.txt").exists()
    assert (cache_root / "g" / param_hash({"name": "b"}) / "data.txt").exists()


def test_cached_underscore_kwargs_excluded_from_hash(cache_root):
    seen = {}

    @cached(cachetype="g", format="json")
    def produce(*, name, _debug=False):
        seen["debug"] = _debug
        return {"name": name}

    produce(name="x", _debug=True)
    # The hash key ignores _debug; the artifact lands at hash({"name":"x"}).
    h = param_hash({"name": "x"})
    artifact = cache_root / "g" / h / "data.json"
    assert artifact.exists()
    with open(artifact) as fh:
        assert json.load(fh) == {"name": "x"}
    assert seen["debug"] is True  # but the body still saw the runtime knob


def test_cached_escape_hatch_forces_recompute(cache_root):
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
    assert (cache_root / "g" / h / "data.txt").read_text() == "z-2"


def test_cached_key_selector_narrows_table(cache_root):
    @cached(cachetype="g", format="txt", key=["name"])
    def produce(*, name, region):
        return f"{name}/{region}"

    produce(name="n", region="r1")
    # Only `name` is hashed; the artifact dir keys on {"name": "n"} alone.
    h = param_hash({"name": "n"})
    artifact_dir = cache_root / "g" / h
    assert (artifact_dir / "data.txt").exists()
    assert config_key_table(read_config(str(artifact_dir))) == {"name": "n"}
