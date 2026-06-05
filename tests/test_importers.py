"""Importing datasets declared by other tools (``datamanifest import``).

Pooch is parsed straight from its registry-file grammar, so these tests need no
pooch install — a static sample registry lives in ``tests/data/pooch_registry.txt``
and cache-adoption cases build their own files + hashes.
"""

import hashlib
import json
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from datamanifest.cache import CachedIndex
from datamanifest.database import Database
from datamanifest.importers import (
    import_csv,
    import_pooch,
    import_urls,
    import_zenodo,
    parse_pooch_registry,
    parse_zenodo_record,
    zenodo_record_id,
)

_DATA = os.path.join(os.path.dirname(__file__), "data")
_FIXTURE = os.path.join(_DATA, "pooch_registry.txt")
_ZENODO = os.path.join(_DATA, "zenodo_record.json")


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


# ----- generic CSV / URL list ------------------------------------------------

def test_import_csv_declares_and_joins_base_url(tmp_path):
    db, toml = _project(tmp_path)
    csv = tmp_path / "files.csv"
    csv.write_text(
        "name,url,sha256\n"
        "temp,https://h/abs/temp.nc,deadbeef\n"            # absolute url, explicit name
        "rel/grid.csv,grid.csv,\n"                          # relative → base_url, no sha
    )
    summary = import_csv(db, str(csv), base_url="https://data.example.org/v1")
    assert "Imported 2 dataset(s)" in summary
    data = _manifest(toml)
    assert data["temp"]["uri"] == "https://h/abs/temp.nc"
    assert data["temp"]["sha256"] == "deadbeef"
    # The relative url is joined onto base_url; the explicit name is honored.
    assert data["rel/grid.csv"]["uri"] == "https://data.example.org/v1/grid.csv"


def test_import_csv_requires_url_column(tmp_path):
    db, _ = _project(tmp_path)
    bad = tmp_path / "bad.csv"
    bad.write_text("name,location\nx,/p\n")
    try:
        import_csv(db, str(bad))
    except ValueError as e:
        assert "url" in str(e)
    else:
        raise AssertionError("expected a ValueError about the missing url column")


def test_import_urls_list(tmp_path):
    db, toml = _project(tmp_path)
    lst = tmp_path / "urls.txt"
    lst.write_text("# a comment\nhttps://h/a/data.nc\n\nhttps://h/b/grid.csv\n")
    summary = import_urls(db, str(lst))
    assert "Imported 2 dataset(s)" in summary
    data = _manifest(toml)
    assert data["data"]["uri"] == "https://h/a/data.nc"
    assert data["grid"]["uri"] == "https://h/b/grid.csv"


def test_import_csv_adopts_cache(tmp_path):
    db, _ = _project(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    blob = b"grid bytes\n"
    (cache / "grid.csv").write_bytes(blob)
    csv = tmp_path / "files.csv"
    csv.write_text(f"name,url,sha256\ngrid,https://h/grid.csv,{_sha256(blob)}\n")

    summary = import_csv(db, str(csv), cache_dir=str(cache))
    assert "1 adopted from the cache" in summary
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of("h/grid.csv")


# ----- Zenodo ----------------------------------------------------------------

def test_zenodo_record_id_detection():
    assert zenodo_record_id("10.5281/zenodo.7654321") == "7654321"
    assert zenodo_record_id("https://zenodo.org/records/7654321") == "7654321"
    assert zenodo_record_id("https://zenodo.org/record/7654321") == "7654321"
    assert zenodo_record_id("https://example.com/data.csv") == ""
    assert zenodo_record_id("10.1234/other.999") == ""


def test_parse_zenodo_record_maps_files():
    with open(_ZENODO) as f:
        record = json.load(f)
    specs = parse_zenodo_record(record)
    by = {s["name"]: s for s in specs}
    # md5 file: no sha256 carried, the DOI + title are attached.
    assert "sha256" not in by["temperature"] or by["temperature"]["sha256"] == ""
    assert by["temperature"]["doi"] == "10.5281/zenodo.7654321"
    assert by["temperature"]["description"] == "Example gridded climatology"
    assert by["temperature"]["uri"].endswith("/temperature.nc/content")
    # sha256 file: carried verbatim.
    assert by["grid"]["sha256"].startswith("aa1122")


def test_parse_zenodo_record_pick_and_prefix():
    with open(_ZENODO) as f:
        record = json.load(f)
    specs = parse_zenodo_record(record, name_prefix="clim", picks=["*.nc"])
    assert len(specs) == 1
    assert specs[0]["name"] == "clim/temperature.nc"


def test_import_zenodo_bundles_by_default(tmp_path):
    db, toml = _project(tmp_path)
    with open(_ZENODO) as f:
        record = json.load(f)
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return record

    summary = import_zenodo(db, "10.5281/zenodo.7654321", fetch_json=fake_fetch)
    # The record's two files bundle into ONE dataset (uris=), named from the title.
    assert "Zenodo record 7654321" in summary and "Imported 1 dataset" in summary
    assert calls == ["https://zenodo.org/api/records/7654321"]
    entry = _manifest(toml)["example-gridded-climatology"]
    assert entry["doi"] == "10.5281/zenodo.7654321"
    assert len(entry["uris"]) == 2


def test_import_zenodo_split_one_dataset_per_file(tmp_path):
    db, toml = _project(tmp_path)
    with open(_ZENODO) as f:
        record = json.load(f)

    summary = import_zenodo(db, "10.5281/zenodo.7654321",
                            fetch_json=lambda u: record, split=True)
    assert "Imported 2 dataset(s)" in summary
    data = _manifest(toml)
    # One dataset per file, each carrying the SAME DOI (supported: distinct names).
    assert data["temperature"]["doi"] == "10.5281/zenodo.7654321"
    assert data["grid"]["doi"] == "10.5281/zenodo.7654321"


def test_import_zenodo_single_file_pick_is_plain_uri(tmp_path):
    db, toml = _project(tmp_path)
    with open(_ZENODO) as f:
        record = json.load(f)
    # A bundle narrowed to one file is tidier as `uri=` (not a one-element uris=).
    import_zenodo(db, "10.5281/zenodo.7654321", fetch_json=lambda u: record,
                  name="temp", picks=["*.nc"])
    entry = _manifest(toml)["temp"]
    assert entry["uri"].endswith("/temperature.nc/content")
    assert "uris" not in entry


def test_import_zenodo_rejects_non_zenodo(tmp_path):
    db, _ = _project(tmp_path)
    try:
        import_zenodo(db, "https://example.com/x.csv", fetch_json=lambda u: {})
    except ValueError as e:
        assert "Zenodo" in str(e)
    else:
        raise AssertionError("expected a ValueError for a non-Zenodo reference")


# ----- add → Zenodo routing (CLI composition root) ---------------------------

def test_add_routes_zenodo_reference(tmp_path, monkeypatch, capsys):
    import types

    from datamanifest import cli, importers

    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n')
    monkeypatch.setenv("DATAMANIFEST_TOML", str(toml))

    seen = {}

    def stub(db, ref, **kw):
        seen.update(ref=ref, kw=kw)
        return "STUB ZENODO"

    monkeypatch.setattr(importers, "import_zenodo", stub)
    args = types.SimpleNamespace(
        uri="10.5281/zenodo.7654321", name="clim", pick=["*.nc"], split=True,
        extract=False, overwrite=False, no_download=False,
    )
    cli._cmd_add(args)

    assert "STUB ZENODO" in capsys.readouterr().out
    assert seen["ref"] == "10.5281/zenodo.7654321"
    assert seen["kw"]["name"] == "clim" and seen["kw"]["picks"] == ["*.nc"]
    assert seen["kw"]["split"] is True


# ----- intake catalogs -------------------------------------------------------

def test_import_intake_catalog(tmp_path):
    import pytest
    pytest.importorskip("yaml")
    from datamanifest.importers import import_intake

    db, toml = _project(tmp_path)
    summary = import_intake(db, os.path.join(_DATA, "intake_catalog.yml"))
    # Two single-file sources become datasets; the globbed one is skipped.
    assert "Imported 2 dataset(s)" in summary
    assert "skipped 1 source" in summary and "yearly" in summary
    data = _manifest(toml)
    assert data["temperature"]["uri"] == "s3://bucket/clim/temp.nc"
    assert data["temperature"]["description"] == "Gridded temperature climatology"
    assert data["table"]["uri"] == "https://host/data/table.csv"
    assert "yearly" not in data


# ----- DVC -------------------------------------------------------------------

def test_import_dvc_reconstructs_remote_uri_and_adopts_cache(tmp_path):
    import pytest
    pytest.importorskip("yaml")
    import hashlib

    from datamanifest.cache import CachedIndex
    from datamanifest.importers import import_dvc

    # A DVC project: a default s3 remote, a cached object, and a plain `dvc add` out.
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(
        '[core]\n    remote = storage\n'
        '[\'remote "storage"\']\n    url = s3://bucket/dvcstore\n'
    )
    blob = b"col\n1\n2\n"
    md5 = hashlib.md5(blob).hexdigest()
    cdir = tmp_path / ".dvc" / "cache" / "files" / "md5" / md5[:2]
    cdir.mkdir(parents=True)
    (cdir / md5[2:]).write_bytes(blob)
    (tmp_path / "data.csv.dvc").write_text(
        f"outs:\n- md5: {md5}\n  size: {len(blob)}\n  path: data.csv\n"
    )

    db, toml = _project(tmp_path)
    summary = import_dvc(db, str(tmp_path / "data.csv.dvc"))
    assert "Imported 1 dataset" in summary and "1 adopted from the cache" in summary

    entry = _manifest(toml)["data"]
    # uri reconstructed from the default remote's content-addressed layout.
    assert entry["uri"] == f"s3://bucket/dvcstore/files/md5/{md5[:2]}/{md5[2:]}"
    # The cached object is adopted in place (state records it; sha256 computed).
    idx = CachedIndex.read(tmp_path / ".datamanifest-state.toml")
    assert idx.dataset_path_of(f"bucket/dvcstore/files/md5/{md5[:2]}/{md5[2:]}")


def test_import_dvc_import_url_uses_dep_url(tmp_path):
    import pytest
    pytest.importorskip("yaml")
    from datamanifest.importers import import_dvc

    (tmp_path / ".dvc").mkdir()
    (tmp_path / "file.csv.dvc").write_text(
        "deps:\n- path: https://host/file.csv\n  etag: abc\n"
        "outs:\n- md5: 0123456789abcdef0123456789abcdef\n  path: file.csv\n"
    )
    db, toml = _project(tmp_path)
    import_dvc(db, str(tmp_path / "file.csv.dvc"))
    # An import-url out takes the dep URL verbatim (no remote needed).
    assert _manifest(toml)["file"]["uri"] == "https://host/file.csv"
