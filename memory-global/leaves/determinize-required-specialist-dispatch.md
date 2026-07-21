---
name: determinize-required-specialist-dispatch
description: The review_dispatch plugin proactively names two engine-required, non-stage-actor specialist spawns (thinker @ plan-review, code-reviewer @ a spawn:developer stage) at the event that mints each obligation, in front of a pre-existing reactive gate it never weakens.
schema: leaf/v1
type: reference
created: 2026-07-22
last_verified: 2026-07-22
---

## Difficulty

The engine already **structurally requires** two specialist invocations that are not plan stages — a thinker plan-review before `approve`, and a code-reviewer review before a `spawn:developer` stage records PASSED — and both already carry a REACTIVE precondition (`gates.plan_review_blockers` refuses `approve`/`replan`/`present-plan --kind essence`; `gates.code_review_blockers` refuses `record-result --status passed`). But nothing PROACTIVELY names the required spawn at the moment each obligation is minted, so the coordinator has to remember, from prose, to spawn the specialist before the gate is reached — the exact "rule determinized, perception still doing the remembering" split CLAUDE.md's "separate rule from perception" principle flags as immature. `gates.plan_review_blockers` already proved the *rule* half was state-decidable; only its *trigger* half was missing.

## Guidance

**The pattern: pair an active trigger with an existing (or newly built) gate, never replace it.** A single plugin, `review_dispatch` ([`scripts/agentctl/plugins_review_dispatch.py`](../../scripts/agentctl/plugins_review_dispatch.py)), covers both slots through one module-level table — `_SLOT_SPECIALIST = {"plan_review": "thinker", "code_review": "code-reviewer"}` — the extension seam for any future non-stage-actor specialist obligation: add one table entry and one observer function, touch nothing else.

- **`plan_review` slot** — observes `submit_plan` (the event that mints the obligation, at `PLAN_READY`), reuses `gates.plan_review_blockers` verbatim, and emits a blocking `PluginDirective` naming the thinker spawn when non-empty.
- **`code_review` slot** — a full new stack: `state.CodeReview` (a per-stage, verdict-bearing, content-bound record — schema 21), `gates.code_review_active` + `gates.code_review_blockers` (a pure internal precondition, deliberately absent from `gates.GUARDIANS` — same charter as `gates.acceptance_review_blockers`), `cli.cmd_code_review` (the recorder), and the fold-in at `cmd_record_result` right after the pre-existing free-text `needs_control()` floor. The `review_dispatch` observer for this slot fires on `dispatch` — the event that mints the obligation once developer code exists to review — reusing `gates.code_review_blockers` verbatim.

**Why `dispatch` and not `replan`.** An `Observer`'s signature is `(state, bag)`, never `args.plan` — the corrected plan a `replan` call applies — and `cmd_replan` early-returns *without* `store.save` on a rejected replan, so a `replan` observer would reload stale on-disk state via `_fire_plugins`'s `store.load` and could name the wrong plan version; `cmd_replan`'s own inline rejection already names the correct target reactively. `cmd_dispatch`, by contrast, saves state before returning on every non-preview path, so the reload always reflects the just-dispatched active stage — a safe target with no staleness risk. This is the load-bearing reason the two slots use different observed events even though both are "proactive trigger in front of a reactive gate."

**Scope fence, held exactly.** Neither slot adds a core `Node`, a `gates.GUARDIANS` entry, or changes `machine.TRANSITIONS`; `cli.COMMANDS` gains only the `code-review` recorder verb. `review_dispatch.gates == {}` — enforcement stays entirely in the two pre-existing pure gates; the plugin's `blocking=True` is an advisory *label* on the directive, not a control-flow branch (nothing in `cli` reads `.blocking`). The pre-existing free-text `--control` floor on a `spawn:developer` stage is kept independent of the new structured `CodeReview` gate — the two never auto-satisfy each other, and a non-substantive / knob-off session's `record-result` behaviour is byte-identical to before this change.

**Reuse over invention.** The `code_review` slot's record/gate/recorder shape is a direct copy of the acceptance-review sibling (`state.StageReview`, `gates.acceptance_review_blockers`, `cli.cmd_stage_review`) — the one genuine divergence (Op-Q1) is that `gates.py` is `ast_purity`-pure (no subprocess/socket reach), so the binding target is a caller-supplied recorded digest (`code_sha256`), never a git sha recomputed inside the gate.

## See also

- [`scripts/agentctl/README.md`](../../scripts/agentctl/README.md) § Plugins — the `review_dispatch` registration, and § Commands rows `submit-plan`/`plan-review`/`approve`/`dispatch`/`record-result`/`code-review`.
- [[question-provenance-gate]] — the `premise` plugin, the immutable template this plugin's `plan_review` slot and `auto_activate` predicate mirror.
