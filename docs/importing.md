# Importing from other tools

`add` takes a *reference to data* (a URL or DOI); `import` ingests *another
tool's catalog file*. Both produce standard manifest entries, and
already-downloaded files are **adopted** in place — verified against their
checksum and recorded, with no re-download:

```bash
datamanifest import pooch registry.txt --base-url URL --cache-dir DIR   # adopts pooch's cache
datamanifest import csv files.csv                     # a CSV with url (+ name, sha256) columns
datamanifest import urls list.txt --base-url URL      # a plain list of URLs
datamanifest import intake catalog.yml                # an intake catalog ([yaml] extra)
datamanifest import dvc path-or-dir                   # *.dvc / dvc.lock (+ .dvc/cache)
```

Per-source detail, and the `add`-side sources (direct URLs, Zenodo and PANGAEA
DOIs), are on the [adding datasets](adding-datasets.md) page.

## Coming from Pooch

If you use [Pooch](https://www.fatiando.org/pooch/), one command converts the
registry and adopts your downloaded files in place:

```bash
datamanifest import pooch registry.txt \
  --cache-dir "$(python -c 'import pooch; print(pooch.os_cache("yourpkg"))')"
```

`datamanifest` covers the same fetch-and-verify workflow, with an explicit,
cross-language manifest file, a dataset-lifecycle CLI, and the
[`@cached`](api.md#caching-computed-results) decorator for your own computed
results. See [related projects](related.md) for a comparison with Pooch,
intake, and pystow.
