"""Subprocess-based smoke tests for the datamanifest CLI (Item 18)."""

import os
import subprocess
import sys

import pytest

# Resolve the CLI binary from the same interpreter that runs this test.
_BIN = os.path.join(os.path.dirname(sys.executable), "datamanifest")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATASETS_TOML = os.path.join(_REPO_ROOT, "datasets.toml")


def _run(*args, env=None):
    """Run *args* via the CLI binary and return the CompletedProcess."""
    return subprocess.run(
        [_BIN, *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _env_with_toml(toml_path=_DATASETS_TOML):
    e = dict(os.environ)
    e["DATAMANIFEST_TOML"] = str(toml_path)
    return e


# ----- version / help -----

def test_version():
    result = _run("--version")
    assert result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    assert output.startswith("datamanifest ")


def test_help_lists_all_subcommands():
    result = _run("--help")
    assert result.returncode == 0
    for sub in ["list", "download", "path", "add", "remove", "show", "verify", "init", "where"]:
        assert sub in result.stdout, f"subcommand {sub!r} missing from --help output"


# ----- list -----

def test_list_missing():
    result = _run("list", "--missing", env=_env_with_toml())
    assert result.returncode == 0


def test_list_help():
    result = _run("list", "--help")
    assert result.returncode == 0
    assert "--present" in result.stdout
    assert "--missing" in result.stdout


# ----- path -----

def test_path_single_line():
    result = _run("path", "herzschuh2023", env=_env_with_toml())
    assert result.returncode == 0
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"path should print exactly one line, got: {result.stdout!r}"


# ----- where -----

def test_where():
    result = _run("where", env=_env_with_toml())
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert any(l.startswith("datasets_toml=") for l in lines)
    assert any(l.startswith("datasets_folder=") for l in lines)


# ----- init -----

def test_init_creates_file(tmp_path):
    result = _run("init", "--folder", str(tmp_path))
    assert result.returncode == 0
    assert (tmp_path / "datasets.toml").exists()


def test_init_refuses_overwrite_without_force(tmp_path):
    _run("init", "--folder", str(tmp_path))
    result = _run("init", "--folder", str(tmp_path))
    assert result.returncode != 0


def test_init_force_overwrites(tmp_path):
    _run("init", "--folder", str(tmp_path))
    result = _run("init", "--folder", str(tmp_path), "--force")
    assert result.returncode == 0
