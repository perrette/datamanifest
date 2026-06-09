<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup-dark.svg">
    <img src="https://raw.githubusercontent.com/perrette/datamanifest.toml/main/design/logo/lockup.svg" alt="datamanifest.toml" height="76">
  </picture>
</p>

# datamanifest[py]

[![pypi](https://img.shields.io/pypi/v/datamanifestpy)](https://pypi.org/project/datamanifestpy)
![python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fperrette%2Fdatamanifest%2Frefs%2Fheads%2Fmain%2Fpyproject.toml)
[![CI](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml/badge.svg)](https://github.com/perrette/datamanifest/actions/workflows/ci.yaml)
[![docs](https://img.shields.io/badge/docs-perrette.github.io%2Fdatamanifest-blue)](https://perrette.github.io/datamanifest/)

Keep track of the datasets used in a scientific project. You declare your data
dependencies — URLs, git repositories, checksums, formats — in a
`datamanifest.toml` file; `datamanifest` downloads, verifies, extracts and loads
them, and caches your own computed results with the same machinery.

<!-- intro-start -->
- **One manifest, committed to git.** Your data dependencies — URLs, DOIs,
  checksums, formats — live in a plain, hand-editable `datamanifest.toml`. Commit
  the recipe; the bytes and per-machine state stay git-ignored, so a collaborator
  clones and runs `datamanifest download`.
- **Declare on the CLI, consume from code.** The CLI manages the project's data —
  add, verify, repair, sync — without touching code; your analysis code just asks
  for data by name (`datamanifest.load_dataset("co2")`) and never edits the
  manifest.
- **Fetch, verify, extract — from anywhere.** Direct URLs, Zenodo/figshare DOIs,
  git repos, object stores (`s3://`, `gs://`, …), and bulk imports from pooch,
  intake or DVC, all checksum-verified and adopted in place when already on disk.
- **Cache your own results too.** A `@cached` decorator stores expensive
  computations keyed by their arguments, sharing the same storage and bookkeeping
  as fetched data.
- **Shared across languages.** The `datamanifest.toml` format is
  [shared across languages](https://github.com/perrette/datamanifest.toml), so
  implementations in other languages (today Julia) read the same file.
<!-- intro-end -->

## Installation

```bash
pip install datamanifestpy
```

With optional loader backends (`csv`, `parquet`, `nc`, `yaml`, `fsspec`, or
`all`):

```bash
pip install "datamanifestpy[all]"
```

See the [installation page](https://perrette.github.io/datamanifest/installation/)
for the per-backend details.

## Quickstart

```bash
datamanifest init                  # create datamanifest.toml here
datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
datamanifest list                  # what's tracked, and where it lives
datamanifest path co2              # resolve the on-disk path (for a script)
```

Then load it from your code:

```python
import datamanifest

df = datamanifest.load_dataset("co2")          # download on first use, then load
path = datamanifest.get_dataset_path("co2")    # just the on-disk path
```

**Commit `datamanifest.toml`** — the recipe of what to fetch and how. The data
and a local `.datamanifest-state.toml` stay git-ignored; a collaborator clones
and runs `datamanifest download`. See the
[quickstart](https://perrette.github.io/datamanifest/quickstart/) for the full
walkthrough.

## Documentation

Full documentation lives at **<https://perrette.github.io/datamanifest/>**:

- [Installation](https://perrette.github.io/datamanifest/installation/)
- [Quickstart](https://perrette.github.io/datamanifest/quickstart/)
- [Using it from your code](https://perrette.github.io/datamanifest/api/) — `load_dataset`, `@cached`, the file-less `Database`
- [Use cases](https://perrette.github.io/datamanifest/use-cases/) — add, repair, store, sync
- [CLI reference](https://perrette.github.io/datamanifest/cli/)
- [Storage model](https://perrette.github.io/datamanifest/storage/)
- [Adding datasets](https://perrette.github.io/datamanifest/adding-datasets/) · [Importing from other tools](https://perrette.github.io/datamanifest/importing/)
- [Language bindings](https://perrette.github.io/datamanifest/language-bindings/) · [Related projects](https://perrette.github.io/datamanifest/related/)

## From the same author

A few other open-source tools I maintain.

**Scientific writing & data**

- [**texmark**](https://perrette.github.io/texmark/) — write scientific articles in Markdown and convert them to journal-ready LaTeX/PDF.
- [**papers**](https://perrette.github.io/papers/) — command-line BibTeX bibliography and PDF library manager.

**Speech to Text (dictate) and Text to Speech (read-aloud) tools**

- [**scribe**](https://perrette.github.io/scribe/) — speech-to-text dictation.
- [**bard**](https://perrette.github.io/bard/) — text-to-speech reader.

## Acknowledgments

`datamanifest` is a Python port of
[`awi-esc/DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl), written
by the same author (Mahé Perrette). The Python port was implemented with
assistance from [Anthropic's Claude](https://www.anthropic.com/claude).
