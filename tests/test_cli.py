"""Subprocess-based smoke tests for the datamanifest CLI (Item 18)."""

import os
import socket
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
    for sub in ["list", "download", "path", "add", "remove", "show", "verify", "update-checksums", "init", "where", "migrate", "import", "refresh", "storage", "format"]:
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
    for flag in ["--cached", "--datasets", "--orphan", "--older-than", "--format", "--delete", "--move"]:
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
    out = result.stdout
    # Shows the manifest, the state file, and the resolved runtime folders.
    for label in ("manifest", "state file", "datasets_dir", "datacache_dir"):
        assert label in out, f"`where` output missing {label!r}: {out!r}"


def test_where_selectors_print_bare_path(tmp_path):
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    env = _env_with_toml(toml)
    # Each selector prints exactly one bare line (no label), scriptable.
    m = _run("where", "--manifest", env=env)
    assert m.returncode == 0 and m.stdout.strip() == str(toml)
    dd = _run("where", "--datasets-dir", env=env)
    assert dd.stdout.strip() == str(tmp_path / "datasets")
    sf = _run("where", "--state-file", env=env)
    assert sf.stdout.strip().endswith(os.path.join(".datamanifest", "state.toml"))
    assert ":" not in sf.stdout.strip()                # no "label : value" form
    # Selectors are mutually exclusive.
    assert _run("where", "--manifest", "--state-file", env=env).returncode != 0


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
    # spec-v4 layout: <datacache_dir>/<cachetype>/<hash> enumerates as a produced
    # artifact (no cached.toml roots it, so it reads as an orphan).
    artifact = cache / cachetype / h
    artifact.mkdir(parents=True)
    (artifact / "data.txt").write_text("v")
    write_config(str(artifact), cachetype, h, key_table)
    (artifact / ".complete").write_text("")
    return artifact, f"{cachetype}/{h}"


def _maint_env(tmp_path, cache):
    (tmp_path / "datasets.toml").write_text("")
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(tmp_path / "datasets.toml")
    return env


def test_list_orphan_reports_unreferenced(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    result = _run("list", "--orphan", "--fields", "kind", "referenced", "key", env=env)
    assert result.returncode == 0, result.stderr
    assert key in result.stdout
    assert "false" in result.stdout
    assert "cached" in result.stdout


def test_list_delete_dry_run_then_applies(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    # --dry-run reports but keeps.
    dry = _run("list", "--orphan", "--delete", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir(), "dry run must not delete"
    assert "dry run" in dry.stdout.lower()

    # The filtered selection applies directly (no --yes).
    run = _run("list", "--orphan", "--delete", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists(), "--delete should remove the orphan"


def test_list_move_relocates_orphan(tmp_path):
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mt", {"g": "5x5"})
    dest = tmp_path / "archive"
    env = _maint_env(tmp_path, cache)

    # --dry-run reports but keeps, and does not create the destination.
    dry = _run("list", "--orphan", "--move", str(dest), "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir()
    assert not dest.exists()

    # The selection applies the move, preserving the <cachetype>/<hash> key path.
    run = _run("list", "--orphan", "--move", str(dest), env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists()
    h = key.split("/", 1)[1]
    assert (dest / "mt" / h / "data.txt").exists()


def _registered_artifact(tmp_path, cache, cachetype, key_table, version=""):
    """A produced artifact materialized under *cache* AND registered (referenced)
    in the project's cached.toml, returning (artifact_dir, hash)."""
    from datamanifest.cache import CachedIndex

    artifact, key = _orphan_artifact(cache, cachetype, key_table)
    if version:  # move the dir under a version segment to match the recipe key
        dest = cache / cachetype / version / artifact.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        artifact.rename(dest)
        artifact = dest
    h = key.split("/", 1)[1]
    idx = CachedIndex(path=str(tmp_path / ".datamanifest" / "state.toml"))
    idx.register(cachetype=cachetype, hash=h, version=version,
                 storage_path=str(artifact), ref="m:f", format="txt")
    idx.write()
    return artifact, h


def test_list_move_keeps_cached_toml_consistent(tmp_path):
    """End-to-end: `list <hash> --move DEST` relocates the bytes, repoints the
    artifact's recorded location in the state file, and the artifact still shows
    in `list` afterwards (enumerated from its recorded location)."""
    from datamanifest.cache import CachedIndex

    cache = tmp_path / "cache"
    artifact, h = _registered_artifact(tmp_path, cache, "ct", {"x": 1})
    env = _maint_env(tmp_path, cache)
    dest = tmp_path / "moved"

    assert h[:12] in _run("list", h, env=env).stdout            # listed before

    run = _run("list", h, "--move", str(dest), env=env)
    assert run.returncode == 0, run.stderr
    moved = dest / "ct" / h
    assert (moved / "data.txt").is_file() and not artifact.exists()   # bytes moved

    # cached.toml repointed at the new home (relative to the manifest dir).
    back = CachedIndex.read(tmp_path / ".datamanifest" / "state.toml")
    assert back.instance_path_of(cachetype="ct", version="", hash=h) == \
        os.path.join("moved", "ct", h)

    # Still listed after the move (found at its recorded location, not datacache).
    assert h[:12] in _run("list", h, env=env).stdout


def test_list_delete_prunes_cached_toml(tmp_path):
    """End-to-end: `list <hash> --delete` removes the bytes AND prunes the
    variation from the state file (the recipe goes too once its last instance is)."""
    from datamanifest.cache import CachedIndex

    cache = tmp_path / "cache"
    artifact, h = _registered_artifact(tmp_path, cache, "ct", {"x": 1})
    env = _maint_env(tmp_path, cache)

    run = _run("list", h, "--delete", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists()                                 # bytes gone

    back = CachedIndex.read(tmp_path / ".datamanifest" / "state.toml")
    assert ("ct", "") not in back.recipes                       # pruned from index
    assert "Nothing to list" in _run("list", h, env=env).stdout


# ----- list action flags forward a REMAINDER tail to the standalone parser ----

def test_list_delete_old_store_true_form_is_gone(tmp_path):
    """The `--delete` flag now captures a REMAINDER tail (delete's own options),
    not a bare store_true. A flag the OLD action group never knew — handed to it
    as part of the tail — is parsed by delete's parser, e.g. `--prune`."""
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    # `list --orphan --delete --prune` forwards [--prune] to delete's parser
    # (an orphan cached artifact has no manifest entry, so --prune is a no-op on
    # it, but the flag must PARSE — under the old store_true form it could not).
    run = _run("list", "--orphan", "--delete", "--prune", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists(), "--delete should still remove the orphan"


def test_list_delete_tail_dry_run_then_applies(tmp_path):
    """`list --orphan --delete --dry-run` previews (tail = [--dry-run]); without
    --dry-run it applies — the REMAINDER tail reaches delete's option parser."""
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mytype", {"grid": "5x5"})
    env = _maint_env(tmp_path, cache)

    dry = _run("list", "--orphan", "--delete", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir(), "dry run must not delete"
    assert "dry run" in dry.stdout.lower()

    run = _run("list", "--orphan", "--delete", env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists()


def test_list_move_tail_dest_then_options(tmp_path):
    """`list --orphan --move DEST --dry-run`: the tail starts with DEST then the
    forwarded options. Dry-run previews; the real run relocates the bytes."""
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mt", {"g": "5x5"})
    dest = tmp_path / "archive"
    env = _maint_env(tmp_path, cache)

    dry = _run("list", "--orphan", "--move", str(dest), "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert artifact.is_dir() and not dest.exists()

    run = _run("list", "--orphan", "--move", str(dest), env=env)
    assert run.returncode == 0, run.stderr
    assert not artifact.exists()
    h = key.split("/", 1)[1]
    assert (dest / "mt" / h / "data.txt").exists()


def test_list_delete_prune_drops_dataset_entry(tmp_path):
    """`list --datasets <name> --delete --prune` forwards --prune to delete's
    parser, dropping the dataset's manifest entry (= `remove`), not just bytes."""
    toml = _id_project(tmp_path)
    env = _env_with_toml(toml)
    _run("download", "mydata", env=env)

    run = _run("list", "--datasets", "mydata", "--delete", "--prune", env=env)
    assert run.returncode == 0, run.stderr
    assert "entry pruned" in run.stdout
    assert "[mydata]" not in toml.read_text()        # --prune dropped the entry


def test_list_filters_narrow_before_the_action_flag(tmp_path):
    """Selection filters come BEFORE the action flag and still narrow: a
    `--datasets` selection leaves a sibling orphan cached artifact untouched."""
    cache = tmp_path / "cache"
    artifact, key = _orphan_artifact(cache, "mt", {"g": "5x5"})
    toml = tmp_path / "datasets.toml"
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")
    toml.write_text(
        '[_META]\nschema = 1\n\n[_STORAGE]\ndatasets_dir = "datasets"\n\n'
        f'[mydata]\nuri = "file://{src / "a.csv"}"\n'
    )
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
    env["DATAMANIFEST_TOML"] = str(toml)
    _run("download", "mydata", env=env)

    # Delete only datasets — the orphan cached artifact must survive.
    run = _run("list", "--datasets", "--delete", env=env)
    assert run.returncode == 0, run.stderr
    assert artifact.is_dir(), "a --datasets selection must not touch cached orphans"


def test_bare_skips_the_size_walk(tmp_path, monkeypatch):
    """--bare (and --fields without size) must not trigger the per-object size
    tree-walk — the scriptable speedup for large datasets."""
    import types

    import datamanifest.cache._inspect as inspect_mod
    from datamanifest.cli import _enumerate_objects, _heavy_fields
    from datamanifest.database import Database

    cache = tmp_path / "cache"
    _registered_artifact(tmp_path, cache, "ct", {"x": 1})
    (tmp_path / "datasets.toml").write_text("[_META]\nschema = 1\n")
    monkeypatch.setenv("DATAMANIFEST_DATACACHE_DIR", str(cache))
    db = Database(datasets_toml=str(tmp_path / "datasets.toml"), persist=False)
    db.datasets_toml = str(tmp_path / "datasets.toml")

    calls = {"n": 0}
    orig = inspect_mod._dir_size
    monkeypatch.setattr(inspect_mod, "_dir_size",
                        lambda p: (calls.__setitem__("n", calls["n"] + 1) or orig(p)))

    _enumerate_objects(db, {"size"})            # rich: walks
    assert calls["n"] >= 1
    calls["n"] = 0
    _enumerate_objects(db, set())               # bare: no walk
    assert calls["n"] == 0

    # _heavy_fields picks the right set per output mode. The action flags are
    # REMAINDER lists now: None = flag absent, a list = given (e.g. [] for a bare
    # `--delete`, or its forwarded tail).
    def ns(**k):
        base = dict(bare=False, fields=None, delete=None, move=None)
        base.update(k)
        return types.SimpleNamespace(**base)
    assert "size" not in _heavy_fields(ns(bare=True))
    assert "size" in _heavy_fields(ns())                     # rich default
    assert "size" in _heavy_fields(ns(fields=["size"]))
    assert "size" not in _heavy_fields(ns(fields=["key"]))
    assert _heavy_fields(ns(delete=[])) == set()             # actions skip it
    assert _heavy_fields(ns(delete=["--prune"])) == set()    # tail too


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
        format=None, orphan=False, older_than="1d", cached=False, datasets=False
    )
    kept = {o.key for o in _filter_objects(objs, args)}
    assert "old/h" in kept and "new/h" not in kept


def test_search_and_hash_filters():
    import types

    from datamanifest.cache._inspect import CacheObject
    from datamanifest.cli import _filter_objects

    objs = [
        CacheObject(kind="cached", location="/x", key="greet/aa11",
                    name="greet", cachetype="greet", hash="aa11bb", format="txt"),
        CacheObject(kind="cached", location="/y", key="analysis.run/cc22",
                    name="analysis.run", cachetype="analysis.run",
                    hash="cc22dd", format="pickle"),
    ]

    def ns(**kw):
        base = dict(search=None, any=False, invert=False, cached=False, datasets=False, format=None,
                    hash=None, orphan=False, older_than=None, all=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    keys = lambda a: {o.key for o in _filter_objects(objs, ns(**a))}

    # Free-text search across key fields; AND by default, OR with --any.
    assert keys(dict(search=["greet"])) == {"greet/aa11"}
    assert keys(dict(search=["GRE"])) == {"greet/aa11"}                  # case-insensitive
    assert keys(dict(search=["greet", "pickle"])) == set()              # AND ⇒ none
    assert keys(dict(search=["greet", "pickle"], any=True)) == \
        {"greet/aa11", "analysis.run/cc22"}                            # OR ⇒ both
    assert keys(dict(search=["greet"], invert=True)) == {"analysis.run/cc22"}  # not-matching

    # --hash matches a prefix, independent of version; several prefixes OR.
    assert keys(dict(hash=["cc22"])) == {"analysis.run/cc22"}
    assert keys(dict(hash=["zz"])) == set()
    assert keys(dict(hash=["cc22", "aa11"])) == \
        {"analysis.run/cc22", "greet/aa11"}                            # paste several


def test_explicit_selector_reveals_orphans():
    """An explicit search/--hash selector bypasses the default orphan-hiding, so
    an unrooted (referenced=False) artifact still surfaces."""
    import types

    from datamanifest.cache._inspect import CacheObject
    from datamanifest.cli import _filter_objects

    orphan = CacheObject(kind="cached", location="/o", key="t/ab12",
                         name="t", cachetype="t", hash="ab12cd", format="txt",
                         referenced=False)

    def ns(**kw):
        base = dict(search=None, any=False, cached=False, datasets=False, format=None, hash=None,
                    orphan=False, older_than=None, all=False)
        base.update(kw)
        return types.SimpleNamespace(**base)

    # Plain list hides the orphan; an explicit --hash or search reveals it.
    assert _filter_objects([orphan], ns()) == []
    assert _filter_objects([orphan], ns(hash=["ab12"]))[0].key == "t/ab12"
    assert _filter_objects([orphan], ns(search=["t"]))[0].key == "t/ab12"


def test_list_kind_data_lists_fetched_dataset(tmp_path):
    # A present fetched dataset (via storage_path) alongside a cached orphan:
    # --datasets shows the fetched entry and excludes the produced artifact.
    cache = tmp_path / "cache"
    _orphan_artifact(cache, "mt", {"g": "5x5"})
    data_file = tmp_path / "external.csv"
    data_file.write_text("a,b\n1,2\n")
    toml = tmp_path / "datasets.toml"
    toml.write_text(f'[mydata]\nstorage_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
    env["DATAMANIFEST_USAGE_LOG"] = str(tmp_path / "usage.toml")
    env["DATAMANIFEST_TOML"] = str(toml)

    result = _run("list", "--datasets", "--fields", "kind", "key", env=env)
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
    toml.write_text(f'[mydata]\nstorage_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
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
    toml.write_text("[mydata]\nstorage_path = \"/nonexistent/x.csv\"\nformat = \"csv\"\n")
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
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
    toml.write_text(f'[mydata]\nstorage_path = "{data_file}"\nformat = "csv"\n')
    env = dict(os.environ)
    env["DATAMANIFEST_DATACACHE_DIR"] = str(cache)
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
    assert (tmp_path / "datamanifest.toml").exists()


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


# ----- config (scoped storage configuration) -----

def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


def _storage_project(tmp_path):
    toml = tmp_path / "datamanifest.toml"
    toml.write_text("[_META]\nschema = 1\n")
    return toml


def test_config_set_defaults_to_local(tmp_path):
    # The default scope is the checkout's git-ignored config file — personal by
    # default; the committed manifest is only written with --project / --host.
    toml = _storage_project(tmp_path)
    r = _run("config", "set", "datacache_dir", "/fast", env=_env_with_toml(toml))
    assert r.returncode == 0, r.stderr
    local = tmp_path / ".datamanifest" / "config.toml"
    assert _load_toml(local)["datacache_dir"] == "/fast"
    assert "_STORAGE" not in _load_toml(toml)
    # The private dir self-ignores (one .gitignore with *).
    assert (tmp_path / ".datamanifest" / ".gitignore").read_text().strip() == "*"


def test_config_set_project_writes_manifest_base(tmp_path):
    toml = _storage_project(tmp_path)
    r = _run("config", "set", "datacache_dir", "cached", "--project",
             env=_env_with_toml(toml))
    assert r.returncode == 0, r.stderr
    assert _load_toml(toml)["_STORAGE"]["datacache_dir"] == "cached"


def test_config_set_host_glob_writes_manifest_host_table(tmp_path):
    toml = _storage_project(tmp_path)
    r = _run("config", "set", "datasets_dir", "/scratch/data",
             "--host", "login*.hpc.edu", env=_env_with_toml(toml))
    assert r.returncode == 0, r.stderr
    host = _load_toml(toml)["_STORAGE"]["_HOST"]["login*.hpc.edu"]
    assert host["datasets_dir"] == "/scratch/data"


def test_config_set_local_host_glob(tmp_path):
    # --host combined with --local scopes within the local config file.
    toml = _storage_project(tmp_path)
    r = _run("config", "set", "datasets_dir", "/scratch/data", "--local",
             "--host", "login*", env=_env_with_toml(toml))
    assert r.returncode == 0, r.stderr
    local = tmp_path / ".datamanifest" / "config.toml"
    assert _load_toml(local)["_HOST"]["login*"]["datasets_dir"] == "/scratch/data"
    assert "_STORAGE" not in _load_toml(toml)


def test_config_set_global_writes_user_file(tmp_path):
    toml = _storage_project(tmp_path)
    env = _env_with_toml(toml)
    r = _run("config", "set", "datasets_dir", "/pool", "--global", env=env)
    assert r.returncode == 0, r.stderr
    user_cfg = os.path.join(env["XDG_CONFIG_HOME"], "datamanifest", "config.toml")
    assert _load_toml(user_cfg)["datasets_dir"] == "/pool"
    # The user-global rung is honored when nothing more specific is set.
    dd = _run("where", "--datasets-dir", env=env)
    assert dd.stdout.strip() == "/pool"


def test_config_scope_precedence_local_over_project_over_global(tmp_path):
    toml = _storage_project(tmp_path)
    env = _env_with_toml(toml)
    _run("config", "set", "datasets_dir", "/from-global", "--global", env=env)
    _run("config", "set", "datasets_dir", "/from-project", "--project", env=env)
    assert _run("where", "--datasets-dir", env=env).stdout.strip() == "/from-project"
    _run("config", "set", "datasets_dir", "/from-local", env=env)
    assert _run("where", "--datasets-dir", env=env).stdout.strip() == "/from-local"


def test_config_unset_removes_and_prunes(tmp_path):
    toml = _storage_project(tmp_path)
    env = _env_with_toml(toml)
    _run("config", "set", "datacache_dir", "/fast", "--host", "h1", env=env)
    r = _run("config", "unset", "datacache_dir", "--host", "h1", env=env)
    assert r.returncode == 0, r.stderr
    # The empty host table (and _HOST) are pruned from the manifest.
    assert "_HOST" not in _load_toml(toml).get("_STORAGE", {})


def test_config_set_rejects_reserved_field(tmp_path):
    toml = _storage_project(tmp_path)
    r = _run("config", "set", "_HOST", "x", "--project", env=_env_with_toml(toml))
    assert r.returncode != 0
    assert "invalid field" in (r.stdout + r.stderr).lower()


def test_config_show_resolves_for_this_host(tmp_path):
    toml = _storage_project(tmp_path)
    env = _env_with_toml(toml)
    _run("config", "set", "datasets_dir", "$user_data_dir/myproj", "--project",
         env=env)
    r = _run("config", "show", env=env)
    assert r.returncode == 0, r.stderr
    assert "Resolved for this host" in r.stdout
    assert "myproj" in r.stdout                       # resolved, $user_data_dir expanded


def test_storage_alias_is_deprecated_and_forwards(tmp_path):
    toml = _storage_project(tmp_path)
    env = _env_with_toml(toml)
    r = _run("storage", "set", "datacache_dir", "cached", "--all-hosts", env=env)
    assert r.returncode == 0, r.stderr
    assert "deprecated" in r.stderr.lower()
    # --all-hosts maps onto the --project scope (the manifest base).
    assert _load_toml(toml)["_STORAGE"]["datacache_dir"] == "cached"


# ----- first-order delete / move (by id, like push/pull) -----

def _id_project(tmp_path):
    """A manifest-backed project with one downloadable file:// dataset."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"col\n1\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        '[_META]\nschema = 1\n\n[_STORAGE]\ndatasets_dir = "datasets"\n\n'
        f'[mydata]\nuri = "file://{src / "a.csv"}"\n'
    )
    return toml


def test_delete_by_name_removes_bytes(tmp_path):
    toml = _id_project(tmp_path)
    env = _env_with_toml(toml)
    _run("download", "mydata", env=env)
    # dry-run keeps it; apply removes the bytes.
    dry = _run("delete", "mydata", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    run = _run("delete", "mydata", env=env)
    assert run.returncode == 0, run.stderr
    assert "Deleted" in run.stdout
    # the manifest still declares it (delete removes bytes, not the spec).
    assert "[mydata]" in toml.read_text()


def test_delete_prune_also_drops_the_entry(tmp_path):
    toml = _id_project(tmp_path)
    env = _env_with_toml(toml)
    _run("download", "mydata", env=env)

    run = _run("delete", "mydata", "--prune", env=env)
    assert run.returncode == 0, run.stderr
    assert "entry pruned" in run.stdout
    # --prune drops the manifest entry too (= `remove`).
    assert "[mydata]" not in toml.read_text()


def test_move_by_name_relocates_and_repoints_state(tmp_path):
    toml = _id_project(tmp_path)
    env = _env_with_toml(toml)
    _run("download", "mydata", env=env)
    dest = tmp_path / "archive"
    run = _run("move", "mydata", str(dest), env=env)
    assert run.returncode == 0, run.stderr
    assert dest.exists()
    state = (tmp_path / ".datamanifest" / "state.toml").read_text()
    assert "archive" in state                       # recorded location repointed
    assert toml.read_text().count("[mydata]") == 1  # manifest unchanged


def test_delete_unknown_id_errors(tmp_path):
    toml = _id_project(tmp_path)
    r = _run("delete", "does-not-exist", env=_env_with_toml(toml))
    assert r.returncode != 0
    assert "no stored object" in (r.stdout + r.stderr).lower()


def _orphan_with_hash(cache, cachetype, h):
    from datamanifest.cache._sidecars import write_config

    artifact = cache / cachetype / h
    artifact.mkdir(parents=True)
    (artifact / "data.txt").write_text("v")
    write_config(str(artifact), cachetype, h, {"x": h})
    (artifact / ".complete").write_text("")
    return artifact


def test_delete_ambiguous_id_needs_batch(tmp_path):
    cache = tmp_path / "cache"
    a = _orphan_with_hash(cache, "ct", "aa11")
    b = _orphan_with_hash(cache, "ct", "aa22")
    env = _maint_env(tmp_path, cache)

    # 'aa' matches both → refuses without --batch.
    r = _run("delete", "aa", env=env)
    assert r.returncode != 0
    assert "ambiguous" in (r.stdout + r.stderr).lower()
    assert a.is_dir() and b.is_dir()

    # --batch deletes all matches.
    r2 = _run("delete", "aa", "--batch", env=env)
    assert r2.returncode == 0, r2.stderr
    assert not a.exists() and not b.exists()


def test_match_cached_by_id_addressing():
    from datamanifest.cache._inspect import CacheObject
    from datamanifest.cli import _match_cached_by_id

    objs = [
        CacheObject(kind="cached", location="", cachetype="ct", hash="abc123"),
        CacheObject(kind="cached", location="", cachetype="ct", hash="abd999"),
        CacheObject(kind="cached", location="", cachetype="other", hash="abc777"),
    ]
    assert len(_match_cached_by_id("ab", objs)) == 3           # prefix matches all
    assert len(_match_cached_by_id("abc", objs)) == 2          # abc123, abc777
    assert {o.hash for o in _match_cached_by_id("ct/abc", objs)} == {"abc123"}
    assert len(_match_cached_by_id("ct/ab", objs)) == 2        # cachetype-scoped prefix


# ----- where note + list --outside + --scan ----------------------------------

def _outside_project(tmp_path):
    """A project where 'a' is conformant (reused from a read pool) and 'c' is
    recorded at an ad-hoc location outside datasets_dir and the pool."""
    pool = tmp_path / "pool" / "example.com"
    pool.mkdir(parents=True)
    (pool / "a.csv").write_bytes(b"a\n")
    (pool / "b.csv").write_bytes(b"b\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text(
        '[_META]\nschema = 1\n'
        f'[_STORAGE]\ndatasets_dir = "datasets"\ndatasets_pools = ["{tmp_path / "pool"}"]\n'
        '[a]\nuri = "https://example.com/a.csv"\n'
        '[b]\nuri = "https://example.com/b.csv"\n'
        '[c]\nuri = "https://example.com/c.csv"\n'
    )
    env = _env_with_toml(toml)
    _run("download", "a", env=env)            # reused from the pool → recorded (conformant)

    # Record c at a location that is neither datasets_dir nor any read pool.
    stray = tmp_path / "stray" / "example.com"
    stray.mkdir(parents=True)
    (stray / "c.csv").write_bytes(b"c\n")
    with open(tmp_path / ".datamanifest" / "state.toml", "a") as f:
        f.write(f'\n[datasets."example.com/c.csv"]\n'
                f'storage_path = "{stray / "c.csv"}"\nsha256 = "deadbeef"\n')
    return env, stray


def test_where_folds_pools_and_notes_outside(tmp_path):
    env, stray = _outside_project(tmp_path)

    out = _run("where", env=env).stdout
    # The read pool is folded as a continuation line under datasets_dir — no
    # separate "read pools" block, and no per-record dump.
    assert str(tmp_path / "pool") in out
    assert "read pools (datasets)" not in out
    # Just a count + a pointer to `list --outside`; only 'c' is off-pattern.
    assert "1 dataset stored outside" in out
    assert "list --outside" in out

    scan = _run("where", "--scan", env=env).stdout
    assert "scan" in scan and "b →" in scan   # b is in the pool, not yet local


def test_list_outside_filters_to_offpattern_only(tmp_path):
    env, stray = _outside_project(tmp_path)

    out = _run("list", "--outside", "--datasets", env=env).stdout
    # 'c' is recorded outside → listed at its stray location (the path tail
    # survives the renderer's keep-tail truncation); conformant 'a' (in the pool)
    # and not-yet-fetched 'b' are excluded.
    assert "c.csv" in out and "stray" in out
    assert "a.csv" not in out and "b.csv" not in out


# ----- import (from pooch) ---------------------------------------------------

def test_import_pooch_cli(tmp_path):
    import hashlib

    # A cache file + a one-line registry referencing it by sha256.
    cache = tmp_path / "cache"
    cache.mkdir()
    blob = b"gravity grid\n"
    (cache / "g.nc").write_bytes(blob)
    reg = tmp_path / "registry.txt"
    reg.write_text(f"g.nc {hashlib.sha256(blob).hexdigest()}\n")

    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    env = _env_with_toml(toml)

    r = _run("import", "pooch", str(reg), "--base-url", "https://data.example.org",
             "--cache-dir", str(cache), env=env)
    assert r.returncode == 0, r.stderr
    assert "Imported 1 dataset" in r.stdout and "adopted from the cache" in r.stdout

    # The dataset is declared and resolves to the in-place cache copy (no download).
    out = _run("list", "--datasets", env=env).stdout
    assert "g" in out
    p = _run("path", "g", env=env)
    assert p.returncode == 0 and str(cache / "g.nc") in p.stdout


def test_import_csv_cli(tmp_path):
    csv = tmp_path / "files.csv"
    csv.write_text("name,url,sha256\nfoo,https://h/a/foo.nc,deadbeef\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    env = _env_with_toml(toml)

    r = _run("import", "csv", str(csv), env=env)
    assert r.returncode == 0, r.stderr
    assert "Imported 1 dataset" in r.stdout
    assert "foo" in _run("list", "--datasets", env=env).stdout


# ----- add / show / remove / download / verify (command coverage) ------------

def _proj_with_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.csv").write_bytes(b"col\n1\n")
    toml = tmp_path / "datamanifest.toml"
    toml.write_text('[_META]\nschema = 1\n[_STORAGE]\ndatasets_dir = "datasets"\n')
    return toml, src


def test_add_show_remove(tmp_path):
    toml, src = _proj_with_source(tmp_path)
    env = _env_with_toml(toml)

    add = _run("add", f"file://{src / 'x.csv'}", "--name", "foo", env=env)
    assert add.returncode == 0, add.stderr
    assert "[foo]" in toml.read_text()

    show = _run("show", "foo", env=env)
    assert show.returncode == 0 and "x.csv" in show.stdout

    rm = _run("remove", "foo", env=env)
    assert rm.returncode == 0 and "[foo]" not in toml.read_text()


def test_download_then_verify(tmp_path):
    toml, src = _proj_with_source(tmp_path)
    env = _env_with_toml(toml)
    _run("add", f"file://{src / 'x.csv'}", "--name", "foo", "--no-download", env=env)

    dl = _run("download", "foo", env=env)
    assert dl.returncode == 0, dl.stderr
    v = _run("verify", "foo", env=env)
    assert v.returncode == 0, v.stderr
