"""Cross-language fetch (fetch-ladder rung 3) — offline, injected runner.

No real ``julia`` is ever invoked: the subprocess runner is replaced with a
stub that captures the argv and simulates materialization by writing the
dataset bytes (plus the ``.complete`` marker) into the shared store. The
``julia`` availability and the Julia-project probe are monkeypatched so the
tests are deterministic offline.
"""

import glob
import os

import pytest

from datamanifest import delegation
from datamanifest.database import Database, init_dataset_entry
from datamanifest.pipelines import download_dataset


# ----- foreign_fetcher_lang -----

def test_foreign_fetcher_lang_detects_julia():
    entry = init_dataset_entry(
        name="b", sha256="x", _LANG={"julia": {"fetcher": "MyPkg:build_b"}}
    )
    assert delegation.foreign_fetcher_lang(entry) == "julia"


def test_foreign_fetcher_lang_detects_table_form():
    entry = init_dataset_entry(
        name="b", _LANG={"julia": {"fetcher": {"ref": "MyPkg:build_b"}}}
    )
    assert delegation.foreign_fetcher_lang(entry) == "julia"


def test_foreign_fetcher_lang_none_for_python_shell_uri():
    entry = init_dataset_entry(
        uri="https://example.com/a.csv",
        _LANG={
            "python": {"fetcher": "mod:func"},
            "shell": {"fetcher": "make -o $download_path"},
        },
    )
    assert delegation.foreign_fetcher_lang(entry) is None


def test_foreign_fetcher_lang_none_without_lang():
    entry = init_dataset_entry(uri="https://example.com/a.csv")
    assert delegation.foreign_fetcher_lang(entry) is None


# ----- julia_project -----

def _write_project_toml(path, with_dep=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if with_dep:
        body = (
            "[deps]\n"
            'DataManifest = "b8ee69ef-a20a-4d38-bceb-a68d72817f72"\n'
            'Downloads = "f43a241f-c20a-4ad4-852c-f6b1247861c6"\n'
        )
    else:
        body = '[deps]\nDownloads = "f43a241f-c20a-4ad4-852c-f6b1247861c6"\n'
    with open(path, "w") as f:
        f.write(body)


def test_julia_project_finds_datamanifest_dep(tmp_path):
    _write_project_toml(str(tmp_path / "Project.toml"))
    found = delegation.julia_project(str(tmp_path), env={})
    assert found == str(tmp_path)


def test_julia_project_none_without_dep(tmp_path):
    _write_project_toml(str(tmp_path / "Project.toml"), with_dep=False)
    assert delegation.julia_project(str(tmp_path), env={}) is None


def test_julia_project_walks_up_from_project_root(tmp_path):
    _write_project_toml(str(tmp_path / "Project.toml"))
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert delegation.julia_project(str(deep), env={}) == str(tmp_path)


def test_julia_project_honors_julia_project_env(tmp_path):
    proj = tmp_path / "env"
    _write_project_toml(str(proj / "Project.toml"))
    env = {"JULIA_PROJECT": str(proj)}
    # project_root has no Project.toml, so the env var is what resolves it.
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert delegation.julia_project(str(other), env=env) == str(proj)


# ----- download_dataset rung 3 -----

def _julia_db(tmp_path, monkeypatch, *, delegate=True, with_julia=True):
    """Build a Database whose sole dataset has only a _LANG.julia.fetcher.

    Sets DATAMANIFEST_DATA_DIR so the $data store root is under tmp_path and the
    composed path is <root>/datasets/<key>. Discovers a Julia project (with the
    DataManifest dep) at the manifest directory, and forces `julia` present.
    """
    data_dir = tmp_path / "store"
    data_dir.mkdir()
    monkeypatch.setenv("DATAMANIFEST_DATASETS_DIR", str(data_dir / "datasets"))

    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project_toml(str(proj / "Project.toml"))
    datasets_toml = proj / "datasets.toml"
    datasets_toml.write_text("[_META]\nschema = 1\n")

    db = Database(datasets_toml=str(datasets_toml), persist=False)
    # persist=False leaves datasets_toml empty; set it so get_project_root()
    # resolves the manifest directory (the real CLI uses persist=True).
    db.datasets_toml = os.path.abspath(str(datasets_toml))
    db.register_dataset(
        name="myset",
        key="example.com/myset.csv",
        sha256="",
        _LANG={"julia": {"fetcher": "MyPkg:build_myset"}},
        delegate=delegate,
        persist=False,
    )

    monkeypatch.setattr(delegation, "julia_available", lambda env=None: with_julia)
    return db, data_dir


def test_rung3_invokes_julia_and_returns_path(tmp_path, monkeypatch):
    db, data_dir = _julia_db(tmp_path, monkeypatch)
    captured = {}

    def fake_runner(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        # Simulate Julia materializing <root>/datasets/<key> + .complete.
        key = "example.com/myset.csv"
        dest = data_dir / "datasets" / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("a,b\n1,2\n")
        (dest.parent / (dest.name + ".complete")).write_text("")

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(delegation, "_runner", fake_runner)

    path = download_dataset(db, "myset")

    assert os.path.isfile(path)
    assert path == str(data_dir / "datasets" / "example.com/myset.csv")
    argv = captured["argv"]
    assert argv[0] == "julia"
    assert argv[1] == f"--project={tmp_path / 'proj'}"
    assert argv[2] == "-e"
    expected_toml = os.path.abspath(str(tmp_path / "proj" / "datasets.toml"))
    assert argv[3] == (
        f'using DataManifest; download_dataset(Database("{expected_toml}"), "myset")'
    )
    # The subprocess inherits os.environ so DATAMANIFEST_* overrides propagate.
    assert captured["env"] is os.environ


def test_rung3_falls_through_when_julia_absent(tmp_path, monkeypatch):
    db, _ = _julia_db(tmp_path, monkeypatch, with_julia=False)
    called = {"n": 0}

    def fake_runner(argv, **kwargs):
        called["n"] += 1

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(delegation, "_runner", fake_runner)

    # No uri, no python/shell fetcher, julia absent -> ladder bottoms out.
    with pytest.raises(ValueError, match="No fetcher available"):
        download_dataset(db, "myset")
    assert called["n"] == 0


def test_rung3_falls_through_to_uri_when_julia_absent(tmp_path, monkeypatch):
    db, data_dir = _julia_db(tmp_path, monkeypatch, with_julia=False)
    # Add a uri so rung 4 can resolve it after delegation is skipped.
    src = tmp_path / "src.csv"
    src.write_text("x,y\n3,4\n")
    db.datasets["myset"].uri = f"file://{src}"
    db.datasets["myset"].scheme = "file"
    db.datasets["myset"].path = str(src)

    def fake_runner(argv, **kwargs):
        raise AssertionError("delegation should not run when julia is absent")

    monkeypatch.setattr(delegation, "_runner", fake_runner)

    path = download_dataset(db, "myset")
    assert os.path.isfile(path)
    with open(path) as f:
        assert f.read() == "x,y\n3,4\n"


def test_rung3_skipped_when_delegate_false(tmp_path, monkeypatch):
    db, _ = _julia_db(tmp_path, monkeypatch, delegate=False, with_julia=True)

    def fake_runner(argv, **kwargs):
        raise AssertionError("delegation should not run when delegate=False")

    monkeypatch.setattr(delegation, "_runner", fake_runner)

    with pytest.raises(ValueError, match="No fetcher available"):
        download_dataset(db, "myset")


def test_native_python_fetcher_never_reaches_rung3(tmp_path, monkeypatch):
    data_dir = tmp_path / "store"
    data_dir.mkdir()
    monkeypatch.setenv("DATAMANIFEST_DATASETS_DIR", str(data_dir / "datasets"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_project_toml(str(proj / "Project.toml"))
    datasets_toml = proj / "datasets.toml"
    datasets_toml.write_text("[_META]\nschema = 1\n")

    # A python fetcher writes the bytes itself; a competing julia fetcher exists.
    import sys
    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    (mod_dir / "myfetch.py").write_text(
        "def build(download_path, **kw):\n"
        "    with open(download_path, 'w') as f:\n"
        "        f.write('python-made')\n"
    )
    sys.path.insert(0, str(mod_dir))
    try:
        db = Database(datasets_toml=str(datasets_toml), persist=False)
        db.register_dataset(
            name="myset",
            key="example.com/myset.csv",
            sha256="",
            _LANG={
                "python": {"fetcher": "myfetch:build"},
                "julia": {"fetcher": "MyPkg:build_myset"},
            },
            persist=False,
        )

        def fake_runner(argv, **kwargs):
            raise AssertionError("a native python fetcher must win at rung 1")

        monkeypatch.setattr(delegation, "_runner", fake_runner)
        monkeypatch.setattr(delegation, "julia_available", lambda env=None: True)

        path = download_dataset(db, "myset")
        with open(path) as f:
            assert f.read() == "python-made"
    finally:
        sys.path.remove(str(mod_dir))


def test_uri_with_foreign_fetcher_falls_through_on_delegation_failure(
    tmp_path, monkeypatch
):
    """A foreign fetcher is tried first (rung 3); on failure the uri (rung 4) wins."""
    db, data_dir = _julia_db(tmp_path, monkeypatch, with_julia=True)
    src = tmp_path / "src.csv"
    src.write_text("u,v\n5,6\n")
    db.datasets["myset"].uri = f"file://{src}"
    db.datasets["myset"].scheme = "file"
    db.datasets["myset"].path = str(src)

    def fake_runner(argv, **kwargs):
        # Julia ran but produced nothing on disk -> delegation fails.
        class R:
            returncode = 1

        return R()

    monkeypatch.setattr(delegation, "_runner", fake_runner)

    path = download_dataset(db, "myset")
    with open(path) as f:
        assert f.read() == "u,v\n5,6\n"


# ----- import-rule guard -----

def test_cache_imports_store_only():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for f in glob.glob(os.path.join(root, "datamanifest", "cache", "**", "*.py"),
                       recursive=True):
        with open(f) as fh:
            text = fh.read()
        if "import" in text and ("pipelines" in text or "database" in text):
            bad.append(f)
    assert bad == []
