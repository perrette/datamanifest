# Roadmap / parked ideas

Deferred design decisions and enhancements, captured so they aren't lost. None of
these is committed work; each needs its own pass.

## List-valued `datasets_dir` / `datacache_dir` as sugar

Accept a list value for `datasets_dir` / `datacache_dir` as **pure desugaring**:
element 0 is the write dir, the rest are *prepended to the resolved read pools*,
while keeping the separate `*_pools` keys and the undefined⇒default-pools rule
intact. `$datasets_dir` still resolves to a scalar (element 0). One read-site
branch; no spec polymorphism on `*_pools`, no symbol ambiguity. The separate
`*_pools` keys are what's implemented; this would be an optional ergonomic
addition on top.

## Cross-language decisions to propagate

See [`design-remote-protocols.md`](https://github.com/perrette/datamanifest/blob/main/design/design-remote-protocols.md) → "Decisions to
propagate to the spec repo + DataManifest.jl": object-store URI schemes,
`lazy_access`, and the ambiguous-identifier fail-loud rule.
