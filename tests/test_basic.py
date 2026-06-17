import hashlib
import os
from pathlib import Path

import pytest


def test_import():
    import datamanifest


def test_build_uri_no_degenerate_scheme():
    # A binding-only / local_path entry with no uri must not get a spurious
    # `uri = "://"` synthesized (which would then be written into the manifest).
    from datamanifest.database import build_uri, init_dataset_entry, to_dict

    entry = init_dataset_entry(local_path="/data/foo.nc")
    assert entry.uri == ""
    assert build_uri(entry) == ""
    assert "uri" not in to_dict(entry)


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


def test_legacy_sha256_migrates_to_checksum(tmp_path):
    """A legacy `sha256 =` is read as `checksum = "sha256:<hex>"` and rewritten in
    that form the next time the manifest is written."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib
    from datamanifest.database import Database

    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n\n'
                    '[foo]\nuri = "https://h/foo.csv"\nsha256 = "a1b2c3"\n')
    db = Database(datasets_toml=str(toml), persist=False, skip_checksum=True)
    entry = db.datasets["foo"]
    assert entry.checksum == "sha256:a1b2c3"
    assert entry.sha256 == "a1b2c3"                       # back-compat view
    assert entry.hash_algo == "sha256" and entry.hash_value == "a1b2c3"

    db.write(str(toml))                                   # rewrite migrates the file
    with open(toml, "rb") as f:
        written = tomllib.load(f)
    assert written["foo"]["checksum"] == "sha256:a1b2c3"
    assert "sha256" not in written["foo"]


def test_md5_checksum_verified_and_preserved(tmp_path):
    """A declared non-sha256 checksum verifies in its own algorithm and is never
    silently rewritten to sha256."""
    from datamanifest.database import verify_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    md5 = hashlib.md5(b"test content").hexdigest()
    entry.checksum = f"md5:{md5}"
    assert verify_checksum(db, entry, persist=False) is True
    assert entry.checksum == f"md5:{md5}"                 # preserved, not → sha256
    assert entry.sha256 == ""                             # sha256 view empty for md5

    entry.checksum = "md5:deadbeef"                       # wrong md5 → mismatch
    with pytest.raises(ValueError, match="Possible resolutions"):
        verify_checksum(db, entry, persist=False)


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


def test_write_keeps_structural_tables_at_top(tmp_path):
    """[_META]/[_STORAGE] are written at the top, not scattered among datasets —
    a plain code-point sort would drop '_' between 'Zebra' and 'apple'."""
    from datamanifest.database import Database

    db = Database(persist=False)
    db.register_dataset("https://h/Zebra/z.csv", name="Zebra", persist=False)
    db.register_dataset("https://h/apple/a.csv", name="apple", persist=False)
    db.extra["_STORAGE"] = {"datasets_dir": "datasets"}
    db.schema_version = 1
    out = tmp_path / "out.toml"
    db.write(str(out))

    tables = [l.strip() for l in out.read_text().splitlines() if l.startswith("[")]
    assert tables == ["[_META]", "[_STORAGE]", "[Zebra]", "[apple]"]


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


def test_write_skips_when_unchanged(tmp_path):
    """A persist whose serialized content matches what is already on disk is skipped,
    so it neither churns the file nor reformats a hand-authored layout; only a real
    content change rewrites. The write is also atomic (no leftover .tmp sibling)."""
    from datamanifest.database import Database

    out = tmp_path / "datamanifest.toml"
    db = Database(datasets_toml=str(out))
    db.register_dataset("https://h/a/x.csv", name="x", persist=False)
    db.write(str(out))
    assert not list(tmp_path.glob("*.tmp"))                   # atomic: no staging left
    # Inject a comment: semantically identical, so the next write must skip and leave
    # the hand-authored layout (the comment) intact.
    out.write_text("# hand-authored note\n\n" + out.read_text())
    db.write(str(out))
    assert "# hand-authored note" in out.read_text()          # not rewritten
    assert Database(datasets_toml=str(out)) == db             # still valid
    # A genuine change rewrites (dropping the comment).
    db.register_dataset("https://h/b/y.csv", name="y", persist=False)
    db.write(str(out))
    assert "# hand-authored note" not in out.read_text()      # rewritten
    assert "y" in Database(datasets_toml=str(out)).datasets


def test_extension_keys_preserved(tmp_path):
    """Unknown (other-language) per-dataset keys round-trip verbatim."""
    from datamanifest.database import Database

    src = tmp_path / "Datasets.toml"
    src.write_text(
        "[foo]\n"
        'uri = "https://example.com/data/foo.csv"\n'
        'sha256 = "abc123"\n'
        'julia = "x -> load(x)"\n'
        'julia_modules = ["CSV", "DataFrames"]\n'
    )
    db = Database(datasets_toml=str(src), persist=False)
    entry = db.datasets["foo"]
    assert entry.extra == {"julia": "x -> load(x)", "julia_modules": ["CSV", "DataFrames"]}

    out = tmp_path / "out.toml"
    db.write(str(out))
    db2 = Database(datasets_toml=str(out), persist=False)
    assert db2.datasets["foo"].extra == entry.extra


def test_read_does_not_rewrite_file(tmp_path):
    """Constructing a Database from a toml must not write the file back."""
    from datamanifest.database import Database

    src = tmp_path / "Datasets.toml"
    original = (
        "[foo]\n"
        'uri = "https://example.com/data/foo.csv"\n'
        'sha256 = "abc123"\n'
        'julia = "x -> load(x)"\n'
    )
    src.write_text(original)
    before = src.read_bytes()
    Database(datasets_toml=str(src))  # persist defaults to True
    assert src.read_bytes() == before


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


def test_database_convenience_methods(tmp_path):
    """A custom / file-less Database is fully usable via methods (no module-level
    default-db wrappers needed): add / download / get_dataset_path / load / delete."""
    import json
    import os

    from datamanifest import Database

    src = tmp_path / "src.json"
    src.write_text(json.dumps({"k": 1}))
    db = Database(datasets_folder=str(tmp_path / "data"), persist=False)

    name, _ = db.add(f"file://{src}", name="x")
    assert name == "x"
    path = db.download_dataset("x")
    assert os.path.exists(path)
    assert db.get_dataset_path("x") == path
    assert db.load_dataset("x") == {"k": 1}      # json loader — no optional deps
    db.delete_dataset("x")
    assert "x" not in db.datasets


def test_add_downloads_by_default(tmp_path):
    """`add` registers and downloads (like the CLI and the Julia tool);
    `skip_download=True` registers only; `register_dataset` never downloads."""
    import json
    import os

    from datamanifest import Database

    # Distinct source files so each entry resolves to its own local path.
    srcs = {}
    for stem in ("dl", "nodl", "reg"):
        srcs[stem] = tmp_path / f"{stem}.json"
        srcs[stem].write_text(json.dumps({"k": stem}))
    db = Database(datasets_folder=str(tmp_path / "data"), persist=False)

    # add: registered AND downloaded.
    name, _ = db.add(f"file://{srcs['dl']}", name="dl")
    assert name == "dl"
    assert os.path.exists(db.get_dataset_path("dl"))

    # add(skip_download=True): registered only.
    name, _ = db.add(f"file://{srcs['nodl']}", name="nodl", skip_download=True)
    assert "nodl" in db.datasets
    assert not os.path.exists(db.get_dataset_path("nodl"))
    # the entry field is untouched — skip_download here only opts out of the
    # immediate download (matching the Julia tool's `add`).
    assert db.datasets["nodl"].skip_download is False

    # register_dataset: registered only (unchanged semantics).
    db.register_dataset(f"file://{srcs['reg']}", name="reg", persist=False)
    assert "reg" in db.datasets
    assert not os.path.exists(db.get_dataset_path("reg"))


def test_module_functions_accept_db_keyword(tmp_path):
    """`datamanifest.X(..., db=mydb)` routes to that db's method; without `db=` it
    uses the auto-discovered default database."""
    import json
    import os

    import datamanifest
    from datamanifest import Database

    src = tmp_path / "s.json"
    src.write_text(json.dumps({"v": 2}))
    db = Database(datasets_folder=str(tmp_path / "d"), persist=False)

    datamanifest.add(f"file://{src}", name="y", db=db)
    assert "y" in db.datasets
    path = datamanifest.download_dataset("y", db=db)
    assert os.path.exists(path)
    assert datamanifest.get_dataset_path("y", db=db) == path
    assert datamanifest.load_dataset("y", db=db) == {"v": 2}
    datamanifest.delete_dataset("y", db=db)
    assert "y" not in db.datasets


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


def test_search_ambiguous_doi_raises():
    # Several datasets sharing one DOI (e.g. a Zenodo `--split` import): resolving
    # by that DOI is ambiguous and must fail loud, naming the candidates — not
    # silently return an arbitrary one.
    import pytest

    from datamanifest.database import Database, search_dataset

    db = Database(persist=False)
    db.datasets_toml = ""
    db.register_dataset("https://h/a.csv", name="a",
                        doi="10.5281/zenodo.99", persist=False)
    db.register_dataset("https://h/b.csv", name="b",
                        doi="10.5281/zenodo.99", persist=False)

    with pytest.raises(ValueError, match="Ambiguous"):
        search_dataset(db, "10.5281/zenodo.99")
    # The shared DOI is ambiguous, but each dataset name still resolves cleanly.
    assert search_dataset(db, "a")[0] == "a"
    assert search_dataset(db, "b")[0] == "b"


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


def test_update_checksum_rewrites_stale(tmp_path):
    from datamanifest.database import update_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    entry.sha256 = "stale_hash_value"
    action = update_checksum(db, entry, persist=False)
    assert action == "updated"
    assert entry.sha256 == hashlib.sha256(b"test content").hexdigest()


def test_update_checksum_fills_empty(tmp_path):
    from datamanifest.database import update_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    assert entry.sha256 == ""
    assert update_checksum(db, entry, persist=False) == "filled"
    assert entry.sha256 == hashlib.sha256(b"test content").hexdigest()


def test_update_checksum_unchanged(tmp_path):
    from datamanifest.database import update_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    entry.sha256 = hashlib.sha256(b"test content").hexdigest()
    assert update_checksum(db, entry, persist=False) == "unchanged"


def test_update_checksum_dry_run_does_not_mutate(tmp_path):
    from datamanifest.database import update_checksum
    db, entry, _ = _make_db_with_file(tmp_path)
    entry.sha256 = "stale_hash_value"
    assert update_checksum(db, entry, persist=False, dry_run=True) == "updated"
    assert entry.sha256 == "stale_hash_value"


def test_update_checksum_missing_file(tmp_path):
    from datamanifest.database import Database, init_dataset_entry, update_checksum
    db = Database(datasets_folder=str(tmp_path), persist=False)
    db.datasets_toml = ""
    entry = init_dataset_entry(uri="https://h/absent.bin")
    entry.key = "absent.bin"
    assert update_checksum(db, entry, persist=False) == "missing"


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


# --- Item 7: pipelines.py — HTTP download + extract ---

import contextlib
import functools
import io
import threading
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


@contextlib.contextmanager
def _http_server(directory):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _make_zip(path, members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    path.write_bytes(buf.getvalue())


def test_download_http_file(tmp_path):
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    served = tmp_path / "served"
    served.mkdir()
    content = b"col1,col2\n1,2\n"
    (served / "foo.csv").write_bytes(content)

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""

    with _http_server(served) as base:
        db.register_dataset(f"{base}/foo.csv", name="foo", persist=False)
        path = download_dataset(db, "foo")

    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == content
    assert db.datasets["foo"].sha256 == hashlib.sha256(content).hexdigest()


def test_download_http_zip_extract(tmp_path):
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    served = tmp_path / "served"
    served.mkdir()
    _make_zip(served / "bar.zip", {"inner.txt": b"hello inside"})

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""

    with _http_server(served) as base:
        db.register_dataset(f"{base}/bar.zip", name="bar", extract=True, persist=False)
        path = download_dataset(db, "bar")

    assert os.path.isdir(path)
    assert (Path(path) / "inner.txt").read_bytes() == b"hello inside"
    # sha256 of the extracted folder is auto-filled.
    assert db.datasets["bar"].sha256 != ""


def test_download_overwrite_false_noop(tmp_path):
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    served = tmp_path / "served"
    served.mkdir()
    content = b"data123"
    (served / "x.bin").write_bytes(content)

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""

    with _http_server(served) as base:
        db.register_dataset(f"{base}/x.bin", name="x", persist=False)
        path1 = download_dataset(db, "x")
        # Second call with overwrite=False is a no-op and re-verifies checksum.
        path2 = download_dataset(db, "x", overwrite=False)

    assert path1 == path2
    with open(path2, "rb") as f:
        assert f.read() == content


def test_download_http_resume_partial(tmp_path):
    from datamanifest.database import Database, get_dataset_path
    from datamanifest.pipelines import download_dataset

    served = tmp_path / "served"
    served.mkdir()
    content = bytes(range(256)) * 64  # 16 KiB of distinctive bytes
    (served / "big.bin").write_bytes(content)

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""

    with _http_server(served) as base:
        _, entry = db.register_dataset(f"{base}/big.bin", name="big", persist=False)
        download_path = get_dataset_path(entry, db.datasets_folder, extract=False)
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        # Seed a partial download with the correct prefix so the Range resume
        # appends the remainder rather than corrupting the file.
        with open(download_path + ".download", "wb") as f:
            f.write(content[:4096])
        path = download_dataset(db, "big")

    with open(path, "rb") as f:
        assert f.read() == content


def test_extract_file_unknown_format(tmp_path):
    from datamanifest.pipelines import extract_file

    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    with pytest.raises(ValueError, match="Unknown format"):
        extract_file(str(src), str(tmp_path / "out"), "rar")


# --- Item 8: Scheme dispatch — git, ssh/rsync, file ---

def test_download_file_uri(tmp_path):
    """file:// URI copies the source file to datasets_folder."""
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    content = b"local,data\n1,2\n"
    (src_dir / "local.csv").write_bytes(content)

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""
    db.register_dataset(f"file://{src_dir}/local.csv", name="localdata", persist=False)
    path = download_dataset(db, "localdata")

    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == content


def test_download_git_clone(tmp_path):
    """git:// URI triggers 'git clone --depth 1 ...' via subprocess."""
    from unittest.mock import patch

    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    db.register_dataset(
        "https://github.com/foo/bar.git", name="myrepo", persist=False
    )

    called_with = {}

    def fake_run(cmd, **kwargs):
        called_with["cmd"] = cmd

        class Result:
            returncode = 0

        # Create the target directory so download_dataset sees it as present
        dest = cmd[-1]
        os.makedirs(dest, exist_ok=True)
        return Result()

    with patch("datamanifest.pipelines.subprocess.run", side_effect=fake_run):
        download_dataset(db, "myrepo")

    assert called_with["cmd"][0] == "git"
    assert called_with["cmd"][1] == "clone"
    assert "--depth" in called_with["cmd"]
    assert "https://github.com/foo/bar.git" in called_with["cmd"]


def test_download_git_clone_with_branch(tmp_path):
    """entry.branch is forwarded to git clone --branch."""
    from unittest.mock import patch

    from datamanifest.database import Database, init_dataset_entry
    from datamanifest.pipelines import download_dataset

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    entry = init_dataset_entry(uri="https://github.com/foo/bar.git", branch="dev")
    db.datasets["myrepo"] = entry

    called_with = {}

    def fake_run(cmd, **kwargs):
        called_with["cmd"] = cmd

        class Result:
            returncode = 0

        dest = cmd[-1]
        os.makedirs(dest, exist_ok=True)
        return Result()

    with patch("datamanifest.pipelines.subprocess.run", side_effect=fake_run):
        download_dataset(db, "myrepo")

    assert "--branch" in called_with["cmd"]
    idx = called_with["cmd"].index("--branch")
    assert called_with["cmd"][idx + 1] == "dev"


# --- Item 9: Multi-URI batch entries ---

def test_multi_uri_key_derivation():
    """init_dataset_entry derives key from common host+path prefix for multi-URI entries."""
    from datamanifest.database import init_dataset_entry

    e = init_dataset_entry(uris=["http://h/a/b/x.csv", "http://h/a/c/y.csv"])
    assert e.key == "h/a"


def test_download_multi_uri(tmp_path):
    """Multi-URI entry downloads each file to datasets_folder/<key>/<rel_path>."""
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    src = tmp_path / "src"
    (src / "b").mkdir(parents=True)
    (src / "c").mkdir(parents=True)
    (src / "b" / "x.csv").write_bytes(b"x data")
    (src / "c" / "y.csv").write_bytes(b"y data")

    uris = [f"file://{src}/b/x.csv", f"file://{src}/c/y.csv"]

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    db.register_dataset(uris=uris, key="testmulti", name="multi", persist=False)
    path = download_dataset(db, "multi")

    assert os.path.isdir(path)
    assert (Path(path) / "b" / "x.csv").read_bytes() == b"x data"
    assert (Path(path) / "c" / "y.csv").read_bytes() == b"y data"


def test_download_ssh_rsync(tmp_path):
    """ssh:// URI builds the correct rsync -arvzL command."""
    from unittest.mock import patch

    from datamanifest.database import Database, init_dataset_entry
    from datamanifest.pipelines import download_dataset

    folder = tmp_path / "cache"
    db = Database(datasets_folder=str(folder), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    entry = init_dataset_entry(uri="ssh://remotehost/data/foo.csv")
    db.datasets["remotedata"] = entry

    called_with = {}

    def fake_run(cmd, **kwargs):
        called_with["cmd"] = cmd

        class Result:
            returncode = 0

        # Create a placeholder file so the orchestrator sees a result
        dest_dir = cmd[-1].rstrip("/")
        os.makedirs(dest_dir, exist_ok=True)
        open(os.path.join(dest_dir, "foo.csv"), "w").close()
        return Result()

    with patch("datamanifest.pipelines.subprocess.run", side_effect=fake_run):
        download_dataset(db, "remotedata")

    cmd = called_with["cmd"]
    assert cmd[0] == "rsync"
    assert "-arvzL" in cmd
    assert any("remotehost:" in a for a in cmd)


# ----- Item 10: requires= + topological order + shell template -----

def _shell_db(tmp_path):
    from datamanifest.database import Database

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    return db


def test_requires_download_order(tmp_path):
    """A dependency declared via requires= is downloaded before the dependent."""
    from datamanifest.database import init_dataset_entry
    from datamanifest.pipelines import download_dataset

    db = _shell_db(tmp_path)
    log = tmp_path / "order.log"
    db.datasets["A"] = init_dataset_entry(
        key="A", shell=f"sh -c 'echo A >> {log}; touch $download_path'"
    )
    db.datasets["B"] = init_dataset_entry(
        key="B", requires=["A"], shell=f"sh -c 'echo B >> {log}; touch $download_path'"
    )

    download_dataset(db, "B")

    assert log.read_text() == "A\nB\n"


def test_requires_circular_dependency(tmp_path):
    """A requires-cycle raises ValueError with a 'Circular dependency' message."""
    from datamanifest.database import init_dataset_entry
    from datamanifest.pipelines import download_dataset

    db = _shell_db(tmp_path)
    db.datasets["A"] = init_dataset_entry(key="A", requires=["B"], shell="touch $download_path")
    db.datasets["B"] = init_dataset_entry(key="B", requires=["A"], shell="touch $download_path")

    with pytest.raises(ValueError, match="Circular dependency"):
        download_dataset(db, "A")


def test_shell_template_content(tmp_path):
    """A shell template expands $download_path and runs, producing file content."""
    from datamanifest.database import init_dataset_entry
    from datamanifest.pipelines import download_dataset

    db = _shell_db(tmp_path)
    db.datasets["hello"] = init_dataset_entry(
        key="hello", shell="sh -c 'echo hello > $download_path'"
    )

    path = download_dataset(db, "hello")

    assert Path(path).read_text() == "hello\n"


def test_shell_template_path_substitution(tmp_path):
    """$path_<i> and $path_<sanitized_ref> substitute the dependency's path."""
    from datamanifest.database import init_dataset_entry, get_dataset_path
    from datamanifest.pipelines import download_dataset, expand_shell_template

    db = _shell_db(tmp_path)
    dep = init_dataset_entry(key="dep/one", shell="sh -c 'echo data > $download_path'")
    db.datasets["dep/one"] = dep
    # B copies the dependency's file using the ordered $path_1 reference.
    db.datasets["B"] = init_dataset_entry(
        key="B", requires=["dep/one"], shell="cp $path_1 $download_path"
    )

    path = download_dataset(db, "B")
    assert Path(path).read_text() == "data\n"

    # Direct unit check of $path_<sanitized_ref> and $requires_paths.
    expanded = expand_shell_template(
        "use $path_dep_one and $requires_paths",
        db.datasets["B"],
        "/tmp/out",
        required_paths_by_ref={"dep_one": "/cache/dep/one"},
        required_paths_ordered=["/cache/dep/one"],
    )
    assert expanded == "use /cache/dep/one and /cache/dep/one"


def test_shell_template_project_root_required():
    """$project_root in a template with no project_root raises ValueError."""
    from datamanifest.database import init_dataset_entry
    from datamanifest.pipelines import expand_shell_template

    entry = init_dataset_entry(key="x", shell="echo $project_root")
    with pytest.raises(ValueError, match="project_root"):
        expand_shell_template("cd $project_root", entry, "/tmp/out", project_root="")


# ----- Item 11: Loader registry + python entry-point hook (no exec) -----


def test_register_and_validate_loader():
    """A named loader resolves to its callable via validate_loader."""
    from datamanifest.database import Database, validate_loader

    from tests.helpers.loaders import my_loader

    db = Database(persist=False)
    db.register_loaders(
        loaders={"myfmt": "tests.helpers.loaders:my_loader"}, persist=False
    )
    fn = validate_loader(db, "myfmt")
    assert fn is my_loader
    assert fn("/some/path") == ("loaded", "/some/path")


def test_python_hook_runs_during_download(tmp_path):
    """entry.python is resolved + called as a download-phase hook with kwargs."""
    from datamanifest.database import Database, init_dataset_entry
    from datamanifest.pipelines import download_dataset

    from tests.helpers.loaders import my_downloader

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    entry = init_dataset_entry(key="hooked", python="tests.helpers.loaders:my_downloader")
    db.datasets["hooked"] = entry

    path = download_dataset(db, "hooked")

    assert Path(path).read_text() == "hook ran"
    assert my_downloader.last_call["entry"] is entry
    assert my_downloader.last_call["key"] == "hooked"
    assert "requires_paths" in my_downloader.last_call


def test_loader_alias_chain_resolves():
    """A loader value that names another loader is resolved transitively."""
    from datamanifest.database import Database, validate_loader

    from tests.helpers.loaders import my_loader

    db = Database(persist=False)
    db.register_loaders(
        loaders={"a": "b", "b": "tests.helpers.loaders:my_loader"}, persist=False
    )
    assert validate_loader(db, "a") is my_loader


def test_loader_alias_cycle_raises():
    """A loader alias cycle raises a clear error."""
    from datamanifest.database import Database, validate_loader

    db = Database(persist=False)
    db.register_loaders(loaders={"a": "b", "b": "a"}, persist=False)
    with pytest.raises(ValueError, match="cycle"):
        validate_loader(db, "a")


def test_python_includes_local_module(tmp_path):
    """python_includes makes a user-local module importable for loader resolution."""
    from datamanifest.database import Database, validate_loader

    (tmp_path / "mymod.py").write_text(
        "def loadit(path):\n    return 'local:' + path\n"
    )
    db = Database(persist=False)
    db.register_loaders(
        loaders={"local": "mymod:loadit"},
        python_includes=[str(tmp_path)],
        persist=False,
    )
    fn = validate_loader(db, "local")
    assert fn("/x") == "local:/x"


# ----- Item 12: Default loaders — text formats -----

def test_default_loader_json(tmp_path):
    from datamanifest.default_loaders import default_loader
    import json

    f = tmp_path / "data.json"
    f.write_text(json.dumps({"a": 1, "b": [2, 3]}))
    result = default_loader("json")(str(f))
    assert result == {"a": 1, "b": [2, 3]}


def test_default_loader_toml(tmp_path):
    from datamanifest.default_loaders import default_loader

    f = tmp_path / "data.toml"
    f.write_text('[section]\nkey = "value"\n')
    result = default_loader("toml")(str(f))
    assert result == {"section": {"key": "value"}}


def test_default_loader_unknown_raises():
    from datamanifest.default_loaders import default_loader

    with pytest.raises(ValueError, match="No default loader for format"):
        default_loader("unknown_xyz")


def test_default_loader_empty_raises():
    from datamanifest.default_loaders import default_loader

    with pytest.raises(ValueError, match="No loader provided"):
        default_loader("")


# ----- Item 13: Default loaders — tabular + archives -----

def test_default_loader_zip(tmp_path):
    """_zip_loader extracts to a temp dir and returns its path."""
    import zipfile
    from datamanifest.default_loaders import default_loader

    z = tmp_path / "archive.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("hello.txt", "hello from zip")

    result = default_loader("zip")(str(z))
    assert os.path.isdir(result)
    assert os.path.isfile(os.path.join(result, "hello.txt"))
    assert open(os.path.join(result, "hello.txt")).read() == "hello from zip"


def test_default_loader_tar(tmp_path):
    """_tar_loader extracts to a temp dir and returns its path."""
    import tarfile
    from datamanifest.default_loaders import default_loader

    content = b"hello from tar"
    member = tmp_path / "member.txt"
    member.write_bytes(content)

    t = tmp_path / "archive.tar"
    with tarfile.open(t, "w") as tf:
        tf.add(str(member), arcname="member.txt")

    result = default_loader("tar")(str(t))
    assert os.path.isdir(result)
    assert open(os.path.join(result, "member.txt"), "rb").read() == content


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("pandas") is None,
    reason="pandas not installed",
)
def test_default_loader_csv(tmp_path):
    """_csv_loader returns a pandas DataFrame."""
    from datamanifest.default_loaders import default_loader

    f = tmp_path / "data.csv"
    f.write_text("# comment\na,b\n1,2\n3,4\n")
    result = default_loader("csv")(str(f))
    import pandas
    assert isinstance(result, pandas.DataFrame)
    assert list(result.columns) == ["a", "b"]
    assert len(result) == 2


def test_default_loader_csv_missing_pandas(monkeypatch):
    """csv loader raises ImportError with pip install hint when pandas is absent."""
    from unittest.mock import patch
    from datamanifest.default_loaders import default_loader

    loader_fn = default_loader("csv")

    def fake_import_module(name):
        if name == "pandas":
            raise ImportError("mocked missing pandas")
        import importlib as _importlib
        return _importlib.import_module(name)

    with patch("datamanifest.default_loaders.importlib.import_module", side_effect=fake_import_module):
        with pytest.raises(ImportError, match="pip install pandas"):
            loader_fn("/any/path.csv")


# --- Item 14: Default loader — NetCDF (xarray) ---

@pytest.mark.skipif(
    __import__("importlib").util.find_spec("xarray") is None,
    reason="xarray not installed",
)
def test_default_loader_nc(tmp_path):
    """_nc_loader returns an xarray.Dataset for a .nc file."""
    import xarray
    from datamanifest.default_loaders import default_loader

    ds = xarray.Dataset({"temperature": (["x"], [1.0, 2.0, 3.0])})
    nc_path = tmp_path / "data.nc"
    ds.to_netcdf(str(nc_path))

    result = default_loader("nc")(str(nc_path))
    assert isinstance(result, xarray.Dataset)
    assert "temperature" in result


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("xarray") is None,
    reason="xarray not installed",
)
def test_default_loader_dimstack(tmp_path):
    """_dimstack_loader is an alias for _nc_loader, returns xarray.Dataset."""
    import xarray
    from datamanifest.default_loaders import default_loader

    ds = xarray.Dataset({"salinity": (["z"], [34.0, 35.0])})
    nc_path = tmp_path / "ocean.nc"
    ds.to_netcdf(str(nc_path))

    result = default_loader("dimstack")(str(nc_path))
    assert isinstance(result, xarray.Dataset)
    assert "salinity" in result


def test_default_loader_nc_missing_xarray():
    """nc loader raises ImportError with pip install hint when xarray is absent."""
    from unittest.mock import patch
    from datamanifest.default_loaders import default_loader

    loader_fn = default_loader("nc")

    def fake_import_module(name):
        if name == "xarray":
            raise ImportError("mocked missing xarray")
        import importlib as _importlib
        return _importlib.import_module(name)

    with patch("datamanifest.default_loaders.importlib.import_module", side_effect=fake_import_module):
        with pytest.raises(ImportError, match="pip install xarray netcdf4"):
            loader_fn("/any/path.nc")


# ----- Item 15: load_dataset pipeline -----

def test_load_dataset_json(tmp_path):
    """load_dataset downloads + parses a json-format dataset, returning the dict."""
    import json

    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.json").write_text(json.dumps({"a": 1, "b": [2, 3]}))

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    db.register_dataset(f"file://{src}/data.json", name="jsonentry", persist=False)

    result = load_dataset(db, "jsonentry")
    assert result == {"a": 1, "b": [2, 3]}


def test_load_dataset_explicit_loader_overrides(tmp_path):
    """An explicit loader= callable overrides the entry's format-based loader."""
    import json

    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.json").write_text(json.dumps({"a": 1}))

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    db.register_dataset(f"file://{src}/data.json", name="jsonentry", persist=False)

    result = load_dataset(db, "jsonentry", loader=lambda path: "OVERRIDDEN")
    assert result == "OVERRIDDEN"


def test_load_dataset_named_loader_precedes_builtin(tmp_path):
    """A named loader in db.loaders matching the format wins over the builtin."""
    import json

    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset

    from tests.helpers.loaders import my_loader

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.json").write_text(json.dumps({"a": 1}))

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True
    db.register_loaders(
        loaders={"json": "tests.helpers.loaders:my_loader"}, persist=False
    )
    db.register_dataset(f"file://{src}/data.json", name="jsonentry", persist=False)

    result = load_dataset(db, "jsonentry")
    # my_loader returns ("loaded", path) rather than the parsed json dict.
    assert result[0] == "loaded"
    assert os.path.isfile(result[1])


def test_lang_loaders_binding_roundtrip(tmp_path):
    """[_LANG.python.loaders] values support the { ref, args, kwargs } table form;
    a bare ref serializes back as a string, a parameterized one as a table."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    from datamanifest.database import Database

    src = tmp_path / "m.toml"
    src.write_text(
        "[_META]\nschema = 1\n\n"
        "[_LANG.python.loaders]\n"
        'csv = "pkg.mod:plain"\n'
        'nc = { ref = "pkg.mod:withargs", kwargs = { grid = "5x5" } }\n'
    )
    db = Database(datasets_toml=str(src), persist=False)
    assert db.lang_python_loaders == {"csv": "pkg.mod:plain", "nc": "pkg.mod:withargs"}
    assert db.lang_python_loaders_args == {}
    assert db.lang_python_loaders_kwargs == {"nc": {"grid": "5x5"}}

    out = tmp_path / "out.toml"
    db.write(str(out))
    with open(out, "rb") as f:
        raw = tomllib.load(f)
    loaders_raw = raw["_LANG"]["python"]["loaders"]
    assert loaders_raw["csv"] == "pkg.mod:plain"          # bare ref → string
    assert isinstance(loaders_raw["nc"], dict)            # parameterized → table
    assert loaders_raw["nc"]["ref"] == "pkg.mod:withargs"
    assert loaders_raw["nc"]["kwargs"] == {"grid": "5x5"}

    # Re-reading the written file reproduces the parsed fields.
    db2 = Database(datasets_toml=str(out), persist=False)
    assert db2.lang_python_loaders == db.lang_python_loaders
    assert db2.lang_python_loaders_kwargs == db.lang_python_loaders_kwargs


def test_lang_loaders_parameterized_execution(tmp_path):
    """A manifest format-default loader given as a { ref, args, kwargs } table is
    called with $var-substituted args/kwargs."""
    import json

    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset
    from tests.helpers import loaders as L

    src = tmp_path / "src"
    src.mkdir()
    (src / "data.json").write_text(json.dumps({"a": 1}))

    toml = tmp_path / "datasets.toml"
    toml.write_text(
        "[_META]\nschema = 1\n\n"
        "[_LANG.python.loaders]\n"
        'json = { ref = "tests.helpers.loaders:param_loader", '
        'args = ["$path"], kwargs = { grid = "5x5" } }\n'
    )
    db = Database(
        datasets_toml=str(toml), datasets_folder=str(tmp_path / "cache"), persist=False
    )
    db.skip_checksum = True
    db.register_dataset(f"file://{src}/data.json", name="j", persist=False)

    result = load_dataset(db, "j")
    assert result[0] == "param"
    assert L.param_loader.last_call["grid"] == "5x5"           # kwargs applied
    assert os.path.isfile(L.param_loader.last_call["path"])    # $path substituted


def test_load_dataset_extracted_archive_returns_dir(tmp_path):
    """An extracted-archive entry with no loader returns the extracted directory."""
    from datamanifest.database import Database
    from datamanifest.pipelines import load_dataset

    served = tmp_path / "served"
    served.mkdir()
    _make_zip(served / "bar.zip", {"inner.txt": b"hello inside"})

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.skip_checksum = True

    with _http_server(served) as base:
        db.register_dataset(f"{base}/bar.zip", name="bar", extract=True, persist=False)
        result = load_dataset(db, "bar")

    assert os.path.isdir(result)
    assert (Path(result) / "inner.txt").read_bytes() == b"hello inside"


# ----- Item 16: local_path, skip_download, project-root resolution -----

def test_get_dataset_path_storage_path_absolute(tmp_path):
    """An exact (no-$key) absolute storage_path is returned verbatim."""
    from datamanifest.database import get_dataset_path, init_dataset_entry

    entry = init_dataset_entry(key="mydata", storage_path="/abs/path/to/data.csv")
    path = get_dataset_path(entry, datasets_folder=str(tmp_path), project_root="/ignored")
    assert path == "/abs/path/to/data.csv"


def test_skip_download_with_storage_path_resolves_it(tmp_path):
    """skip_download + an explicit storage_path resolves the path (the uri is
    returned verbatim only when there is no explicit path) - the rule shared
    with the Julia tool."""
    from datamanifest.database import get_dataset_path, init_dataset_entry

    entry = init_dataset_entry(
        uri="https://example.com/big.zip", key="example.com/big.zip",
        skip_download=True, storage_path="vendor/big.zip")
    path = get_dataset_path(entry, project_root=str(tmp_path))
    assert path == str(tmp_path / "vendor" / "big.zip")

    bare = init_dataset_entry(
        uri="/data/local/big.zip", key="big.zip", skip_download=True)
    assert get_dataset_path(bare, project_root=str(tmp_path)) == "/data/local/big.zip"


def test_get_dataset_path_storage_path_relative(tmp_path):
    """A relative storage_path is anchored to project_root when available."""
    from datamanifest.database import get_dataset_path, init_dataset_entry

    entry = init_dataset_entry(key="mydata", storage_path="data/foo.csv")
    path = get_dataset_path(entry, project_root=str(tmp_path))
    assert path == str(tmp_path / "data" / "foo.csv")


def test_get_dataset_path_storage_path_no_root():
    """A relative storage_path is returned as-is when project_root is empty."""
    from datamanifest.database import get_dataset_path, init_dataset_entry

    entry = init_dataset_entry(key="mydata", storage_path="data/foo.csv")
    path = get_dataset_path(entry, project_root="")
    assert path == "data/foo.csv"


def test_get_dataset_path_v5_default_is_shared_store(tmp_path):
    """spec-v5: the default storage_path ``$datasets_dir/$key`` ⇒ the
    machine-wide shared keyed store with no configuration."""
    import os

    import platformdirs

    from datamanifest.database import get_dataset_path, init_dataset_entry

    entry = init_dataset_entry("https://example.com/host/f.csv")
    assert get_dataset_path(entry, project_root=str(tmp_path)) == os.path.join(
        platformdirs.user_data_dir(), "datamanifest", "shared", "datasets",
        entry.key,
    )


def test_get_dataset_path_v4_datasets_dir_env(tmp_path, monkeypatch):
    """spec-v4: ``DATAMANIFEST_DATASETS_DIR`` overrides the datasets folder; the
    path is ``<datasets_dir>/<key>`` (no ``datasets/`` prefix)."""
    from datamanifest.database import get_dataset_path, init_dataset_entry

    data_dir = tmp_path / "appdata"
    monkeypatch.setenv("DATAMANIFEST_DATASETS_DIR", str(data_dir))

    entry = init_dataset_entry("https://example.com/host/f.csv")
    assert get_dataset_path(entry, project_root=str(tmp_path)) == str(
        data_dir / entry.key
    )


def test_download_honors_manifest_datasets_dir(tmp_path):
    """download writes to [_STORAGE].datasets_dir (not the repo-local default),
    so the same path resolve_existing_path/list reads back. Regression: the
    download pipeline used to omit storage_config from get_dataset_path."""
    from datamanifest.database import Database, resolve_existing_path
    from datamanifest.pipelines import download_dataset

    store = tmp_path / "store"
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("payload")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "datasets.toml").write_text(
        f'[_STORAGE]\ndatasets_dir = "{store}/ds"\n\n'
        f'[d]\nuri = "file://{src}/f.txt"\nsha256 = ""\n'
    )

    db = Database(datasets_toml=str(proj / "datasets.toml"), persist=False)
    db.datasets_toml = str(proj / "datasets.toml")  # so get_project_root resolves
    path = download_dataset(db, "d")

    assert str(store / "ds") in path                     # honored datasets_dir
    assert not (proj / "datasets").exists()              # not the repo-local default
    assert os.path.isfile(path)
    assert resolve_existing_path(db, db.datasets["d"]) == path  # download == read


def test_skip_download_raises_if_path_missing(tmp_path):
    """skip_download=True raises FileNotFoundError with 'documented URI is' when path absent."""
    from datamanifest.database import Database
    from datamanifest.pipelines import download_dataset

    db = Database(datasets_folder=str(tmp_path / "cache"), persist=False)
    db.datasets_toml = ""
    db.register_dataset(
        "https://example.com/data.csv",
        name="skipme",
        skip_download=True,
        persist=False,
    )

    with pytest.raises(FileNotFoundError, match="documented URI is"):
        download_dataset(db, "skipme")


# ----- Item 17: default-database convenience + public API -----

def test_public_api_imports():
    """All expected names are importable directly from datamanifest."""
    from datamanifest import (  # noqa: F401
        Database,
        DatasetEntry,
        add,
        delete_dataset,
        download_dataset,
        download_datasets,
        get_dataset_path,
        load_dataset,
        register_dataset,
    )


def test_default_db_get_dataset_path(tmp_path):
    """get_dataset_path() from the top-level API resolves against a tmp toml."""
    import json
    from datamanifest import database as _db_module

    # Reset the singleton so this test always gets a fresh one.
    _db_module._default_db = None

    toml_content = '[mydata]\nuri = "https://example.com/data.json"\nformat = "json"\n'
    toml_path = tmp_path / "datasets.toml"
    toml_path.write_text(toml_content)

    import datamanifest
    old_env = os.environ.get("DATAMANIFEST_TOML")
    try:
        os.environ["DATAMANIFEST_TOML"] = str(toml_path)
        _db_module._default_db = None  # force re-init with new env
        path = datamanifest.get_dataset_path("mydata")
        assert "mydata" in path or "example.com" in path or path.endswith("data.json")
    finally:
        if old_env is None:
            os.environ.pop("DATAMANIFEST_TOML", None)
        else:
            os.environ["DATAMANIFEST_TOML"] = old_env
        _db_module._default_db = None


def test_find_default_toml_discovery(tmp_path):
    """A datasets toml is discovered by walking up, even without pyproject.toml."""
    from datamanifest.config import _find_default_toml

    # Capital-D Datasets.toml (e.g. a Julia project) found from a subdir.
    (tmp_path / "Datasets.toml").write_text("[datasets]\n")
    sub = tmp_path / "sub" / "deeper"
    sub.mkdir(parents=True)
    assert _find_default_toml(str(sub)) == str(tmp_path / "Datasets.toml")

    # A pyproject.toml anchors the default path even if the toml is absent yet
    # (the canonical datamanifest.toml).
    proj = tmp_path / "py"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert _find_default_toml(str(proj)) == str(proj / "datamanifest.toml")


def test_find_default_toml_precedence(tmp_path):
    """Discovery order shared with the Julia tool: datamanifest.toml >
    DataManifest.toml > datasets.toml > Datasets.toml (first existing wins)."""
    from datamanifest.config import TOML_FILENAMES, _find_default_toml

    assert TOML_FILENAMES == ["datamanifest.toml", "DataManifest.toml",
                              "datasets.toml", "Datasets.toml"]

    (tmp_path / "datasets.toml").write_text("[datasets]\n")
    (tmp_path / "DataManifest.toml").write_text("[datasets]\n")
    assert _find_default_toml(str(tmp_path)) == str(tmp_path / "DataManifest.toml")

    (tmp_path / "datamanifest.toml").write_text("[datasets]\n")
    assert _find_default_toml(str(tmp_path)) == str(tmp_path / "datamanifest.toml")


def test_find_default_toml_none(tmp_path):
    """Returns empty string when neither a toml nor pyproject.toml exists."""
    from datamanifest.config import _find_default_toml

    assert _find_default_toml(str(tmp_path)) == ""


def test_default_db_missing_toml():
    """Default-DB functions raise RuntimeError when no datamanifest.toml is found."""
    import datamanifest
    from datamanifest import database as _db_module

    _db_module._default_db = None
    old_env = {k: os.environ.pop(k, None) for k in ("DATAMANIFEST_TOML", "DATASETS_TOML")}
    old_cwd = os.getcwd()
    import tempfile
    try:
        # Switch to a tmp dir with no pyproject.toml / datasets.toml
        tmpdir = tempfile.mkdtemp()
        os.chdir(tmpdir)
        _db_module._default_db = None
        with pytest.raises(RuntimeError, match="No datamanifest.toml"):
            datamanifest.get_dataset_path("anything")
    finally:
        os.chdir(old_cwd)
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v
        _db_module._default_db = None


