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
