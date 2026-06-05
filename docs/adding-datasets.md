# Adding datasets from external sources (DRAFT — for review)

> Status: **proposal**. This documents a command surface for onboarding datasets
> from common tools and data repositories. Nothing here is implemented yet — it is
> written for review. Once the shape is agreed, the relevant parts fold into the
> README's *CLI usage* section and each source is built + tested.

> Layering note: this page is the **user-facing command surface**. Most of these
> sources are pure *import* (declaration parsing) over already-supported download
> schemes. Only **Git LFS** needs a new *download protocol* (spec-normative,
> implemented in every language); that work is sequenced first and tracked
> separately in [`design-remote-protocols.md`](design-remote-protocols.md).

datamanifest distinguishes two verbs by **what you hand the command**:

| Verb | You give it… | Yields | Examples |
|---|---|---|---|
| `add` | a **reference to data** (URL, DOI, LFS pointer) | one dataset, or all files of a record | direct URL, Zenodo/figshare DOI, Git LFS pointer |
| `import` | **another tool's catalog/registry file** | many datasets | pooch, intake, DVC |

The test: is the argument *another tool's manifest* → `import`; is it *a pointer to
data* → `add`.

Both verbs end at the same place: standard `datamanifest.toml` entries (`uri`,
`sha256`, optional `doi`/`description`/`extract`), plus — where a local copy
already exists — an in-place adoption recorded in the state file so nothing is
re-downloaded.

A note on checksums: several sources publish **md5** (Zenodo, DVC), not sha256.
datamanifest verifies sha256, so for those an entry is declared without `sha256`;
the md5 is verified on first download (or against an adopted local file) and the
**sha256 is computed and recorded** at that point. Git LFS is the exception — its
pointer already carries the sha256, so no download is needed to set it.

---

## `add` — add dataset(s) from a reference

### A direct URL (today)

```bash
datamanifest add https://www.ncei.noaa.gov/woa/temperature.nc
datamanifest add "https://zenodo.org/records/1234567/files/grid.zip" --extract
```

One dataset; downloads and records its sha256 by default (`--no-download` to defer).

### A DOI or data-repository record — Zenodo / figshare / OSF / Dryad

```bash
datamanifest add 10.5281/zenodo.1234567          # by DOI
datamanifest add https://zenodo.org/records/1234567   # or by record URL
```

Resolves the record through the repository's API and adds **one dataset per file**
in it. Each entry gets:

- `uri` — the file's direct download link,
- `doi` — the record DOI (so the provenance is first-class, not just a label),
- `description` — the record title,
- `sha256` — filled on first download (Zenodo/figshare publish md5, which is
  verified during the download).

Options:

- `--name PREFIX` — name the datasets `PREFIX/<filename>` instead of bare filenames.
- `--pick GLOB` (repeatable) — add only the files matching a glob (e.g. `--pick '*.nc'`).
- `--no-download` — declare only.

Why this beats today: instead of pasting every file URL by hand and losing the DOI
link, you hand over the record once and get all its files with checksums and
provenance.

### A Git LFS pointer

```bash
datamanifest add path/to/pointer-file --name bathymetry
datamanifest add other-repo/data.bin.pointer --lfs-url https://github.com/org/repo.git
```

Reads the pointer (`oid sha256:<hex>`, `size`) and adds **one dataset** whose
`sha256` is taken straight from the pointer (no download needed to set it). The
download `uri` is the LFS object, resolved from:

- the **current** repo's LFS endpoint when run inside a git repo with LFS, or
- `--lfs-url <repo>` for a pointer that belongs to **another** repository.

Caveats (LFS is the lowest-value source):

- For files tracked in *your own* repo, git already has them — importing is
  redundant; the useful case is depending on **another** project's LFS object.
- The pointer carries no human metadata, so set `--name` yourself.

---

## `import` — bulk-import another tool's catalog

### pooch (implemented)

```bash
datamanifest import pooch registry.txt --base-url URL [--cache-dir DIR]
```

Each `filename [algo:]hash [url]` line becomes a dataset. `--cache-dir`
(e.g. `pooch.os_cache('pkg')`) adopts already-downloaded files in place,
checksum-verified — no re-download. See the README for full detail.

### intake catalogs

```bash
datamanifest import intake catalog.yml [--driver csv,netcdf,...]
```

An intake catalog lists several named *sources*, each with a `driver` and
`args.urlpath`. Each source with a concrete file `urlpath` becomes a dataset
(`uri = urlpath`). intake catalogs carry **no checksums**, so `sha256` is filled on
first download. Sources whose driver/urlpath isn't a fetchable file (templated
parameters, server protocols) are reported and skipped; `--driver` narrows to
selected drivers.

### DVC

```bash
datamanifest import dvc <path-or-dir> [--cache-dir .dvc/cache]
```

Parses `*.dvc` / `dvc.lock`. Each tracked *out* (with its `md5`/hash) becomes a
dataset. Because DVC stores content addressed by hash under `.dvc/cache`, the
**existing cache is adopted in place** by hash — the strongest reuse story after
pooch. The download `uri` is reconstructed from the DVC **remote** config when
possible (HTTP remotes, or `import-url` stages that record an explicit URL);
outs backed by a non-URL remote (S3, gdrive, ssh) are declared with the hash but
left for DVC/you to resolve, and reported as such.

### Generic CSV / URL list (universal escape hatch)

```bash
datamanifest import csv files.csv            # columns: name,url,sha256 (sha256 optional)
datamanifest import urls list.txt --base-url URL   # one path/URL per line
```

For exporting from anything. Reuses the whole pooch pipeline, including
`--cache-dir` adoption.

---

## What maps where (summary)

| Source | Verb | URL | Checksum | Adopt local cache |
|---|---|---|---|---|
| direct URL | `add` | given | computed on download | — |
| Zenodo/figshare/OSF DOI | `add` | API | md5 → sha256 on download | — |
| Git LFS pointer | `add` | LFS endpoint | **sha256 from pointer** | `.git/lfs/objects` (by sha256) |
| pooch registry | `import` | base_url + filename / 3rd col | sha256 (or md5) | `os_cache` (✓ implemented) |
| intake catalog | `import` | urlpath | none | — |
| DVC | `import` | remote config (partial) | md5/hash | `.dvc/cache` (by hash) |
| CSV / URL list | `import` | the file | optional | `--cache-dir` |
