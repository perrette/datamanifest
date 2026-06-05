"""Tests for the cross-machine ``sync`` capability (push / pull over rsync+ssh).

Offline only: every subprocess call (the ssh env-probe, ssh ``mkdir -p``, and
rsync) is routed through an **injected runner** so no real ssh/rsync/network is
ever invoked — the runner captures argv and returns canned output. Covers:

- address resolution (fetched by name/alias/doi → ``rel == key``; produced by
  ``cachetype/hash``, ``cachetype/version/hash``, and an unambiguous hash prefix;
  ambiguous id raises without ``--batch``);
- remote-env probe + the fallback ladder (probe → ``_HOST`` → ``[_STORAGE]``
  default), and the local/repo-relative refusal;
- command construction (rsync+ssh argv, direction, operands, the ``.complete``
  sibling for a file object, the push ``mkdir -p``);
- ``--dry-run`` reports the selection and never invokes the transfer;
- the import-rule guard (``cache/`` never imports the fetch layer).

spec-v4 storage model
---------------------
Storage is two folder fields in ``[_STORAGE]``: ``datasets_dir`` (fetched) and
``datacache_dir`` (produced). A *relative* folder resolves under the project root
⇒ **local / repo-relative ⇒ out of scope for sync**. To make an object syncable
a test points the folder at a **machine-global absolute** path (the ``proj``
fixture pins both at ``tmp_path``-rooted absolute dirs). An object's
machine-independent address (``rel``) is the dataset ``key`` (fetched) or
``cachetype/[version/]hash`` (produced); it re-attaches under the receiver's
``datasets_dir`` / ``datacache_dir``.
"""

import glob
import os

import pytest

from datamanifest import store, sync
from datamanifest.cache._sidecars import write_config
from datamanifest.database import Database


# ----- a recording runner ----------------------------------------------------

class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class Runner:
    """Records every argv and replies with canned output.

    *env_output* is the stdout the ssh env-probe returns; *env_returncode* its
    exit code (non-zero ⇒ a failed probe). All other calls (mkdir, rsync) return
    success."""

    def __init__(self, env_output="", env_returncode=0):
        self.calls = []
        self.env_output = env_output
        self.env_returncode = env_returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        # The env-probe is the ssh call whose remote command mentions `env`.
        if len(argv) >= 3 and argv[0] == "ssh" and "env" in argv[2]:
            return FakeProc(self.env_returncode, self.env_output)
        return FakeProc(0, "")

    def rsync_calls(self):
        return [c for c in self.calls if c and c[0] == "rsync"]

    def ssh_calls(self):
        return [c for c in self.calls if c and c[0] == "ssh"]


# ----- fixtures ---------------------------------------------------------------

@pytest.fixture
def proj(tmp_path):
    """A project dir whose ``[_STORAGE]`` pins datasets_dir / datacache_dir at
    machine-global (absolute) folders, so fetched datasets and produced
    artifacts are **syncable**. ``repo_ds`` is pinned under ``$repo`` to exercise
    the local/repo-relative refusal."""
    d = tmp_path / "proj"
    d.mkdir()
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    toml = d / "datasets.toml"
    toml.write_text(
        f"""
[_META]
schema = 1

[_STORAGE]
datasets_dir = "{data}"
datacache_dir = "{cache}"

[_STORAGE._HOST."remote*"]
datasets_dir = "/host/data"
datacache_dir = "/host/cache"

[foo]
uri = "https://example.com/data/foo.csv"
aliases = ["foo_alias"]
doi = "10.1234/foo"

[repo_ds]
uri = "https://example.com/r.csv"
storage_path = "$repo/local/r.csv"
"""
    )
    return d


@pytest.fixture
def db(proj):
    # persist=True so the database knows its ``datasets_toml`` path and
    # ``get_project_root`` returns the project dir (the repo-relative refusal
    # needs a real project root to compare against).
    return Database(datasets_toml=str(proj / "datasets.toml"), persist=True)


def _make_artifact(cache_root, cachetype, h, *, version="", file=False):
    """Fabricate a produced artifact dir with a config.toml sidecar at
    ``<cache_root>/<cachetype>/[<version>/]<hash>`` (the spec-v4 layout, no
    scope segment)."""
    parts = [str(cache_root), cachetype]
    if version:
        parts.append(version)
    parts.append(h)
    artifact = os.path.join(*parts)
    os.makedirs(artifact, exist_ok=True)
    write_config(artifact, cachetype=cachetype, hash=h, key_table={"g": "5x5"},
                 version=version)
    with open(os.path.join(artifact, "value.txt"), "w") as f:
        f.write("x")
    open(os.path.join(artifact, ".complete"), "w").close()
    return artifact


def _cache_root(db):
    return store.datacache_dir(
        project_root=db.get_project_root(), storage_config=db.storage_config,
    )


# ----- address resolution: fetched -------------------------------------------

@pytest.mark.parametrize("ident", ["foo", "foo_alias", "10.1234/foo"])
def test_fetched_resolves_by_name_alias_doi(db, ident):
    obj = sync.resolve_object(db, ident)
    assert obj.kind == "datasets"
    # rel is the machine-independent address — the dataset key (no datasets/
    # prefix; that is the receiver's own folder).
    assert obj.rel == os.path.join("example.com", "data", "foo.csv")
    # local_abs re-attaches the key under the local datasets_dir.
    assert obj.local_abs.endswith(obj.rel)


def test_repo_stored_dataset_refused(db):
    # repo_ds is pinned under $repo (storage_path = "$repo/local/r.csv") -> it
    # resolves under the project root -> local / repo-relative -> out of scope.
    with pytest.raises(sync.RemoteRepoError):
        sync.resolve_object(db, "repo_ds")


# ----- address resolution: produced ------------------------------------------

def test_produced_resolves_by_cachetype_hash(db):
    cache_root = _cache_root(db)
    h = "a" * 64
    art = _make_artifact(cache_root, "greet", h)
    obj = sync.resolve_object(db, f"greet/{h}")
    assert obj.kind == "cached"
    assert obj.local_abs == os.path.abspath(art)
    # rel is the machine-independent address: cachetype/hash.
    assert obj.rel == os.path.join("greet", h)


def test_produced_resolves_by_cachetype_version_hash(db):
    cache_root = _cache_root(db)
    h = "b" * 64
    _make_artifact(cache_root, "greet", h, version="v3")
    obj = sync.resolve_object(db, f"greet/v3/{h}")
    assert obj.kind == "cached"
    assert obj.rel == os.path.join("greet", "v3", h)


def test_produced_resolves_by_unambiguous_hash_prefix(db):
    cache_root = _cache_root(db)
    h = "c0ffee" + "0" * 58
    _make_artifact(cache_root, "greet", h)
    obj = sync.resolve_object(db, "greet/c0ffee")
    assert obj.local_abs.endswith(h)


def test_ambiguous_produced_id_raises_without_batch(db):
    cache_root = _cache_root(db)
    _make_artifact(cache_root, "greet", "dead" + "0" * 60)
    _make_artifact(cache_root, "greet", "dead" + "1" * 60)
    with pytest.raises(sync.AmbiguousIdError):
        sync.resolve_object(db, "greet/dead")
    # --batch returns both.
    objs = sync.resolve_objects(db, "greet/dead", batch=True)
    assert len(objs) == 2


# ----- remote env probe + fallback ladder ------------------------------------

def test_remote_env_probe_drives_remote_root(db):
    # The probe exports DATAMANIFEST_DATASETS_DIR -> it drives the remote root.
    runner = Runner(
        env_output="DATAMANIFEST_DATASETS_DIR=/remote/data\nPATH=/bin\n"
    )
    obj = sync.resolve_object(db, "foo")
    rabs = sync.remote_abs(obj, "somehost", db=db,
                           project_root=db.get_project_root(), runner=runner)
    assert rabs.startswith("/remote/data" + os.sep)
    assert rabs.endswith(obj.rel)
    # The probe ran exactly the documented ssh env command.
    probe = runner.ssh_calls()[0]
    assert probe[0] == "ssh" and probe[1] == "somehost"
    assert "source ~/.bashrc" in probe[2] and probe[2].rstrip().endswith("env")


def test_falls_back_to_host_override_when_probe_empty(db):
    # Probe returns no DATAMANIFEST_* vars -> the
    # [_STORAGE._HOST."remote*"] override (datasets_dir = /host/data) wins for a
    # matching hostname.
    runner = Runner(env_output="PATH=/usr/bin\n")
    obj = sync.resolve_object(db, "foo")
    rabs = sync.remote_abs(obj, "remote-box", db=db,
                           project_root=db.get_project_root(), runner=runner)
    assert rabs.startswith("/host/data" + os.sep)


def test_falls_back_to_default_when_probe_fails_and_no_host(db):
    # Probe fails (non-zero) and the host matches no _HOST glob -> the shared
    # default folder, i.e. the [_STORAGE].datasets_dir base value resolved
    # through the ladder (no remote DATAMANIFEST_* and no host override).
    runner = Runner(env_returncode=255)
    obj = sync.resolve_object(db, "foo")
    rabs = sync.remote_abs(obj, "unknown-host", db=db,
                           project_root=db.get_project_root(), runner=runner)
    default_root = store.datasets_dir(
        project_root=db.get_project_root(), storage_config=db.storage_config,
        env={}, host="unknown-host",
    )
    assert rabs == os.path.join(default_root, obj.rel)


# ----- command construction ---------------------------------------------------

def test_push_dir_object_argv_and_mkdir(db):
    # Materialize foo as a directory object locally.
    obj = sync.resolve_object(db, "foo")
    os.makedirs(obj.local_abs, exist_ok=True)
    open(os.path.join(obj.local_abs, ".complete"), "w").close()
    obj = sync.resolve_object(db, "foo")  # re-resolve so is_dir is True
    assert obj.is_dir

    runner = Runner(env_output="DATAMANIFEST_DATASETS_DIR=/remote/data\n")
    plan = sync.transfer(db, obj, "host1", direction="push",
                         project_root=db.get_project_root(), runner=runner)
    # push issues the remote mkdir -p of the parent.
    mkdir = [c for c in runner.ssh_calls() if "mkdir" in c]
    assert mkdir, "push must mkdir -p the remote parent"
    assert mkdir[0][:4] == ["ssh", "host1", "mkdir", "-p"]
    assert mkdir[0][4] == os.path.dirname(plan["remote"])
    # one rsync, recursive dir copy local -> host:remote.
    rsync = runner.rsync_calls()
    assert len(rsync) == 1
    assert rsync[0][:4] == ["rsync", "-a", "-e", "ssh"]
    assert rsync[0][-2] == obj.local_abs
    assert rsync[0][-1] == f"host1:{plan['remote']}"


def test_push_file_object_transfers_complete_sibling(db):
    # foo as a file object (no directory): sibling <file>.complete must go too.
    obj = sync.resolve_object(db, "foo")
    os.makedirs(os.path.dirname(obj.local_abs), exist_ok=True)
    with open(obj.local_abs, "w") as f:
        f.write("data")
    open(obj.local_abs + ".complete", "w").close()
    obj = sync.resolve_object(db, "foo")
    assert not obj.is_dir

    runner = Runner(env_output="DATAMANIFEST_DATASETS_DIR=/remote/data\n")
    plan = sync.transfer(db, obj, "h", direction="push",
                         project_root=db.get_project_root(), runner=runner)
    rsync = runner.rsync_calls()
    assert len(rsync) == 2  # the file and its .complete sibling
    assert rsync[0][-1] == f"h:{plan['remote']}"
    assert rsync[1][-2] == obj.local_abs + ".complete"
    assert rsync[1][-1] == f"h:{plan['remote']}.complete"


def test_pull_direction_and_local_mkdir(db):
    obj = sync.resolve_object(db, "foo")  # not present -> treated as file
    runner = Runner(env_output="DATAMANIFEST_DATASETS_DIR=/remote/data\n")
    plan = sync.transfer(db, obj, "h", direction="pull",
                         project_root=db.get_project_root(), runner=runner)
    # pull makes the local parent and never issues a remote mkdir.
    assert not [c for c in runner.ssh_calls() if "mkdir" in c]
    assert os.path.isdir(os.path.dirname(obj.local_abs))
    rsync = runner.rsync_calls()
    # source is remote, dest is local (pull direction).
    assert rsync[0][-2] == f"h:{plan['remote']}"
    assert rsync[0][-1] == obj.local_abs


# ----- dry run ----------------------------------------------------------------

def test_dry_run_reports_and_does_not_transfer(db):
    obj = sync.resolve_object(db, "foo")
    runner = Runner(env_output="DATAMANIFEST_DATASETS_DIR=/remote/data\n")
    plan = sync.transfer(db, obj, "h", direction="push",
                         project_root=db.get_project_root(), runner=runner,
                         dry_run=True)
    assert plan["argv"] == []
    assert plan["remote"].startswith("/remote/data")
    # No mkdir / rsync was invoked for the transfer (the env-probe may run).
    assert not runner.rsync_calls()
    assert not [c for c in runner.ssh_calls() if "mkdir" in c]


# ----- import-rule guard ------------------------------------------------------

def test_cache_package_does_not_import_fetch_layer():
    bad = []
    for f in glob.glob(
        os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "datamanifest", "cache", "**", "*.py"),
        recursive=True,
    ):
        with open(f) as fh:
            text = fh.read()
        if "import" in text and ("pipelines" in text or "database" in text):
            bad.append(f)
    assert bad == [], f"cache/ must not import the fetch layer: {bad}"
