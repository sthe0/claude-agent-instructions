---
name: abandonment-is-last-resort-for-mechanization
description: When critique traces a difficulty to a mechanization's own cost (false positives, friction) rather than to it being wrong in principle, full reversion to prose/manual must be weighed against a narrowed version and named to the user as an autonomy-reducing trade-off — not defaulted to as "the pragmatic choice."
type: feedback
schema: leaf/v1
created: 2026-09-18
last_verified: 2026-09-18
---

# Abandonment is a last resort for a working mechanization

## Difficulty

A built, wired, and observed-firing mechanization (a hook, an engine gate, a determinized rule) can still fail in production on **cost**, not correctness: it did exactly what it was built to do, and that turned out to be too expensive (too many false positives, too much friction) relative to the risk it guards against. When critique diagnoses that kind of failure, "revert the whole mechanism, fall back to the prose-only norm it replaced" is the cheapest fix to reason about and the fastest to land — but it is also the single costliest option on the coordinator's own **autonomy** axis ([[coordinator-objective]]): it converts a decidable rule the engine enforced automatically back into something only the user's own vigilance (or a future re-litigation) can catch, i.e. it *increases* the user's own involvement rather than reducing it.

Concrete precedent (the permission-self-grant-gate hook, `hook-guard-permission-self-grant.py`, 2026-09-16/18): the hook was built, landed, wired, and its live-run witness confirmed a genuine armed-and-relevant deny — full success against its own done criteria. In production it then denied ordinary `git`/`python3` Bash calls across parallel sessions at a rate nobody had budgeted for, and was reverted the next day, ~22 hours later, deleting the script, its 1925-line test suite, and its live-run evidence in full — nothing of the mechanization survived, not even unwired. The user approved that closure at the time, then — asked afterward for the reason behind a 2/5 quality rating on the same task — named exactly this: *"the proposed plan's solution in fact moved the whole work into manual mode, which fully contradicts one of the purposes of the agent's existence: automating work and reducing my own involvement."* Nothing in the diagnosis had put a **narrower, tunable** version of the gate (a smaller trigger set, a fail-open branch on the ambiguous case, a canary/staged rollout, a warn-then-block ramp) on the table as a named alternative next to full reversion — the choice as framed was "broken-as-shipped vs. back to prose," and the second option was presented as the safe/pragmatic pick rather than as the autonomy cost it actually is.

## Guidance

When `overcome-difficulty`'s critique phase (`[[overcome-difficulty]]` § 3) traces a mechanization's failure to **its own operating cost** rather than to a wrong design premise — `--failure-address ресурсное`, typically, since the norm itself (the rule the mechanism enforces) is usually still correct; only its *implementation's* blast radius was wrong — the replanning task must not jump straight to full reversion:

- **Evaluate a narrowed version first.** Could the observed cost be removed by tightening the trigger condition, adding a fail-open branch on the specific ambiguous case that caused the false positives, staging the rollout behind a canary/warn-only period, or rate-limiting the deny? If a narrowing plausibly preserves most of the mechanized coverage while removing the observed cost, it is cheaper on the autonomy axis than reversion and must be evaluated, not skipped because reversion is quicker to write.
- **Name full reversion for what it costs, not just what it avoids.** If reversion is still the right call (the narrowed version can't be made safe, or the residual risk genuinely doesn't warrant further engineering), say so — but state explicitly, in the replanning task and in what reaches the user, that this trades the mechanized/automated enforcement back for a prose-only norm the user (or a future session) must re-notice on their own. Folding that trade into "the safe/pragmatic fix" without naming it is what produced the low rating in the precedent above.
- **Put both options to the user explicitly** via `AskUserQuestion` when the choice is close enough to be genuinely open — "narrow and re-land" vs. "revert to prose" — rather than pre-deciding on the coordinator's own judgment of cost and presenting only the chosen path for approval. This mirrors [[recurring-normalize-factor-is-architecture-signal]]'s rule for a recurring normalize factor, applied here to the *first* occurrence of a specific failure shape: reversion of a mechanization, because reversion is categorically expensive enough (it deletes working automation) to warrant the same explicit-alternative treatment even on round one.

This is a perception-heavy judgment (is the observed cost narrowable, or fundamental?) and stays a model call — the mechanizable half is only the presence of the choice in the ask, which `AskUserQuestion` already structurally enforces once the coordinator remembers to frame the options that way.

## See also

- [[overcome-difficulty]] § 3 Critique — the phase this leaf constrains; the `--failure-address ресурсное` case specifically.
- [[coordinator-objective]] — the autonomy/user-involvement axis this trade-off is measured against.
- [[recurring-normalize-factor-is-architecture-signal]] — the sibling rule for a *recurring* factor; this leaf covers the categorically-expensive first occurrence (full reversion of working automation) instead of waiting for a second instance.
