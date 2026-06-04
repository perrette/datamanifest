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
    """Run *args* via the CLI binary and return the CompletedProcess.

    Pins ``PYTHONPATH`` to this repo so the binary imports the source tree under
    test rather than whatever the shared editable install resolves to (the
    ``datamanifest`` console-script is invoked by absolute path, so the cwd is
    not on its ``sys.path``; in a git worktree the editable install can point at
    the main checkout instead).
    """
    env = dict(os.environ if env is None else env)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _REPO_ROOT + (os.pathsep + existing if existing else "")
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
    for sub in ["list", "download", "path", "add", "remove", "show", "verify", "update-checksums", "init", "where", "migrate", "format"]:
        assert sub in result.stdout, f"subcommand {sub!r} missing from --help output"


def test_bare_invocation_lists_commands():
    # Running `datamanifest` with no subcommand lists the available commands
    # (and the -h/--help hint) instead of erroring with a bare usage line.
    result = _run()
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    for sub in ["list", "download", "add", "verify", "where"]:
        assert sub in out, f"command {sub!r} missing from bare-invocation output"
    assert "--help" in out


def test_gc_subcommand_removed():
    # spec-v3 retired the automatic collector: `gc` is gone (the maintenance
    # surface is `list --delete`). The subcommand must no longer parse.
    result = _run("gc", "--help")
    assert result.returncode != 0


# ----- list -----

def test_list_missing():
    result = _run("list", "--missing", env=_env_with_toml())
    assert result.returncode == 0


def test_list_help():
    result = _run("list", "--help")
    assert result.returncode == 0
    assert "--present" in result.stdout
    assert "--missing" in result.stdout
    # spec-v3 maintenance surface: filter + action flags on `list`.
    for flag in ["--kind", "--scope", "--orphan", "--older-than", "--format", "--delete", "--move"]:
        assert flag in result.stdout, f"list --help missing {flag}"


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


# ----- list maintenance (spec-v3: replaces gc) -----

def test_parse_duration_units():
    # Still ships — it now backs `list --older-than`.
    from datamanifest.cli import _parse_duration

    assert _parse_duration("3600") == 3600
    assert _parse_duration("90 s") == 90
    assert _parse_duration("36h") == 36 * 3600
    assert _parse_duration("7d") == 7 * 86400
    with pytest.raises(ValueError):
        _parse_duration("5furlongs")


def _orphan_artifact(cache, cachetype, key_table):
    """Materialize a produced orphan (config.toml-bearing dir under cached/)."""
    from datamanifest.cache._hash import param_hash
    from datamanifest.cache._sidecars import write_config

    h = param_hash(key_table)
    # An unscoped layout: <cache>/cached/<cachetype>/<hash> still enumerates as a
    # produced artifact (no cached.toml roots it, so it reads as an orphan).
    artifact = cache / "cached" / cachetype / h
    artifact.mkdir(parents=True)
    (artifact / "data.txt").write_text("v")
    write_config(str(artifact), cachetype, h, key_table)
    (artifact / ".complete").write_text("")
    return artifact, f"{cachetype}/{h}"


def _maint_env(tmp_path, cache):
    (tmp_path / "datasets.toml").write_text("")
    env = dict(os.environ)
    env["DATAMANIFEST_CACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(tmp_path / "datasets.toml")
    return env


def test_list_orphan_reports_unreferenced(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    result = _run("list", "--orphan", "--fields", "kind,referenced,key", env=env)
    assert result.returncode == 0, result.stderr
    assert key in result.stdout
    assert "false" in result.stdout
    assert "cached" in result.stdout


def test_list_delete_dry_run_then_applies(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    # Dry run (default): reports but keeps.
    dry = _run("list", "--orphan", "--delete", env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir(), "dry run must not delete"
    assert "dry run" in dry.stdout.lower()

    # --yes applies.
    run = _run("list", "--orphan", "--delete", "--yes", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists(), "--delete --yes should remove the orphan"


def test_list_move_relocates_orphan(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mt", {"g": "5x5"})
    dest = tmp_path / "archive"
    env = _maint_env(tmp_path, cache)

    # Dry run (default): reports but keeps, and does not create the destination.
    dry = _run("list", "--orphan", "--move", str(dest), env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir()
    assert not dest.exists()

    # --yes applies the move, preserving the <cachetype>/<hash> key path.
    run = _run("list", "--orphan", "--move", str(dest), "--yes", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists()
    h = key.split("/", 1)[1]
    assert (dest / "mt" / h / "data.txt").exists()


def test_older_than_filter_excludes_recent():
    # Unit-test the --older-than filter directly: enumeration bumps a directory's
    # atime (relatime updates it on the walk), so an end-to-end backdate is not
    # reliable — but the filter logic over the last-access field is exact.
    import datetime
    import types

    from datamanifest.cache._inspect import CacheObject
    from datamanifest.cli import _filter_objects

    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    old = (now - datetime.timedelta(days=10)).strftime(stamp)
    fresh = now.strftime(stamp)
    objs = [
        CacheObject(kind="cached", location="/x/old", key="old/h", last_access=old),
        CacheObject(kind="cached", location="/x/new", key="new/h", last_access=fresh),
    ]
    args = types.SimpleNamespace(
        kind=None, scope=None, format=None, orphan=False, older_than="1d"
    )
    kept = {o.key for o in _filter_objects(objs, args)}
    assert "old/h" in kept and "new/h" not in kept


def test_list_kind_data_lists_fetched_dataset(tmp_path):
    # A present fetched dataset (via local_path) alongside a cached orphan:
    # --kind datasets shows the fetched entry and excludes the produced artifact.
    cache = tmp_path / "cache"
    _orphan_artifact(cache, "mt", {"g": "5x5"})
    data_file = tmp_path / "external.csv"
    data_file.write_text("a,b\n1,2\n")
    toml = tmp_path / "datasets.toml"
    toml.write_text(f'[mydata]\nlocal_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_CACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(toml)

    result = _run("list", "--kind", "datasets", "--fields", "kind,key", env=env)
    assert result.returncode == 0, result.stderr
    assert "mydata" in result.stdout
    assert "datasets" in result.stdout
    assert "mt/" not in result.stdout  # the cached orphan is filtered out


def test_default_list_hides_unlisted_cached_unless_all(tmp_path):
    # The bare `list` (no maintenance flags) renders a human view of fetched
    # datasets, but cached artifacts this project's cached.toml does not root
    # are hidden by default and only surface (flagged) under --all.
    cache = tmp_path / "cache"
    _orphan_artifact(cache, "mt", {"g": "5x5"})
    data_file = tmp_path / "external.csv"
    data_file.write_text("a,b\n1,2\n")
    toml = tmp_path / "datasets.toml"
    toml.write_text(f'[mydata]\nlocal_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_CACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(toml)
    env["NO_COLOR"] = "1"  # deterministic, escape-free output

    # Default: the fetched dataset shows; the unlisted orphan does not.
    default = _run("list", env=env)
    assert default.returncode == 0, default.stderr
    assert "Datasets" in default.stdout
    assert "mydata" in default.stdout
    assert "mt/" not in default.stdout

    # --all surfaces the orphan, flagged (grouped under its recipe cachetype).
    allruns = _run("list", "--all", env=env)
    assert allruns.returncode == 0, allruns.stderr
    assert "mydata" in allruns.stdout
    assert "mt" in allruns.stdout              # the recipe (cachetype) header
    assert "orphan" in allruns.stdout


def test_list_filters_keep_the_styled_view(tmp_path):
    # A filter flag narrows the set but does NOT switch to the tab-separated
    # machine view — the grouped, styled headers remain.
    cache = tmp_path / "cache"
    _orphan_artifact(cache, "mt", {"g": "5x5"})
    toml = tmp_path / "datasets.toml"
    toml.write_text("[mydata]\nlocal_path = \"/nonexistent/x.csv\"\nformat = \"csv\"\n")
    env = dict(os.environ)
    env["DATAMANIFEST_CACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(toml)
    env["NO_COLOR"] = "1"

    # --orphan keeps the rich layout (the "Cached" header), not a bare table.
    orphan = _run("list", "--orphan", env=env)
    assert orphan.returncode == 0, orphan.stderr
    assert "Cached" in orphan.stdout
    assert "orphan" in orphan.stdout
    assert "Datasets" not in orphan.stdout  # orphan filter drops datasets

    # A not-yet-fetched dataset shows in the styled view as missing.
    missing = _run("list", "--missing", env=env)
    assert missing.returncode == 0, missing.stderr
    assert "Datasets" in missing.stdout
    assert "mydata" in missing.stdout
    assert "missing" in missing.stdout


def test_list_bare_prints_plain_names(tmp_path):
    # --bare / --names prints a plain newline-separated name list (scriptable),
    # regardless of the styled default.
    cache = tmp_path / "cache"
    artifact, _ = _orphan_artifact(cache, "mt", {"g": "5x5"})
    data_file = tmp_path / "external.csv"
    data_file.write_text("a,b\n1,2\n")
    toml = tmp_path / "datasets.toml"
    toml.write_text(f'[mydata]\nlocal_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_CACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(toml)

    result = _run("list", "--bare", env=env)
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines == ["mydata"]  # no headers, no styling; orphan hidden by default

    # --all --bare includes the orphan recipe's name (the cachetype), deduped.
    allbare = _run("list", "--all", "--bare", env=env)
    assert allbare.returncode == 0, allbare.stderr
    assert "mt" in allbare.stdout.split()
    assert "Cached" not in allbare.stdout


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
