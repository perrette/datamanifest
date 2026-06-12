# Related projects

## The DataManifest family

The `datamanifest.toml` manifest format is defined by a shared,
language-agnostic spec,
[github.com/perrette/datamanifest.toml](https://github.com/perrette/datamanifest.toml),
with two implementations:

- **`datamanifest`** (this package, Python);
- [**`DataManifest.jl`**](https://github.com/awi-esc/DataManifest.jl) (Julia),
  by the same author.

Both read and write the same manifest, so one file can serve a mixed
Python/Julia project — see [language bindings](language-bindings.md).

This site is the documentation for the ecosystem: the
[manifest format](manifest-format.md) and its
[specification](manifest-spec.md), the CLI, the Python library, the
cross-language pages, and the [Julia API reference](julia-api.md).
Julia-specific guides live as Markdown in the
[DataManifest.jl repository](https://github.com/awi-esc/DataManifest.jl/blob/main/docs/doc.md).

## From the same author

A few other open-source tools I maintain.

**Scientific writing & data**

- [**texmark**](https://perrette.github.io/texmark/) — write scientific articles in Markdown and convert them to journal-ready LaTeX/PDF.
- [**papers**](https://perrette.github.io/papers/) — command-line BibTeX bibliography and PDF library manager.

**Speech to Text (dictate) and Text to Speech (read-aloud) tools**

- [**scribe**](https://perrette.github.io/scribe/) — speech-to-text dictation.
- [**bard**](https://perrette.github.io/bard/) — text-to-speech reader.

## Python alternatives

- [`fatiando/pooch`](https://www.fatiando.org/pooch/) — the established tool for
  fetching and verifying data from Python code (it backs SciPy, scikit-image,
  and many others). `datamanifest` covers that ground and centers on three
  things Pooch doesn't aim for: an explicit, cross-language manifest file as the
  single source of truth; a CLI that manages the whole dataset lifecycle — add,
  verify, repair, sync — without touching code; and the
  [`@cached`](api.md#caching-computed-results) cache for your own computed
  results — orthogonal to fetching, but sharing the same storage and
  bookkeeping. Already using Pooch?
  `datamanifest import pooch registry.txt --cache-dir "$(python -c 'import pooch; print(pooch.os_cache("yourpkg"))')"`
  converts the registry and adopts your downloaded files in place
  ([importing](importing.md)).
- [`intake`](https://intake.readthedocs.io) — catalog of data sources with
  drivers that load into pandas/xarray/dask; overlaps with the loader half of
  `datamanifest`.
- [`cthoyt/pystow`](https://github.com/cthoyt/pystow) — lightweight reproducible
  download + cached storage with an OS-appropriate data dir; code-driven rather
  than manifest-driven.

## Julia alternatives

Single-language counterparts on the Julia side:

- [`DataDeps.jl`](https://github.com/oxinabox/DataDeps.jl) —
  download-on-first-access with checksum verification; registration lives in
  code rather than a manifest file.
- [`DataToolkit.jl`](https://discourse.julialang.org/t/ann-datatoolkit-jl-reproducible-flexible-and-convenient-data-management/104757) —
  the most comparable: a rich, declarative data-management ecosystem with lazy
  loading and a broad driver set. It allows in-config code via its meta
  `@syntax`, where DataManifest prefers references to external code.
- [`DrWatson.jl`](https://juliadynamics.github.io/DrWatson.jl/dev/) — broader
  scientific-project organization (simulations, file layout, naming), of which
  data handling is one part.
- [`RemoteFiles.jl`](https://github.com/helgee/RemoteFiles.jl) — keep a local
  file in sync with a remote URL.
- Pkg Artifacts (`Artifacts.toml`) — Julia's built-in TOML manifest of
  content-addressed, hash-pinned data/binary bundles tied to packages.

As a rule of thumb: for code-driven download-and-checksum alone, DataDeps.jl
is lighter; for a rich declarative data ecosystem, DataToolkit.jl is richer;
DataManifest.jl targets multi-dataset, multi-language projects that want the
whole dependency declaration — and the derived-data cache — in one shareable
file.

## Acknowledgments

`datamanifest` is a Python port of
[`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written
by the same author (Mahé Perrette). The Python port was implemented with
assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
