"""Cross-language fetch (fetch-ladder rung 3).

The rare case in the fetch ladder: a dataset whose bytes can be produced only
by a fetcher defined in *another* language (a ``[<ds>._LANG.<other>].fetcher``),
with no native Python fetcher, no ``_LANG.shell`` fetcher, and no ``uri``.

The mechanism is implementation-defined; the Python implementation invokes the
foreign language's runtime directly. The supported case is **Julia**: run the
local ``DataManifest`` Julia package to materialize the dataset into the shared
store, then Python reads the bytes from disk. Loading never crosses languages —
only bytes move.

This module lives in the *fetch layer* (it may import ``store`` and ``database``
types). It MUST NOT be imported by ``datamanifest/cache/**`` — the cache layer
imports only ``datamanifest.store``.

Every subprocess call goes through an injectable runner so tests capture the
argv and simulate materialization offline; the module-level :data:`_runner`
defaults to :func:`subprocess.run` and a ``runner=`` parameter overrides it.
"""

import os
import shutil

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .config import logger
from .database import resolve_existing_path

# DataManifest.jl package identity (src/Project.toml). The uuid lets the env
# probe optionally confirm the dependency rather than matching a name alone.
DATAMANIFEST_JL_UUID = "b8ee69ef-a20a-4d38-bceb-a68d72817f72"

# Injectable subprocess runner; tests replace it (or pass runner=) to capture
# the argv and simulate materialization without invoking a real `julia`.
_runner = None


def _get_runner(runner):
    if runner is not None:
        return runner
    if _runner is not None:
        return _runner
    import subprocess

    return subprocess.run


def foreign_fetcher_lang(entry):
    """Return the language of a *foreign* fetcher binding, or ``None``.

    Inspects ``entry.extra["_LANG"]`` for a ``<lang>`` subtree (other than
    ``python`` — this tool's own language — and ``shell`` — handled by its own
    fetch-ladder rung) that carries a ``fetcher`` binding (a bare ref string or
    a ``{ ref, ... }`` table). Returns that language tag (e.g. ``"julia"``), or
    ``None`` when no foreign fetcher binding exists.
    """
    lang = entry.extra.get("_LANG")
    if not isinstance(lang, dict):
        return None
    for name, subtree in lang.items():
        if name in ("python", "shell"):
            continue
        if not isinstance(subtree, dict):
            continue
        fetcher = subtree.get("fetcher")
        if isinstance(fetcher, str) and fetcher:
            return name
        if isinstance(fetcher, dict) and fetcher.get("ref"):
            return name
    return None


def _project_has_datamanifest(project_toml: str) -> bool:
    """True when *project_toml* declares the ``DataManifest`` dependency."""
    try:
        with open(project_toml, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    deps = data.get("deps")
    if not isinstance(deps, dict):
        return False
    return "DataManifest" in deps


def julia_project(project_root: str, env=os.environ):
    """Locate a local Julia environment that depends on ``DataManifest``.

    Looks for a ``Project.toml`` whose ``[deps]`` table contains the
    ``DataManifest`` key:

    1. at ``$JULIA_PROJECT`` (a directory holding ``Project.toml``, or the
       ``Project.toml`` file itself), then
    2. walking up from *project_root* to the filesystem root.

    Returns the project *directory* (the folder containing ``Project.toml``), or
    ``None`` when no such environment is found.
    """
    candidates = []
    julia_project_env = env.get("JULIA_PROJECT", "")
    if julia_project_env:
        if os.path.basename(julia_project_env) == "Project.toml":
            candidates.append(julia_project_env)
        else:
            candidates.append(os.path.join(julia_project_env, "Project.toml"))

    if project_root:
        current = os.path.abspath(project_root)
        while True:
            candidates.append(os.path.join(current, "Project.toml"))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

    for project_toml in candidates:
        if os.path.isfile(project_toml) and _project_has_datamanifest(project_toml):
            return os.path.dirname(os.path.abspath(project_toml))
    return None


def julia_available(env=os.environ) -> bool:
    """True when a ``julia`` executable is discoverable on ``PATH``."""
    return bool(shutil.which("julia", path=env.get("PATH")))


def _julia_argv(project_dir: str, datasets_toml: str, name: str):
    """Build the ``julia`` argv that materializes *name* via DataManifest.jl.

    Runs, in the located Julia environment, the Julia ``DataManifest`` package's
    own fetch (``download_dataset``) against the shared manifest. The subprocess
    inherits ``os.environ``, so ``DATAMANIFEST_*`` store overrides propagate and
    both ends resolve the same on-disk path.
    """
    code = (
        f'using DataManifest; '
        f'download_dataset(Database("{datasets_toml}"), "{name}")'
    )
    return ["julia", f"--project={project_dir}", "-e", code]


def delegate_fetch(db, entry, name, *, project_root, runner=None):
    """Materialize *entry* by invoking the foreign-language runtime (Julia).

    Builds and runs, via the runner, the Julia ``DataManifest`` invocation that
    fetches *name* into the shared store, then re-resolves the now-materialized
    path. The subprocess inherits ``os.environ`` so store overrides propagate.

    Returns the materialized path on success (a zero exit *and* a present,
    complete entry on disk), or ``None`` to signal failure so the caller falls
    through to the ``uri`` rung.
    """
    project_dir = julia_project(project_root, env=os.environ)
    if project_dir is None or not julia_available(os.environ):
        logger.warning(
            "Dataset %r has a Julia fetcher but no usable Julia environment was "
            "found (need `julia` on PATH and a Project.toml that depends on "
            "DataManifest); skipping cross-language fetch and falling back to the "
            "standard download.", name,
        )
        return None

    datasets_toml = os.path.abspath(db.datasets_toml) if db.datasets_toml else ""
    if not datasets_toml:
        return None  # no manifest path to hand the peer; nothing to delegate

    argv = _julia_argv(project_dir, datasets_toml, name)
    run = _get_runner(runner)
    try:
        result = run(argv, env=os.environ)
    except Exception as exc:  # noqa: BLE001 - toolchain failure → fall back
        logger.warning(
            "Julia cross-language fetch for %r could not be launched (%s); "
            "falling back to the standard download.", name, exc,
        )
        return None

    returncode = getattr(result, "returncode", result)
    if returncode != 0:
        logger.warning(
            "Julia cross-language fetch for %r exited %s; falling back to the "
            "standard download.", name, returncode,
        )
        return None

    path = resolve_existing_path(db, entry)
    if os.path.isfile(path) or os.path.isdir(path):
        return path
    logger.warning(
        "Julia cross-language fetch for %r produced no output on disk; falling "
        "back to the standard download.", name,
    )
    return None
