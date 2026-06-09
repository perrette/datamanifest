# Related projects

## The DataManifest family

The [`datamanifest.toml` format](https://github.com/perrette/datamanifest.toml)
is shared across languages:
[`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl) (Julia,
same author) reads the same manifest via the `_LANG` namespace. See
[conformance](conformance.md) for the shared format and what this implementation
supports, and [language bindings](language-bindings.md) for the `_LANG`
mechanism.

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
