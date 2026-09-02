---
name: plan-control-criterion-hygiene
description: Seven plan-authoring norms for a stage's control criterion — declare the venue a check observes instead of hard-coding a `cd` into the verify_command; never let a criterion assert an unverified fact about current behaviour; take a criterion's number from an explicitly bounded invocation; never let a procedure step rewrite the criterion it is measured by; name the lifecycle state the criterion describes, because verify-final re-runs a criterion authored pre-merge in the post-merge world; sweep exact-shape criteria after ANY revision round, formal replan or ad-hoc; and dry-run the verify_command's own script text against live repo state before submit_plan, because a broken check script is a distinct failure class from a false factual claim.
type: feedback
schema: leaf/v1
created: 2026-08-31
last_verified: 2026-09-02
---

# Plan control-criterion hygiene

## Difficulty

To achieve a stage check that can actually go red for the right reason and
green for the right reason, the criterion has to survive the interval between
plan authoring and stage execution — and the executor has to be able to run it
without repairing it. Seven distinct authoring habits break that, and all
seven were observed live: three of them inside the very plan whose fourth
stage first recorded this leaf, the sixth inside that same plan's own
resolution — a stale exact-shape criterion caught only at `verify-final`,
after two further, fully legitimate review rounds had already moved the
ground it stood on — and the seventh recurring three separate times inside
one unrelated eight-stage plan (stages 3, 5 and 7), each occurrence costing
its own full difficulty cycle before the pattern was named.

The engine makes the cost concrete. `record-result` *runs* the stage's
`verify_command` itself and its exit code overrides the caller's `--status`, so
a defective check writes a fully-delivered stage FAILED and routes the session
into `DIAGNOSING`. And a plan may not be edited at `EXECUTING`
(`hook-state-gate.py`: "a plan is the result-image of active planning"), so the
executor cannot fix the criterion from where the failure is discovered. The
repair costs a full `declare → investigate → critique → normalize → replan`
cycle plus a fresh thinker review bound to the moved plan digest — and if the
review-round budget is already spent, a user override on top. That price is
paid for an authoring slip, every time.

The fourth norm is the one that makes the others enforceable rather than merely
advisable: without it, an executor who notices a broken criterion "fixes" it in
flight, and the plan silently stops being the thing the work was measured
against.

## Guidance

### 1. Declare the venue a check observes; never hard-code a `cd`

A stage check that must observe the **delivery worktree** declares
`verify_venue = "delivery"`. One that must observe the **landed trunk**
declares `verify_venue_at_final = "repo_root"`, or is written as a typed
`kind = "landed"` check. Never prefix the `verify_command` with
`cd <absolute worktree path>`.

A hard-coded path is an address, not a declaration: the engine cannot know what
the check meant to observe, so it cannot re-point it when the venue moves or
adapt when `verify-final` re-runs every stage check after landing — and
`land-branch.py`'s cleanup removes the worktree the path names, at which point
`cli.py`'s `cd <repo_root> && <cmd>` short-circuits to a false FAILED.

**Provenance, and the false defect this norm replaces.** This was originally
filed as a Core *ordering* defect between `land` and `verify-final`. It is not
one. Re-verification against trunk showed `verify_venue_at_final` landed in
`ef1a786` (2026-07-29) and the typed `kind = "landed"` check in `fced7d0`
(2026-07-28) — both **before** the 2026-08-28 plan that hit the collision. The
engine already had both mechanisms; that plan simply did not use them and
hard-coded `cd <worktree>` instead. Recording the honest provenance is what
stops the same non-existent defect being re-derived from the same symptom.

### 2. A criterion never asserts an unverified fact about current behaviour

An `expected_result_image` describes the world the stage must **produce**. Every
clause it contains about a world that already exists is a claim the plan is
asserting on its own authority — and if that claim is wrong, a correct
execution cannot pass, because passing would ratify a falsehood.

This bites hardest on a **live quantity**. A count transcribed into a plan
starts decaying the moment it is written. State what must be cited and from
which live source; let the figure be measured at execution time and recorded in
the observation.

> **Observed, in this leaf's own plan.** Stage 3's criterion fixed "seven
> observed firings in session `3b0247b5`, six acknowledged". By execution the
> live record held **eleven**, ten acknowledged — because that `effort_fires`
> list is appended to by the very mechanism the stage was filing a report
> about. The executor verified against the live record and filed a body citing
> all eleven, *exceeding* the criterion; the acceptance judge blocked the pass
> anyway, correctly, and the repair cost the full difficulty cycle.

The **corollary on refs**: a fact the stage must *read* belongs in
`knowledge_refs`; an artifact the stage *produces or mutates* belongs in
`material_refs`. Putting a read-only fact in the material list turns background
knowledge into a deliverable the criterion then demands.

### 3. A criterion's number comes from an explicitly bounded invocation

Any counting command inside a criterion passes its bound explicitly — for
`gh issue list`, `--limit N`. A count read from a default-bounded listing is a
truncation, not a measurement, and must never enter a criterion.

Prefer a **content-keyed absolute** over a delta against a captured baseline: it
reads 0 when nothing was done, 2 when the stage ran twice, and 1 only for the
intended world; it survives a re-run without depending on a procedure step
remembering not to overwrite a scratch artifact, and it cannot fail open on a
missing one. Where a delta is genuinely required, capture the baseline within
the stage itself rather than trusting an artifact written elsewhere.

> **Observed.** A backlog count of "30" was an artifact of `gh issue list`'s
> default `--limit 30`. The true figure was **129**. Nothing in the command's
> output announces the truncation.

### 4. A procedure step never rewrites the criterion it is measured by

A procedure step re-measures and reports. It does not touch the
`expected_result_image` or `done_criterion` it is being judged against — even
when it can see that the criterion is wrong, and even when the correction would
be in the plan's favour.

The right move on a criterion discovered to be false is to **surface it as a
difficulty** and re-baseline through the gated path: `declare` → `investigate`
→ `critique` → `normalize` → `replan`, with the plan edit made at `DIAGNOSING`,
which is the one execution-side node where authoring a plan is legitimate, and
a fresh review bound to the moved digest. This is more expensive than an
in-flight edit, and that is the point: the extra cost buys an independent read
of the change, which is exactly what an executor editing its own success
condition does not have.

### 5. A criterion names the lifecycle state it describes

A change passes through states: unbuilt, built in a delivery worktree, standing
as an open review, merged into trunk, rolled out. A criterion is only ever true
of **one** of them. Name which one — and prefer the state that is **terminal**
for the stage, because that is the state the criterion will be re-executed in.

`verify-final` re-runs every measurable stage's `verify_command` **after** the
change has landed. So a criterion phrased over the pre-merge world is not merely
at risk of going stale — it is *guaranteed* to be re-run in the post-merge world,
where "an open review request exists" is false precisely *because* the stage
succeeded. Norm 1 governs **where** a check looks; this norm governs **which
lifecycle state** it is allowed to describe. They fail together often: a check
pointed at a delivery worktree usually also asserts something only true before
the branch landed.

Two authoring habits follow:

- **Anchor a past-state clause to something immutable.** A merged revision (or a
  revision pair spanning the change), a run id, an artifact digest — each stays
  true forever. "The review request is open", "the branch is ahead of trunk",
  "the worktree contains N commits" do not. When a stage's real content *is* a
  transient state — a review had to happen, a run had to be launched — assert the
  durable trace it leaves, not the state itself.
- **Sweep the whole plan, not just the stage you touched.** Transient-state
  phrasing clusters: an author writing one such clause has usually written
  several. A single pass over every criterion, asking of each "in which state is
  this sentence true?", is cheap next to one difficulty cycle.

On discovering that a criterion describes a state the work has already left,
norm 4 applies unchanged: surface it as a difficulty and re-baseline through
`declare → … → replan`. Do not edit it in flight, even though the correction is
obviously right — obviousness is what makes this the tempting case.

> **Observed.** A stage's criterion was written against a standing open review.
> The user then merged and rolled the change out themselves, so the stage's
> terminal state became the merge and the criterion's subject no longer existed.
> The repair re-derived both `done_criterion` and `verify_command` against a
> revision pair — a fact no later event can unmake — and a plan-wide sweep
> confirmed no other criterion was still phrased over the superseded state.

### 6. Sweep exact-shape criteria after ANY revision round, formal replan or ad-hoc

An exact-shape criterion — a literal file set, a commit count, a fixed string
comparison — is authored against the delivery the plan expects **at that
moment**. A later revision round that legitimately expands or reshapes the
delivery (a code-reviewer's should-fix finding, a fresh defect a further round
catches) invalidates that shape even when the revision itself was fully
sanctioned and independently reviewed. The engine's own `replan` command
tracks this for a **formally recorded** revision — but a revision round driven
by an ad-hoc, dossier-based specialist spawn (a manual `code-reviewer` dossier
outside `agentctl`'s own dispatch) leaves no engine-tracked event to prompt a
criterion sweep at all. Nothing distinguishes the two from the criterion's
point of view: both change what "done" looks like on disk.

The check is the same as norm 3's bounded-count rule, applied across time
rather than at authoring: after every revision round — ask explicitly whether
any control criterion still names a file set, a count, or a literal string
that predates the round. Do not wait for `verify-final` to discover it by
failing.

> **Observed.** `final_check` 3 in this leaf's own governing plan hardcoded
> "exactly `gates.py`,`test_plan_review_gate.py`, one commit" at authoring
> time. Two further review rounds — each closing a genuine, independently
> reproduced code-reviewer finding, run as manual specialist dossiers with no
> engine-tracked `replan` between them — legitimately expanded delivery to 3
> stacked commits and 6 files. Nothing flagged the stale criterion until
> `verify-final` ran the literal command and it failed for real, routing the
> session into `DIAGNOSING` and costing a full
> `declare → investigate → critique → normalize → replan` cycle to repair — a
> cycle norm 4 already prices in, but one a mid-round sweep would have avoided
> paying at all.

**Corollary: exhaust the sweep before spending the round, not after.** Running
the sweep once and fixing only the finding that blocked the gate is not
enough — a second pass minutes later, after that first fix, routinely
surfaces a second stale exact-shape criterion the first pass's own edit did
not cover (a `done_criterion`/`method` field left describing the pre-round
shape while `final_check`'s command was already corrected in isolation).
Each such fix is its own plan edit, and `done_criterion`/`method` changes are
substantive by the refinement/substantive table (`CLAUDE.md` § Acting
without asking) regardless of how cosmetic the delta reads, so every
incremental fix re-opens the full re-approval gate — present-plan, user
approval, `approve`, `partition` — even moments after the user granted an
override or a fresh review for the *previous* finding. Run the sweep to
exhaustion (every `qenum-*` candidate genuinely researched and dispositioned,
not just the ones blocking the immediate gate) and land every resulting
correction in **one** edit before requesting the review or override that
clears the round-budget gate — not in a follow-up edit after.

> **Observed.** Fixing `final_check` 3's merge-base bug and getting a second
> thinker `pass` on it, then separately discovering and fixing Stage 2's own
> stale `done_criterion`/`method` fields in a follow-up edit, cost a second
> full re-approval round-trip for what was, in substance, the same sweep the
> first round should have completed in one pass. User feedback at the second
> re-approval prompt, quoted verbatim as the evidence for this norm:
> <!-- Language exception: direct user quote, kept verbatim as evidence per CLAUDE.md's quote-citation convention -->
> "Опять перепланирование после приемки задачи. Очень плохо." ("Replanning
> again after the task was accepted. Very bad.")

### 7. Dry-run the verify_command's own script text before it is frozen

Norm 2 governs a criterion asserting a false **claim about the world**. This is
a different failure class: the check **script itself** is broken — a shell
escaping bug, an `else` branch that swallows the exit status of the command it
guards, a Python call into a function or module that does not exist, or one
whose signature has since changed. None of these are factual claims that can
be right or wrong; they are code, and code authored without being run carries
exactly the defect rate any other unrun code does.

A frozen `verify_command` is authored once, at plan time, against a mental
model of the repo — then not touched again until `record-result` actually
executes it, potentially stages and days later. Nothing about `submit_plan` or
`approve` runs the script, so a scripting defect frozen at authoring time
survives every review round untouched and is discovered only when the engine
runs it for real, at the exact moment a genuinely-completed stage is waiting
to be recorded.

The fix is mechanical and cheap relative to the cycle it prevents: before
`submit_plan`, extract each stage's `verify_command` and, at minimum,
syntax-check it (`bash -n` for a shell script) against the real target
worktree or repo checkout; where it calls into project code, confirm each
referenced function or module actually exists and that its call signature
matches, via a direct import and `inspect.signature` rather than by reading
the source and assuming. This is a dry run of the check's **mechanics**, not
of the stage's deliverable — it catches nothing about whether the stage's own
work is correct, only whether the instrument measuring it would fire cleanly
either way.

> **Observed.** One eight-stage plan hit this three separate times, each
> costing a full `declare → investigate → critique → normalize → replan`
> cycle: stage 3's `verify_command` applied an escaping convention
> (`D = chr(36)`) inconsistently across two branches of the same check; stage
> 5's used a shell `else` branch that discarded the negative exit status of the
> command it was meant to be guarding, so a genuine failure there would have
> reported success; stage 7's asserted a literal internal codename string that
> had no live referent in the repo at all and was independently banned by the
> repo's own term-lint gate. None of the three was a false claim about stage
> behaviour — each was the check script itself misbehaving, and a `bash -n`
> plus a direct import-and-signature check at authoring time would have caught
> every one of them before a single stage ever ran.

## See also

- [[experience/2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]]
  — the recurring venue/scope/interface class this leaf's norm 1 generalizes,
  including the venue-**lifetime** variant where landing and the stage checks
  carry mutually exclusive requirements.
- [[plan-cost-tier-empirical-stage-underestimate]] — the sibling authoring norm
  on the *effort* axis; distinct functional ground (pricing a stage) from this
  leaf's (stating a stage's control).
- [[plan-activity-ontology]] — the eight elements a plan must cover; norms 1–3
  and 5 sharpen the *control criterion* element, norm 4 protects it from its own
  executor.
