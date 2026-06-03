"""Command-line interface for datamanifest.

Entry point: ``datamanifest.cli:main``, wired via ``[project.scripts]`` in
``pyproject.toml``.  Uses argparse + ``add_argument_group()`` following the
bard/scribe/texmark convention — no click/typer dependency.

All subcommands use the default-database mechanism (get_default_database()) so
they work without explicit Database instantiation; the active TOML is found via
DATAMANIFEST_TOML env-var or a cwd-upward walk (Item 17).
"""

import argparse
import datetime
import os
import shutil
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


def _add_delegate_flags(group):
    """Add the mutually-exclusive ``--delegate`` / ``--no-delegate`` pair.

    Toggles the cross-language fetch rung (fetch-ladder rung 3) for the run.
    Stored on ``args.delegate``: ``None`` (no flag, keep each entry's own
    setting), ``True`` (``--delegate``), or ``False`` (``--no-delegate``).
    """
    excl = group.add_mutually_exclusive_group()
    excl.add_argument(
        "--delegate", dest="delegate", action="store_true", default=None,
        help="Force the cross-language fetch rung on (run a foreign-language "
             "fetcher via the local Julia DataManifest env when present)",
    )
    excl.add_argument(
        "--no-delegate", dest="delegate", action="store_false",
        help="Disable the cross-language fetch rung for this run",
    )


# ----- subcommand implementations -----

# Fields a maintenance object exposes, in display order. ``key``/``hash`` and
# the timestamps double as the spec-v3 ``datamanifest list`` object schema.
_OBJECT_FIELDS = (
    "kind", "key", "hash", "cachetype", "version", "scope", "format",
    "size", "location", "referenced", "created", "last-access",
)
_DEFAULT_OBJECT_FIELDS = ("kind", "referenced", "key", "location")


def _object_size(path: str) -> int:
    """Total bytes of *path* (a file, or every file under a directory)."""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    from .cache._inspect import _dir_size

    return _dir_size(path)


def _get_field(obj, name: str) -> str:
    """Render the maintenance field *name* of *obj* for display."""
    attr = "last_access" if name == "last-access" else name
    val = getattr(obj, attr, "")
    if name == "referenced":
        return {True: "true", False: "false", None: "?"}[val]
    return "" if val is None else str(val)


def _age_seconds(iso: str, now: float):
    """Seconds between *now* (epoch) and the RFC-3339 stamp *iso* (``None`` when
    *iso* is empty or unparseable)."""
    if not iso:
        return None
    try:
        dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None
    return now - dt.timestamp()


def _is_maintenance(args) -> bool:
    """True when any object-view / maintenance flag is set on ``list``."""
    return any((
        args.kind, args.scope, args.format, args.orphan,
        args.older_than, args.fields, args.delete, args.move,
        getattr(args, "push", None), getattr(args, "pull", None),
    ))


def _enumerate_objects(db):
    """The composition root: enumerate produced artifacts (cache layer) and
    fetched datasets (fetch layer) as a single list of maintenance objects.

    Reachability is resolved here — the one place that bridges both layers: a
    produced artifact is ``referenced`` iff its portable key is rooted by the
    project's ``cached.toml``; a present fetched dataset is referenced by its
    manifest entry. The cache-layer enumeration itself imports only ``store``.
    """
    from . import storage
    from .cache import CACHED_INDEX_NAME, CachedIndex, enumerate_artifacts
    from .cache._inspect import CacheObject
    from .cache._usage import iso_from_mtime, last_access
    from .database import resolve_existing_path

    objects = []

    # Produced artifacts under the resolved $cache root, tagged referenced via
    # the project's sibling cached.toml.
    cache_root = storage.resolve_selector("$cache")
    prefix = storage.content_prefix("cached")
    referenced_keys = set()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    cached_toml = os.path.join(base or ".", CACHED_INDEX_NAME)
    if os.path.isfile(cached_toml):
        try:
            referenced_keys = CachedIndex.read(cached_toml).keys()
        except Exception:  # noqa: BLE001 - an unreadable index roots nothing
            referenced_keys = set()
    for obj in enumerate_artifacts(cache_root, prefix=prefix):
        obj.referenced = obj.key in referenced_keys
        objects.append(obj)

    # Present fetched datasets (always referenced — they are manifest entries).
    for name, entry in db.datasets.items():
        try:
            path = resolve_existing_path(db, entry)
        except Exception:  # noqa: BLE001 - unresolvable entry is not on disk
            continue
        if not (os.path.isfile(path) or os.path.isdir(path)):
            continue
        objects.append(CacheObject(
            kind="datasets",
            location=os.path.abspath(path),
            key=name,
            format=getattr(entry, "format", "") or "",
            size=_object_size(path),
            created=iso_from_mtime(path),
            last_access=last_access(path),
            referenced=True,
        ))
    return objects


def _filter_objects(objects, args):
    """Apply the ``list`` filter flags (``--kind``/``--scope``/``--format``/
    ``--orphan``/``--older-than``) to *objects*."""
    out = objects
    if args.kind:
        out = [o for o in out if o.kind == args.kind]
    if args.scope is not None:
        out = [o for o in out if o.scope == args.scope]
    if args.format:
        out = [o for o in out if o.format == args.format]
    if args.orphan:
        out = [o for o in out if o.referenced is False]
    if args.older_than:
        seconds = _parse_duration(args.older_than)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        kept = []
        for o in out:
            age = _age_seconds(o.last_access, now)
            if age is not None and age > seconds:
                kept.append(o)
        out = kept
    return out


# ----- human-friendly default listing ---------------------------------------

# ANSI styles, applied only when writing to a TTY with NO_COLOR unset.
_STYLES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "green": "\033[32m", "cyan": "\033[36m", "red": "\033[31m",
    "yellow": "\033[33m",
}


def _color_enabled(stream=None) -> bool:
    """Colorize only on an interactive terminal, honoring the ``NO_COLOR``
    convention (and a ``DATAMANIFEST_NO_COLOR`` override)."""
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("DATAMANIFEST_NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text, *styles, on=True) -> str:
    """Wrap *text* in the named ANSI *styles* when *on* (else return it bare)."""
    if not on or not styles:
        return text
    return "".join(_STYLES[s] for s in styles) + text + _STYLES["reset"]


def _osc8(uri, label, *, on=True) -> str:
    """Render *label* as an OSC-8 terminal hyperlink to *uri* (clickable in
    modern terminals), matching the papers CLI convention. A no-op when *on* is
    false or no *uri* is given, so piped/plain output stays clean."""
    if not on or not uri:
        return label
    return f"\033]8;;{uri}\033\\{label}\033]8;;\033\\"


def _fit(text, width, *, keep_tail=False) -> str:
    """Clamp *text* to *width* columns with an ellipsis. With *keep_tail* the
    end is kept (useful for paths — the basename stays visible)."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return "…" + text[-(width - 1):] if keep_tail else text[: width - 1] + "…"


def _default_list_data(db):
    """Rows for the human default listing, as ``(datasets, cached)``.

    *datasets* is every manifest entry with present/size/location resolved;
    *cached* is every produced artifact under ``$cache``, tagged with its
    registry name and reachability from the sibling ``cached.toml``.
    """
    from .database import resolve_existing_path

    datasets = []
    for name, entry in db.datasets.items():
        path, present = "", False
        try:
            path = resolve_existing_path(db, entry)
            present = os.path.isfile(path) or os.path.isdir(path)
        except Exception:  # noqa: BLE001 - an unresolvable entry is just missing
            present = False
        datasets.append({
            "name": name,
            "format": getattr(entry, "format", "") or "",
            "present": present,
            "size": _object_size(path) if present else 0,
            "location": os.path.abspath(path) if present else "",
        })

    from . import storage
    from .cache import CACHED_INDEX_NAME, CachedIndex, enumerate_artifacts

    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    cached_toml = os.path.join(base or ".", CACHED_INDEX_NAME)
    names_by_id = {}
    if os.path.isfile(cached_toml):
        try:
            idx = CachedIndex.read(cached_toml)
        except Exception:  # noqa: BLE001 - an unreadable index names nothing
            idx = None
        if idx is not None:
            for nm, e in idx.entries.items():
                names_by_id[(e.get("cachetype", ""), e.get("hash", ""))] = nm

    cache_root = storage.resolve_selector("$cache")
    prefix = storage.content_prefix("cached")
    cached = []
    for obj in enumerate_artifacts(cache_root, prefix=prefix):
        ident = (obj.cachetype, obj.hash)
        cached.append({
            "name": names_by_id.get(ident) or f"{obj.cachetype}/{obj.hash[:12]}",
            "scope": obj.scope,
            "format": obj.format,
            "size": obj.size,
            "location": obj.location,
            "referenced": ident in names_by_id,
        })
    return datasets, cached


def _print_default_list(db, args):
    """The default ``datamanifest list`` view: one styled line per object,
    datasets and cached artifacts grouped and color-coded, sized to the
    terminal width. ``--present`` / ``--missing`` keep their plain name-only
    output (scriptable)."""
    datasets, cached = _default_list_data(db)

    if args.present or args.missing:
        for d in datasets:
            if (args.present and d["present"]) or (args.missing and not d["present"]):
                print(d["name"])
        return

    on = _color_enabled()
    width = shutil.get_terminal_size((80, 20)).columns

    if not datasets and not cached:
        print(_paint("No datasets or cached artifacts.", "dim", on=on))
        return

    names = [d["name"] for d in datasets] + [c["name"] for c in cached]
    name_w = min(max((len(n) for n in names), default=4), 36)
    name_w = max(name_w, 4)
    fmt_w, size_w = 8, 9
    tail_w = max(12, width - (name_w + fmt_w + size_w + 8))

    def emit(glyph, name, fmt, size_str, tail, *, name_styles,
             tail_styles=("dim",), keep_tail=True, prefix="", link=""):
        # The name itself links to its on-disk location (clickable file://);
        # the tail shows the truncated path (also linked) or a status word.
        uri = f"file://{link}" if link else ""
        g = _paint(glyph, *name_styles, on=on)
        n = _osc8(uri, _fit(name, name_w).ljust(name_w), on=on)
        n = _paint(n, *name_styles, on=on)
        f = _paint(_fit(fmt or "—", fmt_w).ljust(fmt_w), "dim", on=on)
        s = _paint(size_str.rjust(size_w), "dim", on=on)
        tail_fit = _fit(tail, tail_w - len(prefix), keep_tail=keep_tail)
        t = _paint(_osc8(uri, prefix + tail_fit, on=on), *tail_styles, on=on)
        print(f"{g} {n}  {f}  {s}  {t}")

    if datasets:
        print(_paint("Datasets", "bold", on=on))
        for d in datasets:
            if d["present"]:
                emit("●", d["name"], d["format"], _fmt_size(d["size"]),
                     d["location"], name_styles=("bold", "green"),
                     link=d["location"])
            else:
                emit("○", d["name"], d["format"], "—", "missing",
                     name_styles=("dim",), tail_styles=("red",), keep_tail=False)

    if cached:
        if datasets:
            print()
        print(_paint("Cached", "bold", on=on))
        for c in cached:
            # Referenced artifacts are cyan; orphans (no cached.toml root) are
            # flagged in yellow — the colour carries the status, no extra column.
            if c["referenced"]:
                emit("◆", c["name"], c["format"], _fmt_size(c["size"]),
                     c["location"], name_styles=("bold", "cyan"),
                     link=c["location"])
            else:
                emit("◆", c["name"], c["format"], _fmt_size(c["size"]),
                     c["location"], name_styles=("bold", "yellow"),
                     tail_styles=("yellow",), prefix="orphan  ",
                     link=c["location"])


def _cmd_list(args):
    db = _get_db()

    if not _is_maintenance(args):
        _print_default_list(db, args)
        return

    # ----- maintenance object view -----
    objects = _filter_objects(_enumerate_objects(db), args)

    if args.delete or args.move:
        _maintain(objects, args)
        return

    if getattr(args, "push", None) or getattr(args, "pull", None):
        from . import sync

        host = args.push or args.pull
        direction = "push" if args.push else "pull"
        sync_objects = []
        for obj in objects:
            try:
                sync_objects.append(sync.sync_object_from_location(
                    db, kind=obj.kind, ident=obj.key, location=obj.location,
                ))
            except sync.RemoteRepoError as e:
                print(f"Skipped ($repo, out of scope): {obj.key}", file=sys.stderr)
                continue
        _do_transfer(db, sync_objects, host, direction, args)
        return

    fields = (
        [f.strip() for f in args.fields.split(",") if f.strip()]
        if args.fields else list(_DEFAULT_OBJECT_FIELDS)
    )
    for obj in objects:
        print("\t".join(_get_field(obj, f) for f in fields))


def _maintain(objects, args):
    """Run ``--delete`` / ``--move`` over the selected *objects*.

    Both default to a **dry run** (report only); ``--yes`` performs the action.
    Only produced (``kind="cached"``) artifacts are eligible — fetched datasets,
    ``$data``/``$repo`` and ``local_path`` data are reported as skipped and never
    touched.
    """
    from .cache import delete_object, move_object

    do_it = args.yes
    if args.move:
        verb = "Moved" if do_it else "Would move"
    else:
        verb = "Deleted" if do_it else "Would delete"

    acted = 0
    for obj in objects:
        if obj.kind != "cached":
            print(f"Skipped ({obj.kind}, protected): {obj.location}")
            continue
        if args.move:
            dest = os.path.join(args.move, obj.cachetype, obj.hash) if not obj.version \
                else os.path.join(args.move, obj.cachetype, obj.version, obj.hash)
            print(f"{verb}: {obj.key}  {obj.location} -> {dest}")
            if do_it:
                move_object(obj, args.move)
        else:
            print(f"{verb}: {obj.key}  {obj.location}")
            if do_it:
                delete_object(obj)
        acted += 1

    noun = "artifact" if acted == 1 else "artifacts"
    if not do_it:
        print(f"{verb}: {acted} produced {noun} (dry run; pass --yes to apply)")
    else:
        print(f"{verb}: {acted} produced {noun}")


def _fmt_size(n: int) -> str:
    """Human-readable byte size for the sync dry-run report."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _do_transfer(db, objects, host, direction, args):
    """Push/pull each resolved :class:`SyncObject` to/from *host*.

    Shared by the ``push`` / ``pull`` subcommands and ``list --push/--pull``.
    With ``--dry-run`` it reports the selection (id, kind, local & remote paths,
    size) and transfers nothing."""
    from . import sync

    project_root = db.get_project_root()
    verb = direction.capitalize()
    for obj in objects:
        plan = sync.transfer(
            db, obj, host, direction=direction, project_root=project_root,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(
                f"Would {direction}: {plan['kind']} {plan['id']}  "
                f"{_fmt_size(plan['size'])}\n"
                f"  local : {plan['local']}\n"
                f"  remote: {host}:{plan['remote']}"
            )
        else:
            print(f"{verb}ed: {plan['kind']} {plan['id']}  -> {host}:{plan['remote']}"
                  if direction == "push" else
                  f"{verb}ed: {plan['kind']} {plan['id']}  <- {host}:{plan['remote']}")


def _cmd_push(args):
    db = _get_db()
    from . import sync

    objects = sync.resolve_objects(db, args.id, batch=args.batch)
    _do_transfer(db, objects, args.host, "push", args)


def _cmd_pull(args):
    db = _get_db()
    from . import sync

    objects = sync.resolve_objects(db, args.id, batch=args.batch)
    _do_transfer(db, objects, args.host, "pull", args)


def _apply_delegate_override(db, delegate):
    """Apply a run-level --delegate / --no-delegate override to every entry.

    ``delegate`` is ``None`` (no flag — keep each entry's own setting), ``True``
    (force the cross-language fetch rung on), or ``False`` (force it off). The
    override is in-memory only; it is never persisted to the manifest.
    """
    if delegate is None:
        return
    for entry in db.datasets.values():
        entry.delegate = delegate


def _cmd_download(args):
    db = _get_db()
    from .pipelines import download_dataset, download_datasets

    _apply_delegate_override(db, args.delegate)
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

    name, entry = db.register_dataset(args.uri, overwrite=args.overwrite, **kwargs)

    if not args.no_download:
        from .pipelines import download_dataset
        if args.delegate is not None:
            entry.delegate = args.delegate
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

def _parse_duration(text: str) -> float:
    """Parse a human duration (``"7d"``, ``"36h"``, ``"3600"``, ``"90 s"``) into
    seconds. A bare number is seconds. Raises ``ValueError`` on a bad unit.

    Backs the ``datamanifest list --older-than`` age filter."""
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

    # list — dataset listing + the spec-v3 store-maintenance surface.
    p_list = subparsers.add_parser(
        "list",
        help="List datasets, or inspect/maintain stored objects",
        description=(
            "With no flags (or --present/--missing/--all), list dataset names. "
            "Any maintenance flag switches to the object view: produced (cached) "
            "artifacts and fetched datasets with their fields, plus the explicit "
            "--delete / --move actions (dry run unless --yes)."
        ),
    )
    filter_group = p_list.add_argument_group("name filter")
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
    obj_group = p_list.add_argument_group("object view (maintenance)")
    obj_group.add_argument(
        "--kind", choices=["datasets", "cached"],
        help="Only objects of this kind (fetched datasets / produced artifacts)",
    )
    obj_group.add_argument(
        "--scope", metavar="SCOPE",
        help="Only objects under this scope segment (e.g. a project-id)",
    )
    obj_group.add_argument(
        "--format", metavar="FMT", help="Only objects in this serialization format"
    )
    obj_group.add_argument(
        "--orphan", action="store_true",
        help="Only unreferenced produced artifacts (no cached.toml root)",
    )
    obj_group.add_argument(
        "--older-than", dest="older_than", metavar="AGE",
        help="Only objects last accessed more than AGE ago (e.g. 7d, 36h, 3600)",
    )
    obj_group.add_argument(
        "--fields", metavar="F1,F2,...",
        help=(
            "Comma-separated fields to print (default: "
            + ",".join(_DEFAULT_OBJECT_FIELDS)
            + "). Available: " + ",".join(_OBJECT_FIELDS) + "."
        ),
    )
    act_group = p_list.add_argument_group("object actions (maintenance)")
    _act_excl = act_group.add_mutually_exclusive_group()
    _act_excl.add_argument(
        "--delete", action="store_true",
        help="Delete the selected produced artifacts (dry run unless --yes)",
    )
    _act_excl.add_argument(
        "--move", metavar="DEST",
        help="Move the selected produced artifacts under DEST (dry run unless --yes)",
    )
    act_group.add_argument(
        "--yes", "-y", action="store_true",
        help="Actually perform --delete / --move (otherwise a dry run)",
    )
    sync_group = p_list.add_argument_group("object actions (sync)")
    _sync_excl = sync_group.add_mutually_exclusive_group()
    _sync_excl.add_argument(
        "--push", metavar="SSH_HOST",
        help="Push the selected objects to SSH_HOST (rsync over ssh)",
    )
    _sync_excl.add_argument(
        "--pull", metavar="SSH_HOST",
        help="Pull the selected objects from SSH_HOST (rsync over ssh)",
    )
    sync_group.add_argument(
        "--dry-run", action="store_true",
        help="With --push/--pull: report the selection and transfer nothing",
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
    _add_delegate_flags(dl_opts)
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
    _add_delegate_flags(add_opts)
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

    # push / pull — cross-machine sync of a single object (rsync over ssh).
    for name, func, arrow in (("push", _cmd_push, "to"), ("pull", _cmd_pull, "from")):
        p_sync = subparsers.add_parser(
            name,
            help=f"Transfer a stored object {arrow} an SSH host (rsync over ssh)",
            description=(
                f"{name.capitalize()} a single stored object {arrow} SSH_HOST. "
                "The object is addressed by its machine-independent id: a fetched "
                "dataset by name/alias/doi, or a produced artifact by "
                "cachetype[/version]/hash (full or an unambiguous hash prefix). "
                "An ambiguous id errors unless --batch. Writes no manifest; "
                "idempotent. $repo-stored datasets are refused (out of scope)."
            ),
        )
        p_sync.add_argument("id", metavar="ID", help="Object identifier")
        p_sync.add_argument("host", metavar="SSH_HOST", help="user@host or host")
        sync_opts = p_sync.add_argument_group("options")
        sync_opts.add_argument(
            "--dry-run", action="store_true",
            help="Report the selection (id, kind, paths, size) and transfer nothing",
        )
        sync_opts.add_argument(
            "--batch", action="store_true",
            help="Transfer all objects matching an ambiguous id instead of erroring",
        )
        p_sync.set_defaults(func=func)

    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
