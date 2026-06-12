<!--
  Home page. The feature bullets are pulled straight from README.md (single
  source of truth) via the include-markdown plugin; everything else links into
  the guide.
-->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup-dark.svg">
    <img src="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup.svg" alt="datamanifest.toml" height="76">
  </picture>
</p>

# datamanifest

Keep track of the datasets used in a scientific project. You declare each
dataset in a **manifest** — a plain TOML file committed alongside your code —
and three clients read and write it the same way: the `datamanifest`
command line and the Python library (both in the PyPI package
[datamanifestpy](https://pypi.org/project/datamanifestpy/)), and the Julia
package [DataManifest.jl](https://github.com/awi-esc/DataManifest.jl). The
manifest format itself is defined by a shared, language-agnostic
[spec](https://github.com/perrette/datamanifest.toml), so a project's manifest
works across all three.

{%
  include-markdown "../README.md"
  start="<!-- intro-start -->"
  end="<!-- intro-end -->"
%}

## Get started

Code examples across this site come in tabs — pick your client once (CLI,
Python or Julia) and every page follows.

=== "CLI"

    ```bash
    pipx install datamanifestpy
    datamanifest init
    datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
    datamanifest path co2              # the on-disk path, downloaded and verified
    ```

=== "Python"

    ```python
    # pip install datamanifestpy
    import datamanifest

    db = datamanifest.Database("datamanifest.toml")
    db.add("https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv", name="co2")
    df = db.load_dataset("co2")             # downloaded and verified, then loaded
    ```

=== "Julia"

    ```julia
    # using Pkg; Pkg.add("DataManifest")
    using DataManifest

    db = read_dataset("datamanifest.toml")
    add(db, "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"; name="co2")
    path = get_dataset_path(db, "co2")      # the on-disk path, downloaded and verified
    ```

=== "Manifest"

    ```toml
    [co2]
    checksum = "sha256:0058b3788040b5c27b2b5c1dd6d26226b7e4deef85e34c153e64806c37df7c75"
    uri = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv"
    ```

Three ways in, one manifest:

- **Use it from the shell** — [install the CLI](installation.md) and follow the
  [quickstart](quickstart.md); the [CLI reference](cli.md) covers every command
  and flag.
- **Use it from Python** — [Using it from your code](api.md) walks through
  `load_dataset`, the `@cached` decorator and the file-less `Database`; the
  [Python API reference](python-api.md) lists everything.
- **Use it from Julia** — the quickstart and guide pages carry Julia tabs for
  the essentials; the [Julia API reference](julia-api.md) lists everything,
  and the Julia walkthrough lives in the
  [DataManifest.jl repository](https://github.com/awi-esc/DataManifest.jl/blob/main/docs/doc.md).

This site documents the CLI and the Python library in full, and the essentials
for Julia.

## Guide

- [Use cases](use-cases.md) — the CLI workflows end to end: add, repair, store, sync.
- [Storage model](storage.md) — where data lives on disk and how to centralize it.
- [Configuration](configuration.md) — the config scopes and every setting.
- [Adding datasets](adding-datasets.md) — direct URLs, Zenodo / PANGAEA DOIs, object stores.
- [Importing from other tools](importing.md) — pooch, intake, DVC, CSV/URL lists.
- [Language bindings](language-bindings.md) — one manifest across Python and Julia.
- [Related projects](related.md) — the DataManifest family, and Python alternatives.

## From the same author

A few other open-source tools I maintain.

**Scientific writing & data**

- [**texmark**](https://perrette.github.io/texmark/) — write scientific articles in Markdown and convert them to journal-ready LaTeX/PDF.
- [**papers**](https://perrette.github.io/papers/) — command-line BibTeX bibliography and PDF library manager.

**Speech to Text (dictate) and Text to Speech (read-aloud) tools**

- [**scribe**](https://perrette.github.io/scribe/) — speech-to-text dictation.
- [**bard**](https://perrette.github.io/bard/) — text-to-speech reader.

## Development

- [Conformance](conformance.md) — the shared manifest format and what this implementation supports.
- [Roadmap](roadmap.md) — parked ideas and deferred decisions.
