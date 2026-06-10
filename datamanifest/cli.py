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
import socket
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


# ----- shared action option sets -------------------------------------------
#
# Each first-order action (delete / move / push / pull) defines its options in
# exactly ONE place, via the helpers below. The standalone `delete ID` /
# `move ID DEST` / `push ID SSH_HOST` / `pull ID SSH_HOST` subcommands add the
# leading positional(s) and then the shared option set; `list --<action> TAIL`
# reuses the same option set on an id-less parser to parse the forwarded
# REMAINDER tail (the `list` selection replaces the id).


def _add_delete_opts(parser, *, with_batch=True):
    """Add `delete`'s options (``--dry-run`` / ``--prune``, and ``--batch`` for
    the id-addressed standalone form). ``--batch`` is irrelevant in the piped
    ``list --delete`` form (the selection is already explicit) — it is omitted
    there, but still accepted (see :func:`_idless_action_parser`)."""
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without deleting anything")
    if with_batch:
        parser.add_argument("--batch", action="store_true",
                            help="Delete all objects matching an ambiguous id")
    parser.add_argument(
        "--prune", action="store_true",
        help="Also drop the dataset's manifest entry (not just the bytes); no "
             "effect on cached artifacts, which have no entry",
    )


def _add_move_opts(parser, *, with_batch=True):
    """Add `move`'s options (``--dry-run``, plus ``--batch`` for the standalone
    id form). The ``DEST`` positional is added by the caller (it leads the
    forwarded tail in the ``list --move DEST`` form)."""
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without moving anything")
    if with_batch:
        parser.add_argument("--batch", action="store_true",
                            help="Move all objects matching an ambiguous id")


def _add_sync_opts(parser, *, with_batch=True):
    """Add `push`/`pull`'s options (``--dry-run``, plus ``--batch`` for the
    standalone id form). The ``SSH_HOST`` positional is added by the caller."""
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report the selection (id, kind, paths, size) and transfer nothing",
    )
    if with_batch:
        parser.add_argument(
            "--batch", action="store_true",
            help="Transfer all objects matching an ambiguous id instead of erroring",
        )


# The id-less option set each `list` action flag forwards its REMAINDER tail to.
# ``--batch`` is accepted-and-ignored in the piped form: the `list` selection is
# already the explicit set, so the single-vs-ambiguous guard does not apply.
_LIST_ACTION_SPECS = {
    "delete": {"adder": _add_delete_opts, "positionals": ()},
    "move": {"adder": _add_move_opts, "positionals": (("dest", "DEST"),)},
    "push": {"adder": _add_sync_opts, "positionals": (("host", "SSH_HOST"),)},
    "pull": {"adder": _add_sync_opts, "positionals": (("host", "SSH_HOST"),)},
}


def _idless_action_parser(action):
    """Build the id-less argparse parser for *action* used to parse the tail
    forwarded by ``list --<action> TAIL``. It has the action's leading
    positional(s) (``DEST`` / ``SSH_HOST``) but no ``id``, the action's options,
    and an accepted-and-ignored ``--batch`` (no-op here)."""
    spec = _LIST_ACTION_SPECS[action]
    p = argparse.ArgumentParser(prog=f"datamanifest list --{action}",
                                add_help=False)
    for dest, metavar in spec["positionals"]:
        p.add_argument(dest, metavar=metavar)
    spec["adder"](p, with_batch=False)
    # Accept (and ignore) --batch in the piped form for parity with the
    # standalone command; the explicit selection makes it a no-op.
    p.add_argument("--batch", action="store_true", help=argparse.SUPPRESS)
    return p


# ----- subcommand implementations -----

# Fields a maintenance object exposes, in display order. ``key``/``hash`` and
# the timestamps double as the spec-v3 ``datamanifest list`` object schema.
_OBJECT_FIELDS = (
    "kind", "name", "key", "hash", "cachetype", "version", "storage-path",
    "format", "params", "size", "location", "referenced", "present", "dirty",
    "created", "last-access",
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


def _heavy_fields(args):
    """The filesystem-heavy per-object fields (``size`` tree-walk / ``created``
    metadata read) the chosen output actually needs — so enumeration can skip
    them for the scriptable ``--bare`` path and ``--fields`` that don't ask.

    Returns a set ⊆ {"size", "created"}. The rich (default) view shows ``size``;
    ``--fields`` shows exactly what's listed; ``--bare`` shows neither.
    """
    heavy = {"size", "created"}
    if (getattr(args, "delete", None) is not None
            or getattr(args, "move", None) is not None):
        return set()                       # actions report key/location, not size
    if getattr(args, "fields", None):
        return {f.replace("-", "_") for f in args.fields} & heavy
    if getattr(args, "bare", False):
        return set()
    return {"size"}


def _abs_under(sp, project_root):
    """Resolve a (possibly repo-relative) recorded ``storage_path`` to an absolute
    path — relative records anchor to *project_root* (else cwd)."""
    if not sp:
        return ""
    if os.path.isabs(sp):
        return os.path.abspath(sp)
    return os.path.abspath(os.path.join(project_root or os.getcwd(), sp))


def _enumerate_objects(db, heavy=frozenset({"size"})):
    """The composition root: enumerate produced artifacts (cache layer) and
    fetched datasets (fetch layer) as a single list of objects, each tagged with
    its reachability (``referenced``) and its state↔disk status (``dirty``).

    *heavy* selects which filesystem-heavy fields to actually compute (see
    :func:`_heavy_fields`); the rest are left at their cheap defaults.

    Reachability and dirty status are resolved here — the one place that bridges
    both layers — from the project's sibling state file
    (``.datamanifest-state.toml``): a produced artifact is ``referenced`` iff its
    ``(cachetype, version, hash)`` is rooted there; a fetched dataset is always
    referenced by its manifest entry. ``dirty`` compares each object's recorded
    location against where its bytes actually are: ``""`` clean, ``relocated``
    (recorded location stale), ``missing`` (recorded but gone), ``untracked``
    (present but unrecorded). Missing recorded objects (no bytes) are included so
    ``--dirty`` / ``--refresh`` can act on them.
    """
    from . import storage
    from .cache import CachedIndex, enumerate_artifacts
    from .cache._inspect import CacheObject, cache_object_at
    from .cache._usage import iso_from_mtime, last_access
    from .database import get_dataset_path

    objects = []
    with_size = "size" in heavy
    with_created = "created" in heavy
    project_root = db.get_project_root()

    cache_root = storage.datacache_dir(
        project_root=project_root, storage_config=db.storage_config,
    )
    index = None
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    state_path = CachedIndex.locate(base or ".")
    if os.path.isfile(state_path):
        try:
            index = CachedIndex.read(state_path)
        except Exception:  # noqa: BLE001 - an unreadable state file roots nothing
            index = None
    referenced = index.reachable_keys() if index else set()

    # --- produced artifacts ---------------------------------------------------
    # Present artifacts under datacache_dir, keyed by identity.
    found = {}
    for obj in enumerate_artifacts(cache_root, with_size=with_size,
                                   with_created=with_created):
        ident = (obj.cachetype, obj.version, obj.hash)
        obj.referenced = ident in referenced
        found[ident] = obj

    recorded_keys = set()
    if index is not None:
        for rec in index.recipe_records():
            ct, ver = rec["cachetype"], rec["version"]
            for h, sp in rec["instances"].items():
                ident = (ct, ver, h)
                recorded_keys.add(ident)
                rec_abs = _abs_under(sp, project_root)
                if rec_abs and os.path.isdir(rec_abs):
                    if ident not in found:
                        # Recorded at a location outside datacache_dir (e.g. moved
                        # there) — surface it from its recorded home (clean).
                        out = cache_object_at(rec_abs, with_size=with_size,
                                              with_created=with_created)
                        if out is not None:
                            out.referenced = True
                            found[ident] = out
                    # else: present at its recorded location → clean.
                elif ident in found:
                    # Recorded path stale, but a copy lives under datacache_dir.
                    found[ident].dirty = "relocated"
                else:
                    # Recorded but the bytes are gone — a missing (dirty) root.
                    found[ident] = CacheObject(
                        kind="cached", location=rec_abs, key=f"{ct}/{h}", name=ct,
                        hash=h, cachetype=ct, version=ver, present=False,
                        referenced=True, dirty="missing",
                    )

    # A present cached artifact the state file doesn't root is an **orphan**
    # (referenced=False) — its own concept, surfaced by --orphan; it is not
    # tagged "untracked" (untracked is a dataset-only adoption state).
    for obj in found.values():
        objects.append(obj)

    # --- fetched datasets -----------------------------------------------------
    for name, entry in db.datasets.items():
        recorded = index.dataset_path_of(entry.key) if index else ""
        recorded_abs = _abs_under(recorded, project_root)
        try:
            derived_abs = os.path.abspath(get_dataset_path(
                entry, db.datasets_folder,
                project_root=project_root, storage_config=db.storage_config,
            ))
        except Exception:  # noqa: BLE001 - unresolvable entry is simply absent
            derived_abs = ""
        location, present, dirty = _dataset_state(recorded_abs, derived_abs)
        if entry.skip_download:
            dirty = ""                      # user-managed external file: not tracked
        objects.append(CacheObject(
            kind="datasets",
            location=location,
            key=name,
            name=name,
            present=present,
            format=getattr(entry, "format", "") or "",
            size=_object_size(location) if (present and with_size) else 0,
            created=iso_from_mtime(location) if (present and with_created) else "",
            last_access=last_access(location) if present else "",
            referenced=True,
            storage_path=getattr(entry, "storage_path", "") or "",
            dirty=dirty,
        ))
    return objects


def _dataset_state(recorded_abs, derived_abs):
    """``(location, present, dirty)`` for a fetched dataset, comparing its recorded
    location against where the bytes actually are (read-first: recorded wins)."""
    rec_present = bool(recorded_abs) and os.path.exists(recorded_abs)
    der_present = bool(derived_abs) and os.path.exists(derived_abs)
    if recorded_abs:
        if rec_present:
            return recorded_abs, True, ""              # clean
        if der_present:
            return derived_abs, True, "relocated"      # recorded stale
        return "", False, "missing"                    # recorded but gone
    if der_present:
        return derived_abs, True, "untracked"          # present but unrecorded
    return "", False, ""                               # simply not fetched


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


def _filter_objects(objects, args, db=None):
    """Apply the ``list`` *filter* flags to *objects* — narrowing only, never a
    change of output style (the renderer is chosen separately).

    Filters: free-text ``search`` terms (substring of the object's key fields —
    all terms must match, or any with ``--any``; ``--invert`` selects the
    non-matching objects instead); ``--hash`` (one or more hash
    prefixes, OR-matched, any version); ``--cached`` / ``--datasets`` (kind;
    default both) / ``--format`` / ``--older-than`` (object attributes);
    ``--present`` / ``--missing``
    (fetched-dataset presence); ``--orphan`` (only unreferenced produced
    artifacts). By default a produced artifact this project's state file
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
    if getattr(args, "dirty", False):
        out = [o for o in out if getattr(o, "dirty", "")]
    outside = getattr(args, "outside", False)
    if outside and db is not None:
        ds_roots, dc_roots = _conform_roots(db)
        out = [o for o in out if _is_outside(o, ds_roots, dc_roots)]
    if args.orphan:
        out = [o for o in out if o.referenced is False]
    elif (not getattr(args, "all", False) and not explicit_selector
          and not getattr(args, "dirty", False) and not outside):
        # Hide produced artifacts not rooted by this project's state file —
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


# Dirty (state↔disk) status → its rendered marker (label, colour).
_DIRTY_MARK = {
    "missing": ("✗ missing", "red"),
    "relocated": ("✗ relocated", "yellow"),
    "untracked": ("✗ untracked", "yellow"),
}


def _dirty_suffix(obj, on) -> str:
    """A trailing styled marker for a dirty object (state↔disk mismatch), or ""."""
    info = _DIRTY_MARK.get(getattr(obj, "dirty", ""))
    if not info:
        return ""
    label, colour = info
    return " " + _paint(label, colour, on=on)


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
            print(f"{g} {n}  {f}  {s}  {t}{m}{_dirty_suffix(d, on)}")

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
                print(f"    {h}  {p}  {s}{flag}{_dirty_suffix(o, on)}")

    # Hint: anything marked ✗ (untracked / relocated / missing) is reconcilable.
    dirties = [o.dirty for o in objects if getattr(o, "dirty", "")]
    if dirties:
        n = len(dirties)
        msg = (f"\n{n} object{'s' if n != 1 else ''} marked ✗ — run "
               "`datamanifest refresh` to reconcile (record untracked, repoint "
               "moved, drop missing).")
        if "missing" in dirties:
            msg += (" For missing ones, `datamanifest refresh --scan` first looks "
                    "in the read pools and only drops what isn't found there.")
        print(_paint(msg, "dim", on=on))


def _cmd_list(args):
    db = _get_db()

    # Filters narrow the object set; the output style is chosen separately, so a
    # filter flag never changes how the list is rendered. Skip the filesystem-
    # heavy fields the chosen output won't show (notably the size walk under
    # --bare — a big speedup for large datasets).
    objects = _filter_objects(_enumerate_objects(db, _heavy_fields(args)), args, db)

    # ----- actions (operate on the filtered set, report their own output) -----
    # Each action flag captures its tail as an argparse REMAINDER list (``None``
    # = flag not given, ``[]`` = given with no tail). The tail is parsed by the
    # matching action's id-less parser and applied to the `list` selection, so
    # each action's options are defined in exactly one place.
    for action in ("delete", "move", "push", "pull"):
        tail = getattr(args, action, None)
        if tail is None:
            continue
        sub = _idless_action_parser(action).parse_args(tail)
        if action in ("delete", "move"):
            margs = argparse.Namespace(
                delete=(action == "delete"),
                move=(sub.dest if action == "move" else None),
                dry_run=sub.dry_run,
                prune=getattr(sub, "prune", False),
            )
            _maintain(objects, margs, db)
        else:
            _list_sync(objects, sub.host, action, sub.dry_run, db)
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


def _record_portable(path, project_root):
    """Portable form of *path* for the state file: relative to the manifest dir
    when under it, else absolute (mirrors the produce / download record path)."""
    ap = os.path.abspath(path)
    rt = os.path.abspath(project_root) if project_root else ""
    if rt and (ap == rt or ap.startswith(rt + os.sep)):
        return os.path.relpath(ap, rt)
    return ap


def _dataset_protected(db, obj):
    """Whether a fetched dataset object is protected from delete/move — a
    user-managed exact ``storage_path`` (no ``$key``), a ``skip_download`` entry
    (the URI *is* the file), or a ``lazy_access`` entry (opened in place, no local
    copy). Returns the entry too (or ``None``)."""
    from . import storage

    entry = db.datasets.get(obj.key)
    if entry is None:
        return True, None
    protected = (bool(entry.skip_download) or bool(entry.lazy_access)
                 or storage.is_user_managed(entry.storage_path))
    return protected, entry


def _cmd_refresh(args):
    """Reconcile the state file (`.datamanifest-state.toml`) with disk.

    First-order maintenance over the whole inventory: relocate stale recorded
    locations to where the bytes actually are, drop records whose bytes are gone,
    and adopt present-but-untracked datasets. Edits only the git-ignored,
    regenerable state file — no downloads, no file moves, no bytes touched — so it
    **applies by default**; ``--dry-run`` previews. (The live code self-heals the
    same way on access; this is the bulk, no-refetch way to do it at once.)
    """
    db = _get_db()
    ds_pools = _override_pools(db, getattr(args, "datasets_pools", None))
    dc_pools = _override_pools(db, getattr(args, "datacache_pools", None))
    objects = _enumerate_objects(db, heavy=frozenset())
    # Reconcile non-destructively: a "missing" dataset that turns up in a read
    # pool is *recovered* (re-pointed), not dropped. (Pools default to the
    # configured / well-known ones; --datasets-pools overrides for this run.)
    _refresh(objects, db, dry_run=args.dry_run, pools=ds_pools)
    if getattr(args, "scan", False):
        # --scan additionally IMPORTS manifest datasets that live only in a pool
        # (never recorded/local) and cached artifacts from datacache pools.
        _refresh_scan_pools(db, dry_run=args.dry_run,
                            datasets_pools=ds_pools, datacache_pools=dc_pools)


def _refresh_scan_pools(db, *, dry_run, datasets_pools=None, datacache_pools=None):
    """``refresh --scan``: probe the read pools for objects that exist there but
    aren't local yet, and **adopt** them — record the pooled location in the state
    file (no downloads or copies). Datasets are checksum-gated. *datasets_pools* /
    *datacache_pools* override the configured pools for this run. Returns the set
    of dataset names adopted (so the caller can avoid dropping them as "missing").
    The active counterpart to ``where --scan``."""
    from . import storage as storage_mod
    from .cache import (CACHED_INDEX_NAME, CachedIndex, find_produced_artifacts,
                        read_config)
    from .cache._inspect import _guess_format
    from .database import resolve_existing_path, resolve_from_pools

    project_root = db.get_project_root()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    index_path = os.path.join(base or ".", CACHED_INDEX_NAME)
    index = CachedIndex.read_or_empty(index_path)
    touched = False
    recovered = set()

    # --- datasets ---
    for name, entry in db.datasets.items():
        if entry.skip_download or entry.lazy_access or not entry.key:
            continue
        try:
            resolved = resolve_existing_path(db, entry)
            if os.path.isfile(resolved) or os.path.isdir(resolved):
                continue                      # already local / recorded
        except Exception:  # noqa: BLE001
            pass
        pooled = resolve_from_pools(db, entry, pools=datasets_pools)
        if not pooled:
            continue
        verb = "Would adopt" if dry_run else "Adopted"
        print(f"{verb} (pool): {name} → {pooled}")
        if not dry_run:
            sha = "" if (db.skip_checksum or entry.skip_checksum) else (entry.sha256 or "")
            index.register_dataset(
                key=entry.key,
                storage_path=_record_portable(pooled, project_root), sha256=sha,
            )
        recovered.add(name)
        touched = True
    verb = "would adopt" if dry_run else "adopted"
    print(f"Read pools (datasets): {verb} {len(recovered)}")

    # --- produced artifacts (only when a datacache pool is in effect) ---
    dc_pools = (datacache_pools if datacache_pools is not None
                else storage_mod.datacache_pools(
                    project_root=project_root, storage_config=db.storage_config))
    if dc_pools:
        cached_adopted = 0
        for pool in dc_pools:
            for artifact_dir, _key in find_produced_artifacts(pool):
                try:
                    meta = read_config(artifact_dir).get("_META", {})
                except Exception:  # noqa: BLE001
                    continue
                ct, h = meta.get("cachetype", ""), meta.get("hash", "")
                ver = meta.get("version", "")
                if not (ct and h) or CachedIndex._VERSION_SEP in ct:
                    continue
                if index.has_instance(cachetype=ct, version=ver, hash=h):
                    continue
                verb = "Would adopt" if dry_run else "Adopted"
                print(f"{verb} (pool cached): {ct}{('@' + ver) if ver else ''}/{h[:8]}"
                      f" → {artifact_dir}")
                if not dry_run:
                    index.register(cachetype=ct, hash=h, version=ver,
                                   storage_path=_record_portable(artifact_dir, project_root),
                                   format=_guess_format(artifact_dir))
                cached_adopted += 1
                touched = True
        verb = "would adopt" if dry_run else "adopted"
        print(f"Read pools (cached): {verb} {cached_adopted}")

    if not dry_run and touched:
        index.write(index_path)
    return recovered


def _match_cached_by_id(ident, objects):
    """Produced-artifact objects addressed by *ident* — ``cachetype[/version]/hash``
    or an (unambiguous) hash prefix — mirroring ``push``/``pull`` addressing."""
    parts = [p for p in ident.split("/") if p]
    id_hash = parts[-1] if parts else ""
    id_head = parts[:-1]
    out = []
    for o in objects:
        if o.kind != "cached":
            continue
        if id_head:
            if len(id_head) == 1 and id_head[0] != o.cachetype:
                continue
            if len(id_head) == 2 and not (id_head[0] == o.cachetype
                                          and id_head[1] == o.version):
                continue
            if len(id_head) > 2:
                continue
        if o.hash.startswith(id_hash):
            out.append(o)
    return out


def _match_objects_by_id(db, ident, objects):
    """The *objects* (from :func:`_enumerate_objects`) addressed by *ident* — a
    fetched dataset by name/alias/doi, or a produced artifact by
    ``cachetype[/version]/hash`` / hash prefix. Same addressing as ``push``/``pull``
    (but it does **not** refuse repo-local objects, which delete/move may act on).
    """
    from .database import search_datasets

    ds_names = {name for name, _ in search_datasets(db, ident)}
    matched = [o for o in objects if o.kind == "datasets" and o.key in ds_names]
    matched += _match_cached_by_id(ident, objects)
    return matched


def _run_id_action(args, db, *, move):
    """Engine for the first-order ``delete`` / ``move`` commands: resolve ``args.id``
    to object(s), enforce the single-vs-``--batch`` rule (an ambiguous id errors
    unless ``--batch``), then apply via :func:`_maintain`. ``--dry-run`` previews."""
    objects = _enumerate_objects(db, heavy=frozenset())
    matched = _match_objects_by_id(db, args.id, objects)
    if not matched:
        print(f"Error: no stored object found for id {args.id!r}.", file=sys.stderr)
        sys.exit(1)
    if len(matched) > 1 and not args.batch:
        listing = "\n- ".join(f"{o.kind}  {o.key}" for o in matched)
        print(
            f"id {args.id!r} is ambiguous; it matches {len(matched)} objects:\n- "
            f"{listing}\nGive a more specific id, or pass --batch to act on all.",
            file=sys.stderr,
        )
        sys.exit(1)
    margs = argparse.Namespace(
        delete=not move, move=(args.dest if move else None), dry_run=args.dry_run,
        prune=getattr(args, "prune", False),
    )
    _maintain(matched, margs, db)


def _cmd_delete(args):
    """Delete a stored object's bytes (and prune its state-file record), addressed
    by id like ``push``/``pull``. By default this removes the *materialized data*,
    not the manifest entry; ``--prune`` also drops the dataset's entry (= ``remove``;
    no effect on cached artifacts). Protected data (user-managed / skip_download /
    lazy_access) is skipped. ``--dry-run`` previews."""
    _run_id_action(args, _get_db(), move=False)


def _cmd_move(args):
    """Move a stored object's bytes under DEST and repoint its state-file record
    (the manifest is not edited), addressed by id like ``push``/``pull``.
    ``--dry-run`` previews."""
    _run_id_action(args, _get_db(), move=True)


def _maintain(objects, args, db):
    """Run ``--delete`` / ``--move`` over the selected *objects* — produced
    artifacts **and** fetched datasets.

    The ``list`` filter *is* the explicit selection, so the action **applies by
    default**; ``--dry-run`` previews without changing anything (there is no
    ``--yes`` and no batch guard here). On a real run the project's state file is
    kept consistent: ``--move`` repoints the recorded location, ``--delete`` prunes
    the entry. Protected objects are never touched: a fetched dataset with a
    user-managed exact ``storage_path`` or ``skip_download`` (the URI is the file),
    and any non-cached/non-dataset object. The manifest is never edited — only the
    resolved location moves, so a later re-fetch still follows the ``datasets_dir``
    directive (the gold standard).
    """
    import shutil

    from .cache import CACHED_INDEX_NAME, CachedIndex, delete_object, move_object

    do_it = not getattr(args, "dry_run", False)
    if args.move:
        verb = "Moved" if do_it else "Would move"
    else:
        verb = "Deleted" if do_it else "Would delete"

    project_root = db.get_project_root()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    index_path = os.path.join(base or ".", CACHED_INDEX_NAME)
    index = CachedIndex.read_or_empty(index_path) if do_it else None
    index_dirty = False
    manifest_dirty = False
    acted = 0

    for obj in objects:
        if obj.kind == "cached":
            if not obj.present:
                print(f"Skipped (missing, use --refresh): {obj.key}")
                continue
            if args.move:
                dest = (os.path.join(args.move, obj.cachetype, obj.hash)
                        if not obj.version
                        else os.path.join(args.move, obj.cachetype, obj.version, obj.hash))
                print(f"{verb}: {obj.key}  {obj.location} -> {dest}")
                if do_it:
                    new_loc = move_object(obj, args.move)
                    index_dirty |= index.set_instance_path(
                        cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                        storage_path=_record_portable(new_loc, project_root),
                    )
            else:
                print(f"{verb}: {obj.key}  {obj.location}")
                if do_it:
                    delete_object(obj)
                    index_dirty |= index.remove_instance(
                        cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                    )
            acted += 1
        elif obj.kind == "datasets":
            protected, entry = _dataset_protected(db, obj)
            if protected:
                print(f"Skipped (dataset, user-managed/skip_download, protected): "
                      f"{obj.key}")
                continue
            if not obj.present:
                print(f"Skipped (dataset not present): {obj.key}")
                continue
            if args.move:
                dest = os.path.join(args.move, entry.key)
                print(f"{verb}: {obj.key}  {obj.location} -> {dest}")
                if do_it:
                    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                    shutil.move(obj.location, dest)
                    index.register_dataset(
                        key=entry.key,
                        storage_path=_record_portable(dest, project_root),
                    )
                    index_dirty = True
            else:
                prune = getattr(args, "prune", False)
                tag = " (+ entry pruned)" if prune else ""
                print(f"{verb}{tag}: {obj.key}  {obj.location}")
                if do_it:
                    _remove_path_and_markers(obj.location)
                    index_dirty |= index.remove_dataset(entry.key)
                    if prune:
                        db.datasets.pop(obj.key, None)
                        manifest_dirty = True
            acted += 1
        else:
            print(f"Skipped ({obj.kind}, protected): {obj.location}")

    if index_dirty:
        index.write(index_path)
    if manifest_dirty and db.datasets_toml:
        db.write(db.datasets_toml)

    noun = "object" if acted == 1 else "objects"
    if do_it:
        print(f"{verb}: {acted} {noun}")
    else:
        print(f"{verb}: {acted} {noun} (dry run; re-run without --dry-run to apply)")


def _remove_path_and_markers(path):
    """Remove a fetched dataset's bytes (file or directory) and its sibling
    completion / lock markers (best-effort)."""
    import shutil

    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    for suffix in (".complete", ".lock", ".tmp"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _refresh(objects, db, *, dry_run=False, pools=None):
    """Reconcile the state file with disk (no downloads, no file moves) over the
    given *objects*, **non-destructively**: repoint a *relocated* entry to where
    the bytes actually are; for a *missing* dataset, **recover** it if its bytes
    turn up in a read pool (re-point) and only **drop** it when truly gone; and
    **adopt** an *untracked* dataset. A cached orphan is left as an orphan; clean
    objects are untouched. *pools* overrides the read pools consulted for recovery
    (``None`` = the configured / built-in pools). Edits only the git-ignored state
    file, so it **applies by default**; *dry_run* previews without writing."""
    from .cache import CACHED_INDEX_NAME, CachedIndex
    from .database import resolve_from_pools

    do_it = not dry_run
    project_root = db.get_project_root()
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    index_path = os.path.join(base or ".", CACHED_INDEX_NAME)
    index = CachedIndex.read_or_empty(index_path)
    changed = 0

    for obj in objects:
        dirty = getattr(obj, "dirty", "")
        if dirty == "relocated":
            verb = "Refreshed" if do_it else "Would refresh"
            print(f"{verb} (relocated): {obj.key} -> {obj.location}")
            if do_it:
                if obj.kind == "cached":
                    index.set_instance_path(
                        cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                        storage_path=_record_portable(obj.location, project_root),
                    )
                else:
                    entry = db.datasets.get(obj.key)
                    if entry is not None:
                        index.register_dataset(
                            key=entry.key,
                            storage_path=_record_portable(obj.location, project_root),
                        )
            changed += 1
        elif dirty == "missing":
            # A missing dataset whose bytes turn up in a read pool is recovered,
            # not dropped (non-destructive). Cached artifacts have no such
            # checksummed pool recovery here — they are dropped (and re-imported
            # by `--scan` from a datacache pool if one is configured).
            entry = db.datasets.get(obj.key) if obj.kind == "datasets" else None
            pooled = (resolve_from_pools(db, entry, pools=pools)
                      if entry is not None else "")
            if pooled:
                verb = "Recovered" if do_it else "Would recover"
                print(f"{verb} (pool): {obj.key} -> {pooled}")
                if do_it:
                    sha = "" if (db.skip_checksum or entry.skip_checksum) else (entry.sha256 or "")
                    index.register_dataset(
                        key=entry.key,
                        storage_path=_record_portable(pooled, project_root), sha256=sha,
                    )
            else:
                verb = "Dropped" if do_it else "Would drop"
                print(f"{verb} (missing): {obj.key}")
                if do_it:
                    if obj.kind == "cached":
                        index.remove_instance(
                            cachetype=obj.cachetype, version=obj.version, hash=obj.hash,
                        )
                    elif entry is not None:
                        index.remove_dataset(entry.key)
            changed += 1
        elif dirty == "untracked":
            entry = db.datasets.get(obj.key) if obj.kind == "datasets" else None
            if entry is not None:
                # Adopt a present-but-unrecorded dataset: record its location.
                # No re-hash here (refresh touches no bytes); the actual sha256 is
                # recorded on the next download / verify.
                verb = "Adopted" if do_it else "Would adopt"
                print(f"{verb} (untracked): {obj.key} -> {obj.location}")
                if do_it:
                    index.register_dataset(
                        key=entry.key,
                        storage_path=_record_portable(obj.location, project_root),
                    )
                changed += 1
            else:
                # A cached orphan — left as an orphan (not adopted as a root).
                print(f"Left orphan (untracked artifact; not adopted): {obj.key}")

    if do_it and changed:
        index.write(index_path)

    noun = "entry" if changed == 1 else "entries"
    if do_it:
        print(f"State file: reconciled {changed} {noun}")
    else:
        print(f"State file: {changed} {noun} to reconcile "
              "(dry run; run without --dry-run to apply)")


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


def _list_sync(objects, host, direction, dry_run, db):
    """Push/pull the `list`-selected *objects* to/from *host* — the engine behind
    ``list --push`` / ``list --pull``. Resolves each present object to a
    :class:`SyncObject` (skipping repo-local, out-of-scope ones) and transfers
    the set via :func:`_do_transfer`."""
    from . import sync

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
    _do_transfer(db, sync_objects, host, direction,
                 argparse.Namespace(dry_run=dry_run))


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

    # A Zenodo / PANGAEA DOI (or record URL) expands to the dataset's files
    # (declare-only — records can be large; `download` fetches). A plain URL is a
    # single dataset (the normal path below). A PANGAEA ref pinning an explicit
    # ?format= is left as a plain URL (pangaea_dataset_id returns "").
    from .importers import (
        import_pangaea, import_zenodo, pangaea_dataset_id, zenodo_record_id,
    )
    if zenodo_record_id(args.uri):
        print(import_zenodo(db, args.uri, name=args.name or "",
                            picks=getattr(args, "pick", None) or None,
                            split=getattr(args, "split", False),
                            overwrite=args.overwrite))
        return
    if pangaea_dataset_id(args.uri):
        print(import_pangaea(db, args.uri, name=args.name or "",
                             picks=getattr(args, "pick", None) or None,
                             split=getattr(args, "split", False),
                             overwrite=args.overwrite))
        return

    kwargs = {}
    if args.name:
        kwargs["name"] = args.name
    if args.extract:
        kwargs["extract"] = True

    # Lazy access: never download; open the remote URI in place. `lazy_access` is
    # the language-neutral marker; Python pairs it with the built-in fsspec loader
    # (a Python-only `_LANG.python.loader`, so a peer tool honors lazy its own way).
    if getattr(args, "lazy", False):
        from .store.loaders import FSSPEC_LOADER_REF
        kwargs["lazy_access"] = True
        kwargs["lang_python_loader"] = FSSPEC_LOADER_REF
        name, _ = db.register_dataset(args.uri, overwrite=args.overwrite, **kwargs)
        print(f"Registered {name!r} for lazy access (lazy_access; opened in place "
              "via the built-in fsspec loader, not downloaded).")
        return

    name, entry = db.register_dataset(args.uri, overwrite=args.overwrite, **kwargs)

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
    toml_path = os.path.join(folder, "datamanifest.toml")

    if os.path.isfile(toml_path) and not args.force:
        print(
            f"Error: {toml_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    os.makedirs(folder, exist_ok=True)
    with open(toml_path, "w") as f:
        f.write(
            "# datamanifest.toml — dataset manifest.\n"
            "# Add datasets with `datamanifest add <uri>`; data lives under\n"
            "# ./datasets/ and ./cached/ by default (see `datamanifest storage`).\n\n"
            "[_META]\nschema = 1\n"
        )
    print(f"Created: {toml_path}")


def _add_pool_override_flags(parser):
    """Add ``--datasets-pools`` / ``--datacache-pools`` to a scan/discovery
    command, to **explicitly override** the read pools for that one run
    (space-separated dirs; pass the flag with no values for an empty/disabled
    list). Omitting a flag uses the configured / built-in pools."""
    parser.add_argument(
        "--datasets-pools", dest="datasets_pools", nargs="*", metavar="DIR",
        default=None,
        help="Override the datasets read pools for this run (space-separated dirs; "
             "none = disabled); default is the configured / built-in pools",
    )
    parser.add_argument(
        "--datacache-pools", dest="datacache_pools", nargs="*", metavar="DIR",
        default=None,
        help="Override the @cached read pools for this run",
    )


def _override_pools(db, exprs):
    """Resolve a ``--*-pools`` override *exprs* to absolute dirs, or ``None`` when
    the flag was not given (so the caller uses the configured / built-in pools)."""
    if exprs is None:
        return None
    from . import storage as storage_mod

    return storage_mod.resolve_pool_exprs(
        exprs, project_root=db.get_project_root(), storage_config=db.storage_config,
    )


def _under(loc, root):
    """Whether absolute path *loc* is *root* or sits under it."""
    return bool(root) and (loc == root or loc.startswith(root.rstrip(os.sep) + os.sep))


def _conform_roots(db):
    """``(datasets_roots, cached_roots)`` — the configured locations an object may
    sit in without being "outside": datasets_dir + the datasets read pools, and
    datacache_dir + the cached read pools (all absolute)."""
    from . import storage as storage_mod
    root = db.get_project_root()
    cfg = db.storage_config

    def _roots(dir_fn, pools_fn):
        try:
            base = [os.path.abspath(dir_fn(project_root=root, storage_config=cfg))]
        except Exception:  # noqa: BLE001 - an unresolved dir simply isn't a root
            base = []
        try:
            base += list(pools_fn(project_root=root, storage_config=cfg))
        except Exception:  # noqa: BLE001
            pass
        return [r for r in base if r]

    return (_roots(storage_mod.datasets_dir, storage_mod.datasets_pools),
            _roots(storage_mod.datacache_dir, storage_mod.datacache_pools))


def _is_outside(obj, ds_roots, dc_roots):
    """Whether *obj* is materialized outside its configured roots — tracked and
    present, but not under datasets_dir / datacache_dir or a read pool (e.g. data
    fetched into an ad-hoc folder, or moved out of the standard layout)."""
    loc = getattr(obj, "location", "")
    if not loc or not getattr(obj, "present", False):
        return False
    roots = dc_roots if obj.kind == "cached" else ds_roots
    return not any(_under(loc, r) for r in roots)


def _cmd_where(args):
    """Show where this project keeps things: the active manifest and state file,
    the data directories **resolved for this host** (datasets_dir / datacache_dir,
    honoring env vars and `_HOST` overrides), the read pools, and a compact grouped
    summary of what the state file records (with the full inventory left to
    `datamanifest list`).

    With one of ``--manifest`` / ``--state-file`` / ``--datasets-dir`` /
    ``--datacache-dir``, print only that single bare path (scriptable, no label)."""
    from . import storage as storage_mod
    from .cache import STATE_FILE_NAME, CachedIndex

    db = _get_db()
    root = db.get_project_root()
    cfg = db.storage_config
    base = os.path.dirname(db.datasets_toml) if db.datasets_toml else os.getcwd()
    state_path = CachedIndex.locate(base)
    if not os.path.isfile(state_path):
        state_path = os.path.join(base, STATE_FILE_NAME)

    def _resolve(field):
        try:
            return getattr(storage_mod, field)(project_root=root, storage_config=cfg)
        except Exception as e:  # noqa: BLE001 - surface an unresolved symbol inline
            return f"<unresolved: {e}>"

    # Scriptable single-path selectors: print only the bare value, no label.
    selectors = (
        ("manifest", db.datasets_toml),
        ("state_file", state_path),
        ("datasets_dir", None),       # resolved lazily below
        ("datacache_dir", None),
    )
    for attr, value in selectors:
        if getattr(args, attr, False):
            print(value if value is not None else _resolve(attr))
            return

    _ds_override = _override_pools(db, getattr(args, "datasets_pools", None))
    _dc_override = _override_pools(db, getattr(args, "datacache_pools", None))
    ds_pools = (_ds_override if _ds_override is not None
                else storage_mod.datasets_pools(project_root=root, storage_config=cfg))
    dc_pools = (_dc_override if _dc_override is not None
                else storage_mod.datacache_pools(project_root=root, storage_config=cfg))
    on = _color_enabled()
    ds_dir = _resolve("datasets_dir")
    dc_dir = _resolve("datacache_dir")
    width = len("datacache_dir")

    def _row(label, value, pools=()):
        """Print ``label : value``, then any read *pools* (other than *value*
        itself) as unlabeled continuation lines aligned under the value — the
        folders also searched, read-only, before downloading / recomputing."""
        print(f"{label:<{width}} : {value}")
        seen = {os.path.abspath(value)}
        for p in pools:
            if os.path.abspath(p) not in seen:
                print(f"{'':<{width}}   {p}")
                seen.add(os.path.abspath(p))

    _row("manifest", db.datasets_toml or "(none — in-memory database)")
    _row("state file", state_path
         + ("" if os.path.isfile(state_path) else "  (not created yet)"))
    _row("datasets_dir", ds_dir, ds_pools)
    _row("datacache_dir", dc_dir, dc_pools)

    # A glance at tracked data living outside the configured folders — just a
    # count and a pointer; `datamanifest list --outside` enumerates them.
    ds_roots, dc_roots = _conform_roots(db)
    outside = [o for o in _enumerate_objects(db) if _is_outside(o, ds_roots, dc_roots)]
    n_ds = sum(1 for o in outside if o.kind == "datasets")
    n_dc = sum(1 for o in outside if o.kind == "cached")
    parts = ([f"{n_ds} dataset{'s' if n_ds != 1 else ''}"] if n_ds else []) \
        + ([f"{n_dc} cached artifact{'s' if n_dc != 1 else ''}"] if n_dc else [])
    if parts:
        print(_paint(
            f"\n{' and '.join(parts)} stored outside datasets_dir / the read "
            "pools — `datamanifest list --outside` to inspect.", "yellow", on=on))

    # --scan: probe the read pools for datasets present there but not at the
    # resolved location (candidates to adopt by `download` / `migrate`).
    if getattr(args, "scan", False):
        from .database import resolve_existing_path

        found = []
        for name, entry in db.datasets.items():
            if entry.skip_download or entry.lazy_access or not entry.key:
                continue
            try:
                resolved = resolve_existing_path(db, entry)
                here = os.path.isfile(resolved) or os.path.isdir(resolved)
            except Exception:  # noqa: BLE001
                here = False
            if here:
                continue
            for pool in ds_pools:
                cand = os.path.join(pool, entry.key)
                if os.path.isfile(cand) or os.path.isdir(cand):
                    found.append((name, cand))
                    break
        print("\nscan — datasets available in a read pool (not yet local):")
        if found:
            for name, cand in sorted(found):
                print(f"  {name} → {cand}")
            print("  (run `datamanifest download` to adopt, or `migrate` to record)")
        else:
            print("  (none found)")


# ----- storage config editing ([_STORAGE]) -----------------------------------

# Storage fields that hold a list (not a scalar path expression).
_STORAGE_LIST_FIELDS = ("datasets_pools", "datacache_pools")


def _valid_storage_field(field: str) -> bool:
    """Whether *field* is a settable ``[_STORAGE]`` key — a folder field
    (``datasets_dir`` / ``datacache_dir``) or a user ``$symbol`` name (a plain
    identifier). Reserved ``_``-prefixed keys (``_HOST`` / ``_META`` …) are not."""
    return bool(field) and not field.startswith("_") and field.replace("_", "a").isalnum()


def _storage_db(action: str):
    """The active database for a ``storage`` edit, or exit with a clear error when
    there is no manifest to edit."""
    db = _get_db()
    if not db.datasets_toml:
        print(f"Error: no manifest found to {action} (run inside a project with a "
              "datamanifest.toml).", file=sys.stderr)
        sys.exit(1)
    return db


def _cmd_storage_set(args):
    """Set a ``[_STORAGE]`` field. By default it applies to **this host** (written
    under ``[_STORAGE._HOST."<hostname>"]``); ``--host GLOB`` targets a host glob,
    ``--all-hosts`` the project-wide base. Edits the manifest (the committed spec)."""
    if not _valid_storage_field(args.field):
        print(f"Error: invalid field name {args.field!r} (use datasets_dir / "
              "datacache_dir, a $symbol, or a *_pools list).", file=sys.stderr)
        sys.exit(1)
    # A list field (datasets_pools / datacache_pools) takes any number of values
    # (zero = an explicit empty list, i.e. disabled); a scalar takes exactly one.
    if args.field in _STORAGE_LIST_FIELDS:
        value = list(args.value)
    elif len(args.value) == 1:
        value = args.value[0]
    else:
        print(f"Error: {args.field} takes exactly one VALUE (got {len(args.value)}).",
              file=sys.stderr)
        sys.exit(1)

    db = _storage_db("edit")
    storage = db.extra.setdefault("_STORAGE", {})
    if args.all_hosts:
        storage[args.field] = value
        where = "all hosts"
    else:
        host = args.host or socket.gethostname()
        storage.setdefault("_HOST", {}).setdefault(host, {})[args.field] = value
        where = f'host "{host}"' + ("" if args.host else " (this machine)")
    db.write(db.datasets_toml)
    print(f"Set {args.field} = {value!r} for {where}.")


def _cmd_storage_unset(args):
    """Remove a ``[_STORAGE]`` field (same targeting as ``set``: this host by
    default, ``--host GLOB`` or ``--all-hosts``). Prunes now-empty host tables."""
    db = _storage_db("edit")
    storage = db.extra.get("_STORAGE", {})
    removed = False
    if args.all_hosts:
        removed = storage.pop(args.field, None) is not None
        where = "all hosts"
    else:
        host = args.host or socket.gethostname()
        host_tbl = storage.get("_HOST", {})
        if host in host_tbl and args.field in host_tbl[host]:
            del host_tbl[host][args.field]
            removed = True
            if not host_tbl[host]:
                del host_tbl[host]
            if not host_tbl:
                storage.pop("_HOST", None)
        where = f'host "{host}"'
    if removed:
        db.write(db.datasets_toml)
        print(f"Unset {args.field} for {where}.")
    else:
        print(f"Nothing to unset: {args.field} not set for {where}.")


def _cmd_storage_show(args):
    """Show the storage config resolved for this host, plus the raw rules."""
    from . import storage as storage_mod

    db = _get_db()
    cfg = db.extra.get("_STORAGE", {})
    root = db.get_project_root()
    host = socket.gethostname()
    on = _color_enabled()

    print(_paint(f"Host: {host}", "bold", on=on))
    print(_paint("Resolved for this host:", "bold", on=on))
    for field in ("datasets_dir", "datacache_dir"):
        try:
            resolved = getattr(storage_mod, field)(
                project_root=root, storage_config=cfg,
            )
        except Exception as e:  # noqa: BLE001 - report an unresolved symbol inline
            resolved = f"<unresolved: {e}>"
        print(f"  {field:<14} -> {resolved}")
    for field in ("datasets_pools", "datacache_pools"):
        pools = getattr(storage_mod, field)(project_root=root, storage_config=cfg)
        if pools:
            print(f"  {field:<14} -> {', '.join(pools)}")

    print(_paint("[_STORAGE] rules:", "bold", on=on))
    base = {k: v for k, v in cfg.items() if k != "_HOST"}
    if not base and not cfg.get("_HOST"):
        print(_paint("  (none — repo-local defaults: ./datasets, ./cached)", "dim", on=on))
    for k, v in base.items():
        print(f"  {k} = {v!r}")
    for pattern, mapping in cfg.get("_HOST", {}).items():
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                marker = "  ←matches" if _host_matches(host, pattern) else ""
                print(f'  [_HOST "{pattern}"] {k} = {v!r}{_paint(marker, "green", on=on)}')


def _host_matches(host: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(host, pattern)


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
    """Migrate an older manifest to the current format: upgrade inline language
    bindings + the [_STORAGE] storage model, and discover existing data on disk
    (recording its location in the state file). Moves no bytes; see
    :mod:`datamanifest.migrate`."""
    from .migrate import migrate_manifest

    toml_path = os.path.abspath(args.file)
    if not os.path.isfile(toml_path):
        print(f"Error: {toml_path} not found.", file=sys.stderr)
        sys.exit(1)

    print(migrate_manifest(
        toml_path, dry_run=args.dry_run, no_input=args.no_input,
        datasets_pools=args.datasets_pools, datacache_pools=args.datacache_pools,
    ))


def _cmd_import(args):
    """Import datasets declared by another tool (currently pooch) into the active
    manifest: parse the tool's registry into standard dataset entries, and — with
    --cache-dir — adopt already-downloaded files in place (no re-download). See
    :mod:`datamanifest.importers`."""
    from .importers import IMPORTERS

    importer = IMPORTERS.get(args.tool)
    if importer is None:                       # defensive (argparse constrains it)
        print(f"Error: unknown import source {args.tool!r} "
              f"(supported: {', '.join(sorted(IMPORTERS))}).", file=sys.stderr)
        sys.exit(1)
    src = os.path.abspath(args.source)
    if not os.path.isfile(src):
        print(f"Error: {src} not found.", file=sys.stderr)
        sys.exit(1)
    db = _get_db()
    if not db.datasets_toml and not args.dry_run:
        print("Error: no manifest to import into (run inside a project with a "
              "datamanifest.toml, or `datamanifest init` first).", file=sys.stderr)
        sys.exit(1)
    try:
        print(importer(db, src, base_url=args.base_url, cache_dir=args.cache_dir,
                       dry_run=args.dry_run, overwrite=args.overwrite))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


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

    # list — dataset listing + the stored-object maintenance surface.
    p_list = subparsers.add_parser(
        "list",
        help="List datasets, or inspect/maintain stored objects",
        description=(
            "With no maintenance flags, list fetched datasets and the cached "
            "artifacts this project's state file roots, each with its state↔disk "
            "status (--all also shows orphans and other projects'; --dirty only "
            "mismatched objects; --present/--missing print plain dataset names). "
            "Any maintenance flag switches to the object view: produced artifacts "
            "and fetched datasets with their fields, plus the --delete / --move "
            "actions (which apply directly; --dry-run previews)."
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
        help="Only unreferenced produced artifacts (no state-file root)",
    )
    filter_group.add_argument(
        "--dirty", action="store_true",
        help="Only objects whose state-file record disagrees with disk "
             "(missing / relocated / untracked)",
    )
    filter_group.add_argument(
        "--outside", action="store_true",
        help="Only tracked objects stored outside datasets_dir / datacache_dir "
             "and the read pools (data fetched into or moved to an ad-hoc folder)",
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
        help="Also list cached artifacts this project's state file does not "
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
    # Object actions: each flag captures everything after it as a raw REMAINDER
    # tail and forwards it to that action's OWN option parser, applied to the
    # `list` selection (so the options are defined once — see _LIST_ACTION_SPECS).
    # Selection filters come BEFORE the action flag; the action flag owns the
    # tail. The four flags are mutually exclusive.
    act_group = p_list.add_argument_group(
        "object actions",
        description=(
            "Apply a standalone command to the selected objects, forwarding the "
            "rest of the line to that command. Filters come first, then the "
            "action flag and its own options. e.g. "
            "`list --orphan --delete --dry-run --prune`, "
            "`list --datasets --move /archive --dry-run`, "
            "`list --outside --push user@hpc`."
        ),
    )
    _act_excl = act_group.add_mutually_exclusive_group()
    _act_excl.add_argument(
        "--delete", nargs=argparse.REMAINDER, default=None, metavar="...",
        help="Delete the selected objects, forwarding delete's options "
             "(--dry-run / --prune) — artifacts and fetched datasets; protected "
             "data is skipped",
    )
    _act_excl.add_argument(
        "--move", nargs=argparse.REMAINDER, default=None, metavar="DEST ...",
        help="Move the selected objects under DEST, forwarding move's options "
             "(DEST then --dry-run); the manifest is not edited",
    )
    _act_excl.add_argument(
        "--push", nargs=argparse.REMAINDER, default=None, metavar="SSH_HOST ...",
        help="Push the selected objects to SSH_HOST (rsync over ssh), forwarding "
             "push's options (SSH_HOST then --dry-run)",
    )
    _act_excl.add_argument(
        "--pull", nargs=argparse.REMAINDER, default=None, metavar="SSH_HOST ...",
        help="Pull the selected objects from SSH_HOST (rsync over ssh), "
             "forwarding pull's options (SSH_HOST then --dry-run)",
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
    p_add.add_argument(
        "uri", metavar="URI",
        help="Dataset URI, or a Zenodo / PANGAEA DOI / record URL (expands to its "
             "files; a PANGAEA series expands to one entry per child dataset)",
    )
    add_opts = p_add.add_argument_group("options")
    add_opts.add_argument(
        "--name", "-n", metavar="N",
        help="Name for the dataset entry (a name *prefix* for a Zenodo record / a "
             "split PANGAEA collection)",
    )
    add_opts.add_argument(
        "--pick", metavar="GLOB", action="append",
        help="For a Zenodo record / PANGAEA file collection: add only files "
             "matching GLOB (repeatable)",
    )
    add_opts.add_argument(
        "--split", action="store_true",
        help="For a Zenodo record / PANGAEA file collection: add one dataset per "
             "file instead of bundling into a single uris= dataset (the default)",
    )
    add_opts.add_argument(
        "--no-download", action="store_true", help="Register without downloading"
    )
    add_opts.add_argument(
        "--lazy", dest="lazy", action="store_true",
        help="Don't download; open the remote URI (s3://, gs://, …) in place via "
             "the built-in fsspec loader (sets lazy_access + a Python-only loader)",
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
        "verify", help="Re-check checksums (declared algorithm); exits nonzero on mismatch"
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
        help="Recompute stored checksums (declared algorithm) from the files on disk",
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
        "init", help="Create a fresh datamanifest.toml in the current directory"
    )
    init_opts = p_init.add_argument_group("options")
    init_opts.add_argument(
        "--folder", metavar="PATH",
        help="Directory to create datamanifest.toml in (default: cwd)",
    )
    init_opts.add_argument(
        "--force", action="store_true", help="Overwrite an existing datamanifest.toml"
    )
    p_init.set_defaults(func=_cmd_init)

    # where
    p_where = subparsers.add_parser(
        "where", help="Show the active manifest, state file, and resolved data "
                      "directories for this host",
        description=(
            "Show the active manifest, state file, the datasets_dir / "
            "datacache_dir and read pools resolved for this host, and a compact "
            "grouped summary of what the state file records (full list via "
            "`datamanifest list`). With a single selector flag, print only that "
            "bare path (scriptable)."
        ),
    )
    _where_excl = p_where.add_mutually_exclusive_group()
    _where_excl.add_argument("--manifest", action="store_true",
                             help="Print only the manifest path")
    _where_excl.add_argument("--state-file", dest="state_file", action="store_true",
                             help="Print only the state-file path")
    _where_excl.add_argument("--datasets-dir", dest="datasets_dir",
                             action="store_true",
                             help="Print only the resolved datasets_dir")
    _where_excl.add_argument("--datacache-dir", dest="datacache_dir",
                             action="store_true",
                             help="Print only the resolved datacache_dir")
    p_where.add_argument(
        "--scan", action="store_true",
        help="Also probe the read pools for datasets present there but not local "
             "(candidates to adopt via download / migrate)",
    )
    _add_pool_override_flags(p_where)
    p_where.set_defaults(func=_cmd_where)

    # migrate
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Migrate an older manifest to the current format (upgrades language "
             "bindings and the [_STORAGE] storage model); moves no bytes",
    )
    p_migrate.add_argument(
        "file", metavar="FILE",
        help="Path to the manifest to migrate (datamanifest.toml / datasets.toml)",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing anything",
    )
    p_migrate.add_argument(
        "--no-input", action="store_true",
        help="Never prompt: auto-pick on ambiguous discovery (prefer the "
             "repo-local copy) and don't propose host config changes",
    )
    _add_pool_override_flags(p_migrate)
    p_migrate.set_defaults(func=_cmd_migrate)

    # import (from another tool)
    p_import = subparsers.add_parser(
        "import",
        help="Bulk-import datasets from another tool's catalog "
             "(pooch / csv / urls / intake / dvc)",
    )
    p_import.add_argument(
        "tool", choices=sorted(["pooch", "csv", "urls", "intake", "dvc"]),
        help="Source: pooch registry, a name,url,sha256 CSV, a plain URL list, an "
             "intake catalog.yml, or DVC .dvc/dvc.lock files",
    )
    p_import.add_argument(
        "source", metavar="SOURCE",
        help="The catalog file/dir (pooch registry.txt / .csv / URL list / "
             "intake catalog.yml / a .dvc file or DVC project dir)",
    )
    p_import.add_argument(
        "--base-url", dest="base_url", default="", metavar="URL",
        help="Root URL prepended to registry filenames lacking an explicit URL "
             "column (pooch's base_url; required unless every entry carries a URL)",
    )
    p_import.add_argument(
        "--cache-dir", dest="cache_dir", default="", metavar="DIR",
        help="The tool's local cache directory (e.g. pooch.os_cache('pkg')); "
             "already-downloaded files are adopted in place, checksum-verified, "
             "with no re-download",
    )
    p_import.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite manifest entries whose name already exists",
    )
    p_import.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be imported without writing anything",
    )
    p_import.set_defaults(func=_cmd_import)

    # refresh
    p_refresh = subparsers.add_parser(
        "refresh",
        help="Reconcile the state file (.datamanifest-state.toml) with disk: "
             "relocate stale records, drop missing ones, adopt present-but-"
             "untracked datasets. Applies by default (edits only local state)",
        description=(
            "Reconcile the git-ignored state file with what's on disk: repoint "
            "records whose bytes moved, drop records whose bytes are gone, and "
            "adopt present-but-untracked datasets. No downloads, no file moves, "
            "no bytes touched — so it applies by default; use --dry-run to "
            "preview. The live code self-heals the same way on access; this does "
            "it in bulk without re-fetching. Use `list --dirty` to see what would "
            "change first."
        ),
    )
    p_refresh.add_argument(
        "--dry-run", action="store_true",
        help="Preview the reconciliation without writing the state file",
    )
    p_refresh.add_argument(
        "--scan", action="store_true",
        help="Also probe the read pools (incl. the well-known legacy locations) "
             "and adopt datasets present there but not local yet (checksum-gated; "
             "no downloads or copies) — the active twin of `where --scan`",
    )
    _add_pool_override_flags(p_refresh)
    p_refresh.set_defaults(func=_cmd_refresh)

    # storage (edit [_STORAGE] without hand-writing the _HOST syntax)
    p_storage = subparsers.add_parser(
        "storage",
        help="Show or edit the manifest's [_STORAGE] config (folders + per-host "
             "overrides) without hand-editing the _HOST syntax",
        description=(
            "Show or edit [_STORAGE] in datamanifest.toml. `set`/`unset` target "
            "THIS host by default (written under [_STORAGE._HOST.\"<hostname>\"]); "
            "--host GLOB targets a host pattern, --all-hosts the project-wide "
            "base. `show` (the default) prints the config resolved for this host "
            "plus the raw rules."
        ),
    )
    storage_sub = p_storage.add_subparsers(dest="storage_cmd", metavar="{show,set,unset}")

    p_st_show = storage_sub.add_parser(
        "show", help="Show storage config resolved for this host + the raw rules")
    p_st_show.set_defaults(func=_cmd_storage_show)

    def _add_target_flags(p):
        excl = p.add_mutually_exclusive_group()
        excl.add_argument(
            "--host", metavar="GLOB",
            help="Apply on hosts matching GLOB (fnmatch); default: this host only",
        )
        excl.add_argument(
            "--all-hosts", action="store_true",
            help="Apply as the project-wide base value (all hosts)",
        )

    p_st_set = storage_sub.add_parser(
        "set", help="Set a folder field, a $symbol, or a *_pools list")
    p_st_set.add_argument("field", metavar="FIELD",
                          help="datasets_dir, datacache_dir, a $symbol, or "
                               "datasets_pools / datacache_pools (a list)")
    p_st_set.add_argument("value", metavar="VALUE", nargs="*",
                          help="Path expression(s) (may use $user_data_dir, $repo, "
                               "$USER, …); a *_pools field accepts several (or "
                               "none, for an explicit empty list)")
    _add_target_flags(p_st_set)
    p_st_set.set_defaults(func=_cmd_storage_set)

    p_st_unset = storage_sub.add_parser(
        "unset", help="Remove a storage field (this host by default)")
    p_st_unset.add_argument("field", metavar="FIELD")
    _add_target_flags(p_st_unset)
    p_st_unset.set_defaults(func=_cmd_storage_unset)

    p_storage.set_defaults(func=_cmd_storage_show)   # bare `storage` → show

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
        _add_sync_opts(sync_opts)
        p_sync.set_defaults(func=func)

    # delete / move — first-order maintenance of a single stored object, by id
    # (the same addressing as push/pull). Mirrors `list --delete` / `--move` for
    # discoverability; an ambiguous id errors unless --batch; --dry-run previews.
    _ID_DESC = (
        "The object is addressed by its machine-independent id: a fetched dataset "
        "by name/alias/doi, or a produced artifact by cachetype[/version]/hash "
        "(full or an unambiguous hash prefix). An ambiguous id errors unless "
        "--batch."
    )
    p_delete = subparsers.add_parser(
        "delete",
        help="Delete a stored object's bytes by id (--prune also drops the entry)",
        description=(
            "Delete a stored object's materialized bytes and prune its state-file "
            "record. By default this does NOT edit the manifest (the recipe stays, "
            "so it can be re-fetched); pass --prune to also drop the dataset's "
            "manifest entry (= `remove`). Protected data (user-managed / "
            f"skip_download / lazy_access) is skipped. {_ID_DESC}"
        ),
    )
    p_delete.add_argument("id", metavar="ID", help="Object identifier")
    del_opts = p_delete.add_argument_group("options")
    _add_delete_opts(del_opts)
    p_delete.set_defaults(func=_cmd_delete)

    p_move = subparsers.add_parser(
        "move",
        help="Move a stored object's bytes under DEST by id (updates the state file)",
        description=(
            "Move a stored object's bytes under DEST and repoint its state-file "
            "record; the manifest is not edited (a later re-fetch still follows "
            f"the datasets_dir directive). {_ID_DESC}"
        ),
    )
    p_move.add_argument("id", metavar="ID", help="Object identifier")
    p_move.add_argument("dest", metavar="DEST", help="Destination root directory")
    move_opts = p_move.add_argument_group("options")
    _add_move_opts(move_opts)
    p_move.set_defaults(func=_cmd_move)

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
