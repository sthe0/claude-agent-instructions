# Model-facing prompt sites

*Difficulty removed: a model call that receives a plan has no declared bound on how much of
the plan it sees, so a stage's method, knowledge or invariants reach a reader who was never
meant to read them, and a new call site can add that exposure without anyone noticing.*

Every place in `scripts/` that launches a model — or builds the argv of a spawner that
does — is a row below, keyed `<repo-relative file>`:`<enclosing function qualname>`. The
set of keys is pinned against an `ast` enumeration of `scripts/**/*.py` (tests excluded) by
`scripts/tests/test_llm_prompt_sites.py`: a call site added without a row, or a row whose
site is gone, fails the suite.

## How to read a row

- **Plan content passed** — what of a plan the call carries; `none` when it carries no plan.
- **Mechanism** — one of a closed vocabulary:
  - `view` — `render.build_site_prompt` builds the prompt and a view directory the model may
    read (`--add-dir`): the projected fields of every node it names, the full brief of the
    nodes it is meant to read, and `objects/<sha256>` for any longer field. Which nodes are
    shown for each site kind is `render.PROJECTED_FIELDS` plus the per-kind rule in
    `render.build_site_prompt`.
  - `brief` — one node printed inline with every field and no directory (`topo-unit`,
    `enumerate-node`).
  - `inline field` — the caller passes named plan fields as text; the row names them.
  - `caller artifact` — the caller passes an artifact (a brief, a payload, a deliverable) the
    site does not derive from a plan; if that artifact is a typed plan the call is split per
    node (`enumerate-node`) or the plan is read through a view (a spawner), and a file that
    is not a typed plan is passed as its own text up to `render.NON_PLAN_BRIEF_MAX_CHARS`.
  - `no plan content` — the call carries text that is not derived from a plan.
- **Size bound** — what limits the plan content the call can carry.

`scripts/tests/test_llm_prompt_sites.py` runs each row's site on a synthetic plan with a
unique token at the start and end of every key outside `render.PROJECTED_FIELDS`, and
asserts that no token of a node the row does not show reaches the captured argv or stdin.

## Sites

| Site | Plan content passed | Mechanism | Size bound |
|---|---|---|---|
| `scripts/spawn-specialist.py`:`_main` | projected fields of the meta node and the stage the spawn runs; the brief of that stage, a pair or a unit; a reviewer's plan digest | view | each field `plan-field-inline-max-chars`; prompt `dispatch_prompt_ceiling_chars` (refused at launch) |
| `scripts/spawn-cursor-specialist.py`:`main` | the stage brief of `--stage-index` with every field in full; without a stage index the full rendered plan, every reference resolved (a named exception to "never the inlined plan", ADR-0012: the Cursor agent CLI takes no `--add-dir`, so a view could not be read); no field reads as a pointer | inline field | none; this spawner has no prompt ceiling |
| `scripts/agentctl/dispatch.py`:`build_argv` | the plan path and `--stage-index` handed to the spawner | view | none beyond the spawner's |
| `scripts/plan-review-topological.py`:`spawn_pair` | the plan path and a pair or unit id handed to the spawner | view | none beyond the spawner's |
| `scripts/plan-review-topological.py`:`Driver.dry_run` | the plan path and a pair id handed to the spawner (not launched) | view | none beyond the spawner's |
| `scripts/mandate_cycle/driver.py`:`Cycle.spawn_argv` | a work-item brief file handed to the spawner as `--plan`; a file that is not a typed plan is passed as its own text up to `render.NON_PLAN_BRIEF_MAX_CHARS`, longer is refused | caller artifact | the brief file |
| `scripts/agentctl/advisor.py`:`enumerate_claims` | the artifact `ledger-enumerate` reads; one node per call when it is a typed plan | caller artifact | the artifact, or one node |
| `scripts/agentctl/advisor.py`:`enumerate_subprocess_runner` | whatever `enumerate_claims` hands it on stdin | caller artifact | the artifact, or one node |
| `scripts/agentctl/advisor.py`:`judge` | the payload dict a cognition point builds: the goal, weight class, route, the plan summary (the meta in full, each stage by its declared product; `render.plan_summary_text`) and stage titles, a stage's expected result image and its observation, hypotheses | caller artifact | the payload |
| `scripts/agentctl/advisor.py`:`acceptance_judge` | a stage's observation and its expected result image | inline field | the two strings |
| `scripts/agentctl/advisor.py`:`judge_question_materiality` | the control criterion and the question text a premise is bound to | inline field | the two strings |
| `scripts/agentctl/gates.py`:`_semantic_invariants_coverage` | the `conditions` and `invariants` of every stage of the corrected plan, and one invariant of the critique | inline field | the stages' two fields |
| `scripts/agentctl/conditions.py`:`judge_restatement` | one stage's `conditions` and its `depends_on` | inline field | the one field |
| `scripts/agentctl/procedure.py`:`judge_collapse` | one stage's `method` and `procedure` | inline field | the two fields |
| `scripts/agentctl/result_image.py`:`judge_echo` | one stage's `expected_result_image`, `verify_command` and `done_criterion` | inline field | the three fields |
| `scripts/agentctl/advisor.py`:`_judge_user_text` | none | no plan content | the user's text |
| `scripts/agentctl/advisor.py`:`judge_approval_ask` | none | no plan content | the ask text |
| `scripts/agentctl/advisor.py`:`judge_binary_ask` | none | no plan content | the final text |
| `scripts/agentctl/advisor.py`:`judge_deferring_disposition` | none | no plan content | the ask text |
| `scripts/agentctl/advisor.py`:`judge_feedback_signal` | none | no plan content | the user's text |
| `scripts/agentctl/advisor.py`:`judge_landing_discipline_ask` | none | no plan content | the ask text |
| `scripts/agentctl/advisor.py`:`judge_outage_escalation` | none | no plan content | the assistant text |
| `scripts/agentctl/advisor.py`:`judge_published_attachment` | none | no plan content | the attachment excerpt |
| `scripts/agentctl/advisor.py`:`judge_published_text_rules` | none | no plan content | the published body |
| `scripts/agentctl/advisor.py`:`judge_same_difficulty` | none | no plan content | two difficulty descriptions |
| `scripts/agentctl/advisor.py`:`judge_silent_closure` | none | no plan content | the assistant text |
| `scripts/agentctl/cli.py`:`_venue_tree_identity.run` | none | no plan content | a git command line |
| `scripts/agentctl/mandate.py`:`<module>` | none | no plan content | a list of script names, no call |
| `scripts/gen_crutch_registry.py`:`<module>` | none | no plan content | a table key, no call |
| `scripts/measure-marker-extractor-latency.py`:`_one_call` | none | no plan content | a fixed synthetic message |
| `scripts/probe-hook-decision-semantics.py`:`run_add_dir_read_cell` | none | no plan content | a fixed probe prompt |
| `scripts/probe-hook-decision-semantics.py`:`run_add_dir_write_cell` | none | no plan content | a fixed probe prompt |
| `scripts/probe-hook-decision-semantics.py`:`run_headless_cell` | none | no plan content | a fixed probe prompt |
| `scripts/spawn-cursor-escape.py`:`main` | none | no plan content | the difficulty statement |

## Instruction texts

The sentences the engine itself writes into a plan-bearing prompt to tell the model where the
rest of the plan is and how to read it. They are not plan content, so no row above carries
them; they are listed so a reader who changes one finds the others. Each line gives the file
that holds the sentence and a fragment of it; `test_llm_prompt_sites.py` requires every
fragment to still appear in its file.

- `scripts/agentctl/render.py` — `` a field too long to print here reads ``
- `scripts/agentctl/render.py` — `` <sha256>`, a file under that directory. ``
- `scripts/agentctl/render.py` — `` The service's full node file is one `Read` away in the view directory ``
- `scripts/agentctl/render.py` — `` ## Referenced files ``
- `scripts/agentctl/render.py` — `` The plan was assembled from these files, each a file of this view: ``
- `scripts/agentctl/render.py` — `` The plan file itself is ``
- `scripts/agentctl/render.py` — `` directory: its sha256 is the plan digest. ``
- `scripts/agentctl/render.py` — `` directory: read it for the full text of any stage listed above. ``
- `scripts/agentctl/render.py` — `` This is a PROJECTED BRIEF of stage ``
- `scripts/agentctl/render.py` — `` stage(s) in the plan — the other stages are not ``
- `scripts/agentctl/render.py` — `` ## Final verification (labels only — this stage does not need the ``
- `scripts/agentctl/render.py` — `` the coordinator runs them ``
- `scripts/spawn-specialist.py` — `` ## Working plan — topological review unit or pair ``
- `scripts/spawn-specialist.py` — `` the full plan is never inlined for --review-topo — ``
- `scripts/spawn-specialist.py` — `` ## Working plan — stage {args.stage_index} brief ``
- `scripts/spawn-specialist.py` — `` a field too long to print here is read from the view directory ``
- `scripts/spawn-specialist.py` — `` ## Working plan — summary (stage products only; the plan is never inlined, ``
- `scripts/spawn-specialist.py` — `` read from the view directory named below) ``
- `scripts/agentctl/render.py` — `` see the full plan file for those ``

Known residual: the last line is the final-verification note a topological pair bundle still
prints for the base node and for a source-stage service, although a topological reviewer is
denied the plans directory and its view holds no plan file. The text is left as it is on
purpose: changing it moves the node-file bytes that `base_file_digest` and
`service_file_digest` cover, which would stale every recorded pair review.

### Instruction files that speak about plan transport

The instruction files a reader follows that mention how a plan is passed, digested, split or
converted, each with its disposition: `updated` (changed to describe hypertext plans) or
`unaffected` (the mention stays correct, with the reason). The set is found by enumeration,
not recall: every file under `skills/`, `docs/adr/`, `memory-global/leaves/` and the top-level
instruction files that matches the plan-transport pattern of the stage-7 check.

Updated:

- `skills/specializations/planner/SKILL.md` — updated: § Large plans names the reference form, the authoring verbs and who runs `plan-convert-live`.
- `skills/specializations/planner/policy.md` — updated: § Large plans gives both limits, the remedy order and conversion.
- `skills/specializations/thinker/SKILL.md` — updated: view directory, `objects/<sha256>` and the index copy to hash; topological-protocol paragraphs untouched.
- `skills/specializations/_shared/marker-protocol.md` — updated: a plan may arrive as a stage brief plus a view directory; closure digest.
- `scripts/agentctl/README.md` — updated: `## Hypertext plans`.
- `docs/adr/0010-child-contour.md` — updated: note that a child plan may be a referenced sub-document.
- `README.md` — updated: the decisions row names ADR-0012.
- `memory-global/leaves/plan-file-split.md` — updated: marked Superseded by ADR-0012.
- `memory-global/leaves/token-economy-plan.md` — updated: row 6 names ADR-0012, no manual split.
- `memory-global/MEMORY.md` — updated: the pointer no longer advises a manual split.
- `skills/specializations/code-reviewer/SKILL.md` — updated: a typed-plan prompt names a view directory, not a plans-directory path, and the plan digest is the index's sha256 listed by `plan-digest`.
- `skills/tracker-management/SKILL.md` — updated: the attached plan must be one a ticket reader can open, never a bare index of references.

Unaffected:

- `CLAUDE.md` — unaffected: `plan_sha256` stays the presentation receipt's binding, and the plan digest keeps its meaning.
- `docs/adr/0005-activity-act-functional-places.md` — unaffected: a historical decision record that names `plan-render`.
- `docs/adr/0012-hypertext-plans.md` — unaffected: the design the texts above describe.
- `docs/adr/README.md` — unaffected: the ADR index; stage 1 added the 0012 row.
- `skills/overcome-difficulty/SKILL.md` — unaffected: names `check-coverage --new <plan>` on a plan path, unchanged.
- `skills/overcome-difficulty/policy.md` — unaffected: shows a spawn call with `--plan <brief file>`, a brief, not a plan.
- `memory-global/leaves/acting-without-asking.md` — unaffected: approval of a plan file; the digest bound is the index digest.
- `memory-global/leaves/ask-user-question-split-turn.md` — unaffected: receipt binding to `plan_sha256`, which keeps its meaning.
- `memory-global/leaves/behavioral-conformance-control.md` — unaffected: the attested digest is the index digest.
- `memory-global/leaves/coordination/MEMORY.md` — unaffected: an index line about the delivery gate's `plan_sha256` binding.
- `memory-global/leaves/experience-leaf-schema.md` — unaffected: `record-experience.py --plan` is a text argument, not a plan file.
- `memory-global/leaves/plan-activity-ontology.md` — unaffected: `plan-render` is still a projection of the typed plan.
- `memory-global/leaves/published-text-writer-gate.md` — unaffected: a published-text rule that names `plan_sha256` only as an example.
- `memory-global/leaves/question-provenance-gate.md` — unaffected: question lists and `plan-render` are generated views of resolved content.
- `memory-global/leaves/skill-first-dispatch.md` — unaffected: `plan-render` stays the way to read a plan field.
- `memory-global/leaves/spawning-specialists.md` — unaffected: hashing capability for the reviewer-computed digest is unchanged.
- `memory-global/leaves/system-knowledge/agentctl-acceptance-judge-gate.md` — unaffected: a record about plan-digest staling an acceptance review.
- `memory-global/leaves/system-knowledge/agentctl-plan-review-coverage-checks-whole-record-first.md` — unaffected: coverage reads the review record, not plan bytes.
- `memory-global/leaves/system-knowledge/agentctl-stage-vector-staleness.md` — unaffected: `submit-plan --plan <path>` still takes the index path.
- `memory-global/leaves/system-knowledge/agentctl-verify-final-reruns-live-state.md` — unaffected: a record that names `plan-render`.
- `memory-global/leaves/system-knowledge/claude-code-drops-pre-tool-call-text.md` — unaffected: the delivery receipt's `plan_sha256` binding is unchanged.
- `memory-global/leaves/system-knowledge/cursor-agent-cli-spawn.md` — unaffected: a spawn example with `--plan`; the Cursor spawner's handling is in the site rows above.
- `memory-global/leaves/system-knowledge/settings-permission-tiers.md` — unaffected: grants recorded alongside `plan_sha256`, now the index digest.
- `memory-global/leaves/experience/MEMORY.md` — unaffected: the dated experience index, a record of past tasks.
- `memory-global/leaves/experience/2026-07-02-spawn-sandbox-excludes-declared-stage-material.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-07-03-engine-replan-artifact-discipline.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-07-09-gate-must-execute-what-it-attests.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-08-05-scope-substitution-at-plan-authoring.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-08-11-review-loop-cannot-measure-its-own-convergence.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-09-22-tool-scope-judged-by-contract-not-pattern.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-10-12-landed-state-identity-by-coordinate.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-10-12-replan-gate-machinery-wedges-correction.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-10-12-stage-control-authoring-interface-mismatch.md` — unaffected: dated record of a past task.
- `memory-global/leaves/experience/2026-10-12-stage-control-phase-of-evaluation.md` — unaffected: dated record of a past task.

Frozen fixtures, not edited (assumed premise Q7 of ADR-0012) — `benchmark-profile/` and
`benchmark-profile-spawn/` hold copies of the planner skill: unaffected, they stay as the
benchmarks recorded them.

