# Related projects

A few related tools I maintain, useful in a Markdown-based scientific workflow.

**Scientific writing & data**

- [**texmark**](https://perrette.github.io/texmark/) — write scientific articles in Markdown and convert them to journal-ready LaTeX/PDF.
- [**papers**](https://perrette.github.io/papers/) — command-line BibTeX bibliography and PDF library manager.
- [**datamanifest**](https://perrette.github.io/datamanifest/) — declarative, reproducible dataset management. *(See also the [datamanifest.toml](https://perrette.github.io/datamanifest.toml/) format spec and the [DataManifest.jl](https://awi-esc.github.io/DataManifest.jl/) Julia port.)*

**Voice helpers** — handy for dictating and proofreading drafts by ear

- [**scribe**](https://perrette.github.io/scribe/) — speech-to-text dictation (Whisper).
- [**bard**](https://perrette.github.io/bard/) — text-to-speech reader (Kokoro / Piper).

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
