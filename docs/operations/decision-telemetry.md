# Decision telemetry

One log answers "which hook or engine gate fires, how often, and what does it decide":
`<root>/agentctl/decision-log.jsonl` (`config_root.decision_log()`, override with
`$AGENTCTL_DECISION_LOG`). Every registered hook entry and every `agentctl` gate
evaluation appends one row. `scripts/agent-report.py hooks` reads it.

## Row

One JSON line, at most 2048 bytes, written with a single `O_APPEND` write (no lock, no
fsync, no subprocess):

| field | value |
|---|---|
| `ts` | ISO-8601 UTC, seconds |
| `mechanism` | script basename without `.py` (`hook-state-gate`); `engine:<gate label>` for the engine; `arc:<name>` for a sub-hook of an arc dispatcher |
| `event` | hook event name from the payload (`unknown` when the hook read no payload), or `engine` |
| `decision` | `deny`, `nudge` or `silent` |
| `session` | payload `session_id`, else `$CLAUDE_CODE_SESSION_ID`, else `unknown` |
| `test` | true when `PYTEST_CURRENT_TEST` is set, the session is `agentctl-test-session`, or `AGENT_TELEMETRY_TEST=1` |
| `layer` | `core`, `arc` or `engine` |

Decision class, from what the hook wrote: `deny` for a `permissionDecision` of `deny`, a
top-level `"decision": "block"`, or exit code 2; `silent` for empty stdout; `nudge` for any
other stdout (a PreToolUse `ask` included). An engine row is `deny` when the gate refused and
`silent` when it passed.

Telemetry is off, and writes nothing, when the log path cannot be created. It never changes a
hook's stdout, stderr or exit code, crashes included.

## Hooks

Each hook's `__main__` is `sys.exit(hook_telemetry.run(main, mechanism="<name>"))`
(`scripts/lib/hook_telemetry.py`). `run` captures file descriptor 1, so output written by a
child process is classified too, then re-emits it byte for byte. A crash re-raises after the
row is written. `stdin` is wrapped lazily: a hook that never reads it (and a CLI-mode
`verify-*` script) is not blocked on it.

The 42 registered entries are checked by `scripts/tests/test_hook_telemetry_coverage.py`:
every entry of `install-reminder-hooks.sh` is run once with a benign payload and must leave
exactly one valid row.

## Engine gates

`cli._log_gate(state, label, blockers, passed=...)` already wrote the gate log; it now also
writes one engine row (`hook_telemetry.record`, layer `engine`) per call. The labels `escalation_orphans`,
`frozen_stage` and `renorm_final` used to be logged only on refusal; they are now logged on
every evaluation, so a gate's deny rate has a denominator.

Every `*_blockers` / `*_gate` function, and how its evaluation is logged:

| function | where | logged as |
|---|---|---|
| `plan_approval_blockers` | gates.py | engine row `plan_approval` |
| `plan_review_blockers`, `_plan_review_verdict_blockers` | gates.py | `plan_review`; also composed into `plan_approval` |
| `plan_presentation_blockers`, `grants_approval_blockers`, `protected_grant_blockers` | gates.py | composed into `plan_approval` |
| `resolution_blockers`, `_acceptance_review_resolution_blockers` | gates.py | `resolution` |
| `difficulty_blockers` | gates.py | `difficulty_blockers` |
| `difficulty_stack_blockers` | gates.py | `difficulty_stack` |
| `difficulty_close_blockers` | gates.py | `difficulty_close` |
| `normalization_blockers` | gates.py | `normalization_blockers` |
| `normalization_waiver_blockers` | gates.py | `normalization_waiver` |
| `failure_address_blockers` | gates.py | `failure_address_blockers` |
| `normative_edit_blockers` | gates.py | `normative_edit` |
| `renorm_retention_blockers` | gates.py | `renorm_retention` |
| `renormalization_blockers` | gates.py | `renormalization` |
| `pending_renorm_final_blockers` | gates.py | `renorm_final` (every evaluation) |
| `escalation_orphan_blockers` | gates.py | `escalation_orphans` (every evaluation) |
| `replan_authorization_blockers` | gates.py | `replan_authorization` |
| `replan_coverage_blockers` | gates.py | `replan_coverage` |
| `effort_fire_blockers` | gates.py | `effort_fire` |
| `diagnosing_replan_blockers` | gates.py | `diagnosing_replan` |
| `pending_renorm_stage_blockers`, `acceptance_review_blockers`, `code_review_blockers`, `_code_review_verdict_blockers` | gates.py | not logged: a `record-result` refusal, returned to the caller as a directive |
| `resolved_reentry_blockers` | gates.py | not logged: a `reset` refusal |
| `plugin_gate_blockers`, `ledger_blockers`, `_ledger_gate`, `_experience_gate`, `_dummy_resolution_gate`, `_publish_gate`, `_journal_gate`, `_resolution_gate` | agentctl/plugins*.py | composed into `resolution`; at a replan, into `plan_approval_plugin` |
| `premise_blockers`, `_premise_gate` | plugins_premise.py | composed into `plan_approval` |
| `_landing_gate`, `_unlanded_branch_blockers`, `_resolution_ask_gate` | cli.py | composed into `resolution` |
| `_since_user_backstop_gate` | cli.py | not logged: a bookkeeping predicate, not a refusal |
| `_log_gate` | cli.py | the writer itself: appends the gate log line and the engine row for the label it is given |
| `_is_followed_by_gate` | cli.py | not logged: a predicate on the engine-session binding, not a refusal |
| `_with_writer_gate` | cli.py | not logged: attaches the `present-plan` writer-gate outcome to the directive it returns |
| `verify_command_reachability_blockers`, `venue_declaration_blockers` | plan.py | not logged: plan validation at `submit-plan` |
| `self_improvement_blockers`, `escalation_without_diagnosis_blockers`, `prose_binary_ask_blockers`, `silent_closure_blockers`, `self_diagnose_findings_blockers`, `resolution_turn_blockers`, `long_job_autowake_blockers`, `collect_blockers` | hook-turn-end-gate.py | the Stop hook's row (`hook-turn-end-gate`): `deny` when any blocker holds |
| `_smoke_gate`, `_suite_gate` | land-branch.py | not logged: land-time script verdicts, reported by exit code |
| `_resolve_instruction_smoke_gate`, `_resolve_suite_gate` | agentctl/script_effects.py | not logged: resource resolvers for a permission check |
| `_child_project_gate` | spawn-specialist.py | not logged: a refusal of the spawn script |
| `_term_gate` | file-difficulty.py | not logged: a refusal of the filing script |
| `_form_gate` | lib/writer_rules.py | not logged: a pure predicate; the hook that calls it logs its decision |

## Launchers

A launcher is a script that starts a `claude -p` child or a hook for a check rather than for
work. Its rows must not read as real sessions, so each sets `AGENT_TELEMETRY_TEST=1` in the
child's environment (and `test` is then true). Production launchers are not marked.

Marked (the child's environment carries `AGENT_TELEMETRY_TEST=1`):

- `scripts/instruction-sandbox-live.py` (`build_child_env`)
- `scripts/lib/instruction_smoke_gate.py` (`_run_process`)
- `scripts/probe-hook-decision-semantics.py` (`_run_child`)
- `scripts/check-in-harness-observation.py`

Exempt (left unmarked):

- `scripts/spawn-specialist.py`: production spawns of real work, whose rows must read as real.
- `scripts/lib/host_llm.py`: judge call in an isolated config directory, where no fleet hook is registered.
- `scripts/agentctl/advisor.py`: the same isolated judge call.
- `scripts/agentctl/procedure.py`: the same isolated judge call.
- `scripts/agentctl/result_image.py`: the same isolated judge call.
- `scripts/agentctl/conditions.py`: the same isolated judge call.
- `scripts/verify-judge-isolation.py`: the same isolated judge call; no hook fires.
- `scripts/tests/test_advisor.py`: a test; pytest sets `PYTEST_CURRENT_TEST`, so the rows are test rows.
- `scripts/tests/test_enumerate_detach.py`: a test, as above.
- `scripts/tests/test_enumerate_escape.py`: a test, as above.
- `scripts/tests/test_host_llm.py`: a test, as above.
- `scripts/tests/test_judge_usage.py`: a test, as above.
- `scripts/tests/test_norm_staleness_replay_r10_r11.py`: a test, as above.
- `scripts/tests/test_verify_execution.py`: a test, as above.

A test run needs no marking: pytest sets `PYTEST_CURRENT_TEST`, and conftest's autouse
`_isolate_decision_log` points `$AGENTCTL_DECISION_LOG` at a temporary file so no test
appends to the real log (`test_decision_log_isolation.py` checks it with a nonce).

## Report

```
scripts/agent-report.py hooks [--log PATH] [--since YYYY-MM-DD] [--include-test]
```

One row per `(mechanism, layer, event)` with evaluations, deny, nudge, silent, deny share and
the last timestamp. Test rows are left out unless `--include-test` is given.
