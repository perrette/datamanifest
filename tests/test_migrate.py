"""spec-v3 → spec-v4 storage migration (``datamanifest migrate``)."""

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from datamanifest.migrate import migrate_manifest


def _write(tmp_path, body):
    toml = tmp_path / "datasets.toml"
    toml.write_text(body)
    return toml


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
