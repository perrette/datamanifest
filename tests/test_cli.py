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
    for sub in ["list", "download", "path", "add", "remove", "show", "verify", "update-checksums", "init", "where"]:
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


# ----- update-checksums -----

def test_update_checksums_help():
    result = _run("update-checksums", "--help")
    assert result.returncode == 0
    assert "--dry-run" in result.stdout


def test_update_checksums_dry_run_does_not_write(tmp_path):
    # A manifest whose only entry has no file on disk: a dry-run must report
    # nothing to change and leave the manifest byte-for-byte untouched.
    src = tmp_path / "datasets.toml"
    src.write_text('[absent]\nuri = "https://h/absent.bin"\nsha256 = "stale"\n')
    before = src.read_bytes()
    env = _env_with_toml(src)
    result = _run("update-checksums", "--dry-run", env=env)
    assert result.returncode == 0, result.stderr
    assert src.read_bytes() == before


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


# ----- format (canonical / cross-tool byte-identity serializer) -----

def test_format_sorts_canonically_and_is_idempotent(tmp_path):
    src = tmp_path / "m.toml"
    # deliberately unsorted: keys within [zeta] reversed, table after _META
    src.write_text("[zeta]\nb = 2\na = 1\n\n[_META]\nschema = 1\n")
    r1 = _run("format", str(src))
    assert r1.returncode == 0, r1.stderr
    out = r1.stdout
    # `_` (0x5F) sorts before `z` (0x7A): [_META] precedes [zeta]
    assert out.index("[_META]") < out.index("[zeta]")
    # within [zeta], a before b
    assert out.index("a = 1") < out.index("b = 2")
    # idempotent: formatting the canonical output again yields identical bytes
    r2 = subprocess.run([_BIN, "format", "-"], input=out, capture_output=True, text=True)
    assert r2.returncode == 0
    assert r2.stdout == out


def test_format_in_place(tmp_path):
    src = tmp_path / "m.toml"
    src.write_text("[b]\nx = 1\n\n[a]\ny = 2\n")
    result = _run("format", "--in-place", str(src))
    assert result.returncode == 0
    text = src.read_text()
    assert text.index("[a]") < text.index("[b]")
