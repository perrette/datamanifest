"""Command-line interface for datamanifest.

Entry point: ``datamanifest.cli:main``, wired via ``[project.scripts]`` in
``pyproject.toml``.  Uses argparse + ``add_argument_group()`` following the
bard/scribe/texmark convention — no click/typer dependency.

All subcommands use the default-database mechanism (get_default_database()) so
they work without explicit Database instantiation; the active TOML is found via
DATAMANIFEST_TOML env-var or a cwd-upward walk (Item 17).
"""

import argparse
import os
import sys

from . import __version__
from .config import logger


def _get_db():
    from .database import get_default_database
    try:
        return get_default_database()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ----- subcommand implementations -----

def _cmd_list(args):
    db = _get_db()
    from .database import resolve_existing_path

    present = []
    missing = []
    for name, entry in db.datasets.items():
        try:
            path = resolve_existing_path(db, entry)
        except Exception:
            missing.append(name)
            continue
        if os.path.isfile(path) or os.path.isdir(path):
            present.append(name)
        else:
            missing.append(name)

    if args.present:
        for name in present:
            print(name)
    elif args.missing:
        for name in missing:
            print(name)
    else:
        # Default and --all: present first, separator, then missing.
        for name in present:
            print(name)
        if missing:
            print()
            print("# missing")
            for name in missing:
                print(name)


def _cmd_download(args):
    db = _get_db()
    from .pipelines import download_dataset, download_datasets

    overwrite = args.overwrite
    if args.all or not args.name:
        download_datasets(db, overwrite=overwrite)
    else:
        for name in args.name:
            download_dataset(db, name, overwrite=overwrite)


def _cmd_path(args):
    db = _get_db()
    from .database import resolve_existing_path, search_dataset

    _name, entry = search_dataset(db, args.name)
    path = resolve_existing_path(db, entry)
    print(path)


def _cmd_add(args):
    db = _get_db()
    kwargs = {}
    if args.name:
        kwargs["name"] = args.name
    if args.extract:
        kwargs["extract"] = True

    name, _entry = db.register_dataset(args.uri, overwrite=args.overwrite, **kwargs)

    if not args.no_download:
        from .pipelines import download_dataset
        download_dataset(db, name)


def _cmd_remove(args):
    db = _get_db()
    from .database import delete_dataset

    delete_dataset(db, args.name, keep_cache=args.keep_cache)
    print(f"Removed: {args.name}")


def _cmd_show(args):
    db = _get_db()
    from .database import search_dataset, to_dict

    name, entry = search_dataset(db, args.name)
    print(f"[{name}]")
    d = to_dict(entry)
    for k, v in d.items():
        if isinstance(v, bool):
            print(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            print(f'{k} = "{v}"')
        elif isinstance(v, list):
            items = ", ".join(f'"{x}"' for x in v)
            print(f"{k} = [{items}]")
        else:
            print(f"{k} = {v}")


def _cmd_verify(args):
    db = _get_db()
    from .database import search_dataset, verify_checksum

    if args.name:
        entries = [search_dataset(db, n) for n in args.name]
    else:
        entries = list(db.datasets.items())

    failed = []
    for name, entry in entries:
        try:
            verify_checksum(db, entry, persist=False)
        except ValueError as e:
            print(f"MISMATCH: {name}: {e}", file=sys.stderr)
            failed.append(name)

    if failed:
        sys.exit(1)


def _cmd_update_checksums(args):
    db = _get_db()
    from .database import search_dataset, update_checksum

    if args.name:
        entries = [search_dataset(db, n) for n in args.name]
    else:
        entries = list(db.datasets.items())

    changed = []
    for name, entry in entries:
        action = update_checksum(db, entry, persist=False, dry_run=args.dry_run)
        if action in ("updated", "filled"):
            changed.append(name)
            verb = "would update" if args.dry_run else "updated"
            print(f"{verb}: {name}")
        elif action == "missing" and args.name:
            # Only nag about missing files when the user named specific datasets;
            # a bulk run silently skips whatever isn't on disk.
            print(f"missing: {name}", file=sys.stderr)

    if changed and not args.dry_run:
        db.write(db.datasets_toml)

    if not changed:
        msg = "No checksums would change." if args.dry_run else "No checksums changed."
        print(msg)


def _cmd_init(args):
    folder = os.path.abspath(args.folder) if args.folder else os.getcwd()
    toml_path = os.path.join(folder, "datasets.toml")

    if os.path.isfile(toml_path) and not args.force:
        print(
            f"Error: {toml_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    import tomli_w

    os.makedirs(folder, exist_ok=True)
    with open(toml_path, "wb") as f:
        tomli_w.dump({}, f)
    print(f"Created: {toml_path}")


def _cmd_where(args):
    db = _get_db()
    print(f"datasets_toml={db.datasets_toml}")
    print(f"datasets_folder={db.datasets_folder}")


def _cmd_format(args):
    """Rewrite a manifest in canonical form (the cross-tool byte-identity format).

    Reads TOML from FILE (or stdin when omitted / ``-``) and emits canonical
    TOML: every key sorted at every nesting level, via the same recursive sort +
    ``tomli_w`` serialization as :meth:`Database.write`. Peer tools (e.g.
    DataManifest.jl) pipe their output through ``datamanifest format`` to obtain
    byte-identical files. Content is never changed, only re-serialized.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    import tomli_w

    from .database import _sort_recursive

    if args.file in (None, "-"):
        data = tomllib.load(sys.stdin.buffer)
    else:
        path = os.path.abspath(args.file)
        if not os.path.isfile(path):
            print(f"Error: {path} not found.", file=sys.stderr)
            sys.exit(1)
        with open(path, "rb") as f:
            data = tomllib.load(f)

    out = tomli_w.dumps(_sort_recursive(data))

    if args.in_place:
        if args.file in (None, "-"):
            print("Error: --in-place requires a FILE.", file=sys.stderr)
            sys.exit(1)
        with open(os.path.abspath(args.file), "w") as f:
            f.write(out)
    else:
        sys.stdout.write(out)


_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
}

# GC default grace age — produced artifacts younger than this are kept even when
# unreferenced, so an in-flight produce is never reclaimed out from under a
# concurrent reader. Seven days mirrors Julia's depot GC default sensibility.
_GC_DEFAULT_GRACE = "7d"


def _parse_duration(text: str) -> float:
    """Parse a human duration (``"7d"``, ``"36h"``, ``"3600"``, ``"90 s"``) into
    seconds. A bare number is seconds. Raises ``ValueError`` on a bad unit."""
    s = str(text).strip().lower()
    if not s:
        raise ValueError("empty duration")
    try:
        return float(s)  # bare seconds
    except ValueError:
        pass
    num = s
    unit = ""
    for i, ch in enumerate(s):
        if ch.isalpha():
            num, unit = s[:i].strip(), s[i:].strip()
            break
    if unit not in _DURATION_UNITS:
        raise ValueError(
            f"unrecognized duration {text!r}: use seconds or a unit "
            "(s/m/h/d/w), e.g. '7d', '36h', '3600'"
        )
    return float(num) * _DURATION_UNITS[unit]


def _cmd_gc(args):
    """Reclaim unreferenced produced (@cached) artifacts under the $cache folder.

    Composition root (the one place that bridges both layers): it gathers the
    live root set from the cache layer's usage log plus the active
    datasets.toml / cached.toml, computes the union of produced live keys (every
    cached.toml) and fetched $cache live keys (every datasets.toml, via the fetch
    layer's Database), then hands that to the cache-layer GC collector, which
    walks only the resolved $cache folder.
    """
    from . import storage
    from .cache import CachedIndex, collect, known_paths, prune_missing, record_path
    from .cache._index import CACHED_INDEX_NAME
    from .database import Database

    try:
        grace = _parse_duration(args.grace)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # 1. Discover root files: the usage log, plus the active project's
    #    datasets.toml / cached.toml (recorded so future runs find them too).
    prune_missing()
    root_paths = set(known_paths())

    active_toml = ""
    try:
        from .config import get_default_toml
        active_toml = get_default_toml() or ""
    except Exception:  # noqa: BLE001 - no active project is fine
        active_toml = ""
    if active_toml and os.path.isfile(active_toml):
        root_paths.add(os.path.abspath(active_toml))
        sibling = os.path.join(os.path.dirname(active_toml), CACHED_INDEX_NAME)
        if os.path.isfile(sibling):
            root_paths.add(os.path.abspath(sibling))
            record_path(sibling)

    # 2/3. Build the live key set from every still-existing root.
    live_keys = set()
    cached_indexes = []
    datasets_tomls = []
    for path in sorted(root_paths):
        if not os.path.isfile(path):
            continue
        base = os.path.basename(path)
        if base == CACHED_INDEX_NAME:
            cached_indexes.append(path)
        else:
            datasets_tomls.append(path)

    # Produced live keys: every still-existing cached.toml (cache layer only).
    for path in cached_indexes:
        try:
            live_keys |= CachedIndex.read(path).keys()
        except Exception as e:  # noqa: BLE001 - skip an unreadable index
            print(f"Warning: skipping {path}: {e}", file=sys.stderr)

    # Fetched $cache live keys: every still-existing datasets.toml (fetch layer).
    # A fetched entry whose resolved store selector is $cache is a root, keyed by
    # its entry.key, so it is never collected.
    for path in datasets_tomls:
        try:
            db = Database(datasets_toml=path, persist=False)
        except Exception as e:  # noqa: BLE001 - skip an unreadable manifest
            print(f"Warning: skipping {path}: {e}", file=sys.stderr)
            continue
        proj = db.get_project_root()
        for entry in db.datasets.values():
            selector = entry.store or storage.project_default(db.storage_config)
            name, _, _sub = selector.lstrip("$").partition("/")
            if name == "cache" and entry.key:
                live_keys.add(entry.key)

    # 4. Resolve the single $cache root and collect.
    cache_root = storage.resolve_selector("$cache")
    candidates = collect(
        cache_root, live_keys,
        grace_seconds=grace, dry_run=args.dry_run,
    )

    verb = "Would collect" if args.dry_run else "Collected"
    if not candidates:
        kept = "(nothing unreferenced past the grace age)"
        print(f"{verb}: 0 produced artifacts {kept}")
        return
    for cand in candidates:
        print(f"{verb}: {cand.key}  {cand.path}")
    print(f"{verb}: {len(candidates)} produced artifact(s) under {cache_root}")


def _cmd_migrate(args):
    from .database import Database, migrate_v0_to_v1, migrate_v1_to_v2

    toml_path = os.path.abspath(args.file)
    if not os.path.isfile(toml_path):
        print(f"Error: {toml_path} not found.", file=sys.stderr)
        sys.exit(1)

    db = Database(datasets_toml=toml_path, persist=False)
    migrate_v0_to_v1(db)
    migrate_v1_to_v2(db)
    db.write(toml_path)
    print(f"Migrated: {toml_path}")


# ----- argument parser -----

def main():
    parser = argparse.ArgumentParser(
        prog="datamanifest",
        description="Declare and manage data dependencies for scientific projects.",
    )
    parser.add_argument(
        "--version", action="version", version=f"datamanifest {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # list
    p_list = subparsers.add_parser(
        "list", help="List datasets (present first, then missing)"
    )
    filter_group = p_list.add_argument_group("filter")
    _excl = filter_group.add_mutually_exclusive_group()
    _excl.add_argument(
        "--present", action="store_true", help="Show only present datasets"
    )
    _excl.add_argument(
        "--missing", action="store_true", help="Show only missing datasets"
    )
    _excl.add_argument(
        "--all", action="store_true", help="Show all datasets (default)"
    )
    p_list.set_defaults(func=_cmd_list)

    # download
    p_dl = subparsers.add_parser("download", help="Download datasets")
    p_dl.add_argument("name", nargs="*", metavar="NAME", help="Dataset name(s) to download")
    dl_opts = p_dl.add_argument_group("options")
    dl_opts.add_argument("--all", action="store_true", help="Download all datasets")
    dl_opts.add_argument(
        "--overwrite", action="store_true", help="Re-download and overwrite existing files"
    )
    p_dl.set_defaults(func=_cmd_download)

    # path
    p_path = subparsers.add_parser(
        "path", help="Print the resolved on-disk path for a dataset"
    )
    p_path.add_argument("name", metavar="NAME", help="Dataset name")
    p_path.set_defaults(func=_cmd_path)

    # add
    p_add = subparsers.add_parser("add", help="Register (and optionally download) a dataset")
    p_add.add_argument("uri", metavar="URI", help="Dataset URI")
    add_opts = p_add.add_argument_group("options")
    add_opts.add_argument("--name", "-n", metavar="N", help="Name for the dataset entry")
    add_opts.add_argument(
        "--no-download", action="store_true", help="Register without downloading"
    )
    add_opts.add_argument(
        "--extract", action="store_true", help="Extract archive after download"
    )
    add_opts.add_argument(
        "--overwrite", action="store_true", help="Overwrite an existing duplicate entry"
    )
    p_add.set_defaults(func=_cmd_add)

    # remove
    p_rm = subparsers.add_parser("remove", help="Delete a dataset entry")
    p_rm.add_argument("name", metavar="NAME", help="Dataset name")
    rm_opts = p_rm.add_argument_group("options")
    rm_opts.add_argument(
        "--keep-cache", action="store_true", help="Preserve cached files on disk"
    )
    p_rm.set_defaults(func=_cmd_remove)

    # show
    p_show = subparsers.add_parser("show", help="Print full entry detail (TOML-style)")
    p_show.add_argument("name", metavar="NAME", help="Dataset name")
    p_show.set_defaults(func=_cmd_show)

    # verify
    p_verify = subparsers.add_parser(
        "verify", help="Re-check sha256 checksums; exits nonzero on mismatch"
    )
    p_verify.add_argument(
        "name",
        nargs="*",
        metavar="NAME",
        help="Dataset name(s) to verify (default: all present datasets)",
    )
    p_verify.set_defaults(func=_cmd_verify)

    # update-checksums
    p_update = subparsers.add_parser(
        "update-checksums",
        help="Recompute stored sha256 checksums from the files on disk",
    )
    p_update.add_argument(
        "name",
        nargs="*",
        metavar="NAME",
        help="Dataset name(s) to update (default: all present datasets)",
    )
    p_update.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which checksums would change without writing the manifest",
    )
    p_update.set_defaults(func=_cmd_update_checksums)

    # init
    p_init = subparsers.add_parser(
        "init", help="Create a fresh datasets.toml in the current directory"
    )
    init_opts = p_init.add_argument_group("options")
    init_opts.add_argument(
        "--folder", metavar="PATH", help="Directory to create datasets.toml in (default: cwd)"
    )
    init_opts.add_argument(
        "--force", action="store_true", help="Overwrite an existing datasets.toml"
    )
    p_init.set_defaults(func=_cmd_init)

    # where
    p_where = subparsers.add_parser(
        "where", help="Print active datasets_toml and datasets_folder paths"
    )
    p_where.set_defaults(func=_cmd_where)

    # migrate
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Migrate a v0 manifest to v1 _LANG form (in-place)",
    )
    p_migrate.add_argument("file", metavar="FILE", help="Path to datasets.toml to migrate")
    p_migrate.set_defaults(func=_cmd_migrate)

    # format
    p_format = subparsers.add_parser(
        "format",
        help="Rewrite a manifest in canonical form (cross-tool byte-identical)",
    )
    p_format.add_argument(
        "file", metavar="FILE", nargs="?", default="-",
        help="Manifest TOML file (default: stdin)",
    )
    p_format.add_argument(
        "-i", "--in-place", action="store_true",
        help="Rewrite FILE in place instead of writing to stdout",
    )
    p_format.set_defaults(func=_cmd_format)

    # gc
    p_gc = subparsers.add_parser(
        "gc",
        help="Reclaim unreferenced produced (@cached) artifacts under $cache",
    )
    gc_opts = p_gc.add_argument_group("options")
    gc_opts.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be collected without deleting anything",
    )
    gc_opts.add_argument(
        "--grace", "--min-age", dest="grace", metavar="AGE",
        default=_GC_DEFAULT_GRACE,
        help=(
            "Keep artifacts younger than AGE even when unreferenced "
            "(seconds, or a unit: 7d, 36h, 90m, 3600). Default: "
            f"{_GC_DEFAULT_GRACE}."
        ),
    )
    p_gc.set_defaults(func=_cmd_gc)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
