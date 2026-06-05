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
    "kind", "name", "key", "hash", "cachetype", "version", "storage-path",
    "format", "params", "size", "location", "referenced", "present", "created",
    "last-access",
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
    attr = name.replace("-", "_")
    val = getattr(obj, attr, "")
    if name == "referenced":
        return {True: "true", False: "false", None: "?"}[val]
    if name == "present":
        return "true" if val else "false"
    if name == "params":
        return _fmt_params(val or {})
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


def _enumerate_objects(db):
    """The composition root: enumerate produced artifacts (cache layer) and
    fetched datasets (fetch layer) as a single list of objects.

    Reachability is resolved here — the one place that bridges both layers: a
    produced artifact is ``referenced`` iff its ``(cachetype, version, hash)`` is
    rooted by the project's ``cached.toml``; a fetched dataset is referenced by
    its manifest entry. Both **present** and not-yet-fetched datasets are
    included (``present`` tells them apart). The cache-layer enumeration itself
    imports only ``store``.
    """
    from . import storage
    from .cache import CACHED_INDEX_NAME, CachedIndex, enumerate_artifacts
    from .cache._inspect import CacheObject, cache_object_at
    from .cache._usage import iso_from_mtime, last_access
    from .database import resolve_existing_path

    objects = []
    project_root = db.get_project_root()

    # Produced artifacts under the resolved datacache_dir, tagged referenced via
    # the project's sibling cached.toml ((cachetype, version, hash) reachability).
    cache_root = storage.datacache_dir(
        project_root=project_root, storage_config=db.storage_config,
    )
    index = None
    referenced = set()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    cached_toml = os.path.join(base or ".", CACHED_INDEX_NAME)
    if os.path.isfile(cached_toml):
        try:
            index = CachedIndex.read(cached_toml)
            referenced = index.reachable_keys()
        except Exception:  # noqa: BLE001 - an unreadable index roots nothing
            index, referenced = None, set()
    seen_locations = set()
    for obj in enumerate_artifacts(cache_root):
        # name/params come from the artifact's own config.toml (set by
        # enumerate_artifacts).
        obj.referenced = (obj.cachetype, obj.version, obj.hash) in referenced
        objects.append(obj)
        seen_locations.add(obj.location)

    # Artifacts the index records at a location *outside* datacache_dir — e.g.
    # ones that were ``--move``\\d elsewhere — so they still surface in `list`.
    if index is not None:
        for rec in index.recipe_records():
            for sp in rec["instances"].values():
                if not sp:
                    continue
                adir = sp if os.path.isabs(sp) else os.path.join(project_root, sp)
                adir = os.path.abspath(adir)
                if adir in seen_locations:
                    continue
                obj = cache_object_at(adir)
                if obj is None:
                    continue
                obj.referenced = True
                objects.append(obj)
                seen_locations.add(adir)

    # Fetched datasets — present and not-yet-fetched alike (manifest entries are
    # always referenced).
    for name, entry in db.datasets.items():
        path, present = "", False
        try:
            path = resolve_existing_path(db, entry)
            present = os.path.isfile(path) or os.path.isdir(path)
        except Exception:  # noqa: BLE001 - unresolvable entry is simply absent
            present = False
        objects.append(CacheObject(
            kind="datasets",
            location=os.path.abspath(path) if present else "",
            key=name,
            name=name,
            present=present,
            format=getattr(entry, "format", "") or "",
            size=_object_size(path) if present else 0,
            created=iso_from_mtime(path) if present else "",
            last_access=last_access(path) if present else "",
            referenced=True,
            storage_path=getattr(entry, "storage_path", "") or "",
        ))
    return objects


# Object fields a free-text search term is matched against (joined, lowercased).
_SEARCH_FIELDS = (
    "kind", "name", "key", "hash", "cachetype", "version", "format",
    "storage_path", "location",
)


def _search_text(obj) -> str:
    """The lowercased, space-joined searchable text of *obj* (its key fields)."""
    parts = [str(getattr(obj, f, "") or "") for f in _SEARCH_FIELDS]
    return " ".join(p for p in parts if p).lower()


def _matches_search(obj, terms, *, any_=False) -> bool:
    """Whether *obj* matches the free-text *terms* (case-insensitive substrings of
    its searchable text). All terms must match unless *any_* (then any one does."""
    text = _search_text(obj)
    hits = (t.lower() in text for t in terms)
    return any(hits) if any_ else all(hits)


def _filter_objects(objects, args):
    """Apply the ``list`` *filter* flags to *objects* — narrowing only, never a
    change of output style (the renderer is chosen separately).

    Filters: free-text ``search`` terms (substring of the object's key fields —
    all terms must match, or any with ``--any``; ``--invert`` selects the
    non-matching objects instead); ``--hash`` (one or more hash
    prefixes, OR-matched, any version); ``--cached`` / ``--datasets`` (kind;
    default both) / ``--format`` / ``--older-than`` (object attributes);
    ``--present`` / ``--missing``
    (fetched-dataset presence); ``--orphan`` (only unreferenced produced
    artifacts). By default a produced artifact this project's ``cached.toml``
    does not root is hidden — surfaced by ``--all`` (with datasets), ``--orphan``
    (orphans only), or any explicit ``search`` / ``--hash`` selector (which
    reveals matches regardless of root status).
    """
    out = objects
    terms = getattr(args, "search", None)
    hashes = getattr(args, "hash", None)
    invert = getattr(args, "invert", False)
    # A *positive* explicit selector (search terms / --hash) means the user is
    # hunting for specific objects: reveal matches regardless of root status
    # (skip the default orphan-hiding below). An inverted search is an exclusion,
    # not a hunt, so it keeps the normal orphan-hiding.
    explicit_selector = (bool(terms) and not invert) or bool(hashes)
    if terms:
        any_ = getattr(args, "any", False)
        out = [o for o in out
               if _matches_search(o, terms, any_=any_) != invert]
    show_cached = getattr(args, "cached", False)
    show_datasets = getattr(args, "datasets", False)
    # Neither flag (or both) ⇒ both kinds; one flag narrows to that kind.
    if show_cached and not show_datasets:
        out = [o for o in out if o.kind == "cached"]
    elif show_datasets and not show_cached:
        out = [o for o in out if o.kind == "datasets"]
    if args.format:
        out = [o for o in out if o.format == args.format]
    if hashes:
        prefs = [h.lower() for h in hashes]
        # Hash identifies the params, independent of version; multiple prefixes
        # select all of them (OR) — paste several hashes at once.
        out = [o for o in out
               if o.hash and any(o.hash.lower().startswith(p) for p in prefs)]
    if getattr(args, "present", False):
        out = [o for o in out if o.kind == "datasets" and o.present]
    if getattr(args, "missing", False):
        out = [o for o in out if o.kind == "datasets" and not o.present]
    if args.orphan:
        out = [o for o in out if o.referenced is False]
    elif not getattr(args, "all", False) and not explicit_selector:
        # Hide produced artifacts not rooted by this project's cached.toml —
        # unless an explicit selector (search / --hash) asked for them.
        out = [o for o in out if not (o.kind == "cached" and o.referenced is False)]
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


def _fmt_params(params: dict) -> str:
    """Compact one-line rendering of a produced variation's key table."""
    if not params:
        return "()"
    return ", ".join(f"{k}={params[k]}" for k in sorted(params))


def _render_bare(objects):
    """Plain newline-separated names of the (already filtered) *objects* — the
    scriptable form selected by ``--bare`` / ``--names``. Cached artifacts are
    deduplicated to one line per recipe (cachetype)."""
    seen = set()
    for o in objects:
        label = o.cachetype if o.kind == "cached" else o.name
        if label in seen:
            continue
        seen.add(label)
        print(label)


def _render_rich(objects):
    """The default styled ``list`` view of the (already filtered) *objects*.

    Fetched datasets are one styled line each; produced artifacts are **grouped
    by recipe** (``cachetype`` [+ ``version``]) with each parameter variation
    listed under it (its short hash — a clickable OSC-8 ``file://`` link — plus
    the params it was produced with and its size). Colour carries status: green
    present datasets, red missing, cyan referenced artifacts, yellow orphans.
    The layout is independent of the filters that produced *objects*."""
    datasets = [o for o in objects if o.kind == "datasets"]
    cached = [o for o in objects if o.kind == "cached"]

    on = _color_enabled()
    width = shutil.get_terminal_size((80, 20)).columns

    if not objects:
        print(_paint("Nothing to list.", "dim", on=on))
        return

    if datasets:
        print(_paint("Datasets", "bold", on=on))
        name_w = min(max((len(d.name) for d in datasets), default=4), 36)
        tail_w = max(12, width - name_w - 25)
        for d in datasets:
            uri = f"file://{d.location}" if d.location else ""
            styles = ("bold", "green") if d.present else ("dim",)
            g = _paint("●" if d.present else "○", *styles, on=on)
            n = _paint(_osc8(uri, _fit(d.name, name_w).ljust(name_w), on=on),
                       *styles, on=on)
            f = _paint(_fit(d.format or "—", 8).ljust(8), "dim", on=on)
            if d.present:
                s = _paint(_fmt_size(d.size).rjust(9), "dim", on=on)
                t = _paint(_osc8(uri, _fit(d.location, tail_w, keep_tail=True),
                                 on=on), "dim", on=on)
            else:
                s = _paint("—".rjust(9), "dim", on=on)
                t = _paint("missing", "red", on=on)
            # Flag datasets that deviate from the global $datasets_dir/$key
            # default (a custom / user-managed storage_path).
            m = (_paint(" ⚑custom", "yellow", on=on)
                 if getattr(d, "storage_path", "") else "")
            print(f"{g} {n}  {f}  {s}  {t}{m}")

    if cached:
        if datasets:
            print()
        print(_paint("Cached", "bold", on=on))
        groups = {}
        for c in cached:
            groups.setdefault((c.cachetype, c.version), []).append(c)
        params_w = max(8, width - 35)
        for key in sorted(groups):
            cachetype, version = key
            insts = sorted(groups[key], key=lambda o: o.hash)
            fmt = insts[0].format
            any_ref = any(o.referenced for o in insts)
            head_styles = ("bold", "cyan") if any_ref else ("bold", "yellow")
            label = cachetype + (f" @{version}" if version else "")
            head = _paint("◆ " + label, *head_styles, on=on)
            meta = _paint(
                f"  {fmt or '—'}  {len(insts)}×  {_fmt_size(sum(o.size for o in insts))}",
                "dim", on=on,
            )
            print(head + meta)
            for o in insts:
                colour = "cyan" if o.referenced else "yellow"
                uri = f"file://{o.location}" if o.location else ""
                h = _paint(_osc8(uri, o.hash[:12] or "?", on=on), colour, on=on)
                p = _paint(_fit(_fmt_params(o.params), params_w).ljust(params_w),
                           "dim", on=on)
                s = _paint(_fmt_size(o.size).rjust(9), "dim", on=on)
                flag = _paint(" orphan", "yellow", on=on) if not o.referenced else ""
                print(f"    {h}  {p}  {s}{flag}")


def _cmd_list(args):
    db = _get_db()

    # Filters narrow the object set; the output style is chosen separately, so a
    # filter flag never changes how the list is rendered.
    objects = _filter_objects(_enumerate_objects(db), args)

    # ----- actions (operate on the filtered set, report their own output) -----
    if args.delete or args.move:
        _maintain(objects, args, db)
        return

    if getattr(args, "push", None) or getattr(args, "pull", None):
        from . import sync

        host = args.push or args.pull
        direction = "push" if args.push else "pull"
        sync_objects = []
        for obj in objects:
            if not obj.present:  # nothing on disk to transfer
                continue
            try:
                sync_objects.append(sync.sync_object_from_location(
                    db, kind=obj.kind, ident=obj.key, location=obj.location,
                ))
            except sync.RemoteRepoError:
                print(f"Skipped (local, out of scope for sync): {obj.key}",
                      file=sys.stderr)
                continue
        _do_transfer(db, sync_objects, host, direction, args)
        return

    # ----- output style (explicit; independent of the filters) -----
    if args.fields:
        # Machine-readable tab-separated columns (explicit field selection).
        for obj in objects:
            print("\t".join(_get_field(obj, f) for f in args.fields))
    elif getattr(args, "bare", False):
        _render_bare(objects)
    else:
        _render_rich(objects)


def _maintain(objects, args, db):
    """Run ``--delete`` / ``--move`` over the selected *objects*.

    Both default to a **dry run** (report only); ``--yes`` performs the action.
    On a real run the project's ``cached.toml`` is kept consistent: ``--move``
    repoints the artifact's recorded location, ``--delete`` prunes it.
    Only produced (``kind="cached"``) artifacts are eligible — fetched datasets,
    ``$data``/``$repo`` and ``local_path`` data are reported as skipped and never
    touched.
    """
    from .cache import CACHED_INDEX_NAME, CachedIndex, delete_object, move_object

    do_it = args.yes
    if args.move:
        verb = "Moved" if do_it else "Would move"
    else:
        verb = "Deleted" if do_it else "Would delete"

    project_root = db.get_project_root()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    index_path = os.path.join(base or ".", CACHED_INDEX_NAME)
    index = CachedIndex.read_or_empty(index_path) if do_it else None
    index_dirty = False

    def _record(parent):
        """Portable form of an artifact dir for cached.toml: relative to the
        manifest dir when under it, else absolute (mirrors the produce path)."""
        ap = os.path.abspath(parent)
        rt = os.path.abspath(project_root) if project_root else ""
        if rt and (ap == rt or ap.startswith(rt + os.sep)):
            return os.path.relpath(ap, rt)
        return ap

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
                new_loc = move_object(obj, args.move)
                # Keep cached.toml pointing at the moved artifact's new home.
                index_dirty |= index.set_instance_path(
                    cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                    storage_path=_record(new_loc),
                )
        else:
            print(f"{verb}: {obj.key}  {obj.location}")
            if do_it:
                delete_object(obj)
                # Prune the deleted variation from the index.
                index_dirty |= index.remove_instance(
                    cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                )
        acted += 1

    if index_dirty:
        index.write(index_path)

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
    """Freeze a spec-v3 manifest's storage locations into the spec-v4 two-field
    model (datasets_dir / datacache_dir + per-dataset storage_path). Moves no
    bytes; see :mod:`datamanifest.migrate`."""
    from .migrate import migrate_manifest

    toml_path = os.path.abspath(args.file)
    if not os.path.isfile(toml_path):
        print(f"Error: {toml_path} not found.", file=sys.stderr)
        sys.exit(1)

    print(migrate_manifest(toml_path, dry_run=args.dry_run))


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
    # Not required: a bare `datamanifest` prints the help (the command list +
    # the -h/--help hint) rather than erroring with a bare usage line.
    subparsers.required = False

    # list — dataset listing + the spec-v3 store-maintenance surface.
    p_list = subparsers.add_parser(
        "list",
        help="List datasets, or inspect/maintain stored objects",
        description=(
            "With no maintenance flags, list fetched datasets and the cached "
            "artifacts this project's cached.toml roots (--all also shows "
            "orphans and other projects'; --present/--missing print plain "
            "dataset names). Any maintenance flag switches to the object view: "
            "produced (cached) artifacts and fetched datasets with their fields, "
            "plus the explicit --delete / --move actions (dry run unless --yes)."
        ),
    )
    p_list.add_argument(
        "search", nargs="*", metavar="TERM",
        help="Free-text search term(s) matched (case-insensitive substring) "
             "against each object's key fields (name/key/cachetype/version/"
             "format/storage_path/location/hash). All terms must match unless "
             "--any is given.",
    )
    filter_group = p_list.add_argument_group("filters")
    filter_group.add_argument(
        "--any", action="store_true",
        help="Match objects where ANY search term matches (default: all terms)",
    )
    filter_group.add_argument(
        "--invert", action="store_true",
        help="Invert the search-term match (select objects that do NOT match)",
    )
    filter_group.add_argument(
        "--cached", action="store_true",
        help="Only produced (cached) artifacts (default: both kinds)",
    )
    filter_group.add_argument(
        "--datasets", action="store_true",
        help="Only fetched datasets (default: both kinds)",
    )
    filter_group.add_argument(
        "--format", metavar="FMT", help="Only objects in this serialization format"
    )
    filter_group.add_argument(
        "--hash", nargs="+", metavar="PREFIX",
        help="Only produced artifacts whose hash starts with one of these "
             "PREFIX(es) — paste several hashes to select them all; matched "
             "across any version",
    )
    filter_group.add_argument(
        "--orphan", action="store_true",
        help="Only unreferenced produced artifacts (no cached.toml root)",
    )
    filter_group.add_argument(
        "--older-than", dest="older_than", metavar="AGE",
        help="Only objects last accessed more than AGE ago (e.g. 7d, 36h, 3600)",
    )
    _excl = filter_group.add_mutually_exclusive_group()
    _excl.add_argument(
        "--present", action="store_true", help="Show only present datasets"
    )
    _excl.add_argument(
        "--missing", action="store_true", help="Show only missing datasets"
    )
    _excl.add_argument(
        "--all", action="store_true",
        help="Also list cached artifacts this project's cached.toml does not "
             "root (orphans and other projects')",
    )
    style_group = p_list.add_argument_group("output style")
    style_group.add_argument(
        "--bare", "--names", dest="bare", action="store_true",
        help="Print a plain newline-separated list of names (scriptable); "
             "default is the styled, grouped view",
    )
    style_group.add_argument(
        "--fields", nargs="+", metavar="FIELD",
        help=(
            "Fields to print, space-separated (default: "
            + " ".join(_DEFAULT_OBJECT_FIELDS)
            + "). Available: " + " ".join(_OBJECT_FIELDS) + "."
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
        help="Freeze a spec-v3 manifest's storage into the spec-v4 two-field "
             "model (datasets_dir/datacache_dir + storage_path); moves no bytes",
    )
    p_migrate.add_argument("file", metavar="FILE", help="Path to datasets.toml to migrate")
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing the manifest",
    )
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
    if getattr(args, "func", None) is None:
        # No subcommand: show the command list and the -h/--help hint.
        parser.print_help()
        return
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
