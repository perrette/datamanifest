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

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import tomli_w

from .config import (
    COMPRESSED_FORMATS,
    DEFAULT_DATASETS_FOLDER_PATH,
    HIDE_STRUCT_FIELDS,
    get_default_toml,
    get_extract_path,
    logger,
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
) -> str:
    """Return the on-disk path for *entry*.

    ``local_path`` and ``skip_download`` extensions are wired in a later item.
    """
    if extract is None:
        extract = entry.extract
    key = entry.key
    if extract:
        key = get_extract_path(key)
    folder = datasets_folder if datasets_folder else DEFAULT_DATASETS_FOLDER_PATH
    return os.path.join(folder, key)


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
):
    """Verify or auto-fill the sha256 checksum for *dataset* (Databases.jl:472-502)."""
    if extract is not None and extract != dataset.extract:
        logger.warning(
            "dataset.extract=%s but required extract=%s. Skip verifying checksum.",
            dataset.extract,
            extract,
        )
        return
    local_path = get_dataset_path(dataset, db.datasets_folder)
    if db.skip_checksum or dataset.skip_checksum:
        return True
    if not os.path.isfile(local_path) and not os.path.isdir(local_path):
        return True
    if os.path.isdir(local_path) and db.skip_checksum_folders:
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
    existing_datapath = get_dataset_path(oldentry, db.datasets_folder)
    new_datapath = get_dataset_path(newentry, db.datasets_folder)
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
    download_path = get_dataset_path(entry, db.datasets_folder, extract=False)
    if entry.extract:
        local_path = get_dataset_path(entry, db.datasets_folder, extract=True)
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
        if datasets_folder == "":
            datasets_folder = DEFAULT_DATASETS_FOLDER_PATH
        if datasets_toml == "" and persist:
            datasets_toml = get_default_toml()
        toml_path = (
            os.path.abspath(datasets_toml) if persist and datasets_toml != "" else ""
        )
        self.datasets_toml = toml_path
        self.datasets_folder = datasets_folder
        self.skip_checksum = skip_checksum
        self.skip_checksum_folders = skip_checksum_folders
        # Loader registry (behaviour filled in by a later item).
        self.loaders: dict = {}
        self.loaders_python_includes: list = []
        self.loader_cache: dict = {}
        if datasets_toml and os.path.isfile(datasets_toml):
            self.register_datasets(datasets_toml, **kwargs)

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
        for key, entry in self.datasets.items():
            result[key] = to_dict(entry)
        return result

    def write(self, datasets_toml: str) -> None:
        data = self.to_dict()
        # Sorted output for reproducible diffs: `_LOADERS` first, then dataset
        # keys alphabetically (mirrors Julia's TOML.print(...; sorted=true)).
        ordered: dict = {}
        if "_LOADERS" in data:
            ordered["_LOADERS"] = data["_LOADERS"]
        for key in sorted(k for k in data if k != "_LOADERS"):
            ordered[key] = data[key]
        with open(datasets_toml, "wb") as f:
            tomli_w.dump(ordered, f)

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

    def register_datasets(self, datasets, **kwargs):
        if isinstance(datasets, str):
            ext = os.path.splitext(datasets)[1]
            if ext != ".toml":
                raise ValueError(f"Only toml file type supported. Got: {ext}")
            return self.register_datasets_toml(datasets, **kwargs)

        loaders_section = datasets.get("_LOADERS", datasets.get("_loaders"))
        if isinstance(loaders_section, dict):
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
                self.loaders[str(k)] = v if isinstance(v, str) else repr(v)

        names = [k for k in datasets if k not in ("_LOADERS", "_loaders")]
        for i, name in enumerate(names):
            info = dict(datasets[name])
            persist_on_last_iteration = i == len(names) - 1
            self.register_dataset(
                name=name, persist=persist_on_last_iteration, **{**info, **kwargs}
            )

    def register_datasets_toml(self, datasets_toml, **kwargs):
        with open(datasets_toml, "rb") as f:
            config = tomllib.load(f)
        self.register_datasets(config, **kwargs)
