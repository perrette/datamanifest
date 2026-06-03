"""DatasetEntry dataclass + URI parsing helpers.

Port of the types + path/URI helpers from DataManifest.jl's ``Databases.jl``
(lines 10-462). The ``Database`` class itself is added in a later item; this
module currently provides only ``DatasetEntry`` and the free helper functions.

Julia adaptations (see roadmap §C):
- ``julia`` (inline code) becomes ``python`` (an entry-point reference
  ``"pkg.mod:func"`` resolved via importlib; no inline code execution).
- ``julia_modules`` has no Python execution context to preimport into, so it is
  not interpreted here.
- ``callable`` is accepted on read as an alias and normalized into ``python``
  at ``init_dataset_entry`` time; it is never stored as a field nor serialized.

Per the datamanifest.toml spec, unknown per-dataset keys (another tool's or
language's extension keys, e.g. ``julia`` / ``julia_modules``) are not errors:
they are preserved verbatim in ``DatasetEntry.extra`` and re-emitted on write,
so a cross-language manifest survives a read/write round-trip intact.
"""

import os
import socket
import sys
from dataclasses import dataclass, field, fields
from urllib.parse import parse_qs, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import tomli_w

from . import storage
from .config import (
    COMPRESSED_FORMATS,
    HIDE_STRUCT_FIELDS,
    get_default_toml,
    get_extract_path,
    logger,
    project_root_from_paths,
    sha256_path,
)


# ----- Types (DatasetEntry) -----
@dataclass(eq=False)
class DatasetEntry:
    uri: str = ""
    uris: list = field(default_factory=list)
    host: str = ""
    path: str = ""
    scheme: str = ""
    version: str = ""
    branch: str = ""
    doi: str = ""
    aliases: list = field(default_factory=list)
    description: str = ""
    key: str = ""
    local_path: str = ""
    sha256: str = ""
    skip_checksum: bool = False
    skip_download: bool = False
    extract: bool = False
    format: str = ""
    shell: str = ""
    python: str = ""
    loader: str = ""
    # Bare per-dataset bindings (spec-v3.4, language-implicit): read as the
    # running tool's OWN language, equivalent to [<ds>._LANG.<self>].fetcher /
    # .loader but without the wrapper. Each accepts the bare-ref string or the
    # parameterized { ref, args, kwargs } table form (the ref lives in the
    # field, args/kwargs in the paired fields). `loader` (above) is the bare
    # loader ref; `loader_args`/`loader_kwargs` carry its table form. An explicit
    # [<ds>._LANG.python] binding overrides the bare one (see resolve_fetcher /
    # resolve_loader_binding). A bare binding that fails to resolve in Python
    # warns and falls through the ladder (tolerant); an explicit one hard-errors.
    fetcher: str = ""
    fetcher_args: list = field(default_factory=list)
    fetcher_kwargs: dict = field(default_factory=dict)
    loader_args: list = field(default_factory=list)
    loader_kwargs: dict = field(default_factory=dict)
    requires: list = field(default_factory=list)
    # v1 _LANG.python bindings (read via _LANG namespace; written back in Item 4).
    # Each binding may be a bare ref string or a parameterized
    # ``{ ref, args, kwargs }`` table; the ref lives in the *_fetcher/*_loader
    # field and any args (ordered list) / kwargs (dict) in the paired fields.
    lang_python_fetcher: str = ""
    lang_python_loader: str = ""
    lang_python_fetcher_args: list = field(default_factory=list)
    lang_python_fetcher_kwargs: dict = field(default_factory=dict)
    lang_python_loader_args: list = field(default_factory=list)
    lang_python_loader_kwargs: dict = field(default_factory=dict)
    # Store selection: "$data" (default), "$cache", "$repo", or any "$folder"
    # selector defined in [_STORAGE]. Empty string selects the project default
    # and is elided on write.
    store: str = ""
    # Cross-language fetch (fetch-ladder rung 3) opt-out. Delegation is on by
    # default (probe-gated: it no-ops silently unless a foreign-language fetcher
    # and a usable foreign toolchain are actually present). Set to False to opt
    # this dataset out. Only the non-default `delegate = false` is written.
    delegate: bool = True
    # Passthrough for fields this port does not model — other tools' / other
    # languages' extension keys (e.g. Julia's `julia` / `julia_modules`). Kept
    # verbatim so they round-trip instead of being dropped on write. Per the
    # datamanifest.toml spec, readers must ignore unknown fields, not error.
    extra: dict = field(default_factory=dict)

    def __eq__(self, other):
        # Mirror Julia's Base.:(==) for DatasetEntry: compare every field
        # except :sha256 and :skip_checksum (Databases.jl:35-48).
        if not isinstance(other, DatasetEntry):
            return NotImplemented
        for f in fields(self):
            if f.name in ("sha256", "skip_checksum"):
                continue
            if getattr(self, f.name) != getattr(other, f.name):
                return False
        return True

    def __str__(self):
        d = to_dict(self)
        lines = [f"{type(self).__name__}:"]
        for k, v in d.items():
            lines.append(f"- {k}={v}")
        return "\n".join(lines)

    def __repr__(self):
        d = to_dict(self)
        parts = [f"{k}={trimstring(repr(v), 30)}" for k, v in d.items()]
        return f"{type(self).__name__}({', '.join(parts)})"


# ----- repr / string helpers (Databases.jl:105-145) -----
def trimstring(s: str, n: int, path: bool = True) -> str:
    if len(s) <= n:
        return s
    if not path:
        return s[:n] + "..."
    while len(s) > n:
        parts = s.split(os.sep)
        if len(parts) <= 1:
            return s
        s = os.sep.join(parts[:-1])
    return s + "..."


def string_short(entry: DatasetEntry) -> str:
    return entry.key


# ----- key / format / dict helpers (Databases.jl:50-103) -----
def build_dataset_key(entry: DatasetEntry, path: str = "") -> str:
    clean_path = (path if path else entry.path).strip("/")
    key = os.path.join(entry.host, clean_path)
    if entry.version:
        key = key + "#" + entry.version
    return key.strip("/")


def guess_file_format(entry: DatasetEntry) -> str:
    """Infer file format from the dataset key (e.g. ``data/out.csv`` -> ``csv``,
    ``archive.tar.gz`` -> ``tar.gz``). Strips any version ``#`` fragment first.
    """
    key = entry.key.rstrip("/")
    if "#" in key:
        key = key.split("#", 1)[0]
    if not key:
        return ""
    base, ext = os.path.splitext(key)
    if ext == ".gz":
        base2, ext2 = os.path.splitext(base)
        if ext2 == ".tar":
            ext = ext2 + ext
    return ext.lstrip(".")


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {} or value is False


def _python_binding(ref: str, args, kwargs):
    """Render a Python binding for ``[<ds>._LANG.python].fetcher/loader``.

    A bare ref with no args/kwargs serializes as the plain ``ref`` string
    (back-compat). When args (ordered) or kwargs (dict) are present, it
    serializes as a ``{ ref, args, kwargs }`` table; kwargs keys are sorted on
    write so the output is canonical.
    """
    if not args and not kwargs:
        return ref
    binding: dict = {"ref": ref}
    if args:
        binding["args"] = list(args)
    if kwargs:
        binding["kwargs"] = {k: kwargs[k] for k in sorted(kwargs)}
    return binding


def _split_python_binding(binding):
    """Split a Python binding value into ``(ref, args, kwargs)``.

    The inverse of :func:`_python_binding`: accepts a bare ``ref`` string or a
    parameterized ``{ ref, args, kwargs }`` table, and returns the ref plus its
    (possibly empty) ordered ``args`` list and ``kwargs`` dict. An empty/unknown
    value yields ``("", [], {})``.
    """
    if isinstance(binding, dict):
        return (
            str(binding.get("ref", "")),
            list(binding.get("args", []) or []),
            dict(binding.get("kwargs", {}) or {}),
        )
    if binding:
        return str(binding), [], {}
    return "", [], {}


def to_dict(entry: DatasetEntry) -> dict:
    output = {}
    for f in fields(entry):
        name = f.name
        if name == "extra":
            continue
        value = getattr(entry, name)
        if name in HIDE_STRUCT_FIELDS:
            continue
        # lang_python_* are serialized inside the regenerated [<ds>._LANG.python]
        # block below (not as flat keys).
        if name in {
            "lang_python_fetcher",
            "lang_python_loader",
            "lang_python_fetcher_args",
            "lang_python_fetcher_kwargs",
            "lang_python_loader_args",
            "lang_python_loader_kwargs",
        }:
            continue
        # `delegate` defaults to True (delegation on); only the non-default
        # opt-out (`delegate = false`) is written, so the common case stays
        # absent from the manifest.
        if name == "delegate":
            if value:
                continue
            output[name] = value
            continue
        if _is_empty(value):
            continue
        if name == "key" and value == build_dataset_key(entry):
            continue
        if name == "format" and value == guess_file_format(entry):
            continue
        if name == "store" and value == "":
            continue
        output[name] = value
    # Re-emit preserved extension keys verbatim (cross-language passthrough).
    # Appended last so any table-valued extra serializes after scalar fields.
    # `_LANG` is handled specially below: its foreign subtrees are spliced in
    # alongside this tool's regenerated `python` block.
    for k, v in entry.extra.items():
        if k == "_LANG":
            continue
        output.setdefault(k, v)
    # Regenerate this tool's own [<ds>._LANG.python] block (when we own a
    # fetcher/loader for it) and splice every foreign [<ds>._LANG.<other>]
    # subtree back verbatim from `extra`, for a lossless multi-language round-trip.
    lang_table: dict = {}
    python_block: dict = {}
    if entry.lang_python_fetcher:
        python_block["fetcher"] = _python_binding(
            entry.lang_python_fetcher,
            entry.lang_python_fetcher_args,
            entry.lang_python_fetcher_kwargs,
        )
    if entry.lang_python_loader:
        python_block["loader"] = _python_binding(
            entry.lang_python_loader,
            entry.lang_python_loader_args,
            entry.lang_python_loader_kwargs,
        )
    if python_block:
        lang_table["python"] = python_block
    foreign_lang = entry.extra.get("_LANG")
    if isinstance(foreign_lang, dict):
        for k, v in foreign_lang.items():
            lang_table.setdefault(k, v)
    if lang_table:
        output["_LANG"] = lang_table
    return output


# ----- URI parsing (Databases.jl:287-366) -----
def parse_uri_metadata(uri: str) -> dict:
    if uri.startswith("git@"):
        uri = uri.replace(":", "/")
        uri = uri.replace("git@", "git://")
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    scheme = parsed.scheme or ""
    path = parsed.path.rstrip("/")
    fragment = parsed.fragment or ""
    query = parse_qs(parsed.query)
    version_q = query.get("version", [""])[0]
    ref_q = query.get("ref", [""])[0]
    format_q = query.get("format", [""])[0]
    if fragment:
        version = fragment
    elif version_q:
        version = version_q
    else:
        version = ref_q
    return {
        "uri": uri,
        "scheme": scheme,
        "host": host,
        "path": path,
        "format": format_q,
        "version": version,
    }


def get_dataset_key(entry: DatasetEntry) -> str:
    if entry.key:
        return entry.key
    return build_dataset_key(entry)


def build_uri(meta: DatasetEntry) -> str:
    uri = meta.uri if meta.uri else ""
    if uri == "":
        # Nothing to reconstruct from: a binding-only / local_path / skip_download
        # entry has no scheme/host/path, so return "" (elided on write) rather
        # than the degenerate "://".
        if not (meta.scheme or meta.host or meta.path):
            return ""
        uri = f"{meta.scheme}://{meta.host}"
        if meta.path:
            uri += "/" + meta.path.strip("/")
        if meta.version:
            uri += "#" + meta.version
    return uri


# ----- Entry construction (Databases.jl:368-462) -----
def init_dataset_entry(uri=None, uris=None, ref: str = "", downloads=None, **kwargs):
    if downloads is None:
        downloads = []

    # `callable` is a read-only alias for `python`; normalize it now and never
    # store it as a field (roadmap §C). Always serialized back as `python`.
    if "callable" in kwargs:
        callable_ref = kwargs.pop("callable")
        if callable_ref:
            if kwargs.get("python"):
                raise ValueError("Cannot provide both `callable` and `python`")
            kwargs["python"] = callable_ref

    # Bare per-dataset bindings (spec-v3.4): `fetcher` / `loader` may each be a
    # bare ref string or a parameterized { ref, args, kwargs } table. Split a
    # table form into the ref field + the paired args/kwargs fields, so the
    # dataclass fields always hold scalar ref + list args + dict kwargs.
    for binding_key, args_field, kwargs_field in (
        ("fetcher", "fetcher_args", "fetcher_kwargs"),
        ("loader", "loader_args", "loader_kwargs"),
    ):
        bv = kwargs.get(binding_key)
        if isinstance(bv, dict):
            ref_v, args_v, kwargs_v = _split_python_binding(bv)
            kwargs[binding_key] = ref_v
            if args_v and args_field not in kwargs:
                kwargs[args_field] = args_v
            if kwargs_v and kwargs_field not in kwargs:
                kwargs[kwargs_field] = kwargs_v

    # Normalize: uri can be a list (same as uris).
    if isinstance(uri, (list, tuple)):
        if uris:
            raise ValueError("Cannot provide both `uri` as a list and `uris`")
        uris = [str(u) for u in uri]
        uri = ""
    if uri is None:
        uri = ""
    if uris is None:
        uris = []
    else:
        uris = [str(u) for u in uris]

    # Parse _LANG namespace (v1 schema): extract Python bindings into named fields;
    # keep every foreign _LANG.<other> subtree in extra for verbatim round-trip.
    lang_data = kwargs.pop("_LANG", None)
    lang_foreign = {}
    if isinstance(lang_data, dict):
        python_lang = lang_data.get("python", {})
        if isinstance(python_lang, dict):
            # Each binding is either a bare ref string or a parameterized
            # ``{ ref, args, kwargs }`` table; split it into the ref/args/kwargs
            # fields (kwargs not provided by the caller take precedence).
            for binding_key, ref_field, args_field, kwargs_field in (
                (
                    "fetcher",
                    "lang_python_fetcher",
                    "lang_python_fetcher_args",
                    "lang_python_fetcher_kwargs",
                ),
                (
                    "loader",
                    "lang_python_loader",
                    "lang_python_loader_args",
                    "lang_python_loader_kwargs",
                ),
            ):
                binding = python_lang.get(binding_key, "")
                if isinstance(binding, dict):
                    ref_val = binding.get("ref", "")
                    args_val = binding.get("args", [])
                    kwargs_val = binding.get("kwargs", {})
                    if ref_val and ref_field not in kwargs:
                        kwargs[ref_field] = str(ref_val)
                    if args_val and args_field not in kwargs:
                        kwargs[args_field] = list(args_val)
                    if kwargs_val and kwargs_field not in kwargs:
                        kwargs[kwargs_field] = dict(kwargs_val)
                elif binding and ref_field not in kwargs:
                    kwargs[ref_field] = str(binding)
        lang_foreign = {k: v for k, v in lang_data.items() if k != "python"}

    # Fields this port does not model — another tool's / language's extension
    # keys (e.g. Julia's `julia` / `julia_modules`) — are preserved verbatim in
    # `extra` rather than dropped, so they survive a read/write round-trip and a
    # cross-language manifest is not silently mangled. (`extra` itself is not a
    # public schema key, hence excluded from `known`.)
    known = {f.name for f in fields(DatasetEntry)} - {"extra"}
    extra = {k: kwargs.pop(k) for k in list(kwargs) if k not in known}
    if lang_foreign:
        extra["_LANG"] = lang_foreign

    entry = DatasetEntry(uri=uri, uris=list(uris), extra=extra, **kwargs)

    # Multiple-URI entry: key derived from common host + path prefix if not given.
    if entry.uris:
        if entry.key == "":
            parsed_list = [parse_uri_metadata(u) for u in entry.uris]
            host = parsed_list[0]["host"]
            dir_segs = []
            for p in parsed_list:
                segs = [s for s in p["path"].split("/") if s]
                dir_segs.append(segs[:-1])
            n_common = 0
            if dir_segs and dir_segs[0]:
                for i in range(len(dir_segs[0])):
                    if all(len(s) > i and s[i] == dir_segs[0][i] for s in dir_segs):
                        n_common = i + 1
                    else:
                        break
            common_path = "/".join(dir_segs[0][:n_common])
            entry.key = host if not common_path else f"{host}/{common_path}"
        return entry

    if downloads:
        logger.warning("The `downloads` field is deprecated. Use `uri` instead.")
        if entry.uri:
            raise ValueError("Cannot provide both uri and downloads")
        if len(downloads) > 1:
            raise ValueError(
                f"Only one download URL is supported at the moment. Got: {len(downloads)}"
            )
        entry.uri = downloads[0]

    if entry.uri:
        parsed = parse_uri_metadata(entry.uri)
        entry.host = parsed["host"] if parsed["host"] else entry.host
        entry.path = parsed["path"] if parsed["path"] else entry.path
        entry.scheme = parsed["scheme"] if parsed["scheme"] else entry.scheme
        entry.format = parsed["format"] if parsed["format"] else entry.format
        if parsed["version"]:
            entry.version = parsed["version"]
        elif not entry.version:
            entry.version = ref
    else:
        if entry.shell == "" and entry.python == "" and entry.fetcher == "":
            entry.uri = build_uri(entry)

    entry.key = entry.key if entry.key else get_dataset_key(entry)
    if entry.format == "":
        entry.format = guess_file_format(entry)
    else:
        entry.format = entry.format.lstrip(".")
    entry.extract = entry.extract and (entry.format in COMPRESSED_FORMATS)
    if entry.requires:
        entry.requires = [str(r) for r in entry.requires]
    return entry


# ----- v1 resolution ladders (design §6) -----
def lang_shell_fetcher(entry: DatasetEntry) -> str:
    """Return the dataset's ``[<ds>._LANG.shell].fetcher`` template, or ``""``.

    The ``shell`` execution context is foreign to this (Python) tool, so its
    subtree is kept verbatim in ``entry.extra["_LANG"]`` (Item 2). This reads the
    fetcher command template back out so the fetch ladder can use it as a rung.
    """
    lang = entry.extra.get("_LANG")
    if isinstance(lang, dict):
        shell = lang.get("shell")
        if isinstance(shell, dict):
            fetcher = shell.get("fetcher", "")
            if isinstance(fetcher, str):
                return fetcher
    return ""


def resolve_fetcher(entry: DatasetEntry):
    """Resolve *entry*'s effective fetch binding via the v1 fetch ladder (design §6).

    Returns a ``(kind, value)`` pair:

    - ``("python", ref)`` — in-process entry-point hook: own
      ``[<ds>._LANG.python].fetcher`` (v1) or legacy ``python=``.
    - ``("shell", template)`` — shell command template: own
      ``[<ds>._LANG.shell].fetcher`` (v1) or legacy ``shell=``.
    - ``("uri", None)`` — plain URI download (``uri`` / ``uris``).
    - ``(None, None)`` — nothing to fetch with; the caller raises.

    The peer-tool *delegation* rung (design §6, between shell and uri) is
    intentionally NOT implemented in this roadmap (§D), so it is skipped.
    """
    python_ref = entry.lang_python_fetcher or entry.python
    if python_ref:
        return ("python", python_ref)
    shell_template = lang_shell_fetcher(entry) or entry.shell
    if shell_template:
        return ("shell", shell_template)
    if entry.uri or entry.uris:
        return ("uri", None)
    return (None, None)


def resolve_loader_binding(db, entry: DatasetEntry):
    """Resolve *entry*'s effective Python loader binding (design §6 load ladder).

    Returns ``(ref, args, kwargs)``. Ladder: own ``[<ds>._LANG.python].loader``
    (with its args/kwargs) or legacy ``loader=`` → manifest
    ``[_LANG.python.loaders][format]`` (with its args/kwargs). The manifest
    format-default loader supports the same parameterized ``{ ref, args, kwargs }``
    form as a per-dataset binding. Returns ``("", [], {})`` when neither applies,
    so the caller falls through to a named ``_LOADERS`` loader and then the
    built-in format default. Loaders never delegate (design §6).
    """
    if entry.lang_python_loader:
        return (
            entry.lang_python_loader,
            list(entry.lang_python_loader_args),
            dict(entry.lang_python_loader_kwargs),
        )
    if entry.loader:
        return entry.loader, [], {}
    fmt = (entry.format or "").strip().lower()
    if fmt:
        for name, ref in db.lang_python_loaders.items():
            if str(name).strip().lower() == fmt:
                return (
                    ref,
                    list(db.lang_python_loaders_args.get(name, [])),
                    dict(db.lang_python_loaders_kwargs.get(name, {})),
                )
    return "", [], {}


def resolve_loader_ref(db, entry: DatasetEntry) -> str:
    """The effective Python loader ref only (see :func:`resolve_loader_binding`)."""
    return resolve_loader_binding(db, entry)[0]


def is_a_git_repo(entry: DatasetEntry) -> bool:
    segments = entry.path.strip("/").split("/")
    if len(segments) < 2 or not segments[0] or not segments[1]:
        return False
    app = entry.host.split(".")[0]
    known_git_hosts = {
        "github.com",
        "bitbucket.org",
        "codeberg.org",
        "gitea.com",
        "sourcehut.org",
        "git.savannah.gnu.org",
        "git.kernel.org",
        "dev.azure.com",
    }
    if entry.host in known_git_hosts or app == "gitlab":
        return True
    return False


# ----- search / list / repr (Databases.jl:632-732) -----
def list_alternative_keys(entry: DatasetEntry) -> list:
    alternatives = list(entry.aliases)
    if entry.doi:
        alternatives.append(entry.doi)
    alternatives.append(entry.key)
    alternatives.append(entry.path)
    if is_a_git_repo(entry):
        segs = entry.path.strip("/").split("/")
        if len(segs) >= 2:
            alternatives.append(segs[1])
    seen = set()
    unique = []
    for alt in alternatives:
        if alt and alt not in seen:
            seen.add(alt)
            unique.append(alt)
    return unique


def list_dataset_keys(db, alt: bool = True, flat: bool = False) -> list:
    entries = []
    for name, dataset in db.datasets.items():
        row = [name]
        if alt:
            row.extend(list_alternative_keys(dataset))
        entries.append(row)
    if flat:
        return [key for row in entries for key in row]
    return entries


def repr_datasets(db, alt: bool = True) -> str:
    header = "Datasets including aliases:" if alt else "Datasets:"
    lines = [header]
    for keys in list_dataset_keys(db, alt=alt):
        lines.append("- " + " | ".join(keys))
    return "\n".join(lines)


def print_dataset_keys(db, alt: bool = True) -> None:
    print(repr_datasets(db, alt=alt))


def search_datasets(db, name: str, alt: bool = True, partial: bool = False) -> list:
    datasets = db.datasets
    matches = []
    seen_keys: set = set()
    name_lower = name.lower()

    for key, dataset in datasets.items():
        if key.lower() == name_lower and key not in seen_keys:
            matches.append((key, dataset))
            seen_keys.add(key)
    if alt:
        for key, dataset in datasets.items():
            if key not in seen_keys and name_lower in [
                a.lower() for a in list_alternative_keys(dataset)
            ]:
                matches.append((key, dataset))
                seen_keys.add(key)
    if partial:
        for key, dataset in datasets.items():
            if key not in seen_keys and name_lower in key.lower():
                matches.append((key, dataset))
                seen_keys.add(key)
    if alt and partial:
        for key, dataset in datasets.items():
            if key not in seen_keys and any(
                name_lower in a.lower() for a in list_alternative_keys(dataset)
            ):
                matches.append((key, dataset))
                seen_keys.add(key)
    return matches


def search_dataset(db, name: str, raise_: bool = True, **kwargs):
    results = search_datasets(db, name, **kwargs)
    if not results:
        if raise_:
            available = ", ".join(db.datasets.keys())
            raise ValueError(
                f"No dataset found for: `{name}`.\n"
                f"Available datasets: {available}\n"
                f"{repr_datasets(db)}"
            )
        return None
    if len(results) > 1:
        message = (
            f"Multiple datasets found for {name}:\n- "
            + "\n- ".join(
                " | ".join(list_alternative_keys(ds)) for _, ds in results
            )
        )
        logger.warning(message)
    return results[0]


# ----- Path resolution (Databases.jl:319-346) -----
def get_dataset_path(
    entry: "DatasetEntry",
    datasets_folder: str = "",
    extract=None,
    project_root: str = "",
    storage_config=None,
) -> str:
    """Return the on-disk *write* path for *entry* (Databases.jl:319-343).

    ``local_path`` is a **path expression** (spec-v2): ``$folder`` / ``${folder}``
    interpolate a folder variable (or environment variable) and ``~`` expands to
    home before the abs/relative decision — absolute → returned verbatim;
    relative → joined to *project_root* when available; otherwise returned as-is.
    ``skip_download``: returns ``entry.uri`` directly (the user manages the
    file; the pipeline raises if that path is absent).

    Otherwise the path is composed (spec-v3) as
    ``<root>/datasets/<key>`` where ``<root>`` is the **bare** root of the
    entry's store **selector** (``$folder[/subpath]``), via
    :func:`datamanifest.storage.composed_path` with ``kind="datasets"`` (scope
    is empty for fetched datasets). An empty ``store`` uses the project default
    selector (:func:`datamanifest.storage.project_default`). An
    explicitly-provided *datasets_folder* overrides the default ``$data`` root
    and is used verbatim, with no ``datasets/`` prefix (back-compat with callers
    that pass a fixed folder).
    """
    if entry.local_path != "":
        host = socket.gethostname()
        profile = os.environ.get("DATAMANIFEST_PROFILE", "")
        local_path = storage._interpolate(
            entry.local_path,
            project_root=project_root,
            storage_config=storage_config or {},
            env=os.environ,
            host=host,
            profile=profile,
            resolving=(),
        )
        if os.path.isabs(local_path):
            return local_path
        elif project_root != "":
            return os.path.join(project_root, local_path)
        else:
            return local_path
    if entry.skip_download:
        return entry.uri
    if extract is None:
        extract = entry.extract
    key = entry.key
    if extract:
        key = get_extract_path(key)
    selector = entry.store or storage.project_default(storage_config)
    # An explicit datasets_folder overrides the default $data root and is used
    # verbatim (no datasets/ prefix), for back-compat with callers passing a
    # fixed folder. Otherwise compose the spec-v3 path <root>/datasets/<key>.
    if datasets_folder and selector == "$data":
        return os.path.join(datasets_folder, key)
    return storage.composed_path(
        selector, key, kind="datasets",
        project_root=project_root, storage_config=storage_config,
    )


# Read-resolution search order: a present, complete entry in a higher-priority
# store shadows the others (Theme A / spec-v1.1 portable storage model).
_READ_STORE_ORDER = ("repo", "data", "cache")

_LEGACY_DIR_WARNED = False


def _warn_legacy_dir_once() -> None:
    """One-time notice that datasets resolve from the legacy read-only location,
    with the manual-migration escape hatch (the tool ships no auto-migration)."""
    global _LEGACY_DIR_WARNED
    if _LEGACY_DIR_WARNED:
        return
    _LEGACY_DIR_WARNED = True
    legacy = storage.legacy_data_root()
    current = storage.store_root("data")
    logger.warning(
        "Reading datasets from the legacy location %s (pre-v1.1 default; "
        "read-only). New downloads go to the current data store at %s. To keep "
        "using the legacy folder, set DATAMANIFEST_DATA_DIR=%s; otherwise "
        "migrate it manually (e.g. with rsync) at your convenience.",
        legacy, current, legacy,
    )


def resolve_existing_path(db: "Database", entry: "DatasetEntry", extract=None) -> str:
    """Return the on-disk path to read *entry* from.

    Probes, in order, the entry's own resolved ``store`` **selector** then the
    built-in folders in :data:`_READ_STORE_ORDER` (``repo`` → ``data`` →
    ``cache``), each composed (spec-v3) under the ``datasets/`` content prefix
    via :func:`datamanifest.storage.composed_path` (``<root>/datasets/<key>``),
    and returns the first that exists. A present copy in a higher-priority
    folder shadows the others (portable storage model). When none exist, the
    legacy read-only locations are probed: the pre-v1.1 default
    (``~/.cache/Datasets/<key>``) and the v0.5.0 spec-v2 layout
    (``<root>/Datasets/<key>``), skipped when the data dir is pinned via
    ``DATAMANIFEST_DATA_DIR`` / ``DATAMANIFEST_DIR``. When nothing is found,
    falls back to the *write* path for the entry's selected store (so a
    subsequent fetch materializes there).
    """
    project_root = db.get_project_root()
    if entry.local_path != "" or entry.skip_download:
        return get_dataset_path(
            entry,
            db.datasets_folder,
            extract=extract,
            project_root=project_root,
            storage_config=db.storage_config,
        )
    if extract is None:
        extract = entry.extract
    key = entry.key
    if extract:
        key = get_extract_path(key)

    # Candidate paths in priority order: the entry's own resolved store selector
    # first, then the built-in folders repo -> data -> cache, each composed under
    # the spec-v3 ``datasets/`` content prefix (``<root>/datasets/<key>``). The
    # ``datasets_folder`` back-compat override still wins for the default data
    # store and is used verbatim (no prefix).
    candidates = []
    selector = entry.store or storage.project_default(db.storage_config)
    if db.datasets_folder and selector == "$data":
        candidates.append(os.path.join(db.datasets_folder, key))
    else:
        candidates.append(
            storage.composed_path(
                selector, key, kind="datasets",
                project_root=project_root, storage_config=db.storage_config,
            )
        )
    for store in _READ_STORE_ORDER:
        if store == "data" and db.datasets_folder:
            candidates.append(os.path.join(db.datasets_folder, key))
        else:
            candidates.append(
                storage.composed_path(
                    "$" + store, key, kind="datasets",
                    project_root=project_root, storage_config=db.storage_config,
                )
            )

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate) or os.path.isdir(candidate):
            return candidate
    # Legacy read-only back-compat probes, checked last so any new-layout copy
    # wins. Covers the pre-v1.1 default (~/.cache/Datasets) and the v0.5.0
    # spec-v2 layout (<root>/Datasets/<key> per built-in store, via the suffixed
    # folder_root resolver). Skipped when the user has pinned the data dir
    # (DATAMANIFEST_DATA_DIR / DATAMANIFEST_DIR). New writes never go here.
    if not (
        os.environ.get("DATAMANIFEST_DATA_DIR") or os.environ.get("DATAMANIFEST_DIR")
    ):
        legacy_roots = [storage.legacy_data_root()]
        for store in _READ_STORE_ORDER:
            legacy_roots.append(
                storage.folder_root(
                    store, project_root=project_root,
                    storage_config=db.storage_config,
                )
            )
        seen_legacy = set()
        for root in legacy_roots:
            if root in seen_legacy:
                continue
            seen_legacy.add(root)
            legacy_candidate = os.path.join(root, key)
            if os.path.isfile(legacy_candidate) or os.path.isdir(legacy_candidate):
                _warn_legacy_dir_once()
                return legacy_candidate
    return get_dataset_path(
        entry,
        db.datasets_folder,
        extract=extract,
        project_root=project_root,
        storage_config=db.storage_config,
    )


# ----- Checksum, update, delete (Databases.jl:464-617) -----
def _maybe_persist_database(db: "Database", persist: bool = True) -> None:
    if persist and db.datasets_toml:
        tail = db.datasets_toml[-60:]
        logger.info("Write database to ...%s", tail)
        db.write(db.datasets_toml)


def verify_checksum(
    db: "Database",
    dataset: "DatasetEntry",
    persist: bool = True,
    extract=None,
    skip_if_complete: bool = False,
):
    """Verify or auto-fill the sha256 checksum for *dataset* (Databases.jl:472-502)."""
    if extract is not None and extract != dataset.extract:
        logger.warning(
            "dataset.extract=%s but required extract=%s. Skip verifying checksum.",
            dataset.extract,
            extract,
        )
        return
    local_path = get_dataset_path(
        dataset,
        db.datasets_folder,
        project_root=db.get_project_root(),
        storage_config=db.storage_config,
    )
    if db.skip_checksum or dataset.skip_checksum:
        return True
    if not os.path.isfile(local_path) and not os.path.isdir(local_path):
        return True
    if os.path.isdir(local_path) and db.skip_checksum_folders:
        return True
    if skip_if_complete and dataset.sha256 != "" and os.path.exists(storage.marker_path(local_path)):
        return True
    checksum = sha256_path(local_path)
    if dataset.sha256 == "":
        dataset.sha256 = checksum
        _maybe_persist_database(db, persist)
        return True
    if dataset.sha256 != checksum:
        raise ValueError(
            f"Checksum mismatch for dataset at {local_path}. "
            f"Expected: {dataset.sha256}, got: {checksum}. "
            "Possible resolutions:"
            "\n- remove the file"
            "\n- reset the `sha256` field"
            "\n- use a different `key`"
            "\n- remove Entry checksum checks (`dataset.skip_checksum = true`)"
            "\n- remove Database checksum checks (`db.skip_checksum = true`)"
        )
    return True


def update_checksum(
    db: "Database",
    dataset: "DatasetEntry",
    persist: bool = True,
    extract=None,
    dry_run: bool = False,
) -> str:
    """Recompute the sha256 from the on-disk file and overwrite the stored value.

    Unlike :func:`verify_checksum`, which raises on mismatch and only auto-fills
    an *empty* checksum, this unconditionally re-hashes whatever is on disk and
    replaces ``dataset.sha256``. It is the engine behind ``datamanifest
    update-checksums``.

    The file is located with :func:`resolve_existing_path`, so a dataset present
    in any read store (repo/data/cache or the legacy read-only location) is
    re-hashed in place. Returns one of:

    - ``"filled"``    — checksum was empty, now set
    - ``"updated"``   — checksum differed from disk, now replaced
    - ``"unchanged"`` — stored checksum already matches disk
    - ``"missing"``   — nothing on disk to hash
    - ``"skipped"``   — checksums disabled for this entry/database/folder

    With ``dry_run=True`` the dataset is not mutated and nothing is persisted;
    the returned action still reflects what *would* happen.
    """
    if db.skip_checksum or dataset.skip_checksum:
        return "skipped"
    local_path = resolve_existing_path(db, dataset, extract=extract)
    if not os.path.isfile(local_path) and not os.path.isdir(local_path):
        return "missing"
    if os.path.isdir(local_path) and db.skip_checksum_folders:
        return "skipped"
    checksum = sha256_path(local_path)
    old = dataset.sha256
    if old == checksum:
        return "unchanged"
    if not dry_run:
        dataset.sha256 = checksum
        _maybe_persist_database(db, persist)
    return "filled" if old == "" else "updated"


def update_entry(
    db: "Database",
    oldname: str,
    oldentry: "DatasetEntry",
    newname: str,
    newentry: "DatasetEntry",
    overwrite: bool = False,
    persist: bool = True,
):
    """Replace or rename an existing entry (Databases.jl:504-551)."""
    if (
        oldentry.key != newentry.key
        and oldentry.uri != newentry.uri
        and oldentry.version != newentry.version
        and oldname != newname
    ):
        raise ValueError(
            "At least one of the name or any of the following fields must match "
            "to update: key, uri"
        )
    if oldentry == newentry and oldname == newname:
        logger.info("Dataset entry [%s] already exists.", newname)
        return (oldname, oldentry)
    verify_checksum(db, oldentry, persist=False)
    verify_checksum(db, newentry, persist=False)
    if oldentry == newentry:
        if not overwrite:
            raise ValueError(
                f"Dataset entry already exists with name {oldname!r}. "
                f"Pass `overwrite=True` to update with new name {newname!r}."
            )
        logger.warning("Rename %s => %s", oldname, newname)
        del db.datasets[oldname]
        db.datasets[newname] = newentry
        _maybe_persist_database(db, persist)
        return (newname, newentry)
    message = f"Possible duplicate found {oldname} =>\n{oldentry}"
    existing_datapath = get_dataset_path(
        oldentry,
        db.datasets_folder,
        project_root=db.get_project_root(),
        storage_config=db.storage_config,
    )
    new_datapath = get_dataset_path(
        newentry,
        db.datasets_folder,
        project_root=db.get_project_root(),
        storage_config=db.storage_config,
    )
    if existing_datapath != new_datapath and (
        os.path.isfile(existing_datapath) or os.path.isdir(existing_datapath)
    ):
        if os.path.isfile(new_datapath) or os.path.isdir(new_datapath):
            message += (
                "\n\nBoth old and new datasets exist on disk at:"
                f"\n    {existing_datapath} SHA-256: {oldentry.sha256}"
                f"\n    {new_datapath} SHA-256: {newentry.sha256}"
            )
        else:
            message += f"\nExisting dataset found at\n    {existing_datapath}\n."
        message += (
            "\n\nCleanup manually if needed."
            "Note you may explicitly specify the keys to point to a dataset, e.g."
            f'\n    key="{oldentry.key}"'
            f'\n    key="{newentry.key}"'
        )
    if overwrite:
        logger.warning("%s\n\nOverwriting with new entry %s =>\n%s", message, newname, newentry)
        if oldname in db.datasets:
            del db.datasets[oldname]
        db.datasets[newname] = newentry
        _maybe_persist_database(db, persist)
        return (newname, newentry)
    raise ValueError(
        f"{message}\n\nPlease manually remove the old entry or set `overwrite=True` "
        f"to update with dataset {newname} =>\n{newentry} or pass "
        "`check_duplicate=False` to register nonetheless"
    )


def _remove_dataset_from_disk(db: "Database", entry: "DatasetEntry") -> None:
    """Delete the on-disk files for *entry* (Databases.jl:589-605)."""
    if entry.skip_download or entry.local_path != "":
        return
    download_path = get_dataset_path(
        entry,
        db.datasets_folder,
        extract=False,
        project_root=db.get_project_root(),
        storage_config=db.storage_config,
    )
    if entry.extract:
        local_path = get_dataset_path(
            entry,
            db.datasets_folder,
            extract=True,
            project_root=db.get_project_root(),
            storage_config=db.storage_config,
        )
        if os.path.isdir(local_path):
            import shutil
            shutil.rmtree(local_path, ignore_errors=True)
    if os.path.isfile(download_path):
        os.remove(download_path)
    elif os.path.isdir(download_path):
        import shutil
        shutil.rmtree(download_path, ignore_errors=True)


def delete_dataset(
    db: "Database",
    name: str,
    keep_cache: bool = False,
    persist: bool = True,
) -> None:
    """Remove a dataset entry and (optionally) its cached files (Databases.jl:607-617)."""
    resolved_name, entry = search_dataset(db, name)
    if not keep_cache:
        _remove_dataset_from_disk(db, entry)
    del db.datasets[resolved_name]
    if persist and db.datasets_toml:
        db.write(db.datasets_toml)


# Canonical key ordering now lives in the Layer 0 substrate
# (``datamanifest.store.serialize``) so the cache layer can share the one
# normative byte ordering without importing ``database``. Re-imported here under
# the historical private name so existing callers (and ``cli.py``) keep working.
from .store import sort_recursive as _sort_recursive  # noqa: E402


# ----- Database (Databases.jl:147-258, 553-825) -----
class Database:
    """Registry of :class:`DatasetEntry` objects, with TOML persistence.

    Port of Julia's ``Database`` (Databases.jl:147-172). The loader registry
    fields are present here as empty stubs; actual loader resolution behaviour
    is added in a later item. Without inline code execution there is no
    ``loaders_*_modules`` field (see roadmap §C): user-local modules are made
    importable via ``loaders_python_includes`` (paths prepended to ``sys.path``).
    """

    def __init__(
        self,
        datasets_toml: str = "",
        datasets_folder: str = "",
        persist: bool = True,
        skip_checksum: bool = False,
        skip_checksum_folders: bool = False,
        datasets=None,
        **kwargs,
    ):
        self.datasets = dict(datasets) if datasets is not None else {}
        # An explicit datasets_folder overrides the `data` store root; when left
        # empty, the `data` store is resolved via the `storage` module
        # (platformdirs roots). The legacy DEFAULT_DATASETS_FOLDER_PATH is no
        # longer silently forced here.
        if datasets_toml == "" and persist:
            datasets_toml = get_default_toml()
        toml_path = (
            os.path.abspath(datasets_toml) if persist and datasets_toml != "" else ""
        )
        self.datasets_toml = toml_path
        self.datasets_folder = datasets_folder
        self.skip_checksum = skip_checksum
        self.skip_checksum_folders = skip_checksum_folders
        # Loader registry (behaviour filled in by a later item). `loaders` is the
        # bare, language-implicit [_LOADERS] format→binding map (spec-v3.4); a
        # value may be a bare ref or a { ref, args, kwargs } table — the ref
        # lives here, any args/kwargs in the parallel maps. It is the
        # language-implicit counterpart of [_LANG.python.loaders] and is the
        # lower-precedence rung (explicit wins).
        self.loaders: dict = {}
        self.loaders_args: dict = {}
        self.loaders_kwargs: dict = {}
        self.loaders_python_includes: list = []
        self.loader_cache: dict = {}
        # v1 _LANG.python.loaders: format→ref map from [_LANG.python.loaders].
        # A value may be a bare ref or a parameterized { ref, args, kwargs }
        # table; the ref lives here and any args/kwargs in the parallel maps.
        self.lang_python_loaders: dict = {}
        self.lang_python_loaders_args: dict = {}
        self.lang_python_loaders_kwargs: dict = {}
        # Database-level passthrough for unknown _* top-level tables (mirrors
        # per-dataset extra). schema_version comes from [_META].schema; None => v0.
        self.extra: dict = {}
        self.storage_config: dict = {}
        self.schema_version = None
        if datasets_toml and os.path.isfile(datasets_toml):
            # Loading from the toml must never write it back — read commands
            # (`list`, `where`, ...) would otherwise silently rewrite the user's
            # file (reordered, and stripped of any unsupported fields).
            self.register_datasets(datasets_toml, persist=False, **kwargs)

    # ----- equality (Databases.jl:174-179, julia_modules dropped) -----
    def __eq__(self, other):
        if not isinstance(other, Database):
            return NotImplemented
        return (
            self.datasets == other.datasets
            and self.datasets_folder == other.datasets_folder
            and self.datasets_toml == other.datasets_toml
            and self.loaders == other.loaders
            and self.loaders_python_includes == other.loaders_python_includes
            and self.lang_python_loaders == other.lang_python_loaders
            and self.lang_python_loaders_args == other.lang_python_loaders_args
            and self.lang_python_loaders_kwargs == other.lang_python_loaders_kwargs
            and self.extra == other.extra
            and self.schema_version == other.schema_version
        )

    __hash__ = None

    def __getitem__(self, name: str) -> DatasetEntry:
        return search_dataset(self, name)[1]

    def __repr__(self) -> str:
        n = len(self.datasets)
        return (
            f"Database({n} dataset{'s' if n != 1 else ''}, "
            f"toml={self.datasets_toml!r})"
        )

    def __str__(self) -> str:
        return repr_datasets(self)

    def get_project_root(self) -> str:
        """Return the project root derived from ``datasets_toml`` (Config.jl:98-131)."""
        return project_root_from_paths(self.datasets_toml)

    # ----- TOML serialization (Databases.jl:184-258) -----
    def to_dict(self) -> dict:
        loaders_table: dict = {}
        if self.loaders_python_includes:
            loaders_table["python_includes"] = list(self.loaders_python_includes)
        for n, c in self.loaders.items():
            if not _is_empty(c):
                loaders_table[n] = c
        result: dict = {}
        if loaders_table:
            result["_LOADERS"] = loaders_table
        if self.schema_version is not None:
            result["_META"] = {"schema": self.schema_version}
        # Re-emit unknown _* tables verbatim (database-level passthrough).
        # `_LANG` is handled specially below so the regenerated python block can
        # be merged with the foreign subtrees.
        for k, v in self.extra.items():
            if k == "_LANG":
                continue
            result[k] = v
        # Regenerate the top-level [_LANG.python] block (our own loaders map) and
        # splice every foreign top-level [_LANG.<other>] subtree back verbatim.
        lang_table: dict = {}
        if self.lang_python_loaders:
            lang_table["python"] = {
                "loaders": {
                    fmt: _python_binding(
                        ref,
                        self.lang_python_loaders_args.get(fmt),
                        self.lang_python_loaders_kwargs.get(fmt),
                    )
                    for fmt, ref in self.lang_python_loaders.items()
                }
            }
        foreign_lang = self.extra.get("_LANG")
        if isinstance(foreign_lang, dict):
            for k, v in foreign_lang.items():
                lang_table.setdefault(k, v)
        if lang_table:
            result["_LANG"] = lang_table
        for key, entry in self.datasets.items():
            result[key] = to_dict(entry)
        return result

    def write(self, datasets_toml: str) -> None:
        data = self.to_dict()
        with open(datasets_toml, "wb") as f:
            tomli_w.dump(_sort_recursive(data), f)

    # ----- registry (Databases.jl:553-792) -----
    def register_dataset(
        self,
        uri: str = "",
        name: str = "",
        overwrite: bool = False,
        persist: bool = True,
        check_duplicate: bool = True,
        uris=None,
        **kwargs,
    ):
        entry = init_dataset_entry(uri=uri, uris=uris, **kwargs)
        if name == "":
            if is_a_git_repo(entry):
                name = "/".join(entry.path.strip("/").split("/")[:2])
            else:
                name = entry.key.strip()
            name = os.path.splitext(name)[0]
        if check_duplicate and name in self.datasets:
            existing = self.datasets[name]
            if existing == entry and not overwrite:
                logger.info("Dataset entry [%s] already exists.", name)
                return (name, existing)
            if not overwrite:
                raise ValueError(
                    f"Dataset entry already exists with name {name!r}. "
                    f"Pass `overwrite=True` to replace it."
                )
        self.datasets[name] = entry
        if persist and self.datasets_toml != "":
            self.write(self.datasets_toml)
        return (name, entry)

    def register_datasets(self, datasets, persist: bool = True, **kwargs):
        if isinstance(datasets, str):
            ext = os.path.splitext(datasets)[1]
            if ext != ".toml":
                raise ValueError(f"Only toml file type supported. Got: {ext}")
            return self.register_datasets_toml(datasets, persist=persist, **kwargs)

        _legacy: set = set()

        loaders_section = datasets.get("_LOADERS", datasets.get("_loaders"))
        if isinstance(loaders_section, dict):
            _legacy.add("_LOADERS")
            includes = loaders_section.get(
                "python_includes", loaders_section.get("julia_includes", [])
            )
            if isinstance(includes, list):
                self.loaders_python_includes.extend(str(x) for x in includes)
            for k, v in loaders_section.items():
                if k in (
                    "python_includes",
                    "julia_includes",
                    "python_modules",
                    "julia_modules",
                ):
                    continue
                # [_LOADERS] is a language-implicit format→binding map
                # (spec-v3.4): each value is a bare ref string or a
                # { ref, args, kwargs } table. Split it across the parallel maps.
                if isinstance(v, dict):
                    ref, a, kw = _split_python_binding(v)
                    if not ref:
                        continue
                    self.loaders[str(k)] = ref
                    if a:
                        self.loaders_args[str(k)] = a
                    if kw:
                        self.loaders_kwargs[str(k)] = kw
                else:
                    self.loaders[str(k)] = v if isinstance(v, str) else repr(v)

        meta_section = datasets.get("_META")
        if isinstance(meta_section, dict):
            schema_val = meta_section.get("schema")
            if schema_val is not None:
                self.schema_version = schema_val

        # Parse [_LANG.python.loaders]; keep foreign _LANG.<other> in db-level extra.
        lang_top = datasets.get("_LANG")
        if isinstance(lang_top, dict):
            python_top = lang_top.get("python", {})
            if isinstance(python_top, dict):
                loaders_map = python_top.get("loaders", {})
                if isinstance(loaders_map, dict):
                    # Each format→loader value is a bare ref or a parameterized
                    # { ref, args, kwargs } table (same form as a per-dataset
                    # binding); split it across the ref/args/kwargs maps.
                    for fmt, val in loaders_map.items():
                        ref, a, kw = _split_python_binding(val)
                        if not ref:
                            continue
                        self.lang_python_loaders[str(fmt)] = ref
                        if a:
                            self.lang_python_loaders_args[str(fmt)] = a
                        if kw:
                            self.lang_python_loaders_kwargs[str(fmt)] = kw
            foreign_lang = {k: v for k, v in lang_top.items() if k != "python"}
            if foreign_lang:
                self.extra["_LANG"] = foreign_lang

        # Capture unknown _* top-level tables into db-level extra (mirrors per-dataset extra).
        _known_structural = {"_LOADERS", "_loaders", "_META", "_LANG"}
        for k, v in datasets.items():
            if k.startswith("_") and k not in _known_structural:
                self.extra[k] = dict(v) if isinstance(v, dict) else v

        # Expose [_STORAGE] as a parsed read-only config dict (verbatim copy stays in extra).
        self.storage_config = dict(self.extra.get("_STORAGE", {}))

        names = [k for k in datasets if not k.startswith("_")]
        for i, name in enumerate(names):
            info = dict(datasets[name])
            for _leg in ("python", "callable", "shell", "loader"):
                if info.get(_leg):
                    _legacy.add(_leg)
            persist_on_last_iteration = persist and i == len(names) - 1
            self.register_dataset(
                name=name, persist=persist_on_last_iteration, **{**info, **kwargs}
            )

        if _legacy:
            logger.warning(
                "Legacy v0 fields detected (%s). "
                "Run `datamanifest migrate <file>` to upgrade to v1.",
                ", ".join(sorted(_legacy)),
            )

    def register_datasets_toml(self, datasets_toml, persist: bool = True, **kwargs):
        # Prepend the manifest's directory to sys.path so refs like "module:func"
        # resolve against modules sitting next to the manifest file.
        project_root = os.path.dirname(os.path.abspath(datasets_toml))
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        with open(datasets_toml, "rb") as f:
            config = tomllib.load(f)
        self.register_datasets(config, persist=persist, **kwargs)

    # ----- loader registry (Databases.jl:734-749) -----
    def register_loaders(self, loaders=None, python_includes=None, persist: bool = True):
        """Register named loaders / python include paths (Databases.jl:734-749).

        Loader values are ``"pkg.mod:func"`` entry-point references (or the name
        of another loader, treated as an alias) — never inline code. Resetting
        the registry clears the resolution cache.
        """
        if loaders is not None:
            self.loaders = {}
            self.loaders_args = {}
            self.loaders_kwargs = {}
            for k, v in loaders.items():
                if isinstance(v, dict):
                    ref, a, kw = _split_python_binding(v)
                    if not ref:
                        continue
                    self.loaders[str(k)] = ref
                    if a:
                        self.loaders_args[str(k)] = a
                    if kw:
                        self.loaders_kwargs[str(k)] = kw
                else:
                    self.loaders[str(k)] = v if isinstance(v, str) else repr(v)
        if python_includes is not None:
            self.loaders_python_includes = [str(x) for x in python_includes]
        self.loader_cache.clear()
        if persist and self.datasets_toml != "":
            self.write(self.datasets_toml)


# ----- v0 → v1 migration -----
def migrate_v0_to_v1(db: "Database") -> None:
    """Migrate *db* from v0 flat bindings to v1 _LANG form (in-place).

    Moves each dataset's ``python=`` to ``[<ds>._LANG.python].fetcher`` and
    ``loader=`` to ``[<ds>._LANG.python].loader``.  Moves the ``[_LOADERS]``
    format→ref map to ``[_LANG.python.loaders]``.  Sets ``_META.schema = 1``.
    ``shell=`` and all foreign keys are left verbatim.
    """
    for _name, entry in db.datasets.items():
        if entry.python and not entry.lang_python_fetcher:
            entry.lang_python_fetcher = entry.python
            entry.python = ""
        if entry.loader and not entry.lang_python_loader:
            entry.lang_python_loader = entry.loader
            entry.loader = ""
        if entry.shell:
            lang = entry.extra.setdefault("_LANG", {})
            shell_block = lang.setdefault("shell", {})
            if not isinstance(shell_block, dict):
                shell_block = {}
                lang["shell"] = shell_block
            if not shell_block.get("fetcher"):
                shell_block["fetcher"] = entry.shell
                entry.shell = ""
    if db.loaders and not db.lang_python_loaders:
        db.lang_python_loaders = dict(db.loaders)
        db.loaders = {}
    db.schema_version = 1


# ----- v1 → v2 migration -----
def migrate_v1_to_v2(db: "Database") -> None:
    """Migrate *db* from v1 bare-store names to v2 $-selector form (in-place).

    Each dataset's ``store`` field is rewritten: bare names like ``"cache"``
    become ``"$cache"``; ``"data"`` and ``""`` (both meaning the default data
    store) are normalised to ``""`` (the elided default).  ``[_STORAGE]``
    folder-variable definitions (bare keys like ``scratch = "/path"``) are
    left untouched — only the per-dataset ``store`` selector is migrated.
    """
    for _name, entry in db.datasets.items():
        s = entry.store
        if not s or s == "data":
            entry.store = ""
        elif not s.startswith("$"):
            entry.store = "$" + s


# ----- default database (process-wide singleton) -----
_default_db: "Database | None" = None


def get_default_database() -> "Database":
    """Return the process-wide default :class:`Database`, creating it lazily.

    The database is constructed from :func:`~datamanifest.config.get_default_toml`
    (env-var / cwd-walk logic). Raises ``RuntimeError`` when no ``datasets.toml``
    can be located, so callers get a clear message rather than a silent no-op.
    """
    global _default_db
    if _default_db is None:
        toml_path = get_default_toml()
        if not toml_path or not os.path.isfile(toml_path):
            raise RuntimeError(
                "No datasets.toml found. Activate a project (a directory "
                "containing datasets.toml) or pass a Database explicitly."
            )
        _default_db = Database(datasets_toml=toml_path)
    return _default_db


# ----- loader validation (Databases.jl:751-762) -----
def validate_loader(db: "Database", name: str):
    """Resolve loader *name* to its callable, raising if it cannot (Databases.jl:751-754)."""
    from .pipelines import _get_loader_function

    return _get_loader_function(db, name)


def validate_loaders(db: "Database") -> None:
    """Eagerly resolve every registered loader (Databases.jl:756-762)."""
    for name in list(db.loaders.keys()):
        validate_loader(db, name)
