"""Manifest migration (``datamanifest migrate``): language + storage upgrade and
disk discovery into the state file."""

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import datamanifest.migrate as M
from datamanifest.cache import CachedIndex
from datamanifest.migrate import migrate_manifest


def _write(tmp_path, body):
    toml = tmp_path / "datasets.toml"
    toml.write_text(body)
    return toml


def _isolate_legacy_roots(tmp_path, monkeypatch):
    """Point platformdirs at empty tmp dirs so discovery never probes the real
    machine's user data/cache locations. Returns the fake user_data_dir root."""
    userdata = tmp_path / "_userdata"
    usercache = tmp_path / "_usercache"
    monkeypatch.setattr(M.platformdirs, "user_data_dir",
                        lambda *a, **k: str(userdata))
    monkeypatch.setattr(M.platformdirs, "user_cache_dir",
                        lambda *a, **k: str(usercache))
    return userdata


def test_migrate_writes_default_two_fields_and_strips_retired(tmp_path):
    toml = _write(tmp_path, """
[_META]
schema = 1

[_STORAGE]
default = "$data"
scratch = "/scratch/bob"

[plain]
uri = "https://example.com/host/a.csv"

[exact]
uri = "https://example.com/host/c.csv"
local_path = "/mnt/archive/c.csv"
""")
    summary = migrate_manifest(str(toml))
    assert "Updated" in summary

    with open(toml, "rb") as f:
        data = tomllib.load(f)

    storage = data["_STORAGE"]
    # The two fields are written at their spec-v4 defaults.
    assert storage["datasets_dir"] == "datasets"
    assert storage["datacache_dir"] == "cached"
    # User-defined symbols are preserved; the retired keys are gone.
    assert storage["scratch"] == "/scratch/bob"
    assert "default" not in storage

    # A plain dataset is covered by datasets_dir — no per-dataset field.
    assert "storage_path" not in data["plain"]
    # An explicit local_path is carried over losslessly to storage_path.
    assert data["exact"]["storage_path"] == "/mnt/archive/c.csv"
    assert "local_path" not in data["exact"]


def test_migrate_surfaces_dropped_store_selector(tmp_path):
    toml = _write(tmp_path, """
[_META]
schema = 1

[custom]
uri = "https://example.com/host/b.csv"
store = "$scratch"
""")
    summary = migrate_manifest(str(toml))
    assert "Needs manual attention" in summary
    assert "custom" in summary

    with open(toml, "rb") as f:
        data = tomllib.load(f)
    # The retired selector is dropped, and no bogus storage_path is invented.
    assert "store" not in data["custom"]
    assert "storage_path" not in data["custom"]


def test_migrate_promotes_python_and_julia_inline(tmp_path):
    """migrate also upgrades v0 inline-code bindings: python= and a flat julia=
    are promoted to the [<ds>._LANG.<lang>].fetcher form."""
    toml = _write(tmp_path, """
[_META]
schema = 0

[a]
uri = "https://h/a.csv"
python = "mymod:fetch"

[b]
uri = "https://h/b.csv"
julia = "MyMod.fetch"
""")
    summary = migrate_manifest(str(toml))
    assert "a.python" in summary and "b.julia" in summary

    with open(toml, "rb") as f:
        data = tomllib.load(f)
    assert data["a"]["_LANG"]["python"]["fetcher"] == "mymod:fetch"
    assert "python" not in data["a"]                    # flat field promoted away
    assert data["b"]["_LANG"]["julia"]["fetcher"] == "MyMod.fetch"
    assert "julia" not in data["b"]
    assert data["_META"]["schema"] == 1


def test_migrate_dry_run_does_not_write(tmp_path):
    toml = _write(tmp_path, "[_META]\nschema = 1\n\n[d]\nuri = \"https://x.com/h/a.csv\"\n")
    before = toml.read_text()
    summary = migrate_manifest(str(toml), dry_run=True)
    assert "Would update" in summary
    assert toml.read_text() == before


def test_migrate_default_dataset_resolves_repo_local(tmp_path):
    """After migration a plain dataset resolves under the repo-local default."""
    from datamanifest.store import locations

    toml = _write(tmp_path, "[_META]\nschema = 1\n\n[d]\nuri = \"https://x.com/h/a.csv\"\n")
    migrate_manifest(str(toml))
    with open(toml, "rb") as f:
        cfg = tomllib.load(f)["_STORAGE"]

    resolved = locations.dataset_path(
        "", "x.com/h/a.csv", project_root=str(tmp_path), storage_config=cfg,
    )
    assert resolved == str(tmp_path / "datasets" / "x.com/h/a.csv")


# ----- discovery → state file --------------------------------------------------

def _state(tmp_path):
    return CachedIndex.read(tmp_path / ".datamanifest-state.toml")


def test_migrate_discovers_legacy_dataset_into_state(tmp_path, monkeypatch):
    """Data sitting in a legacy location (the old $user_data_dir/datamanifest/
    datasets) is found and its location recorded in the state file; the manifest
    stays at the clean repo-local default."""
    userdata = _isolate_legacy_roots(tmp_path, monkeypatch)
    key = "example.com/a.csv"
    legacy = userdata / "datasets" / key
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"data")

    toml = _write(tmp_path, '[_META]\nschema = 1\n\n[a]\nuri = "https://example.com/a.csv"\n')
    migrate_manifest(str(toml), no_input=True)

    idx = _state(tmp_path)
    sp = idx.dataset_path_of(key)
    assert sp, "legacy data should be recorded in the state file"
    assert os.path.abspath(os.path.join(tmp_path, sp)) == str(legacy)
    # Manifest keeps the clean default (no machine path committed).
    assert tomllib.loads(toml.read_text())["_STORAGE"]["datasets_dir"] == "datasets"


def test_migrate_records_every_discovered_dataset(tmp_path, monkeypatch):
    """The state file is a complete inventory: data is recorded even when it's
    already at the repo-local default ./datasets (transparency / full migration)."""
    _isolate_legacy_roots(tmp_path, monkeypatch)
    key = "example.com/a.csv"
    here = tmp_path / "datasets" / key
    here.parent.mkdir(parents=True)
    here.write_bytes(b"data")

    toml = _write(tmp_path, '[_META]\nschema = 1\n\n[a]\nuri = "https://example.com/a.csv"\n')
    migrate_manifest(str(toml), no_input=True)

    assert _state(tmp_path).dataset_path_of(key) == os.path.join("datasets", key)


def test_migrate_skips_user_managed_and_skip_download(tmp_path, monkeypatch):
    """Discovery leaves explicit storage_path / skip_download datasets alone."""
    userdata = _isolate_legacy_roots(tmp_path, monkeypatch)
    key = "example.com/a.csv"
    legacy = userdata / "datasets" / key
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"data")

    toml = _write(tmp_path,
                  '[_META]\nschema = 1\n\n[a]\nuri = "https://example.com/a.csv"\n'
                  'storage_path = "/custom/a.csv"\n')
    migrate_manifest(str(toml), no_input=True)
    # storage_path is user-managed → not adopted by discovery.
    assert not (tmp_path / ".datamanifest-state.toml").exists() \
        or _state(tmp_path).dataset_path_of(key) == ""


def test_migrate_discovers_cached_artifact(tmp_path, monkeypatch):
    """A produced artifact under the repo-local ./cached is recorded in the
    datacache namespace of the state file."""
    from datamanifest.cache._sidecars import write_config

    _isolate_legacy_roots(tmp_path, monkeypatch)
    art = tmp_path / "cached" / "mypkg.run" / "abc123"
    art.mkdir(parents=True)
    (art / "data.txt").write_text("v")
    write_config(str(art), "mypkg.run", "abc123", {"x": 1})
    (art / ".complete").write_text("")

    toml = _write(tmp_path, "[_META]\nschema = 1\n")
    migrate_manifest(str(toml), no_input=True)

    idx = _state(tmp_path)
    assert idx.instance_path_of(cachetype="mypkg.run", version="", hash="abc123")


def test_candidate_datacache_roots_scopes_global_cache(tmp_path):
    """The global cache is included only scoped to this project's id — never the
    bare shared root (which would claim other projects' artifacts)."""
    roots = M._candidate_datacache_roots(str(tmp_path), {})
    pid = M._legacy_project_id(str(tmp_path))
    assert any(r.endswith(os.path.join("cached", pid)) for r in roots)
    assert not any(r.endswith(os.sep + "cached") and pid not in r
                   for r in roots if "_usercache" not in r and str(tmp_path) not in r)


def test_migrate_dry_run_discovers_but_writes_nothing(tmp_path, monkeypatch):
    userdata = _isolate_legacy_roots(tmp_path, monkeypatch)
    key = "example.com/a.csv"
    legacy = userdata / "datasets" / key
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"data")
    toml = _write(tmp_path, '[_META]\nschema = 1\n\n[a]\nuri = "https://example.com/a.csv"\n')
    before = toml.read_text()

    summary = migrate_manifest(str(toml), no_input=True, dry_run=True)
    assert "Would update" in summary and "a → " in summary       # reported
    assert toml.read_text() == before                            # manifest untouched
    assert not (tmp_path / ".datamanifest-state.toml").exists()  # state not written


def test_migrate_pool_override(tmp_path, monkeypatch):
    """`--datasets-pools` overrides the built-in discovery roots: data is found
    only in the given pool."""
    _isolate_legacy_roots(tmp_path, monkeypatch)
    alt = tmp_path / "alt"
    key = "example.com/a.csv"
    (alt / "example.com").mkdir(parents=True)
    (alt / key).write_bytes(b"data")
    toml = _write(tmp_path, '[_META]\nschema = 1\n\n[a]\nuri = "https://example.com/a.csv"\n')

    migrate_manifest(str(toml), no_input=True, datasets_pools=[str(alt)])
    sp = _state(tmp_path).dataset_path_of(key)
    assert sp and os.path.abspath(os.path.join(tmp_path, sp)) == str(alt / key)


def test_migrate_host_dir_footer_when_accepted(tmp_path, monkeypatch):
    """When the dominant-root datasets_dir offer is accepted, the summary says new
    downloads go to that host location (not the repo-local default)."""
    import socket

    monkeypatch.setattr(M, "_confirm", lambda *a, **k: True)   # accept the offer
    _isolate_legacy_roots(tmp_path, monkeypatch)
    alt = tmp_path / "alt"
    key = "h/a.csv"
    (alt / "h").mkdir(parents=True)
    (alt / key).write_bytes(b"x")
    toml = _write(tmp_path, '[_META]\nschema = 1\n\n[a]\nuri = "https://h/a.csv"\n')

    summary = migrate_manifest(str(toml), datasets_pools=[str(alt)])
    assert "new downloads on this host go to" in summary
    assert "repo-local defaults" not in summary
    data = tomllib.loads(toml.read_text())
    assert data["_STORAGE"]["_HOST"][socket.gethostname()]["datasets_dir"] == str(alt)
