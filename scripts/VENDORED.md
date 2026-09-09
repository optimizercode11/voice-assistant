# Vendored harness

These four scripts are copied from `/mnt/inference-engine/scripts/` at commit
`890e8e4`, unmodified:

* `guarded-run` — device/CPU containment and the manifest every label writes;
* `guarded-hostrun` — sync one checkout to the host and run through it;
* `guardrail-check` — decide whether recorded evidence actually closes a profile;
* `worktree-fingerprint` — hash the source that evidence claims to describe.

They are copied rather than symlinked or referenced for one reason: a run's
manifest binds the source fingerprint of **the tree it ran from**.  Pointing at
the engine repo's copies would make this repo's evidence claim a fingerprint of
a tree it is not.

They are also the reason `worktree-fingerprint` includes `scripts/*` in the
fingerprint: evidence recorded through a guard must go stale when the guard
changes, or a reverted guard is indistinguishable from a re-run.

**Re-sync when the engine harness changes**, and treat a harness change as
invalidating this repo's recorded evidence — that is the intended behaviour, not
an inconvenience:

```bash
cp /mnt/inference-engine/scripts/{guarded-run,guarded-hostrun,guardrail-check,worktree-fingerprint} scripts/
```

Note what the fingerprint does **not** cover: `.html` and `.js` are not in its
extension list, so `web/chat.html` and `web/chat.js` are not bound by
`GUARD_SOURCE_FP`.  Any gate that claims something about the page must hash
those files explicitly, the way the voice-chat campaign did.
