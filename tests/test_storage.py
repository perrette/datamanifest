"""spec-v4 storage-location resolution (the two-field model + $-symbols)."""

import os

import platformdirs

from datamanifest.store import locations


def test_default_fields_are_repo_local(tmp_path):
    root = str(tmp_path)
    assert locations.datasets_dir(project_root=root) == os.path.join(root, "datasets")
    assert locations.datacache_dir(project_root=root) == os.path.join(root, "cached")


def test_dataset_path_default_is_datasets_dir_key(tmp_path):
    root = str(tmp_path)
    assert locations.dataset_path("", "host/a.csv", project_root=root) == \
        os.path.join(root, "datasets", "host/a.csv")


def test_user_data_dir_symbol_is_bare(tmp_path):
    # $user_data_dir has NO appname (bare platformdirs).
    got = locations.dataset_path(
        "$user_data_dir/pool/$key", "host/a.csv", project_root=str(tmp_path),
    )
    assert got == os.path.join(platformdirs.user_data_dir(), "pool", "host/a.csv")


def test_exact_storage_path_is_verbatim(tmp_path):
    got = locations.dataset_path(
        "/mnt/archive/c.csv", "ignored/key", project_root=str(tmp_path),
    )
    assert got == "/mnt/archive/c.csv"


def test_env_overrides_field(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAMANIFEST_DATASETS_DIR", "/data/pool")
    assert locations.dataset_path("", "k", project_root=str(tmp_path)) == "/data/pool/k"


def test_host_override(tmp_path):
    cfg = {"_HOST": {"login*": {"datacache_dir": "/work/cache"}}}
    got = locations.datacache_dir(
        project_root=str(tmp_path), storage_config=cfg, host="login3.hpc.edu",
    )
    assert got == "/work/cache"


def test_is_local_path(tmp_path):
    root = str(tmp_path)
    # Default (relative) datasets_dir is repo-local.
    assert locations.is_local_path("$datasets_dir/$key", key="k", project_root=root)
    # An absolute machine-global path is not local.
    assert not locations.is_local_path("/data/pool/k", project_root=root)


def test_is_user_managed():
    assert locations.is_user_managed("/mnt/archive/c.csv")
    assert not locations.is_user_managed("")  # default ⇒ keyed
    assert not locations.is_user_managed("$scratch/$key")  # $key ⇒ keyed
