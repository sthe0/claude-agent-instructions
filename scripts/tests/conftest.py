"""Make the `agentctl` package importable and provide shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agentctl.store import FileStateStore  # noqa: E402

# The plan-level places a SUBSTANTIVE plan owes the submission seam, as TOML an author
# would write. Every fixture plan in the suite that expects to SUBMIT CLEAN splices these
# in; a fixture testing a plan-level refusal writes its own defective form instead.
#
# Shared rather than copied into each test module because the substantive grade keeps
# growing — `external_research`, then `knowledge`/`preconditions`, now the order — and a
# grade whose fixture is duplicated eight times is a grade nobody can extend without
# finding all eight. `_SUBSTANTIVE_META_FIELDS`/`_ORDER_PARTS` in submission.py are the
# requirement; this is the one compliant answer the suite writes against.
#
# `requirements` is spelled as [[meta.order.requirements]] array-of-tables rather than the
# inline `{ id = "...", text = "..." }` form the real plans use — the two parse identically,
# and the inline form's braces would be read as fields by the `.format()` calls several of
# these templates make.
SUBSTANTIVE_ORDER = """
[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "the norm governing an act of activity, in a test"

[[meta.order.requirements]]
id = "R1"
text = "the fixture plan meets the substantive grade"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
"""

# A plan-level end-to-end check, likewise required of a substantive plan. Written to be
# spliced at the END of a plan (a top-level array-of-tables closes any table context), and
# deliberately trivial: fixtures that care what a final_check DOES declare their own.
SUBSTANTIVE_FINAL_CHECK = """
[[final_check]]
command = "true"
expected_exit = 0
"""


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
def _advisor_off_by_default(monkeypatch):
    """Default the advisory judge OFF for the suite at large, the same accommodation as
    `_plan_review_gate_off_by_default` above and for the same reason.

    `advisor.resolve_enabled` falls back to config.md's `advisor-mode` (this repo's is
    `substantive`) whenever AGENTCTL_ADVISOR is unset, so any SUBSTANTIVE-weight-class
    session resolves the advisor live. That fallback is what `cmd_submit_plan`,
    `cmd_approve` and `cmd_replan` now resolve a runner through (the
    `run = runner if runner is not None else advisor.subprocess_runner` idiom, matching
    the pre-existing ledger/question/acceptance-review call sites) so the echo-warning
    judge is reachable in production — but those three commands are the ones the
    overwhelming majority of substantive-flow tests drive, almost none of them about the
    advisor. AGENTCTL_ADVISOR=0 is the documented force-off knob — byte-identical to the
    advisor being absent.

    The advisor's real enabled/fail-open/warning behaviour is proven end-to-end by
    test_advisor.py, test_result_image_echo.py and the other files that already set this
    var explicitly (test_acceptance_review_gate.py, test_code_review.py,
    test_plan_review_gate.py, test_confirm_delivery.py), which override this default —
    their own `monkeypatch.setenv`/`delenv` calls run after this fixture's within the same
    test and win."""
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")


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
def _no_ambient_recursion_depth(monkeypatch):
    """Drop the ambient AGENT_RECURSION_DEPTH for the suite at large.

    hook-turn-end-gate's decide() returns None outright at depth >= 1 (a spawned
    specialist's turn-end contract is its return marker, not a root obligation),
    so a suite run from inside a spawned specialist silently allows every case
    that asserts a block. The three tests that own that predicate set the var
    themselves, which overrides this."""
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)


@pytest.fixture
def store(tmp_path):
    return FileStateStore(tmp_path / "state")


@pytest.fixture
def fixtures_dir():
    return Path(__file__).resolve().parent / "fixtures"
