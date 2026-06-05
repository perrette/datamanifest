# Remote download protocols vs. import (DRAFT — for review)

> Status: **proposal / spec input**. Splits the work of "support more sources" into
> two layers that must NOT be conflated, because they have very different blast
> radii. Nothing here is implemented yet.

## The two layers

| Layer | What it is | Where it lives | Spec-normative? |
|---|---|---|---|
| **Download protocol** | *how to fetch the bytes* given a declared entry (the wire protocol behind a `scheme`) | `pipelines._fetch_into_path` + the cross-language spec | **Yes** — every sibling implementation (e.g. `DataManifest.jl`) must fetch the same `datamanifest.toml` |
| **Import** | *where the declarations come from* — parse another tool's catalog/API into ordinary entries | `datamanifest/importers.py`, the `add`/`import` CLI | **No** — produces standard entries using existing schemes; Python-only tooling |

The rule: a new **import** source that resolves to an already-supported scheme
(plain HTTP, git, ssh, file) needs **no spec change** — it is pure declaration
parsing. A source that needs a **new way to fetch bytes** is a download-protocol
change and must be specced and implemented in every language first.

## Existing download protocols (already specced/implemented)

Scheme-dispatched in `pipelines.py`:

- `http` / `https` — streaming GET (`_http_download`, pipelines.py:396)
- `git` / `ssh+git` / `https://*.git` — `git clone --depth 1 [--branch]` (pipelines.py:607)
- `ssh` / `sshfs` / `rsync` — rsync over ssh (`_rsync_into`, pipelines.py:626)
- `file` — copy / copytree, or rsync from a remote host (pipelines.py:630)

## Extraction: what each proposed source needs to *download*

| Source | Download mechanism | New protocol? |
|---|---|---|
| direct URL | `https` GET | no — exists |
| **Zenodo / figshare / OSF / Dryad** (by DOI) | API resolves to file URLs → `https` GET | **no** — import-time *resolver* only |
| pooch / intake / CSV / URL list | `urlpath` is `http(s)`/`file`/`ssh` | no — exists (import only) |
| **DVC** | content in a DVC *remote* | **only** for non-`http`, non-`ssh` remotes (S3/GCS/gdrive) → object stores |
| **Git LFS** | LFS **batch API** → returned href GET | **YES — new wire protocol** |

Two non-obvious points:

- **Zenodo & friends need no new protocol.** The DOI/record resolution is an
  *import-time* API call to enumerate files; the files themselves are plain HTTPS
  GETs. So the whole repository family is import work, not protocol work.
- **Same-repo Git LFS already works** through the existing `git clone` path *if
  `git-lfs` is installed* (clone smudges LFS files automatically). The genuinely
  new thing is fetching a **single LFS object by its oid from an endpoint** —
  the cross-repo / no-clone case.

## Conclusion: one new protocol now, one deferred track

1. **Git LFS object fetch — the one bounded new protocol to spec + implement now**
   (Python **and** Julia). Mechanism:
   `POST <endpoint>/objects/batch` with `{operation:"download", objects:[{oid,size}]}`
   → response `actions.download.href` (+ short-lived auth headers) → GET that href.
   The `oid` is a sha256 — **the same value datamanifest already stores as
   `sha256`** — so verification is free.

2. **Object stores (S3 / GCS / Azure / gdrive)** — **implemented in Python via
   fsspec** (`s3://` / `gs://` / `az://` … schemes dispatch to `_fsspec_download`;
   optional `[fsspec]` extra + the backend, e.g. `s3fs`/`gcsfs`/`adlfs`). The
   fetched copy is sha256-verified like any other download. The *spec* defines the
   schemes, not the mechanism — Julia would implement them with its own backends
   (or `delegate`); fsspec is a Python implementation detail. This also gives DVC
   non-HTTP remotes and intake object-store urlpaths a fetch path. **On-the-fly
   access shipped** as `add --on-the-fly` = `skip_download` + a built-in fsspec
   loader (so users don't hand-write one); a standalone streaming *access mode* and
   retention/TTL were **dropped**.

So the dependency-correct order is:

- **Phase 1 — protocol (spec-normative, cross-language):** Git LFS object fetch.
  Spec it, implement in Python + Julia, add conformance fixtures.
- **Phase 2 — import (Python-only, no spec change):** Zenodo/figshare/OSF resolver,
  intake, DVC (local-cache adopt + HTTP remote), CSV/URL list. These emit standard
  entries over existing schemes.

## Spec questions to settle for Git LFS (Phase 1)

These are the decisions the spec update hinges on — to discuss before coding:

1. **How is an LFS object represented in `datamanifest.toml`?** Options:
   - a dedicated scheme, e.g. `uri = "lfs+https://<host>/<org>/<repo>.git"` with the
     object identified by the entry's `sha256` (= oid) and a `size`;
   - or keep `uri` as the repo and add an explicit marker/field (`lfs = true`,
     `size = N`).
2. **Where does the endpoint come from?** Default to the file's own repo LFS
   endpoint (`<remote>/info/lfs`) when inside a git checkout; require it explicitly
   for a foreign-repo pointer.
3. **`size`** — LFS batch requests require the object size. It must be carried on
   the entry (new optional field) since the pointer provides it.
4. **Auth** — anonymous for public GitHub/GitLab LFS; defer credentialed endpoints
   (env/token) or reuse git's credential helper?
5. **Verification** — `oid == sha256` already; confirm datamanifest verifies it
   against the same `local_path` rule as other datasets.

See `adding-datasets.md` for the user-facing command surface that sits on top of
these protocols.

## ⇪ Decisions to propagate to the spec repo + DataManifest.jl

These were settled on the Python side but are **cross-language** and must be
reflected in the canonical spec (`datamanifest.toml`) and ported to (or
consciously skipped by) the Julia implementation. They are **not** Python-only
tooling.

1. **Object-store download schemes are normative.** `s3://`, `gs://`, `gcs://`,
   `az://`, `abfs://`, `abfss://`, `adl://`, `gdrive://` are valid `uri` schemes
   that mean "fetch this object from the named store, then verify `sha256` as
   usual." The spec defines the *schemes and semantics*, not the mechanism: Python
   implements them via fsspec (optional `[fsspec]` extra + backend); Julia
   implements the ones it can with its own packages, and otherwise `delegate`s or
   errors with "unsupported scheme." HTTP/HTTPS keep their existing dedicated path
   and are deliberately **not** in this set.

2. **`lazy_access` — a language-neutral marker for "open in place, don't download."**
   (This supersedes the earlier idea of overloading `skip_download`.) `skip_download`
   keeps its original sole meaning: "the `uri` is an existing **local** file the user
   manages." A separate boolean **`lazy_access = true`** marks a dataset that is
   **not downloaded** — its `uri` (typically an object store, `s3://`…) is opened in
   place by a loader. Behaviour the spec/Julia must mirror:
   - `download_dataset` returns the `uri` as-is for a `lazy_access` entry — **no
     local existence check, no state-file record**; `get_dataset_path` / the `path`
     accessor returns the **remote URI string** (consumers must tolerate that).
   - The loader is bound **language-specifically**: Python sets
     `[<ds>._LANG.python].loader = "datamanifest.store.loaders:fsspec_loader"`. A
     peer-language tool reads the language-neutral `lazy_access` flag and honors it
     its own way (e.g. an `_LANG.julia.loader`), or treats the entry as unsupported.
     The flag is the cross-language contract; the loader binding is per-language.
   - A `lazy_access` entry is **external** — like `skip_download`, it is excluded
     from maintenance (delete/move are protected), from pool scan/adoption
     (`refresh --scan`, `where --scan`, `migrate` discovery), and is never recorded
     in the state file. Julia must replicate that protection.

3. **An ambiguous identifier is a fail-loud error.** `search_dataset` resolving an
   identifier (name / alias / **DOI**) that matches **more than one** dataset now
   **raises**, naming the candidates, instead of silently returning the first match
   + a warning. This matters because a DOI may be shared across several datasets (a
   Zenodo `--split` import), and silently acting on an arbitrary one of N is a
   correctness footgun. The spec should state that single-object resolution is
   exact-or-error on ambiguity (consistent with the maintenance surface, where an
   ambiguous id already requires `--batch`); Julia's `search_dataset` port must do
   the same.
