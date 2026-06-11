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

# datamanifest[py]

Keep track of the datasets used in a scientific project.

{%
  include-markdown "../README.md"
  start="<!-- intro-start -->"
  end="<!-- intro-end -->"
%}

## Get started

```bash
pip install datamanifestpy
datamanifest init
datamanifest add https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.csv --name co2
```

```python
import datamanifest
df = datamanifest.load_dataset("co2")   # download on first use, then load
```

- **[Installation](installation.md)** — the package and its optional loader backends.
- **[Quickstart](quickstart.md)** — declare your first dataset and load it.
- **[Using it from your code](api.md)** — `load_dataset`, the `@cached` decorator, the file-less `Database`.
- **[CLI reference](cli.md)** — every command and flag.

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
