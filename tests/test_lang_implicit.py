"""Local offline tests for spec-v3.4 (bare, language-implicit fetcher/loader
and [_LOADERS]) and spec-v3.5 (bare, language-agnostic shell).

The resolution assertions mirror the authoritative conformance fixture
``lang_implicit.toml`` / ``lang_implicit.expected.json`` (the ``python`` view):
each dataset's effective fetcher/loader rung + ref must match the fixture. The
remaining tests cover the string/table binding forms, explicit-over-bare
precedence, tolerant warn-and-fall-through, round-trip (bare stays bare), and
the shell precedence flip + migrate inversion.
"""

import io

import pytest
import tomli_w

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from datamanifest.database import (
    Database,
    migrate_v0_to_v1,
    resolve_fetcher,
    resolve_loader_rungs,
    _sort_recursive,
)
from datamanifest.pipelines import load_dataset


# The conformance fixture, replicated inline (offline). Matches
# tests/fixtures/lang_implicit.toml in datamanifest.toml.
LANG_IMPLICIT_TOML = """
[_META]
schema = 1

[_LOADERS]
csv = "myproject.io:read_csv"
nc  = "myproject.io:read_nc"

[temperature]
uri    = "https://example.com/temperature.csv"
sha256 = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
format = "csv"
loader = "myproject.loaders:load_temperature"

[derived]
sha256  = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3"
format  = "nc"
fetcher = "myproject.build:derived"

[bathymetry]
uri    = "https://example.com/bathy.nc"
sha256 = "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
format = "nc"

[precedence]
uri    = "https://example.com/p.csv"
sha256 = "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5"
format = "csv"
loader = "myproject.loaders:bare_one"

[precedence._LANG.python]
loader = "myproject.loaders:python_one"

[precedence._LANG.julia]
loader = "MyProject:julia_one"
"""


def _write(tmp_path, text, name="datasets.toml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _db(tmp_path, text=LANG_IMPLICIT_TOML):
    return Database(datasets_toml=_write(tmp_path, text), persist=False, skip_checksum=True)


def _fetch_rung_ref(entry):
    kind, value = resolve_fetcher(entry)
    if kind == "python":
        return "own-fetcher", value
    if kind == "shell":
        return "shell", value
    if kind == "uri":
        return "uri", None
    return "error", None


def _load_rung_ref(db, entry):
    rungs = resolve_loader_rungs(db, entry)
    if rungs:
        ref = rungs[0][0]
        if entry.lang_python_loader or entry.loader:
            return "per-dataset", ref
        return "manifest-format-default", ref
    return "error", None


# ----- Conformance mirror: effective fetcher/loader rung + ref -----

EXPECTED = {
    "temperature": {
        "fetcher": ("uri", None),
        "loader": ("per-dataset", "myproject.loaders:load_temperature"),
    },
    "derived": {
        "fetcher": ("own-fetcher", "myproject.build:derived"),
        "loader": ("manifest-format-default", "myproject.io:read_nc"),
    },
    "bathymetry": {
        "fetcher": ("uri", None),
        "loader": ("manifest-format-default", "myproject.io:read_nc"),
    },
    "precedence": {
        "fetcher": ("uri", None),
        "loader": ("per-dataset", "myproject.loaders:python_one"),
    },
}


@pytest.mark.parametrize("ds_name", sorted(EXPECTED))
def test_conformance_resolution(tmp_path, ds_name):
    db = _db(tmp_path)
    entry = db.datasets[ds_name]
    assert _fetch_rung_ref(entry) == EXPECTED[ds_name]["fetcher"]
    assert _load_rung_ref(db, entry) == EXPECTED[ds_name]["loader"]


def test_precedence_explicit_wins_over_bare(tmp_path):
    """An explicit [_LANG.python].loader overrides the bare loader (spec-v3.4)."""
    db = _db(tmp_path)
    entry = db.datasets["precedence"]
    # bare loader present, but explicit python loader wins
    assert entry.loader == "myproject.loaders:bare_one"
    assert entry.lang_python_loader == "myproject.loaders:python_one"
    ref = resolve_loader_rungs(db, entry)[0][0]
    assert ref == "myproject.loaders:python_one"


def test_precedence_roundtrip_preserves_all_three(tmp_path):
    """precedence round-trips preserving _LANG.python + _LANG.julia + bare loader."""
    db = _db(tmp_path)
    written = _roundtrip(db)
    p = written["precedence"]
    assert p["loader"] == "myproject.loaders:bare_one"          # bare, not promoted
    assert p["_LANG"]["python"]["loader"] == "myproject.loaders:python_one"
    assert p["_LANG"]["julia"]["loader"] == "MyProject:julia_one"


# ----- Binding forms: string and { ref, args, kwargs } table -----

def test_bare_fetcher_string_and_table_forms(tmp_path):
    text = """
[_META]
schema = 1

[a]
sha256 = "aa"
format = "nc"
fetcher = "pkg.mod:plain"

[b.fetcher]
ref = "pkg.mod:withargs"
args = ["$download_path"]
kwargs = { grid = "5x5" }
"""
    db = _db(tmp_path, text)
    a = db.datasets["a"]
    assert a.fetcher == "pkg.mod:plain"
    assert a.fetcher_args == [] and a.fetcher_kwargs == {}
    b = db.datasets["b"]
    assert b.fetcher == "pkg.mod:withargs"
    assert b.fetcher_args == ["$download_path"]
    assert b.fetcher_kwargs == {"grid": "5x5"}


def test_bare_loader_string_and_table_forms(tmp_path):
    text = """
[_META]
schema = 1

[a]
uri = "https://example.com/a.csv"
sha256 = "aa"
loader = "pkg.mod:plain"

[b]
uri = "https://example.com/b.nc"
sha256 = "bb"

[b.loader]
ref = "pkg.mod:withargs"
kwargs = { skip = true }
"""
    db = _db(tmp_path, text)
    assert db.datasets["a"].loader == "pkg.mod:plain"
    b = db.datasets["b"]
    assert b.loader == "pkg.mod:withargs"
    assert b.loader_kwargs == {"skip": True}


def test_loaders_table_form_with_var_substitution(tmp_path):
    """[_LOADERS] table form runs with $var substitution (spec-v3.4)."""
    from tests.helpers import loaders as L

    text = """
[_META]
schema = 1

[_LOADERS.csv]
ref = "tests.helpers.loaders:param_loader"
args = ["$path"]
kwargs = { grid = "$key", scale = 3 }

[d]
uri = "https://example.com/d.csv"
sha256 = "dd"
skip_download = true
local_path = "%s"
"""
    f = tmp_path / "d.csv"
    f.write_text("x")
    db = _db(tmp_path, text % str(f))
    assert db.loaders["csv"] == "tests.helpers.loaders:param_loader"
    assert db.loaders_kwargs["csv"] == {"grid": "$key", "scale": 3}
    result = load_dataset(db, "d")
    # $key substituted into the grid kwarg
    assert result[0] == "param"
    assert L.param_loader.last_call["grid"] == db.datasets["d"].key
    assert L.param_loader.last_call["scale"] == 3


# ----- Tolerance: bare fails -> warn + fall through; explicit fails -> raise -----

def test_bare_loader_failure_falls_through(tmp_path, caplog):
    """A bare loader whose ref can't import warns and falls through to built-in."""
    f = tmp_path / "d.json"
    f.write_text('{"k": 1}')
    text = """
[_META]
schema = 1

[d]
uri = "https://example.com/d.json"
sha256 = "dd"
skip_download = true
format = "json"
loader = "MyJulia:load_it"
local_path = "%s"
""" % str(f)
    db = _db(tmp_path, text)
    with caplog.at_level("WARNING"):
        result = load_dataset(db, "d")
    # Built-in json loader ran (fell through), no exception.
    assert result == {"k": 1}
    assert any("falling through the load ladder" in r.message for r in caplog.records)


def test_explicit_python_loader_failure_raises(tmp_path):
    """An explicit _LANG.python.loader that can't import still hard-errors."""
    f = tmp_path / "d.json"
    f.write_text('{"k": 1}')
    text = """
[_META]
schema = 1

[d]
uri = "https://example.com/d.json"
sha256 = "dd"
skip_download = true
format = "json"
local_path = "%s"

[d._LANG.python]
loader = "nonexistent_module_xyz:load_it"
""" % str(f)
    db = _db(tmp_path, text)
    with pytest.raises(Exception):
        load_dataset(db, "d")


# ----- Round-trip: bare stays bare -----

def _roundtrip(db):
    data = db.to_dict()
    buf = io.BytesIO()
    tomli_w.dump(_sort_recursive(data), buf)
    buf.seek(0)
    return tomllib.load(buf)


def test_bare_fetcher_roundtrips_bare_as_string(tmp_path):
    db = _db(tmp_path)
    written = _roundtrip(db)
    derived = written["derived"]
    assert derived["fetcher"] == "myproject.build:derived"   # string, bare ref
    assert "_LANG" not in derived                            # not promoted


def test_bare_loader_roundtrips_bare(tmp_path):
    db = _db(tmp_path)
    written = _roundtrip(db)
    assert written["temperature"]["loader"] == "myproject.loaders:load_temperature"
    assert "_LANG" not in written["temperature"]


def test_loaders_roundtrips_bare_map(tmp_path):
    db = _db(tmp_path)
    written = _roundtrip(db)
    assert written["_LOADERS"] == {
        "csv": "myproject.io:read_csv",
        "nc": "myproject.io:read_nc",
    }
    # not promoted to top-level _LANG.python.loaders
    assert "_LANG" not in written


def test_bare_binding_with_args_roundtrips_as_table(tmp_path):
    text = """
[_META]
schema = 1

[b]
sha256 = "bb"
format = "nc"

[b.fetcher]
ref = "pkg.mod:f"
kwargs = { grid = "5x5" }
"""
    db = _db(tmp_path, text)
    written = _roundtrip(db)
    assert written["b"]["fetcher"] == {"ref": "pkg.mod:f", "kwargs": {"grid": "5x5"}}


def test_shell_roundtrips_as_top_level_string(tmp_path):
    text = """
[_META]
schema = 1

[s]
sha256 = "ss"
shell = "make-it -o $download_path"
"""
    db = _db(tmp_path, text)
    written = _roundtrip(db)
    assert written["s"]["shell"] == "make-it -o $download_path"
    assert "_LANG" not in written["s"]


# ----- Shell: bare wins over legacy, legacy preserved, migrate demotes -----

SHELL_BOTH = """
[_META]
schema = 1

[s]
sha256 = "ss"
shell = "bare-cmd -o $download_path"

[s._LANG.shell]
fetcher = "legacy-cmd -o $download_path"
"""


def test_bare_shell_wins_over_legacy_lang_shell(tmp_path):
    db = _db(tmp_path, SHELL_BOTH)
    entry = db.datasets["s"]
    kind, value = resolve_fetcher(entry)
    assert kind == "shell"
    assert value == "bare-cmd -o $download_path"   # bare wins


def test_legacy_lang_shell_preserved_verbatim(tmp_path):
    db = _db(tmp_path, SHELL_BOTH)
    written = _roundtrip(db)
    assert written["s"]["shell"] == "bare-cmd -o $download_path"
    assert written["s"]["_LANG"]["shell"]["fetcher"] == "legacy-cmd -o $download_path"


def test_migrate_demotes_lang_shell_to_bare(tmp_path):
    text = """
[_META]
schema = 1

[s]
sha256 = "ss"

[s._LANG.shell]
fetcher = "legacy-cmd -o $download_path"
"""
    db = _db(tmp_path, text)
    assert db.datasets["s"].shell == ""           # starts only as legacy
    migrate_v0_to_v1(db)
    entry = db.datasets["s"]
    assert entry.shell == "legacy-cmd -o $download_path"   # demoted to bare
    # the now-empty _LANG.shell block is dropped
    assert "_LANG" not in entry.extra or "shell" not in entry.extra.get("_LANG", {})
    written = _roundtrip(db)
    assert written["s"]["shell"] == "legacy-cmd -o $download_path"
    assert "_LANG" not in written["s"]
