import hashlib
import os
import pytest


def test_import():
    import datamanifest


def test_sha256_path(tmp_path):
    from datamanifest.config import sha256_path

    f = tmp_path / "hello.txt"
    content = b"hello world"
    f.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    assert sha256_path(str(f)) == expected


def test_get_extract_path():
    from datamanifest.config import get_extract_path

    assert get_extract_path("foo.zip") == "foo"
    assert get_extract_path("foo.tar.gz") == "foo"
    assert get_extract_path("foo.tar") == "foo"
    assert get_extract_path("foo?format=zip") == "foo"
    assert get_extract_path("foo") == "foo.d"


def test_parse_uri_metadata_http():
    from datamanifest.database import parse_uri_metadata

    m = parse_uri_metadata("https://github.com/foo/bar/archive/refs/tags/v1.zip")
    assert m["scheme"] == "https"
    assert m["host"] == "github.com"
    assert m["path"] == "/foo/bar/archive/refs/tags/v1.zip"


def test_parse_uri_metadata_git_ssh():
    from datamanifest.database import parse_uri_metadata

    m = parse_uri_metadata("git@github.com:foo/bar.git")
    assert m["scheme"] == "git"
    assert m["host"] == "github.com"
    assert m["path"] == "/foo/bar.git"


def test_parse_uri_metadata_version_fragment():
    from datamanifest.database import parse_uri_metadata

    assert parse_uri_metadata("https://h/p#v1.0")["version"] == "v1.0"
    assert parse_uri_metadata("https://h/p?version=v2")["version"] == "v2"
    assert parse_uri_metadata("https://h/p?format=zip")["format"] == "zip"


def test_init_dataset_entry_github_archive():
    from datamanifest.database import build_dataset_key, init_dataset_entry

    e = init_dataset_entry(
        uri="https://github.com/foo/bar/archive/refs/tags/v1.0.zip"
    )
    assert e.host == "github.com"
    assert e.path == "/foo/bar/archive/refs/tags/v1.0.zip"
    assert e.format == "zip"
    assert e.key == build_dataset_key(e)
    assert e.key == "github.com/foo/bar/archive/refs/tags/v1.0.zip"


def test_is_a_git_repo():
    from datamanifest.database import init_dataset_entry, is_a_git_repo

    e_git = init_dataset_entry(uri="git@github.com:foo/bar.git")
    assert is_a_git_repo(e_git)
    e_http = init_dataset_entry(uri="https://example.org/foo/bar.csv")
    assert not is_a_git_repo(e_http)


def test_entry_equality_ignores_sha256():
    from datamanifest.database import init_dataset_entry

    a = init_dataset_entry(uri="https://h/foo/bar.csv")
    b = init_dataset_entry(uri="https://h/foo/bar.csv")
    a.sha256 = "abc"
    b.sha256 = "def"
    a.skip_checksum = True
    b.skip_checksum = False
    assert a == b


def test_to_dict_round_trip():
    from datamanifest.database import init_dataset_entry, to_dict

    e = init_dataset_entry(uri="https://github.com/foo/bar/archive/v1.zip")
    d = to_dict(e)
    # Hidden derived fields are never serialized.
    for hidden in ("host", "path", "scheme"):
        assert hidden not in d
    # key/format are omitted when equal to their derived values.
    assert "key" not in d
    assert "format" not in d
    assert d["uri"] == "https://github.com/foo/bar/archive/v1.zip"


def test_callable_normalized_to_python():
    from datamanifest.database import init_dataset_entry, to_dict

    e = init_dataset_entry(uri="https://h/foo/bar.csv", callable="pkg.mod:fn")
    assert e.python == "pkg.mod:fn"
    assert not hasattr(e, "callable")
    d = to_dict(e)
    assert d["python"] == "pkg.mod:fn"
    assert "callable" not in d


def test_database_read_fixture():
    from datamanifest.database import Database

    fixture = os.path.join(os.path.dirname(__file__), "..", "datasets.toml")
    db = Database(datasets_toml=fixture, persist=False)
    for key in ("CMIP6_lgm_tos", "herzschuh2023", "jonkers2024", "jesstierney/lgmDA"):
        assert key in db.datasets
    assert db.datasets["herzschuh2023"].format == "zip"
    assert db.datasets["herzschuh2023"].extract is True


def test_database_round_trip(tmp_path):
    from datamanifest.database import Database

    out = tmp_path / "out.toml"
    db = Database(datasets_toml=str(out))
    db.register_dataset("https://h/a/x.csv", name="x", persist=False)
    db.register_dataset("https://h/b/y.csv", name="y", persist=False)
    db.register_dataset("https://h/c/z.csv", name="z", persist=False)
    db.write(str(out))

    text = out.read_text()
    assert text.index("[x]") < text.index("[y]") < text.index("[z]")

    db2 = Database(datasets_toml=str(out))
    assert db == db2


def test_database_loaders_first(tmp_path):
    from datamanifest.database import Database

    out = tmp_path / "out.toml"
    db = Database(datasets_toml=str(out))
    db.loaders["csvloader"] = "pandas.io.parsers:read_csv"
    db.register_dataset("https://h/a/b.csv", name="b", persist=False)
    db.write(str(out))

    text = out.read_text()
    assert text.index("[_LOADERS]") < text.index("[b]")

    db2 = Database(datasets_toml=str(out))
    assert db2.loaders.get("csvloader") == "pandas.io.parsers:read_csv"


# --- Item 5: Search, list, repr ---

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "datasets.toml")


def _fixture_db():
    from datamanifest.database import Database
    return Database(datasets_toml=FIXTURE, persist=False)


def test_getitem_by_key():
    db = _fixture_db()
    entry = db["herzschuh2023"]
    assert entry.format == "zip"


def test_search_by_doi():
    from datamanifest.database import search_dataset
    db = _fixture_db()
    key, entry = search_dataset(db, "10.1594/PANGAEA.930512")
    assert key == "herzschuh2023"


def test_search_by_repo_name_alias():
    from datamanifest.database import search_dataset
    db = _fixture_db()
    key, entry = search_dataset(db, "lgmDA")
    assert key == "jesstierney/lgmDA"


def test_search_missing_raises():
    from datamanifest.database import search_dataset
    db = _fixture_db()
    with pytest.raises(ValueError, match="Available datasets:"):
        search_dataset(db, "nonexistent_dataset_xyz")


def test_repr_datasets():
    from datamanifest.database import repr_datasets
    db = _fixture_db()
    text = repr_datasets(db)
    lines = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(lines) == len(db.datasets)


# --- Item 6: Checksum verify, delete, update ---

def _make_db_with_file(tmp_path, filename="data.bin", content=b"test content"):
    from datamanifest.database import Database, init_dataset_entry
    f = tmp_path / filename
    f.write_bytes(content)
    db = Database(datasets_folder=str(tmp_path), persist=False)
    db.datasets_toml = ""
    entry = init_dataset_entry(uri=f"https://h/{filename}")
    entry.key = filename
    return db, entry, f


def test_verify_checksum_autofill(tmp_path):
    from datamanifest.database import verify_checksum
    db, entry, f = _make_db_with_file(tmp_path)
    assert entry.sha256 == ""
    result = verify_checksum(db, entry, persist=False)
    assert result is True
    assert entry.sha256 == hashlib.sha256(b"test content").hexdigest()


def test_verify_checksum_mismatch_raises(tmp_path):
    from datamanifest.database import verify_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    entry.sha256 = "wrong_hash_value"
    with pytest.raises(ValueError, match="Possible resolutions"):
        verify_checksum(db, entry, persist=False)


def test_verify_checksum_skip(tmp_path):
    from datamanifest.database import verify_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    entry.sha256 = "wrong_hash_value"
    db.skip_checksum = True
    assert verify_checksum(db, entry, persist=False) is True


def test_delete_dataset_removes_entry(tmp_path):
    from datamanifest.database import Database, delete_dataset
    db = Database(datasets_folder=str(tmp_path), persist=False)
    db.datasets_toml = ""
    db.register_dataset("https://h/a/b.csv", name="testentry", persist=False)
    assert "testentry" in db.datasets
    delete_dataset(db, "testentry", keep_cache=True, persist=False)
    assert "testentry" not in db.datasets


def test_delete_dataset_removes_file(tmp_path):
    from datamanifest.database import Database, delete_dataset, init_dataset_entry
    db = Database(datasets_folder=str(tmp_path), persist=False)
    db.datasets_toml = ""
    f = tmp_path / "b.csv"
    f.write_bytes(b"data")
    entry = init_dataset_entry(uri="https://h/a/b.csv")
    entry.key = "b.csv"
    db.datasets["b"] = entry
    delete_dataset(db, "b", keep_cache=False, persist=False)
    assert not f.exists()
    assert "b" not in db.datasets
