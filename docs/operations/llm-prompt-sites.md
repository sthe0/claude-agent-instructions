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
| `scripts/spawn-specialist.py`:`_main` | projected fields of the meta node and the stage the spawn runs; the brief of that stage, a pair or a unit; a reviewer's plan digest | view | each field `PLAN_FIELD_INLINE_MAX_CHARS_DEFAULT`; prompt `dispatch_prompt_ceiling_chars` (refused at launch) |
| `scripts/spawn-cursor-specialist.py`:`main` | the stage brief of `--stage-index`, else the plan summary | view | each field `PLAN_FIELD_INLINE_MAX_CHARS_DEFAULT` |
| `scripts/agentctl/dispatch.py`:`build_argv` | the plan path and `--stage-index` handed to the spawner | view | none beyond the spawner's |
| `scripts/plan-review-topological.py`:`spawn_pair` | the plan path and a pair or unit id handed to the spawner | view | none beyond the spawner's |
| `scripts/plan-review-topological.py`:`Driver.dry_run` | the plan path and a pair id handed to the spawner (not launched) | view | none beyond the spawner's |
| `scripts/mandate_cycle/driver.py`:`Cycle.spawn_argv` | a work-item brief file handed to the spawner as `--plan`; a file that is not a typed plan is passed as its own text up to `render.NON_PLAN_BRIEF_MAX_CHARS`, longer is refused | caller artifact | the brief file |
| `scripts/agentctl/advisor.py`:`enumerate_claims` | the artifact `ledger-enumerate` reads; one node per call when it is a typed plan | caller artifact | the artifact, or one node |
| `scripts/agentctl/advisor.py`:`enumerate_subprocess_runner` | whatever `enumerate_claims` hands it on stdin | caller artifact | the artifact, or one node |
| `scripts/agentctl/advisor.py`:`judge` | the payload dict a cognition point builds: the goal, weight class, route, the plan path and stage titles, a stage's expected result image and its observation, hypotheses | caller artifact | the payload |
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
| `scripts/mandate_cycle/driver.py`:`real_run` | none | no plan content | a runner for the cycle's own commands |
| `scripts/measure-marker-extractor-latency.py`:`_one_call` | none | no plan content | a fixed synthetic message |
| `scripts/probe-hook-decision-semantics.py`:`run_add_dir_read_cell` | none | no plan content | a fixed probe prompt |
| `scripts/probe-hook-decision-semantics.py`:`run_add_dir_write_cell` | none | no plan content | a fixed probe prompt |
| `scripts/probe-hook-decision-semantics.py`:`run_headless_cell` | none | no plan content | a fixed probe prompt |
| `scripts/spawn-cursor-escape.py`:`main` | none | no plan content | the difficulty statement |

## Instruction texts

