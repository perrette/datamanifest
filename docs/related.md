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

This site is the main documentation for the ecosystem: the manifest format,
the CLI, the Python library, and the cross-language pages. The
[DataManifest.jl site](https://awi-esc.github.io/DataManifest.jl/) hosts the
Julia API reference and a Julia-specific quickstart.

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

## Acknowledgments

`datamanifest` is a Python port of
[`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written
by the same author (Mahé Perrette). The Python port was implemented with
assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
