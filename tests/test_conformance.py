"""
Conformance test suite against the pinned datamanifest.toml spec fixtures.

Source of truth: https://github.com/perrette/datamanifest.toml
Conformance claim: spec_tag and per-file SHA-256 content hashes in
tests/conformance_pin.toml. The tag + hash pin is the machine-checkable record
of which spec version and fixture set this tool is conformant with.

Only fixtures whose ``capabilities`` array is a subset of SUPPORTED_CAPABILITIES
are executed; others are skipped with a reason.
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

import pytest
import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from datamanifest import default_loaders
from datamanifest.cache import CachedIndex, config_key_table, param_hash
from datamanifest.database import (
    Database,
    resolve_fetcher,
    resolve_loader_rungs,
    _sort_recursive,
)
from datamanifest.store.locations import PREDEFINED_SYMBOLS

SELF_LANG = "python"
# Capabilities this tool implements (see docs/conformance.md); drives which
# fixtures are run. Fixtures whose `capabilities` are not a subset of this set
# are skipped with a reason.
SUPPORTED_CAPABILITIES = {
    "lang-read",
    "lang-write",
    "shell-fetch",
    "storage",
    "binding-args",
    "byte-identity",
    "cache-produce",  # @cached produce-or-load layer (datamanifest.cache)
    "inspect",        # state-file inventory + `datamanifest list` maintenance
    "delegation",     # cross-language fetch rung via the Julia peer tool
    "sync",           # cross-machine push/pull (no fixture exercises it yet)
}

_HERE = Path(__file__).parent
_PIN_FILE = _HERE / "conformance_pin.toml"
_CACHE_DIR = _HERE / ".conformance_cache"


def _load_pin():
    with open(_PIN_FILE, "rb") as f:
        return tomllib.load(f)


def _fixture_names():
    """Derive fixture base names from the pin file (names of .expected.json entries)."""
    pin = _load_pin()
    names = []
    for rel in pin["files"]:
        if rel.endswith(".expected.json"):
            base = os.path.basename(rel)
            names.append(base[: -len(".expected.json")])
    return sorted(names)


@pytest.fixture(scope="session")
def fixtures_dir():
    """Download (once), cache, and integrity-verify the pinned spec fixtures.

    Fails loudly on missing files, hash mismatches, or unexpected extra files —
    no silent fallback. Download is cached so reruns are offline.
    """
    pin = _load_pin()
    spec_tag = pin["spec_tag"]
    tarball_url = (
        "https://github.com/perrette/datamanifest.toml/archive/refs/tags/"
        f"{spec_tag}.tar.gz"
    )
    _CACHE_DIR.mkdir(exist_ok=True)
    cache_tarball = _CACHE_DIR / f"{spec_tag}.tar.gz"

    if not cache_tarball.exists():
        try:
            urllib.request.urlretrieve(tarball_url, cache_tarball)
        except Exception as exc:
            pytest.fail(
                f"Could not download spec tarball from {tarball_url}: {exc}\n"
                "Ensure the tag is pushed to GitHub and the network is reachable."
            )

    # GitHub auto-archives extract to "<repo>-<tag>/"
    extract_root = _CACHE_DIR / f"datamanifest.toml-{spec_tag}"
    if not extract_root.exists():
        with tarfile.open(cache_tarball, "r:gz") as tf:
            tf.extractall(_CACHE_DIR)

    # Verify every pinned file against its recorded per-file SHA-256.
    expected_files = pin["files"]
    fixtures_path = extract_root / "tests" / "fixtures"
    for rel, expected_hash in expected_files.items():
        abs_path = extract_root / rel
        if not abs_path.exists():
            pytest.fail(f"Pinned file missing from downloaded tarball: {rel}")
        actual_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            pytest.fail(
                f"SHA-256 mismatch for {rel}\n"
                f"  expected: {expected_hash}\n"
                f"  actual:   {actual_hash}"
            )

    # Fail on extra fixture files not recorded in the pin.
    for f in fixtures_path.iterdir():
        rel = f"tests/fixtures/{f.name}"
        if rel not in expected_files:
            pytest.fail(f"Extra file in fixtures not recorded in pin: {rel}")

    return fixtures_path


def _fetch_rung_ref(entry):
    """Map resolve_fetcher output to (rung, ref) per the spec's expected JSON schema."""
    kind, value = resolve_fetcher(entry)
    if kind == "python":
        return "own-fetcher", value
    if kind == "shell":
        return "shell", value
    if kind == "uri":
        return "uri", None
    return "error", None


def _load_rung_ref(db, entry):
    """Walk the v1 load ladder and return (rung, ref) per the spec's expected JSON schema.

    Rung 1 (own per-dataset loader: explicit _LANG.python.loader or bare loader)
    and rung 2 (manifest format default: [_LANG.python.loaders] or [_LOADERS])
    are both expressed by resolve_loader_rungs; the first own-language rung is a
    "per-dataset" loader and a format-default rung is "manifest-format-default".
    Rung 3 is the built-in default loader for the format.
    """
    rungs = resolve_loader_rungs(db, entry)
    fmt = (entry.format or "").strip().lower()
    if rungs:
        ref = rungs[0][0]
        # The first rung is a per-dataset loader when the dataset declares its
        # own (explicit _LANG.python.loader or bare loader); otherwise it is a
        # manifest format default ([_LANG.python.loaders] / [_LOADERS]).
        if entry.lang_python_loader or entry.loader:
            return "per-dataset", ref
        return "manifest-format-default", ref
    if fmt:
        try:
            default_loaders.default_loader(fmt)
            return "built-in", None
        except (ValueError, KeyError):
            pass
    return "error", None


def _assert_keys_sorted(obj, path=""):
    """Recursively assert every dict level has keys in Unicode code-point order."""
    if isinstance(obj, dict):
        keys = list(obj.keys())
        assert keys == sorted(keys), (
            f"keys not sorted at {path or '<root>'}: {keys}"
        )
        for k, v in obj.items():
            _assert_keys_sorted(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_keys_sorted(v, f"{path}[{i}]")


@pytest.mark.parametrize("fixture_name", _fixture_names())
def test_conformance(fixture_name, fixtures_dir):
    toml_path = fixtures_dir / f"{fixture_name}.toml"
    expected_path = fixtures_dir / f"{fixture_name}.expected.json"

    with open(expected_path) as f:
        expected = json.load(f)

    caps = set(expected.get("capabilities", []))
    missing = caps - SUPPORTED_CAPABILITIES
    if missing:
        pytest.skip(f"Fixture requires unsupported capabilities: {sorted(missing)}")

    # --- cache-produce: the fixture .toml is a config.toml sidecar, NOT a
    # datasets.toml. Recompute the param hash from the key table (every root
    # key except [_META]) and check it equals both the recorded _META.hash
    # (the re-hashability contract) and the fixture's expected hash + key.
    cs = expected.get("config_sidecar")
    if cs is not None:
        with open(toml_path, "rb") as f:
            cfg = tomllib.load(f)
        meta = cfg["_META"]
        key_table = config_key_table(cfg)
        ph = param_hash(key_table)
        assert ph == cs["param_hash"], (
            f"{fixture_name}: param_hash {ph} != expected {cs['param_hash']}"
        )
        assert meta["hash"] == ph, (
            f"{fixture_name}: sidecar _META.hash {meta['hash']} is not "
            f"re-hashable to {ph}"
        )
        assert meta["cachetype"] == cs["cachetype"]
        assert f"{meta['cachetype']}/{ph}" == cs["key"]
        assert key_table == cs["key_table"]
        return

    # --- inspect: the fixture .toml is a cached.toml produced-dataset index
    # (legacy schema-2 nested form, still read), NOT a datasets.toml. Check each
    # recipe's identity + fields + per-variation instances; the index is
    # self-verifying (each instance hash == the param-hash of its params).
    ci = expected.get("cached_index")
    if ci is not None:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["_META"]["schema"] == ci.get("schema", 2)
        # forbidden_keys: a negative writer-contract assertion against _META
        # and every recipe (pins removed/renamed keys, e.g. project/scope).
        forbidden = set(ci.get("forbidden_keys", []))
        if forbidden:
            assert not forbidden & set(raw["_META"]), (
                f"{fixture_name}: forbidden keys in _META: "
                f"{sorted(forbidden & set(raw['_META']))}"
            )
            for rrec in raw.get("produced", []):
                assert not forbidden & set(rrec), (
                    f"{fixture_name}: forbidden keys in recipe: "
                    f"{sorted(forbidden & set(rrec))}"
                )
        idx = CachedIndex.from_dict(raw)
        exp_keys = set()
        exp_reachable = set()
        for er in ci["recipes"]:
            ct, ver = er["cachetype"], er.get("version", "")
            assert (ct, ver) in idx.recipes, (
                f"{fixture_name}: recipe ({ct!r}, {ver!r}) not read from index"
            )
            rec = idx.recipes[(ct, ver)]
            assert rec["ref"] == er.get("ref", "")
            assert rec["format"] == er.get("format", "")
            for inst in er["instances"]:
                h = inst["hash"]
                assert h in rec["instances"], (
                    f"{fixture_name}/{ct}: instance {h} not read from index"
                )
                params = inst.get("params", {})
                assert param_hash(params) == h, (
                    f"{fixture_name}/{ct}: params do not re-hash to {h}"
                )
                exp_keys.add(f"{ct}/{h}")
                exp_reachable.add((ct, ver, h))
        assert idx.keys() == exp_keys
        assert idx.reachable_keys() == exp_reachable
        return

    # Load manifest (persist=False: no write-on-read; skip_checksum: fixtures use
    # placeholder SHA-256 values that don't correspond to real downloaded files)
    db = Database(datasets_toml=str(toml_path), persist=False, skip_checksum=True)

    # --- Resolution tests (Python lang only) ---
    python_resolution = expected.get("resolution", {}).get(SELF_LANG, {})
    for ds_name, expected_res in python_resolution.items():
        entry = db.datasets[ds_name]

        exp_fetch = expected_res["fetcher"]
        act_rung, act_ref = _fetch_rung_ref(entry)
        assert act_rung == exp_fetch["rung"], (
            f"{fixture_name}/{ds_name} fetcher rung: got {act_rung!r}, "
            f"expected {exp_fetch['rung']!r}"
        )
        assert act_ref == exp_fetch["ref"], (
            f"{fixture_name}/{ds_name} fetcher ref: got {act_ref!r}, "
            f"expected {exp_fetch['ref']!r}"
        )

        exp_load = expected_res["loader"]
        act_rung, act_ref = _load_rung_ref(db, entry)
        assert act_rung == exp_load["rung"], (
            f"{fixture_name}/{ds_name} loader rung: got {act_rung!r}, "
            f"expected {exp_load['rung']!r}"
        )
        assert act_ref == exp_load["ref"], (
            f"{fixture_name}/{ds_name} loader ref: got {act_ref!r}, "
            f"expected {exp_load['ref']!r}"
        )

    # --- Verbatim preservation (read → write round-trip) ---
    data = db.to_dict()
    buf = io.BytesIO()
    tomli_w.dump(_sort_recursive(data), buf)
    buf.seek(0)
    written = tomllib.load(buf)

    with open(toml_path, "rb") as f:
        original = tomllib.load(f)

    preserve = expected.get("preserve_verbatim", {})

    # Unknown structural top-level keys (e.g. _FUTURE)
    for key in preserve.get("unknown_structural", []):
        assert key in written, (
            f"{fixture_name}: missing top-level key {key!r} after round-trip"
        )
        assert written[key] == original[key], (
            f"{fixture_name}: {key!r} not preserved verbatim after round-trip"
        )

    # Top-level _LANG.<lang> — foreign only (Python tool owns _LANG.python)
    for lang_key in preserve.get("lang_namespaces", {}).get("top_level", []):
        lang = lang_key.split(".", 1)[1]
        if lang == SELF_LANG:
            continue
        assert "_LANG" in written and lang in written["_LANG"], (
            f"{fixture_name}: missing top-level _LANG.{lang} after round-trip"
        )
        assert written["_LANG"][lang] == original["_LANG"][lang], (
            f"{fixture_name}: _LANG.{lang} not preserved verbatim after round-trip"
        )

    # Per-dataset _LANG.<lang> — foreign only
    per_dataset = preserve.get("lang_namespaces", {}).get("per_dataset", {})
    for ds_name, lang_keys in per_dataset.items():
        for lang_key in lang_keys:
            lang = lang_key.split(".", 1)[1]
            if lang == SELF_LANG:
                continue
            assert ds_name in written, (
                f"{fixture_name}: dataset {ds_name!r} missing after round-trip"
            )
            assert "_LANG" in written[ds_name] and lang in written[ds_name]["_LANG"], (
                f"{fixture_name}/{ds_name}: missing _LANG.{lang} after round-trip"
            )
            assert written[ds_name]["_LANG"][lang] == original[ds_name]["_LANG"][lang], (
                f"{fixture_name}/{ds_name}: _LANG.{lang} not preserved verbatim"
            )

    # --- Parameterized binding args/kwargs (Python lang only) ---
    binding_args = expected.get("binding_args", {}).get(SELF_LANG, {})
    for ds_name, bindings in binding_args.items():
        entry = db.datasets[ds_name]
        for kind, spec in bindings.items():
            if kind == "fetcher":
                act_args = entry.lang_python_fetcher_args
                act_kwargs = entry.lang_python_fetcher_kwargs
            elif kind == "loader":
                act_args = entry.lang_python_loader_args
                act_kwargs = entry.lang_python_loader_kwargs
            else:
                raise AssertionError(
                    f"{fixture_name}/{ds_name}: unknown binding kind {kind!r}"
                )
            if "args" in spec:
                assert act_args == spec["args"], (
                    f"{fixture_name}/{ds_name} {kind} args: got {act_args!r}, "
                    f"expected {spec['args']!r}"
                )
            if "kwargs" in spec:
                assert act_kwargs == spec["kwargs"], (
                    f"{fixture_name}/{ds_name} {kind} kwargs: got {act_kwargs!r}, "
                    f"expected {spec['kwargs']!r}"
                )

    # --- Storage model (spec-v4/v5: the two folder fields, the user-symbol
    # namespace, the _HOST patterns, and per-dataset storage_path expressions).
    # Raw [_STORAGE] values are path expressions; compared verbatim against the
    # manifest layer (not resolved — resolution is machine-dependent, and the
    # layered scoped config could be contaminated by this machine's user config).
    storage = expected.get("storage")
    if storage is not None:
        manifest_storage = db.extra.get("_STORAGE", {})

        # The two folder fields + the project name: raw [_STORAGE] values.
        for fieldname in ("datasets_dir", "datacache_dir", "project"):
            if fieldname in storage:
                assert manifest_storage.get(fieldname, "") == storage[fieldname], (
                    f"{fixture_name}: [_STORAGE].{fieldname} "
                    f"{manifest_storage.get(fieldname, '')!r} != "
                    f"{storage[fieldname]!r}"
                )

        # User-defined symbols: bare [_STORAGE] keys that are neither reserved
        # fields, nor predefined symbols, nor structural _-tables (_HOST).
        reserved = {
            "datasets_dir", "datacache_dir", "datasets_pools",
            "datacache_pools", "project",
        }
        symbols = sorted(
            k for k in manifest_storage
            if not k.startswith("_")
            and k not in reserved
            and k not in PREDEFINED_SYMBOLS
        )
        assert symbols == sorted(storage.get("symbols", [])), (
            f"{fixture_name}: user symbols {symbols} != {storage.get('symbols')}"
        )

        # _HOST glob patterns.
        host_patterns = sorted(manifest_storage.get("_HOST", {}).keys())
        assert host_patterns == sorted(storage.get("host_patterns", [])), (
            f"{fixture_name}: host_patterns {host_patterns} != "
            f"{storage.get('host_patterns')}"
        )

        # Per-dataset storage_path expressions (raw, pre-expansion).
        for ds_name, sp_exp in storage.get("storage_paths", {}).items():
            assert ds_name in db.datasets, (
                f"{fixture_name}: dataset {ds_name!r} missing"
            )
            assert db.datasets[ds_name].storage_path == sp_exp, (
                f"{fixture_name}/{ds_name}: storage_path "
                f"{db.datasets[ds_name].storage_path!r} != {sp_exp!r}"
            )

    # --- Self-consistent byte-identity (serialize → parse → serialize stable) ---
    # The canonical structure fed to the dumper is code-point-sorted at every
    # nesting level (the Item 9 helper); serializing that, re-parsing, and
    # re-serializing must reproduce identical bytes (a fixed point).
    canonical = _sort_recursive(data)
    _assert_keys_sorted(canonical)
    buf1 = io.BytesIO()
    tomli_w.dump(canonical, buf1)
    bytes1 = buf1.getvalue()
    reparsed = tomllib.load(io.BytesIO(bytes1))
    buf2 = io.BytesIO()
    tomli_w.dump(_sort_recursive(reparsed), buf2)
    bytes2 = buf2.getvalue()
    assert bytes1 == bytes2, (
        f"{fixture_name}: serialize→parse→serialize is not byte-stable"
    )
