import hashlib
import os


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
