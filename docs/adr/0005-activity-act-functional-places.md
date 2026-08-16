# ADR-0005: The activity-act functional places the engine was missing

- Status: Accepted 2026-08-16
- Plan: `smd-act-defects-8.toml` (stages 2–13)

## Context

Plan `smd-act-defects-8` set out to close eight categorical gaps between the SMD/MMK
activity-ontology model documented in
[plan-activity-ontology.md](../../memory-global/leaves/plan-activity-ontology.md) and what
`scripts/agentctl/plan.py` / `state.py` actually enforced: places a functional analysis of the
activity requires, that the code model either left unrepresented or conflated with a neighbour.
This ADR is Stage 11's record of that plan: what was fixed, where, with what corpus evidence;
what was deliberately left out; the corrections earlier drafts of this same record got wrong;
the control-criterion difficulties the work itself produced while measuring its own done
criteria; and the defects the engine exhibited simply by being executed as the plan's own means.

## Decision — the eight defects fixed

Each defect is cited by the plan-file line at which its stage begins in
`smd-act-defects-8.toml`.

### Defect 1 — knowledge as a functional place of its own (stage 2, toml line 136)

Before this plan, a stage's `material_refs` conflated "what the stage transforms" with "what
the stage merely relies on and must not disturb." Stage 2 split the two into `material_refs`
(transformed) and `knowledge_refs` (relied on, left alone), added `stage.knowledge` as element
2' of the ontology, and wired the submission seam (`scripts/agentctl/submission.py`) to require
it of every substantive stage at exactly three points bytes enter a session: `submit-plan`,
`replan`'s new side, and `approve`'s in-place-edit refresh — never in the loader, which stays
lenient over plans already accepted. `NORMALIZATION_DESTINATIONS` now names all five places a
factor can be re-normed onto: `материал`, `средство`, `норма`, `способ`, `знание`.

### Defect 5 — the echo-detecting judge (stage 3, toml line 195)

An acceptance-review pass could previously be satisfied by an observation that merely restated
the expected result image. Stage 3 added a prefilter (recall ≥0.70, false-flag rate ≤0.25 against
a hand-labelled corpus) backed by the fail-open acceptance judge described below. Measured over
the 200-stage fixture: 11 of 200 stages (5.5%) carry an echo. An earlier figure of 58 of 200
(29.0%) differed largely **definitionally** rather than by refutation — the two counts were
counting different things. The 0.70 recall floor and the 0.25 false-positive ceiling were fixed
**before** the labelling and stayed unchanged after it; the stage forbids re-tightening a
threshold in response to a result, so the measurement did not tune them. What the measurement did
change is the **remedy**: at 5.5% prevalence, refusing a submission on a model's reading of what
a sentence means would spend an author's round trip on a coin flip, so the remedy was downsized
from a hard submission refusal to an author-facing warning — "an echo NEVER refuses"
(`submission.py:652`). The thresholds now govern what the warning fires on, not what the engine
refuses.

### Defect 4 — `material_refs`/`knowledge_refs` on the operative surface (stage 4, toml line 239)

`gates.py`'s `_operative_surface` — what the engine executes or dispatches on, as opposed to the
plan's prose — deliberately excludes narrative fields so that no amount of rewriting can satisfy
the CHANGE half of the replan-coverage gate. But that exclusion also made a **re-selected
material invisible** to the gate: a stage could change what it transforms and the change would
register as prose. Stage 4 admitted `material_refs`/`knowledge_refs` to the surface, which works
precisely because they are typed: they cannot be reworded, only re-declared, so a re-selection
becomes observable without the CHANGE half becoming satisfiable by prose.

### Defect 6 — preconditions as their own place (stage 5, toml line 275)

`stage.conditions` was doing double duty: the conditions the transformation runs UNDER, and the
preconditions of STARTING at all (which, before this stage, were usually just a restatement of
`depends_on`). Stage 5 split `stage.preconditions` out as its own field, required at the
submission seam, and added a restatement-refusal check — fail-open, i.e. an infra failure to run
the check never blocks submission, only a positive match does.

### Defect 7 — the element-name vocabulary (stage 6, toml line 318)

`text_shape.ELEMENT_NAMES`, the vocabulary a `Supply.element` may use to name what a stage
provides a downstream stage, was missing several of the ontology's own element names. Stage 6
completed it. Measured over the 55-plan / 200-stage corpus fixture: only 3 of 151 supply edges
(2.0%) carry a typed element name — the vocabulary is complete, but the corpus predates it and
was not retrofitted (see Deliberately not done).

### Defect 8 — the typed `Order` object (stage 7, toml line 365)

`meta.goal` was a free string; nothing distinguished the order's own requirements and coverage
from prose. Stage 7 added `state.Order` (parsed from a `[meta.order]` TOML table, with
`requirements`/`coverage`/`customer_id` sub-fields) alongside `meta.goal`, which the loader still
accepts and never refuses — `[meta.order]` is additive, not a replacement, precisely so a plan
authored before it existed keeps loading. The corpus was **not** migrated onto it (see
Deliberately not done). The bindings verified in the codebase are the loader, the submission seam,
the acceptance path's customer-id check (`cli.py:2808`) and the coverage lint; `plan-render` was
named in an earlier draft and is **not** one — `render.py` mentions "order" only in a docstring
about stage ordering.

### Defect 2 — control vs. acceptance (stage 8, toml line 453, cost_tier = large)

The engine had one path for "did the result match," conflating an objective check (element 3,
control) with a customer's subjective sign-off. Stage 8 introduced `AcceptanceReview` — bound to
the **accepted plan digest**, authored only by `[meta.order].customer_id` (a mismatch is refused
at write time, `cli.py:2808`), carrying one verdict per order requirement, and gated at
`resolution_blockers` via `_acceptance_review_resolution_blockers` — and `AcceptanceBypass`, an
explicit override gated on a non-empty `--bypass-reason` ("a bypass is a reasoned override, not a
shrug"). Together they are the typed hand-off to the customer that the plan's own reasoning
already leaned on before it was a type (see Corrections). The pre-existing stage-level
`StageReview`, bound to `sha256(observation)`, is a **different** record and predates this plan
(`e3041f4`); an earlier draft of this ADR described stage 8's work using `StageReview`'s
properties and thereby credited stage 8 with machinery that already existed. This is the
stage that produced the **139 of 4226** control-criterion difficulty and the cost-tier
under-pricing finding, both recorded below.

### Defect 3 — means (fixed instrument) vs. method/procedure (way of use) (stage 9, toml line 500, cost_tier = large)

`stage.means.method` conflated two different things a plan has to say about instruments already
fixed in `stage.means.means`: the **requirement on the way of acting** — what the transformation
must be an instance of, which is the planner's and the customer's and moves only through review
and approval — and the **sequence of operations** by which the actor satisfies that requirement,
which may legitimately vary. (`means.means`, the instrument itself, was never in question and is
untouched by this defect.) Stage 9
split `stage.means.procedure` out from `method`, added a distinctness refusal (the two fields
must not merely restate each other after normalization), and wired the field into
renormalization (`replan --renormalize` transplants only `means.procedure` onto a live stage,
never `means.means`, so the fixed instrument really does stay fixed across the renorm).
Measured: 0 of 200 fixture stages catch on the distinctness check via normalized-equality — the
refusal is real but has not yet fired against real plan prose.

## Deliberately not done

Five source distinctions this plan did not implement, named so a later plan does not have to
rediscover the gap by surprise:

1. The **content half of the goal deriving from knowledge about the material** — the engine
   records `stage.knowledge` (Defect 1) but does not derive the order's content from it; the
   content/form unity documented in the ontology leaf's `failure_address` section is asserted in
   prose, not computed.
2. The **object space and the selection of a material among candidates** — the engine records
   the material a stage was GIVEN, never a space of candidate materials or the act of choosing
   one from it.
3. **Forecast as a status of knowledge** — nothing distinguishes a knowledge claim held with
   forecast confidence from one already confirmed; `stage.knowledge` has no confidence axis of
   its own (contrast `Principle.confidence`, which is a different element).
4. The **structural link between means and conditions** — that "conditions are secured by the
   means" is stateable in this ontology but the engine has no field that represents the
   securing relation itself, only the two ends of it (`means.means`, `stage.conditions`)
   separately.
5. The **result/product distinction as a type** — stage 8's own reasoning already needs "the
   result the customer accepted" and "the product the actor actually produced" as two different
   things (an `AcceptanceBypass` can accept a product that is not quite the declared result
   image), but the engine has no type that distinguishes them; it is used in reasoning, not
   represented in code (see Corrections, second item).

## Corrections to earlier drafts of this record

Three claims an earlier draft of this ADR made were wrong, and are corrected here rather than
silently dropped, because the wrong claims were themselves evidence about how this task's own
measurement discipline had to improve mid-flight:

1. **The fixed-instrument/way-of-use distinction is implemented**, not missing. Stage 9's
   `means.procedure` split IS that distinction in code (see Defect 3 above). An earlier draft
   claimed this was still undone; it was already landed by the time that draft was written.
2. **The result/product distinction is used**, though not as a type. An earlier draft claimed
   "no typed hand-off to the customer exists." That is contradicted by stage 8's own
   `AcceptanceReview`/`AcceptanceBypass` machinery, which IS a typed hand-off — what is missing is
   only the finer result-vs-product type inside that hand-off (Deliberately-not-done item 5), not
   the hand-off itself. The earlier, broader claim is retracted.
3. **The factor-discovery re-entry cycle is not missing.** An earlier draft implied the engine had
   no path back from a discovered factor to a corrected plan. It does: a FAILED stage already
   routes to `DIAGNOSING`, which runs declare → investigate → critique → normalize and returns to
   `replan`. What is actually missing is narrower: the **link** from that existing cycle to the
   new destination axis Defect 1 introduced (`NORMALIZATION_DESTINATIONS`) is not automatic — an
   engine defect discovered mid-stage (see the eight below) still has to be appended by hand to a
   list rather than routed there structurally.

## The judges' degraded-mode behavior with no advisor reachable

**One observation from this task**, made against this machine's live config
(`advisor-mode = substantive`, per `~/.claude-agent/config.md`) — not a general property of the
environment, since a different config or a different machine could reach the advisor and never
exhibit it: with no advisor process reachable, the **fail-open** checks Defects 5 and 6 added
(the echo prefilter at stage 3, the restatement-refusal at stage 5) go **inert** — an
unreachable judge returns no advisory and the gate simply does not fire, by design, so neither
stage's guard degrades the pass into a false block. Stage 8's acceptance path is the opposite
shape: the judge itself is fail-open (a timeout records no verdict), but the **gate is
fail-closed**, at two different levels. At the stage level,
`gates.acceptance_review_blockers` reads the per-stage `StageReview` and refuses PASSED when no
matching review exists, so an unreachable advisor drives the stage toward a recorded `override`
verdict (reviewer + note). At the plan level, the accept path writes `AcceptanceBypass` once, and
an unreachable acceptance judge is answered by `--bypass --bypass-reason` rather than by silence.
The asymmetry (two checks going quiet, two gates each
forcing a recorded escape) is the direct, observed consequence of which of the two design
patterns — fail-open guard vs. fail-open-judge/fail-closed-gate — each defect's stage chose.

## The submission-vs-load seam decision

Every new requirement Defects 1 and 6 added binds at the **submission seam**
(`scripts/agentctl/submission.py`), never in the loader (`parse_plan`'s `if strict:` branches).
The reason is retroactivity: a requirement placed in the loader applies to every plan a live
session might re-read, including ones already accepted under an older norm, with no recovery
edge for a plan that fails the new check on a re-read it did not ask for. A requirement placed at
the submission seam instead binds only at the three points plan bytes actually **enter** a
session — `submit-plan`, `replan`'s new side, `approve`'s in-place refresh — so a plan already
running under the old norm keeps running unchanged, and only a plan offered as a new norm is held
to it.

**The corpus migration was declined.** The 55-plan / 200-stage fixture corpus was not retrofitted
onto the new `Order` type (Defect 8), the completed element-name vocabulary (Defect 7), or the
`means.procedure` split (Defect 3). Reasons: the loader's own leniency already makes migration
optional for continued operation (see above); a strict-load pass over the full corpus was
measured to fail on 30 of the corpus plans, and those 30 failures are pre-existing debt this
stage records, not debt this plan's stages were ordered to discharge.

## Control-criterion difficulties encountered while measuring done criteria

### Stage 8: 139 of 4226, and a tension left unresolved

Stage 8's own done-criterion measurement narrowed its `verify_command` to a named subset —
**139 of 4226** collected test IDs — via an explicit `--deselect` of
`test_hook_wiring.py::test_an_unsearchable_chain_member_is_unaccounted_for_not_absent`, the one
member of the exception set this plan could not bring to green without exceeding its own scope.
This sits in tension with an advisory lint the engine already ships for exactly this shape —
`verify_command_scope_warnings` — but not in the direction an earlier draft claimed. The lint
warns when a command runs an **aggregate suite without a scope flag**, and its advice is to
narrow ("scope it to the gate that enforces it … so pre-existing unrelated reds cannot false-fail
the stage"). So the lint pushes toward exactly what stage 8 did. The real disagreement is with
this plan's own full-suite-corroboration norm, and it is about *which* red a stage should be
allowed to see: the lint would spare a stage every red it does not own, the norm insists a stage
see the whole tree it is landing into. The two are left pointing in opposite directions here
rather than adjudicated.

**Correction to the plan's own stage count.** Eleven of the thirteen stages declared a
`verify_command` that ran a named subset rather than the full suite. Four of those eleven —
stages 8 through 11 — were widened during this plan, which is why they left the set. Seven
remain, and all seven are among stages 1–7, which are PASSED and are deliberately not reopened by
this stage. An earlier count of "eight of the thirteen" circulated in an earlier draft of the
plan's narrative; it was caught in review and is superseded by the eleven/seven figures above.

### Stages 10 and 13: `b90bade` and the `_normalize_stderr` retraction

A merge at sha `b90bade` (2026-08-11, "reconcile the material **stage 10** transforms" — not the
stage-13 reconciliation, which is `0314158` of 2026-08-16) brought a second trunk-red node onto
the branch partway through stage 10. It was admitted into the exception set at the stage-10
difficulty of 2026-08-12, and **retracted from the set** on 2026-08-13: re-measured, it was green
standalone and green in the full suite while byte-identical to `origin/main`. Its recorded cause —
a drifted traceback line number — was retracted in the same act, because `_normalize_stderr` had
already redacted that very line number at the revision the original reading was taken from, which
makes the recorded cause impossible to have produced the symptom. So the set is back to one
member. No replacement cause was established, and none is owed: the node is not red.

### The residual: cause-provenance

What the 2026-08-13 retraction leaves open, in a term coined for this record because no shorter
one already existed: **cause-provenance**. The exception set's mechanical form proves that a
named member is RED at every run — that much is checked, every time, by construction. It proves
nothing about the cause recorded beside that member. A recorded cause can be wrong (as the
retracted member's was) without the exception-set mechanism itself ever noticing, because the mechanism was built to
answer "is this member still red," not "is the reason we wrote down still the reason." This
residual is not closed by this plan.

## The ordering fault, its bound, and the currency of the overrun

The plan's five mechanical enumerators (Stage 10; see the source's own `procedure` field) sat
**tenth by index and eleventh in execution order** — downstream of every repair stage they would,
had they run earlier, have been able to feed findings back into. This is recorded as an ordering
fault in the plan's own construction, not corrected retroactively (stage 10 is PASSED and not
reopened).

**The bound on what enumeration can cover.** A mechanical enumerator only covers a defect class
whose site-set is derivable from an artifact the plan already holds (a field list, a corpus
fixture, a `--deselect` set). It cannot cover a judgment call no enumerator can decide — concretely,
the plan's own **cost-tier pricing** defect (`cost_tier = "large"` under-pricing the actual draw
of every code stage that declares it — 8, 9, 10, 11 and 13) is exactly such a judgment, and no
enumerator in this plan touches it.
The plan's own reading is that this defect "stood wrong in every code stage at once with no first
instance at all" — which is the interesting part: a defect with no first instance has no moment
at which a reviewer could have caught it by noticing a change.

**Retraction: the ordering fault is not evidenced by `effort-replan-absolute`.** An earlier draft
of this record anchored the ordering-fault's currency claim on the `effort-replan-absolute`
scale's having tripped, treating that trip as parity evidence for the cost-tier pricing defect.
That anchoring is wrong and is retracted here. The `effort-replan-absolute` scale tripped for an
unrelated reason (a sub-plan accounting seam — see the eight engine defects below, item 3) at
exactly its own floor of 3, while the two **continuous** ratio scales this task actually measured
against the 5.0x trigger sat at 1.80x (spend) and 1.89x (active wall-clock) — both comfortably
under the trigger. The correct reading is that the overrun this task actually exhibited was never
one of **size** (neither continuous scale crossed its trigger); it was one of **currency** — the
plan priced its large-tier stages in a unit (the flat per-tier label) that did not track what
those stages actually cost to execute against.

### Effort history

Stage 7's own effort record: roughly 28 USD over 3 rounds against an 84 USD estimate scaled for a
12-stage plan; the raised tiers this plan settled on carry that 12-stage figure forward to
approximately 99 USD and 405 active minutes. Code-review passes are charged on a separate,
flat-medium term and are not folded into the per-stage tier figure above, so a stage's `cost_tier`
label is not the same quantity as its iteration count. The `effort-replan-absolute` config key
(the discrete replan-count scale, distinct from the two continuous ratio scales discussed above)
is the scale that actually tripped on this task, at its floor of 3 — see the retraction above for
why that trip is not evidence for the cost-tier pricing defect.

## Eight engine defects surfaced by executing the engine

This plan's material is the engine that executes it, so every stage has a second, unpriced
product beside its declared one: defects in the executor, found only by running it. Per this
stage's own standing rule (recorded in `conditions`), a defect found in flight during any stage
is **appended to this list**, not repaired in flight and not raised as a difficulty, unless it
blocks the stage outright.

1. **`spawn-specialist.py` inlines the whole plan into a stage prompt**, so prompt size scales
   with plan size until it exceeds a spawned child's context window. This is what forced the
   `dispatch-stage-projection` sub-plan earlier in this task.
2. **`_apply_refined_stage_fields` carries a plan's criterion fields into session state by a
   hand-written enumeration**, so a field a later schema version adds is silently dropped on the
   next replan rather than carried forward.
3. **`pop-subplan` marks the originating stage PASSED with no actual, no cost, and no spawn
   count** when the sub-plan it closes supplied that stage's MEANS rather than its RESULT — the
   record left behind understates what was actually spent to reach the pass.
4. **`pop-subplan` leaves the node at EXECUTING**, a state from which `cmd_next_stage` has no
   entry edge, so the very next ordinary command after a sub-plan closes has nowhere defined to
   go.
5. **`spawn-specialist.py` reports `MALFORMED — no known return marker`** when its second-pass
   marker extractor (`AGENTCTL_MARKER_EXTRACTOR`) merely TIMES OUT after 30 seconds — a legacy
   line-start fallback exists but is offered only behind an environment variable rather than
   taken automatically on extractor failure, so a well-formed `COMPLETED:` response is reported
   identically to an empty one.
6. **A child that never reached the API is reported exactly as a child that answered and was
   mis-parsed.** The stage-9 dispatch of 2026-08-13 died on `API Error: Unable to connect to API
   (ENOTFOUND)` after 24.7 minutes and 0.68 USD, and arrived as `MALFORMED: specialist output
   contained no known return marker line`. The two states demand opposite responses — retry a
   transient network fault, diagnose a parse fault — so collapsing them into one report costs a
   wrong first move.
7. **A well-formed child is reported `MALFORMED` for emitting two markers, or one in markdown
   emphasis.** The second stage-9 dispatch returned a complete report carrying both
   `**COMPLETED:**` and `**PERMISSION-REQUEST:**`, each wrapped in emphasis; the line-start scan
   found neither. Cost: 3.66 USD and a full re-verification by hand.
8. **Context exhaustion arrives as `MALFORMED` too — and the same run shows there is no
   re-attestation path.** The stage-10 dispatch of 2026-08-16 ran 28.4 minutes and 5.52 USD and
   returned `Prompt is too long` with nothing committed. Its launch prompt was 56 003 characters,
   so the brief projection defect 1 names was already mitigated; the exhaustion was the child's
   own, after 43 Bash calls and 5 sub-agent spawns spent re-establishing that stage 10's
   deliverables were already committed and already reviewed. A stage recorded PASSED and then
   re-armed by a substantive replan is re-dispatched at full cost with no way to say
   "re-attest only": stages 9 and 10 together cost 9.86 USD to re-attest work already in the tree.

None of the eight has been repaired by this plan. They are **recorded** here, for a later plan to
pick up as its order — but recording is not filing: the plan's standing rule requires them to go
through the same channel, in the same act, under the same publication confirmation as the Core
defect below, and that channel is unreachable from this machine. So all **nine** filings — the
trunk-red Core defect and these eight — are outstanding on the user, not just the one the next
section composes a command for. Items 6 to 8 were appended by the root coordinator rather than by
this stage's executor, which observed none of them: they occurred in the parent session,
dispatching this plan's own stages.

## Filing the one remaining trunk-red Core defect

This stage's `procedure` requires filing the one Core-repository defect this task's own
measurements leave trunk-red —
`test_hook_wiring.py::test_an_unsearchable_chain_member_is_unaccounted_for_not_absent` — via
`scripts/file-difficulty.py`, after running `scripts/check-org-neutral.py` (the public-venue
neutrality check), and after asking the user to confirm publication before the filing command
actually runs, because the difficulty channel for this machine is `github` — a public venue.

**That confirmation step could not be reached, because the channel itself is unreachable from
this machine** (probed 2026-08-16): `GITHUB_TOKEN` is unset, `~/.github-token` is absent, and the
`gh` CLI is not installed. The channel's own refusal text, verbatim:

> no GitHub write token (set GITHUB_TOKEN, create ~/.github-token, or run `gh auth login`)

The fully composed filing command, so that discharging this obligation later is mechanical
rather than a re-derivation of what was to be filed:

```
python3 scripts/file-difficulty.py --target scripts/lib/hook_wiring.py --ground "probe() reports a chain member it cannot READ as ABSENT rather than as unaccounted-for, on any CPython whose Path.is_file() IGNORES EACCES and returns False: the not-on-disk branch then skips the member silently, so a root-owned mode-700 directory in the chain is counted as one that is not there. Probed 2026-08-13 in the delivery venue, uid 501, on a mode-000 directory holding a real file: python 3.14.5 returns False and the node is red, python 3.9.6 raises PermissionError errno 13 and it is green — one and the same tree. An existence the filesystem will not report is not a proven absence" --severity medium --layer core --evidence "red under the interpreter this venue runs (python 3.14.5) since the node was added at 5a3c737, NOT introduced by that commit — its own message records its premise as measured under python 3.12.3, where EACCES still propagated and the module's try/except OSError classified the member correctly; a later CPython removed the behaviour it had measured, so the redness is interpreter-relative. Re-measured red in the delivery venue 2026-08-16, exit 1 standalone. Node: scripts/tests/test_hook_wiring.py::test_an_unsearchable_chain_member_is_unaccounted_for_not_absent"
```

This act is recorded as **outstanding on the user**: filing this Core defect requires either a
`GITHUB_TOKEN`, a `~/.github-token` file, or an authenticated `gh` on this machine, none of which
this task can create for itself, and the confirmation-before-filing step this stage's own
procedure requires cannot be asked of the user until the channel is reachable to receive it.

## Consequences

- No engine behavior changes as a result of this stage; it is a documentation-only record.
- Everything this ADR leaves open — the 30 strict-load corpus failures, the nine outstanding
  filings, the cause-provenance residual, the lint-vs-norm tension — is the standing input to
  whatever plan next takes up this engine as its material. This ADR is their durable address in
  the repository, which is what makes them survivable independently of whether the difficulty
  channel is ever reachable from a given machine.

## See also

- [plan-activity-ontology.md](../../memory-global/leaves/plan-activity-ontology.md) — the 8/9-element
  model these defects were measured against.
- [scripts/agentctl/README.md](../../scripts/agentctl/README.md) — the engine internals each
  defect above was fixed inside.
