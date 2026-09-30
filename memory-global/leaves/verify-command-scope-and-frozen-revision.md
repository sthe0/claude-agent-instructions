---
name: verify-command-scope-and-frozen-revision
description: a plan stage's verify_command must scope tolerance/strictness to ids/fields it itself changed, and read historical claims from a frozen revision, not the live tree
type: feedback
created: 2026-09-30
last_verified: 2026-09-30
---

## Difficulty

A stage's `verify_command`, once written, keeps running against a working tree that later stages keep editing. Two axes get conflated when the check is authored loosely:

1. **Ownership scope.** "Does the working tree still match what I pushed" is not the same claim as "does every field on every id I care about still match" — the former is correct only when narrowed to the ids/fields the stage **itself** added or set. A later, separately-authorized stage may legitimately add a NEW field to an id the earlier stage never touched (e.g. a customer-directed billing-routing override added by a follow-up commit on the same branch). If the earlier stage's tolerance/strictness loop iterates every id in the section instead of just its own additions, that legitimate downstream extension reads as a regression the earlier stage caused.
2. **Time scope.** An assertion that states what the stage itself certified at push time ("this is what I claimed back then") must dereference a **frozen revision reference** (a VCS show-content-at-revision call, pinned to the exact commit the stage pushed), never a live `open()` on the mutable working tree — otherwise the assertion silently re-evaluates against whatever the tree currently says, and a claim that was true at push time can become factually stale while the check keeps mechanically passing (or, worse, starts failing for the wrong reason).

Concrete instance (a substantive multi-stage plan doing a production config edit): an additive stage added new entries to a shared broker/routing config and pushed a pinned revision `h`. A later, customer-authorized commit on the same branch added an override field to four pre-existing entries — none of which the additive stage had itself added. That stage's own verify_command wrongly (a) tolerance-listed one of those pre-existing entries as if the stage owned it, and (b) read a historical claim about its own change from a live `open()` instead of a frozen-revision helper already defined in the same script. The mirror-image bug existed in the stage that verifies the **final landed state**: it applied strict byte-equality to those same four entries with no exception for the now-authorized override, guaranteeing a red result once the extension became part of what actually ships.

## Guidance

- Define, per additive-edit stage, the exact set of ids/fields it itself introduces (a `NEW` set) and scope every tolerance/strictness assertion in that stage's `verify_command` to that set only. State explicitly in the stage's `done_criterion`/`invariants` that it makes **no claim, tolerant or strict**, about any id it did not add — that is the job of whichever later stage verifies the fully-landed state.
- The stage that verifies the **final landed state** (post-merge/post-deploy) is the correct place to carry a tolerance exception for a later, separately-authorized extension to a shared artifact — not the stage that only verifies its own narrow addition.
- Any assertion inside a `verify_command` that states a historical fact about what an earlier pinned revision contained must read that content via a frozen-revision helper (a VCS show-content-at-revision call), never a plain filesystem `open()` against the current working tree. Watch for `subprocess.run` without `text=True` — such a helper returns `bytes` and callers must `.decode('utf-8')` before using string methods.
- When narrowing a scope inside a shared multi-occurrence code block (e.g. two near-identical lines differing only by a variable-name prefix), do not blindly `replace_all` — check whether the near-duplicate occurrence checks a *different* field that was never touched by the same later change; if so it does not need the same fix, and forcing symmetry would be a needless additional edit.

## See also

- [[recurring-normalize-factor-is-architecture-signal]] — when the same underlying coupling recurs across normalize cycles, the fix is a structural alternative, not another patch.
- [[plan-control-criterion-hygiene]] — sibling authoring norms for control criteria (declared venue, no unverified current-behaviour fact, bounded counts, no self-rewrite).
