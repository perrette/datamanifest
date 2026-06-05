"""spec-v3 → spec-v4 storage migration (``datamanifest migrate``)."""

import tomllib

import platformdirs

from datamanifest.migrate import migrate_manifest


def _user_data_appname():
    return platformdirs.user_data_dir("datamanifest")


def _write(tmp_path, body):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "myproj"\n')
    toml = tmp_path / "datasets.toml"
    toml.write_text(body)
    return toml


def test_migrate_writes_two_fields_and_freezes_deviations(tmp_path):
    toml = _write(tmp_path, """
[_META]
schema = 1

[_STORAGE]
scratch = "/scratch/bob"

[plain]
uri = "https://example.com/host/a.csv"

[custom]
uri = "https://example.com/host/b.csv"
store = "$scratch"

[exact]
uri = "https://example.com/host/c.csv"
local_path = "/mnt/archive/c.csv"
""")
    summary = migrate_manifest(str(toml), env={})
    assert "Updated" in summary

    with open(toml, "rb") as f:
        data = tomllib.load(f)

    storage = data["_STORAGE"]
    assert storage["datasets_dir"] == "$user_data_dir/datamanifest/datasets"
    assert storage["datacache_dir"] == "$user_cache_dir/datamanifest/cached/myproj"
    # User-defined symbols are preserved; the retired keys are gone.
    assert storage["scratch"] == "/scratch/bob"
    assert "default" not in storage

    # A default-store dataset is covered by datasets_dir — no per-dataset field.
    assert "storage_path" not in data["plain"]
    assert "store" not in data["plain"]
    # A deviating dataset is frozen to its actual old absolute location.
    assert data["custom"]["storage_path"] == "/scratch/bob/datasets/example.com/host/b.csv"
    assert data["exact"]["storage_path"] == "/mnt/archive/c.csv"


def test_migrate_dry_run_does_not_write(tmp_path):
    toml = _write(tmp_path, "[_META]\nschema = 1\n\n[d]\nuri = \"https://x.com/h/a.csv\"\n")
    before = toml.read_text()
    summary = migrate_manifest(str(toml), env={}, dry_run=True)
    assert "Would update" in summary
    assert toml.read_text() == before


def test_migrate_default_dataset_path_matches_new_datasets_dir(tmp_path):
    """The frozen datasets_dir resolves a default dataset to its old location."""
    from datamanifest.store import locations

    toml = _write(tmp_path, "[_META]\nschema = 1\n\n[d]\nuri = \"https://x.com/h/a.csv\"\n")
    migrate_manifest(str(toml), env={})
    with open(toml, "rb") as f:
        cfg = tomllib.load(f)["_STORAGE"]

    # $datasets_dir/$key under the migrated config == the old default location.
    resolved = locations.dataset_path(
        "", "x.com/h/a.csv", storage_config=cfg,
    )
    assert resolved == _user_data_appname() + "/datasets/x.com/h/a.csv"
