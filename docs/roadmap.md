# Roadmap / parked ideas

Deferred design decisions and enhancements, captured so they aren't lost. None of
these is committed work; each needs its own pass.

## `list` action verbs as REMAINDER-forwarding flags

Reshape the `list` maintenance actions so `list` doesn't duplicate each action
command's options. Keep the `--delete` / `--move` / `--push` / `--pull` flags (not
verbs), but give each `nargs=argparse.REMAINDER`: everything after the action flag
is captured raw and forwarded to that command's own parser, applied to the `list`
selection.

```
datamanifest list --cached --orphan --delete --dry-run --prune
datamanifest list --datasets --older-than 30d --move /archive --dry-run
datamanifest list --outside --push user@hpc
```

- Selection flags come before the action flag; the action flag owns the tail.
- The four action flags sit in a mutually-exclusive group (one action per run).
- Implementation: define each action's options **once** in a shared helper; the
  standalone command = `id` + those opts, the `list` path parses the captured tail
  with *just* those opts (no `id` — the `list` selection replaces it) and feeds the
  selected objects into the shared `_maintain` / sync engine.
- The one real refactor: make the forwarded verb parse its tail without an `id`.

Goal: remove the `--delete`/`--move`/`--push`/`--pull` option duplication from the
`list` parser; one definition site per action.

## List-valued `datasets_dir` / `datacache_dir` as sugar

Accept a list value for `datasets_dir` / `datacache_dir` as **pure desugaring**:
element 0 is the write dir, the rest are *prepended to the resolved read pools*,
while keeping the separate `*_pools` keys and the undefined⇒default-pools rule
intact. `$datasets_dir` still resolves to a scalar (element 0). One read-site
branch; no spec polymorphism on `*_pools`, no symbol ambiguity. (Investigated;
keep design A — separate `*_pools` keys — for now; this is the optional ergonomic
affordance to add later if wanted.)

## Cross-language decisions to propagate

See [`design-remote-protocols.md`](design-remote-protocols.md) → "Decisions to
propagate to the spec repo + DataManifest.jl": object-store URI schemes,
`lazy_access`, and the ambiguous-identifier fail-loud rule.
