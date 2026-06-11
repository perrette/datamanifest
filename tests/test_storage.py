"""spec-v4 storage-location resolution (the two-field model + $-symbols)."""

import os

import platformdirs

from datamanifest.store import locations


def test_default_fields_are_machine_global(tmp_path):
    # spec-v5: datasets default to the machine-wide shared keyed store; the
    # produced cache is per-project ($project = the project-root basename).
    root = str(tmp_path)
    assert locations.datasets_dir(project_root=root) == os.path.join(
        platformdirs.user_data_dir(), "datamanifest", "shared", "datasets")
    assert locations.datacache_dir(project_root=root) == os.path.join(
        platformdirs.user_cache_dir(), "datamanifest", "projects",
        os.path.basename(root), "cached")


def test_project_symbol_is_overridable(tmp_path):
    root = str(tmp_path)
    got = locations.datacache_dir(
        project_root=root, storage_config={"project": "myproj"})
    assert got == os.path.join(platformdirs.user_cache_dir(), "datamanifest",
                               "projects", "myproj", "cached")


def test_dataset_path_default_is_datasets_dir_key(tmp_path):
    root = str(tmp_path)
    assert locations.dataset_path("", "host/a.csv", project_root=root) == \
        os.path.join(platformdirs.user_data_dir(), "datamanifest", "shared",
                     "datasets", "host/a.csv")


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
    # The default datasets_dir is machine-global (syncable), not repo-local.
    assert not locations.is_local_path("$datasets_dir/$key", key="k", project_root=root)
    # An explicitly repo-local datasets_dir is local.
    assert locations.is_local_path(
        "$datasets_dir/$key", key="k", project_root=root,
        storage_config={"datasets_dir": "datasets"})
    # An absolute machine-global path is not local.
    assert not locations.is_local_path("/data/pool/k", project_root=root)


def test_is_user_managed():
    assert locations.is_user_managed("/mnt/archive/c.csv")
    assert not locations.is_user_managed("")  # default ⇒ keyed
    assert not locations.is_user_managed("$scratch/$key")  # $key ⇒ keyed


# ----- scoped configuration (the v5 ladder) -----------------------------------

def test_scoped_config_layer_precedence(tmp_path):
    """local config > manifest [_STORAGE] > user-global config; within a layer
    the _HOST glob wins over the base value; env beats everything."""
    cfg = locations.ScopedConfig(
        local={"_HOST": {"login*": {"datasets_dir": "/local-host"}},
               "datasets_dir": "/local-base"},
        manifest={"datasets_dir": "/manifest-base"},
        user={"datasets_dir": "/user-base"},
    )
    root = str(tmp_path)
    # local _HOST beats local base on a matching host.
    assert locations.datasets_dir(project_root=root, storage_config=cfg,
                                  host="login1") == "/local-host"
    assert locations.datasets_dir(project_root=root, storage_config=cfg,
                                  host="other") == "/local-base"
    # Without the local layer, the manifest wins over user-global.
    cfg2 = locations.ScopedConfig(manifest={"datasets_dir": "/manifest-base"},
                                  user={"datasets_dir": "/user-base"})
    assert locations.datasets_dir(project_root=root, storage_config=cfg2) \
        == "/manifest-base"
    cfg3 = locations.ScopedConfig(user={"datasets_dir": "/user-base"})
    assert locations.datasets_dir(project_root=root, storage_config=cfg3) \
        == "/user-base"
    # Env var beats every layer.
    assert locations.datasets_dir(
        project_root=root, storage_config=cfg,
        env={"DATAMANIFEST_DATASETS_DIR": "/from-env"}) == "/from-env"


def test_load_scoped_config_reads_both_files(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".datamanifest").mkdir(parents=True)
    (root / ".datamanifest" / "config.toml").write_text(
        'datasets_dir = "/from-local"\n')
    user_dir = tmp_path / "xdg" / "datamanifest"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text('datacache_dir = "/from-user"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    cfg = locations.load_scoped_config(project_root=str(root),
                                       manifest_config={"foo": "bar"})
    assert cfg.local == {"datasets_dir": "/from-local"}
    assert cfg.manifest == {"foo": "bar"}
    assert cfg.user == {"datacache_dir": "/from-user"}
    assert locations.datasets_dir(project_root=str(root), storage_config=cfg) \
        == "/from-local"
    assert locations.datacache_dir(project_root=str(root), storage_config=cfg) \
        == "/from-user"


def test_load_scoped_config_worktree_falls_back_to_main_checkout(tmp_path, monkeypatch):
    """A linked git worktree without a ``.datamanifest/config.toml`` of its own
    reads the main checkout's (the same rationale as the spec-v5.1 state-file
    fallback); a worktree-local config file always wins."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        import pytest

        pytest.skip("git not available")

    def _git(*args):
        subprocess.run(["git", *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    main = tmp_path / "main"
    main.mkdir()
    (main / "README").write_text("x\n")
    _git("-C", str(main), "init", "-q")
    _git("-C", str(main), "add", "-A")
    _git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "init")
    (main / ".datamanifest").mkdir()
    (main / ".datamanifest" / "config.toml").write_text('datasets_dir = "/from-main"\n')
    wt = tmp_path / "wt"
    _git("-C", str(main), "worktree", "add", "-q", str(wt))

    cfg = locations.load_scoped_config(project_root=str(wt))
    assert cfg.local == {"datasets_dir": "/from-main"}

    (wt / ".datamanifest").mkdir()
    (wt / ".datamanifest" / "config.toml").write_text('datasets_dir = "/from-wt"\n')
    cfg = locations.load_scoped_config(project_root=str(wt))
    assert cfg.local == {"datasets_dir": "/from-wt"}


def test_pools_resolve_from_any_layer(tmp_path):
    cfg = locations.ScopedConfig(user={"datasets_pools": ["/pool-from-user"]})
    assert locations.datasets_pools(project_root=str(tmp_path),
                                    storage_config=cfg) == ["/pool-from-user"]


def test_pool_defaults_include_repo_local_and_shared_store(tmp_path):
    root = str(tmp_path)
    pools = locations.datasets_pools(project_root=root)
    assert pools[0] == os.path.join(root, "datasets")
    assert os.path.join(platformdirs.user_data_dir(), "datamanifest", "shared",
                        "datasets") in pools
    # A rootless context skips the $repo entry instead of probing a bogus
    # filesystem-root path ("" + "/datasets").
    rootless = locations.datasets_pools(project_root="")
    assert len(rootless) == len(pools) - 1
    assert os.path.abspath("/datasets") not in rootless


def test_ensure_ignored_dir_self_ignores(tmp_path):
    d = tmp_path / ".datamanifest"
    locations.ensure_ignored_dir(str(d))
    assert (d / ".gitignore").read_text() == "*\n"
    # Idempotent; an existing (possibly user-edited) .gitignore is kept.
    (d / ".gitignore").write_text("custom\n")
    locations.ensure_ignored_dir(str(d))
    assert (d / ".gitignore").read_text() == "custom\n"
