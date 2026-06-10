# Importing from other tools

`add` takes a *reference to data*; `import` ingests *another tool's catalog*.
Both end at standard manifest entries, and already-downloaded files are adopted
in place (checksum-verified) — no re-download:

```bash
datamanifest import pooch registry.txt --base-url URL --cache-dir DIR   # adopts pooch's cache
datamanifest import csv files.csv                     # a name,url,sha256 table
datamanifest import urls list.txt --base-url URL      # a plain list of URLs
datamanifest import intake catalog.yml                # an intake catalog ([yaml] extra)
datamanifest import dvc path-or-dir                   # *.dvc / dvc.lock (+ .dvc/cache)
```

Per-source detail, and the `add`-side sources (direct URLs, Zenodo/figshare/OSF
and PANGAEA DOIs, Git LFS pointers), are on the [adding datasets](adding-datasets.md) page.

## Migrating from Pooch

Already using [Pooch](https://www.fatiando.org/pooch/)? Convert the registry and
adopt your downloaded files in place:

```bash
datamanifest import pooch registry.txt \
  --cache-dir "$(python -c 'import pooch; print(pooch.os_cache("yourpkg"))')"
```

`datamanifest` covers the same fetch-and-verify ground and adds an explicit,
cross-language manifest file, a full dataset-lifecycle CLI, and the
[`@cached`](api.md#caching-computed-results) cache for your own computed
results. See [related projects](related.md) for how it compares to Pooch,
intake and pystow.
