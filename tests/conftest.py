"""Shared test fixtures.

Every test runs against an isolated per-test HOME / XDG tree, so the
machine-global defaults (the shared dataset store under ``$user_data_dir``,
the per-project cache under ``$user_cache_dir``) and the scoped config files
(``~/.config/datamanifest/config.toml``) never touch — or get polluted by —
the developer's real account.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    # A sibling of (never inside) each test's tmp_path: tests use tmp_path as a
    # project root, and the fake home must not resolve as repo-local.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # A developer's own environment must not steer test resolution.
    for var in ("DATAMANIFEST_TOML", "DATASETS_TOML", "DATAMANIFEST_DATASETS_DIR",
                "DATAMANIFEST_DATACACHE_DIR", "DATAMANIFEST_DATASETS_POOLS",
                "DATAMANIFEST_DATACACHE_POOLS"):
        monkeypatch.delenv(var, raising=False)
    return home
