"""Stage 5 (R8b) of convergence-levers-r8-r9-r10-r2: the cap decision.

Merges every field-measured latency row (samples/judge-latency/
field-inputs-sample.json, stage 4's output) into the MEASURED rows for
feedback_signal, binary_ask and deferring_disposition -- zero outliers
excluded -- and fixes both judge-calling gates' whole-invocation budgets at a
flat 300s worst-case wait, per the user's decision recorded machine-readably
in samples/judge-latency/field-cap-decision.json.

Imports of the touched modules (`lib.judge_latency`, the two hook files) are
placed INSIDE each test function, not at module level, so these tests FAIL
individually against the pre-lever tree rather than making the whole file
error at collection.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = SCRIPTS_DIR.parent / "samples" / "judge-latency"
DECISION_PATH = SAMPLES_DIR / "field-cap-decision.json"


def _load_hook(filename: str):
    name = filename.replace("-", "_").removesuffix(".py")
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cap_decision_file_recorded():
    """The decision record exists, names all three merged judges with zero
    excluded outliers, and states both fixed 300s gate budgets."""
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    for judge in ("feedback_signal", "binary_ask", "deferring_disposition"):
        block = decision["judges"][judge]
        assert block["excluded_outliers"] == [], (
            f"{judge}: the decision was 'ничего не исключать' -- no field row "
            "may be dropped as an outlier"
        )
        assert "field-inputs-sample.json" in block["merged_provenance"]
    assert decision["gate_budgets"]["hook-turn-end-gate.py"]["_TURN_JUDGE_BUDGET_S"] == 300
    assert decision["gate_budgets"]["hook-deferring-disposition-gate.py"]["_ASK_JUDGE_BUDGET_S"] == 300


def test_hook_constants_follow_cap_decision():
    """The two gates' live constants match the decision record exactly --
    the record is not just documentation, it is what the hooks actually run."""
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    turn_end = _load_hook("hook-turn-end-gate.py")
    deferring = _load_hook("hook-deferring-disposition-gate.py")

    assert turn_end._TURN_JUDGE_BUDGET_S == 300
    assert turn_end._TURN_BINARY_ASK_CALL_CAP_S == (
        decision["judges"]["binary_ask"]["resulting_cap_s"]
    )
    assert turn_end._TURN_FEEDBACK_CALL_CAP_S == (
        decision["judges"]["feedback_signal"]["resulting_cap_s"]
    )
    # Floors are untouched by this stage: only the ceilings/budgets move.
    assert turn_end._TURN_FEEDBACK_MIN_CALL_S == 18
    assert turn_end._TURN_BINARY_ASK_MIN_CALL_S == 19

    assert deferring._ASK_JUDGE_BUDGET_S == 300
    assert deferring._ASK_JUDGE_MIN_CALL_S == (
        decision["judges"]["deferring_disposition"]["resulting_floor_s"]
    )


def test_the_ceiling_sum_invariant_holds():
    """feedback's cap + binary_ask's cap + silent_closure's cap (unchanged) +
    outage's floor (unchanged) + 1s head-room == the whole-gate budget exactly
    -- the arithmetic the cap decision and the hook's own comment both state."""
    turn_end = _load_hook("hook-turn-end-gate.py")
    total = (
        turn_end._TURN_FEEDBACK_CALL_CAP_S
        + turn_end._TURN_BINARY_ASK_CALL_CAP_S
        + turn_end._TURN_SILENT_CLOSURE_CALL_CAP_S
        + turn_end._TURN_OUTAGE_MIN_CALL_S
        + 1
    )
    assert total == turn_end._TURN_JUDGE_BUDGET_S == 300
    assert turn_end._TURN_SILENT_CLOSURE_CALL_CAP_S == 36
    assert turn_end._TURN_OUTAGE_MIN_CALL_S == 26


def test_merged_rows_exclude_no_field_observation():
    """Every judge's merged `n` accounts for its old series PLUS all 32 field
    rows -- nothing silently dropped in the merge."""
    from lib import judge_latency

    field = json.loads((SAMPLES_DIR / "field-inputs-sample.json").read_text(encoding="utf-8"))
    for judge in ("feedback_signal", "binary_ask", "deferring_disposition"):
        row = judge_latency.row(judge)
        field_n = len(field[judge]["rows"])
        assert field_n == 32
        # provenance lists >=1 old-series entries plus exactly one
        # field-inputs-sample.json entry; n must be large enough to have
        # absorbed all 32 field rows on top of whatever the old series held.
        assert row.n >= field_n
        assert ("field-inputs-sample.json", judge) in row.provenance


def test_last_resort_ceiling_covers_the_new_feedback_max():
    """feedback_signal's merged max (the 183.14s field outlier, kept standing)
    is now the family-wide slowest observed run, so LAST_RESORT_CEILING_S must
    have moved to track it -- and every advisor default that borrows this
    ceiling must have moved with it."""
    from lib import judge_latency

    row = judge_latency.row("feedback_signal")
    assert row.max_s > 180
    assert judge_latency.LAST_RESORT_CEILING_S >= int(row.max_s) + 1


def test_published_text_writer_budget_clears_the_new_last_resort_ceiling():
    """hook-published-text-writer-gate.py's own whole-invocation budget must
    stay at or above LAST_RESORT_CEILING_S + SIZE_HEADROOM_S even after the
    ceiling grew from feedback_signal's merge -- the ripple this stage's own
    edits create, not something the cap decision named directly."""
    from lib import judge_latency

    published_text = _load_hook("hook-published-text-writer-gate.py")
    needed = judge_latency.LAST_RESORT_CEILING_S + judge_latency.SIZE_HEADROOM_S
    assert published_text._PUBLISHED_TEXT_JUDGE_BUDGET_S >= needed


def test_harness_registrations_cover_the_new_budgets():
    """install-reminder-hooks.sh registers each judge-calling hook at or above
    that hook's own whole-invocation budget -- a registration below the budget
    would let the harness kill the process before the judge call it is timed
    to survive ever returns."""
    install_text = (SCRIPTS_DIR / "install-reminder-hooks.sh").read_text(encoding="utf-8")
    turn_end = _load_hook("hook-turn-end-gate.py")
    deferring = _load_hook("hook-deferring-disposition-gate.py")
    published_text = _load_hook("hook-published-text-writer-gate.py")

    assert f'"hook-turn-end-gate.py",   {turn_end._TURN_JUDGE_BUDGET_S + 5})' in install_text
    assert (
        f'"hook-deferring-disposition-gate.py", {deferring._ASK_JUDGE_BUDGET_S + 5})'
        in install_text
    )
    assert (
        f'"hook-published-text-writer-gate.py", '
        f'{published_text._PUBLISHED_TEXT_JUDGE_BUDGET_S})' in install_text
    )


def test_drift_monitoring_command_is_documented_not_reimplemented():
    """The cap decision explicitly declines to add new logging -- it points at
    the pre-existing ledger + judge-usage-report.py --check-drift. Pin that the
    exact command survives in both the decision record and the README, rather
    than a new logging mechanism appearing instead."""
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    command = decision["drift_monitoring"]["command"]
    assert command == "python3 scripts/judge-usage-report.py --check-drift --since 30d"

    readme = (SAMPLES_DIR / "README.md").read_text(encoding="utf-8")
    assert "### Cap decision" in readme
    assert command in readme
