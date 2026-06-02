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
from datamanifest.database import Database, resolve_fetcher

SELF_LANG = "python"
SUPPORTED_CAPABILITIES = {"lang-read", "lang-write", "shell-fetch"}

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
    """Walk the v1 load ladder and return (rung, ref) per the spec's expected JSON schema."""
    own = entry.lang_python_loader or entry.loader
    if own:
        return "per-dataset", own
    fmt = (entry.format or "").strip().lower()
    if fmt:
        for name, ref in db.lang_python_loaders.items():
            if str(name).strip().lower() == fmt:
                return "manifest-format-default", ref
    if fmt:
        try:
            default_loaders.default_loader(fmt)
            return "built-in", None
        except (ValueError, KeyError):
            pass
    return "error", None


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
    ordered: dict = {}
    if "_LOADERS" in data:
        ordered["_LOADERS"] = data["_LOADERS"]
    for key in sorted(k for k in data if k != "_LOADERS"):
        ordered[key] = data[key]
    buf = io.BytesIO()
    tomli_w.dump(ordered, buf)
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
