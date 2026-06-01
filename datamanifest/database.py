"""DatasetEntry dataclass + URI parsing helpers.

Port of the types + path/URI helpers from DataManifest.jl's ``Databases.jl``
(lines 10-462). The ``Database`` class itself is added in a later item; this
module currently provides only ``DatasetEntry`` and the free helper functions.

Julia adaptations (see roadmap §C):
- ``julia`` (inline code) becomes ``python`` (an entry-point reference
  ``"pkg.mod:func"`` resolved via importlib; no inline code execution).
- ``julia_modules`` is dropped (no execution context to preimport into).
- ``callable`` is accepted on read as an alias and normalized into ``python``
  at ``init_dataset_entry`` time; it is never stored as a field nor serialized.
"""

import os
from dataclasses import dataclass, field, fields
from urllib.parse import parse_qs, urlparse

from .config import COMPRESSED_FORMATS, HIDE_STRUCT_FIELDS, logger


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
    requires: list = field(default_factory=list)

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


def to_dict(entry: DatasetEntry) -> dict:
    output = {}
    for f in fields(entry):
        name = f.name
        value = getattr(entry, name)
        if name in HIDE_STRUCT_FIELDS:
            continue
        if _is_empty(value):
            continue
        if name == "key" and value == build_dataset_key(entry):
            continue
        if name == "format" and value == guess_file_format(entry):
            continue
        output[name] = value
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

    entry = DatasetEntry(uri=uri, uris=list(uris), **kwargs)

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
        if entry.shell == "" and entry.python == "":
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
