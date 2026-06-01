# The `datamanifest.toml` schema

`datamanifest` (Python) and [`DataManifest.jl`](https://github.com/awi-esc/DataManifest.jl)
(Julia) read and write the *same* TOML manifest: one table per dataset (plus an
optional `_LOADERS` table), with a set of common `DatasetEntry` fields and a small,
language-specific extension surface. Because the common fields are identical and each
tool ignores the other's extension keys, a single `datasets.toml` can be consumed by
either implementation. The normative spec lives in its own repository so neither
implementation owns it.

See: https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md

## Cross-reference

| Concern | Julia — `DataManifest.jl` | Python — `datamanifest` | Schema spec |
|---|---|---|---|
| Implementation | [awi-esc/DataManifest.jl](https://github.com/awi-esc/DataManifest.jl) | [perrette/datamanifest](https://github.com/perrette/datamanifest) | — |
| Download-phase code hook | `julia=` (inline code) + `julia_modules=` | `python=` / `callable=` (entry-point ref, no inline exec) + `python_includes=` | [Extensions](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#extensions) |
| Common fields | `Databases.jl` `DatasetEntry` | `database.py` `DatasetEntry` | [Common fields](https://github.com/perrette/datamanifest.toml/blob/main/SCHEMA.md#common-fields) |
