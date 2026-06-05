"""Importing datasets declared by other tools (``datamanifest import``).

Pooch is parsed straight from its registry-file grammar, so these tests need no
pooch install — a static sample registry lives in ``tests/data/pooch_registry.txt``
and cache-adoption cases build their own files + hashes.
"""

import hashlib
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from datamanifest.cache import CachedIndex
from datamanifest.database import Database
from datamanifest.importers import import_pooch, parse_pooch_registry

_FIXTURE = os.path.join(os.path.dirname(__file__), "data", "pooch_registry.txt")


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


def _project(tmp_path):
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    return Database(datasets_toml=str(toml)), toml


def _manifest(toml):
    with open(toml, "rb") as f:
        return tomllib.load(f)


# ----- registry parsing (grammar) --------------------------------------------

def test_parse_pooch_registry_grammar():
    entries = parse_pooch_registry(_FIXTURE)
    # Four data lines; the two `#` comment lines are skipped.
    assert len(entries) == 4
    by_name = {e[0]: e for e in entries}

    # A bare hash defaults to sha256 (pooch's rule).
    fn, algo, _, url = by_name["gravity-disturbance.nc"]
    assert algo == "sha256" and url == ""
    # An explicit `algo:` prefix is honored.
    assert by_name["santiago.tif"][1] == "md5"
    assert by_name["data/density.csv"][1] == "sha256"
    # The optional third column is a per-file URL.
    assert by_name["special.bin"][3] == "https://mirror.example.org/special.bin"


# ----- declaration (offline) -------------------------------------------------

def test_import_pooch_declares_entries(tmp_path):
    db, toml = _project(tmp_path)
    summary = import_pooch(db, _FIXTURE, base_url="https://data.example.org/v1")
    assert "Imported 4 dataset(s)" in summary
    data = _manifest(toml)

    # base_url + filename (subdir preserved in the URL).
    assert data["density"]["uri"] == "https://data.example.org/v1/data/density.csv"
    # A sha256 hash is carried over verbatim.
    assert data["density"]["sha256"].startswith("aa1122")
    # The third-column URL overrides base_url.
    assert data["special"]["uri"] == "https://mirror.example.org/special.bin"
    # An md5 entry has no cached file → no sha256 invented.
    assert "sha256" not in data["santiago"]


def test_import_pooch_requires_base_url(tmp_path):
    db, _ = _project(tmp_path)
    # The fixture has entries without a URL column → base_url is mandatory.
    try:
        import_pooch(db, _FIXTURE)
    except ValueError as e:
        assert "base URL" in str(e)
    else:
        raise AssertionError("expected a ValueError demanding --base-url")


# ----- cache adoption (the no-re-download path) ------------------------------

def _cache_registry(tmp_path):
    """A cache dir with three files and a matching registry (sha256, sha256:, md5:)."""
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    g = b"gravity\n"
    d = b"density\n"
    m = b"mass\n"
    (cache / "g.nc").write_bytes(g)
    (cache / "sub" / "d.csv").write_bytes(d)
    (cache / "m.bin").write_bytes(m)
    reg = tmp_path / "registry.txt"
    reg.write_text(
        f"g.nc {_sha256(g)}\n"
        f"sub/d.csv sha256:{_sha256(d)}\n"
        f"m.bin md5:{hashlib.md5(m).hexdigest()}\n"
    )
    return cache, reg, {"g": _sha256(g), "d": _sha256(d), "m": _sha256(m)}


def test_import_pooch_adopts_cache(tmp_path):
    db, toml = _project(tmp_path)
    cache, reg, sha = _cache_registry(tmp_path)

    summary = import_pooch(db, reg, base_url="https://data.example.org",
                           cache_dir=str(cache))
    assert "3 adopted from the cache" in summary

    # Each file is recorded in the state file at its actual cache location — so
    # resolution finds it there with no re-download.
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    rec = idx.dataset_path_of("data.example.org/g.nc")
    assert os.path.abspath(os.path.join(tmp_path, rec)) == str(cache / "g.nc")
    assert idx.dataset_path_of("data.example.org/sub/d.csv")

    # The md5 entry's sha256 is computed from the cached file (manifest + state).
    assert _manifest(toml)["m"]["sha256"] == sha["m"]
    assert idx.datasets["data.example.org/m.bin"]["sha256"] == sha["m"]


def test_import_pooch_cache_mismatch_not_adopted(tmp_path):
    db, toml = _project(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "g.nc").write_bytes(b"actual bytes")
    reg = tmp_path / "registry.txt"
    reg.write_text(f"g.nc {_sha256(b'DIFFERENT bytes')}\n")   # hash of other content

    summary = import_pooch(db, reg, base_url="https://h", cache_dir=str(cache))
    assert "checksum mismatch" in summary
    # Declared (the manifest keeps the registry's hash) but NOT adopted.
    assert "g" in _manifest(toml)
    assert not (tmp_path / ".datamanifest-state.toml").exists()


def test_import_pooch_dry_run_writes_nothing(tmp_path):
    db, toml = _project(tmp_path)
    cache, reg, _ = _cache_registry(tmp_path)
    before = toml.read_text()

    summary = import_pooch(db, reg, base_url="https://h", cache_dir=str(cache),
                           dry_run=True)
    assert "Would import 3 dataset(s)" in summary
    assert toml.read_text() == before                                # manifest untouched
    assert not (tmp_path / ".datamanifest-state.toml").exists()       # state untouched
