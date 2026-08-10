"""Make the `agentctl` package importable and provide shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.store import FileStateStore  # noqa: E402
from lib import judge_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_task_quality_ledger(tmp_path, monkeypatch):
    """The scorecard aggregates the real ledger into degradation flags, so a
    test resolve must never append to it (unlike GATE_LOG, which is inert
    telemetry)."""
    from agentctl import cli
    monkeypatch.setattr(cli, "TASK_QUALITY_LOG", tmp_path / "task-quality.jsonl")


@pytest.fixture(autouse=True)
def _plan_review_gate_off_by_default(monkeypatch):
    """Default the thinker-review gate OFF for the suite at large.

    The gate blocks `approve`/`replan` on every SUBSTANTIVE session until a bound
    thinker review is recorded (gates.plan_review_blockers). The overwhelming
    majority of substantive-flow tests exercise unrelated machinery (partition,
    tracker plugin, cost, dispatch, coverage) and are not about the review gate;
    coupling them all to it would make a plan-review change cascade failures across
    a dozen unrelated modules. So we set the documented force-off knob
    (AGENTCTL_PLAN_REVIEW=0) by default — byte-identical to the gate being absent —
    exactly as the fixture above isolates the quality ledger.

    The gate's real block/pass/stale/override/scope behaviour is proven end-to-end
    by test_plan_review_gate.py and the test_spine_walk_* integration tests, which
    explicitly re-enable it (setenv "1" / a live subprocess env)."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "0")


@pytest.fixture(autouse=True)
def _plan_presentation_gate_off_by_default(monkeypatch):
    """Default the presentation/delivery gate OFF for the suite at large, for the
    same reason as `_plan_review_gate_off_by_default` above: the overwhelming
    majority of substantive-flow tests are not about presentation/delivery, and
    coupling them all to it would make this gate's change cascade failures across
    unrelated modules. AGENTCTL_PLAN_PRESENTATION=0 is the documented force-off
    knob (gates.plan_presentation_active) — byte-identical to the gate being
    absent. Its real block/pass/stale/override/fail-closed behaviour is proven
    end-to-end by test_plan_presentation.py, which explicitly re-enables it."""
    monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "0")


@pytest.fixture(autouse=True)
def _premise_gate_off_by_default(monkeypatch):
    """Default the premise (question-provenance) plan_approval gate OFF for the
    suite at large — the same accommodation as `_plan_review_gate_off_by_default`
    above, for the same reason.

    `plugins_premise` auto-activates for EVERY SUBSTANTIVE session (weight_class
    alone) and fail-closes `approve` until every raised question is disposed AND the
    enumeration cross-check has run. The CLI verbs that discharge it land in a later
    stage, so with the gate live every substantive-cycle e2e test (walkthrough,
    tracker, ledger, resolve-marker, partition, …) would wedge at approve on
    machinery it does not exercise. We set the documented force-off knob
    (AGENTCTL_PREMISE=0) by default — byte-identical to the plugin never
    auto-activating — exactly as the fixture above neutralizes the review gate.

    The plugin's real arming / gate / stale-enumeration behaviour is proven by
    test_plugins_premise.py, which deletes the knob so the plain weight_class
    predicate runs."""
    monkeypatch.setenv("AGENTCTL_PREMISE", "0")


@pytest.fixture(autouse=True)
def _no_real_enumeration_launch_by_default(monkeypatch):
    """Stub `cli._spawn_enumeration_worker` for the suite at large.

    Stage 4 wired `_launch_enumeration` into `cmd_submit_plan`/`cmd_replan`
    unconditionally whenever a premise bag exists, so ANY test that drives one of
    those commands with the premise plugin genuinely live (AGENTCTL_PREMISE
    deleted/"1" — see `_premise_gate_off_by_default` above) launches a REAL
    detached child process that shells out to `claude -p` for a live advisor
    call. That launch is fire-and-forget and never blocks the caller's return, so
    it would never fail a test's assertions — it would just silently spend real
    API cost and wall-clock in the background on every such test run, including
    pre-existing tests (e.g. test_replan.py's #48(b) deadlock tests) that predate
    Stage 4 and were never written to expect a live subprocess. Same
    force-off-by-default accommodation as the gate fixtures above, applied to a
    side effect rather than a gate.

    Patches `cli._spawn_enumeration_worker` — the thin `cli`-owned wrapper around
    `proc_tree.launch_supervised` — rather than `cli.proc_tree.launch_supervised`
    itself: `cli.proc_tree` is the SAME module object `test_proc_tree.py` and
    test_kill_tree_cli.py` import directly (`import proc_tree`), so mutating that
    shared attribute here stubbed the real function out from under THEIR own
    subject-under-test too, for every test in the suite. Only the leaf spawn is
    suppressed; `_launch_enumeration`'s bag mutations (the deadline stamp and the
    not-run clear) still run for real, since pre-existing tests depend on them.

    A test that means to exercise the REAL launch mechanics (deadline stamping,
    sidecar landing, argv recording) re-patches `cli._spawn_enumeration_worker`
    itself — see test_enumerate_detach.py's behavioral launch tests.
    `test_sidecar_lands_after_launcher_process_exits` runs one fully unstubbed
    launch against a stub `claude` on PATH and asserts the child reaches the
    worker entry point, so the spawn-to-worker wiring is covered.

    KNOWN COVERAGE GAP, the price of this fixture: that unstubbed launch enters
    at `cli._launch_enumeration`, so no test runs one originating from
    `cmd_submit_plan`/`cmd_replan` — the command-level wiring is pinned only by
    recorder stubs."""
    from agentctl import cli
    monkeypatch.setattr(cli, "_spawn_enumeration_worker", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _code_review_gate_off_by_default(monkeypatch):
    """Default the code-review gate OFF for the suite at large, the same
    accommodation as `_plan_review_gate_off_by_default` above and for the same
    reason: `gates.code_review_blockers` fold-in blocks `record-result --status
    passed` on every SUBSTANTIVE spawn:developer stage until a bound passing
    CodeReview is recorded, but the overwhelming majority of substantive-flow
    tests (cost, dispatch, ledger, tracker, resolve-marker, drive-close, …) drive
    such a stage to PASSED to exercise unrelated machinery. AGENTCTL_CODE_REVIEW=0
    is the documented force-off knob (gates.code_review_active) — byte-identical
    to the gate being absent. Its real block/pass/stale/override behaviour is
    proven end-to-end by test_code_review.py, which explicitly re-enables it."""
    monkeypatch.setenv("AGENTCTL_CODE_REVIEW", "0")


@pytest.fixture(autouse=True)
def _isolate_self_diagnose_store(tmp_path, monkeypatch):
    """Redirect the self-diagnose findings store to tmp for the suite at large.

    hook-turn-end-gate's build_context reads the store to decide whether an open
    actionable finding blocks this turn. The real store on a live machine
    normally HAS open findings, so without this every unrelated turn-gate test
    would pick up a spurious extra blocker — and a test that writes would corrupt
    live runtime state. Same accommodation as `_isolate_task_quality_ledger`."""
    monkeypatch.setenv(
        "CLAUDE_SELF_DIAGNOSE_STORE", str(tmp_path / "self-diagnose-findings.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_judge_ledger(tmp_path, monkeypatch):
    """Redirect the judge execution ledger to tmp for the suite at large.

    lib/judge_ledger.py writes on every hook_start(), so any test that drives one
    of the three judge-calling hooks appends to the live ledger unless the env
    override is set — and that ledger is what a future reader will count real
    judge executions from, so suite lines in it are not clutter but wrong data.
    Same accommodation as `_isolate_self_diagnose_store`."""
    monkeypatch.setenv("AGENTCTL_JUDGE_LEDGER", str(tmp_path / "judge-usage-ledger.jsonl"))
    judge_ledger._state.update(
        {"invocation_id": None, "source": None, "hook": None, "judge": None}
    )


@pytest.fixture(autouse=True)
def _no_ambient_recursion_depth(monkeypatch):
    """Drop the ambient AGENT_RECURSION_DEPTH for the suite at large.

    hook-turn-end-gate's decide() returns None outright at depth >= 1 (a spawned
    specialist's turn-end contract is its return marker, not a root obligation),
    so a suite run from inside a spawned specialist silently allows every case
    that asserts a block. The three tests that own that predicate set the var
    themselves, which overrides this."""
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_project_dir(monkeypatch):
    """Drop the ambient CLAUDE_PROJECT_DIR for the suite at large.

    lib/hook_wiring.settings_chain() appends `$CLAUDE_PROJECT_DIR/.claude/
    settings*.json` to every chain it builds, INCLUDING one built from an
    explicit root. So a suite run under the harness takes two on-disk files
    nobody wrote as input: a project settings file registering a judge hook
    below its minimum, or one that will not parse, flips
    test_canon_guard_wired_check's strict-mode expectations from a fixture the
    test wrote to a file the machine happens to have. Same accommodation as
    `_no_ambient_recursion_depth` above — the tests that own the predicate
    (test_hook_wiring, test_dispatch_witness) set the variable themselves,
    which overrides this."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture
def store(tmp_path):
    return FileStateStore(tmp_path / "state")


@pytest.fixture
def fixtures_dir():
    return Path(__file__).resolve().parent / "fixtures"
