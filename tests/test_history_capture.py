"""D9: `state.log(event, **fields)` (state.py) already accepts arbitrary keyword
fields; five call sites used it impoverished (`state.log("declare")` with no
fields at all) while plan_review's own call site was already rich. One test per
capture point (declare, critique, replan x2, present_plan, plan_review's
reviewer_token/reviewer_raw), driving the real CLI path in-process against a
temporary state store and asserting the field lands in history — plus a
control asserting none of the five commands gained a new refusal path when its
D9 optional argument is absent. Helpers mirror
test_diagnosing_customer_renegotiation_e2e.py's `_to_diagnosing`/`_bare_replan`
and test_replan.py's `_no_replan_authorization_gate` conventions exactly, so
this module drives real CLI dispatch end-to-end rather than calling gate
functions in isolation.

Lives at the worktree-root `tests/` rather than beside the rest of the suite in
scripts/tests/: the stage's frozen verify_command runs at verify_venue
"delivery" (SessionState.resolve_check_venue resolves that to the delivery
worktree root), and names this file as the literal repo-root-relative path
`tests/test_history_capture.py`. Rather than duplicate scripts/tests/
conftest.py's ~15 autouse isolation fixtures (env-var gate defaults, ledger/
store redirection), this module puts scripts/tests/ on sys.path and imports
them directly — pytest registers an imported fixture exactly like a local one,
and each fixture's own `__file__`-relative logic (e.g. `fixtures_dir`) still
resolves against conftest.py's real location, so the legacy fixtures/ directory
is reused rather than copied."""
from __future__ import annotations

import hashlib
import sys
from argparse import Namespace
from pathlib import Path

import pytest

_LEGACY_TESTS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "tests"
if str(_LEGACY_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LEGACY_TESTS_DIR))

import conftest  # noqa: E402,F401 - side effect: puts scripts/ on sys.path for agentctl below
from agentctl import cli  # noqa: E402
from agentctl.state import Node  # noqa: E402
from conftest import (  # noqa: E402,F401 - reuse the suite's fixtures rather than duplicate them
    store,
    fixtures_dir,
    _isolate_task_quality_ledger,
    _plan_review_gate_off_by_default,
    _plan_presentation_gate_off_by_default,
    _premise_gate_off_by_default,
    _no_real_enumeration_launch_by_default,
    _code_review_gate_off_by_default,
    _stage_review_gate_off_by_default,
    _acceptance_gate_off_by_default,
    _advisor_off_by_default,
    _isolate_self_diagnose_store,
    _isolate_judge_ledger,
    _isolate_task_accumulator,
    _no_ambient_recursion_depth,
    _no_ambient_project_dir,
    _default_claude_runtime_host,
)


def ns(**kw) -> Namespace:
    return Namespace(**kw)


def _sha256_file(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _no_replan_authorization_gate(monkeypatch):
    """Every replan exercised here is either DIAGNOSING-driven (the gate bypasses
    itself outright once the difficulty record is complete) or a bare
    non-DIAGNOSING refinement — the same carve-out test_replan.py uses, for the
    same reason: this module is not what replan_authorization_blockers itself is
    tested by (that is test_replan_authorization.py)."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def _to_executing_stage1(store, sid: str, plan: str, task: str) -> None:
    cli.cmd_start(ns(session=sid, task=task, goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


def _to_diagnosing(store, plan: str, sid: str, task: str) -> None:
    _to_executing_stage1(store, sid, plan, task)
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value


def _last_event(store, sid: str, name: str) -> dict:
    """The most recent `name` history event — NOT necessarily history[-1]: a bare
    (non-DIAGNOSING) no_change replan chains straight into `next_stage`, which
    appends its own event right after "replan"."""
    return next(e for e in reversed(store.load(sid).history) if e["event"] == name)


def _rendering(tmp_path, name="rendering.txt") -> str:
    """A `full`-kind rendering covering exactly plan_two_stage.toml's two stage
    anchors, so cmd_present_plan's completeness check never refuses it."""
    p = tmp_path / name
    p.write_text("[stage 1]\nfoo\n\n[stage 2]\nbar\n", encoding="utf-8")
    return str(p)


def _refined_plan(fixtures_dir, tmp_path, name="plan_refined.toml") -> str:
    """A prose-only edit (stage 1's title) of plan_two_stage.toml: same stages,
    same structural signature, so diff_plans() classifies it 'refinement' — the
    kind whose cli.py branch logs unconditionally, unlike the true byte-for-byte
    'no_change' no-op (which, outside DIAGNOSING, returns without touching
    history at all — a pre-existing, out-of-scope behavior, not a D9 capture
    point)."""
    text = (fixtures_dir / "plan_two_stage.toml").read_text(encoding="utf-8")
    text = text.replace('title = "Scaffold module"', 'title = "Scaffold module (refined)"')
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- declare -----------------------------------------------------------------

def test_declare_event_captures_declaration(store, fixtures_dir):
    sid = "hist-d1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_diagnosing(store, plan, sid, task="hist-declare")

    d = cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    assert d.ok is True

    event = store.load(sid).history[-1]
    assert event["event"] == "declare"
    assert event["expected"] == "e"
    assert event["actual"] == "a"
    assert event["mismatch"] == "m"


# --- critique ------------------------------------------------------------------

def test_critique_event_captures_critique(store, fixtures_dir):
    sid = "hist-c1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_diagnosing(store, plan, sid, task="hist-critique")
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)

    d = cli.cmd_critique(ns(
        session=sid, functional_ground="fg", replanning_task="rt",
        invariants_to_preserve=["inv1"], differences_to_remove=["diff1"],
        failure_address="нормативное",
    ), store=store)
    assert d.ok is True

    event = store.load(sid).history[-1]
    assert event["event"] == "critique"
    assert event["functional_ground"] == "fg"
    assert event["replanning_task"] == "rt"
    assert event["invariants_to_preserve"] == ["inv1"]
    assert event["differences_to_remove"] == ["diff1"]
    assert event["failure_address"] == "нормативное"


# --- replan --------------------------------------------------------------------

def test_replan_event_captures_cause_from_difficulty(store, fixtures_dir):
    sid = "hist-r1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_diagnosing(store, plan, sid, task="hist-replan-diff")
    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg-cause", replanning_task="rt-cause",
                        failure_address="нормативное"), store=store)
    cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)

    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)
    assert d.ok is True, d.detail

    event = _last_event(store, sid, "replan")
    assert event["cause_source"] == "difficulty"
    assert event["functional_ground"] == "fg-cause"
    assert event["replanning_task"] == "rt-cause"


def test_replan_event_captures_cause_from_explicit_reason(store, fixtures_dir, tmp_path):
    sid = "hist-r2"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan, task="hist-replan-reason")

    refined = _refined_plan(fixtures_dir, tmp_path, name="plan_refined_reason.toml")
    d = cli.cmd_replan(
        ns(session=sid, plan=refined, reason="user asked to fix a typo"), store=store,
    )
    assert d.ok is True, d.detail

    event = _last_event(store, sid, "replan")
    assert event["cause_source"] == "reason"
    assert event["reason"] == "user asked to fix a typo"


def test_replan_event_omits_cause_when_neither_available(store, fixtures_dir, tmp_path):
    sid = "hist-r3"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan, task="hist-replan-none")

    refined = _refined_plan(fixtures_dir, tmp_path, name="plan_refined_none.toml")
    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is True, d.detail

    event = _last_event(store, sid, "replan")
    assert "cause_source" not in event


# --- present_plan ----------------------------------------------------------------

def test_present_plan_event_captures_rejection_text(store, fixtures_dir, tmp_path):
    sid = "hist-p1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan, task="hist-present-plan")

    d = cli.cmd_present_plan(ns(
        session=sid, kind="full", plan=None, rendering_file=_rendering(tmp_path),
        emit_skeleton=False, rejection_text="too verbose, trim stage 2",
    ), store=store)
    assert d.ok is True, d.detail

    event = store.load(sid).history[-1]
    assert event["event"] == "present_plan"
    assert event["rejection_text"] == "too verbose, trim stage 2"


# --- plan_review reviewer_token / reviewer_raw ------------------------------------

def test_plan_review_event_captures_reviewer_token_and_raw(store, fixtures_dir):
    sid = "hist-pr1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan, task="hist-plan-review")

    d = cli.cmd_plan_review(ns(
        session=sid, target=None, scope=None, verdict="pass",
        reviewer="Thinker (fresh context)", concerns=None, note="",
        plan_digest=_sha256_file(plan), findings_blocking=None, findings_nonblocking=None,
    ), store=store)
    assert d.ok is True, d.detail

    event = store.load(sid).history[-1]
    assert event["event"] == "plan_review"
    assert event["reviewer"] == "Thinker (fresh context)"
    assert event["reviewer_raw"] == "Thinker (fresh context)"
    assert event["reviewer_token"] == "thinker"


# --- no-new-argument behavior-unchanged control -----------------------------------

def test_five_commands_unchanged_without_new_arguments(store, fixtures_dir, tmp_path):
    """Mutation-catalogue anchor for the 7th ('refusal') mutation. None of
    declare/critique/replan/present_plan/plan_review may grow a new refusal path
    when its D9 optional argument is absent — every Namespace below carries
    exactly the fields these commands accepted before D9 (no --reason, no
    --rejection-text), the same shape every pre-existing test in this suite
    builds them with. If a mutation adds e.g. `if not getattr(args, "reason",
    None): return Directive(False, ...)` to cmd_replan (or the present_plan
    equivalent on `rejection_text`), this test goes red."""
    sid = "hist-nr1"
    plan = str(fixtures_dir / "plan_two_stage.toml")
    _to_executing_stage1(store, sid, plan, task="hist-no-new-args")

    # a FAILED stage routes to DIAGNOSING via an ok=False "go declare" directive —
    # not a command failure; mirrors _to_diagnosing's own (unchecked-for-.ok) call.
    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value

    # declare: exact Directive match
    d = cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    assert d == cli.Directive(
        ok=True,
        node="DIAGNOSING",
        action="investigate",
        detail="declaration recorded; localize the divergence next (investigate)",
        marker=None,
        data={},
    ), f"declare mismatch: got {d}"

    # investigate: exact Directive match
    d = cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                              hypotheses=["h1", "h2"]), store=store)
    assert d == cli.Directive(
        ok=True,
        node="DIAGNOSING",
        action="critique",
        detail="investigation recorded; state the functional ground + replanning task (critique)",
        marker=None,
        data={},
    ), f"investigate mismatch: got {d}"

    # critique: exact Directive match
    d = cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt",
                            failure_address="нормативное"), store=store)
    assert d == cli.Directive(
        ok=True,
        node="DIAGNOSING",
        action="replan",
        detail="difficulty cycle complete; replan is now unblocked",
        marker=None,
        data={},
    ), f"critique mismatch: got {d}"

    # normalize: exact Directive match
    d = cli.cmd_normalize(ns(session=sid, factor="reproducible cause", level="note"), store=store)
    assert d == cli.Directive(
        ok=True,
        node="DIAGNOSING",
        action="replan",
        detail="renorming recorded; replan is now unblocked",
        marker=None,
        data={},
    ), f"normalize mismatch: got {d}"

    # replan: legacy Namespace, no `reason` attribute at all
    # Exits DIAGNOSING back to VERIFYING on difficulty closure; detail message is stable.
    d = cli.cmd_replan(ns(session=sid, plan=plan), store=store)
    assert d.ok is True, d.detail
    assert d.node in ("VERIFYING", "EXECUTING"), f"replan node: expected 'VERIFYING' or 'EXECUTING', got {d.node!r}"
    assert d.action in ("next_stage", "continue"), f"replan action: got {d.action!r}"
    assert d.marker is None, f"replan marker: expected None, got {d.marker!r}"
    assert "difficulty worked through" in d.detail, f"replan detail: expected 'difficulty worked through' in {d.detail!r}"
    # data may contain advisories, which is OK (advisory list is not a refusal)
    assert isinstance(d.data, dict), f"replan data must be a dict, got {type(d.data)}"

    # present_plan: legacy Namespace, no `rejection_text` attribute at all
    # State is now VERIFYING after replan exited DIAGNOSING. Volatile detail field contains
    # "FINAL text message" choreography; data hashes are deterministic from file content.
    rendering_file = _rendering(tmp_path, name="rendering_legacy.txt")
    d = cli.cmd_present_plan(ns(
        session=sid, kind="full", plan=None,
        rendering_file=rendering_file,
        emit_skeleton=False,
    ), store=store)
    assert d.ok is True, d.detail
    assert d.node in ("VERIFYING", "EXECUTING"), f"present_plan node: expected 'VERIFYING' or 'EXECUTING', got {d.node!r}"
    assert d.action == "continue", f"present_plan action: expected 'continue', got {d.action!r}"
    assert d.marker is None, f"present_plan marker: expected None, got {d.marker!r}"
    assert "rendering_sha256" in d.data, f"present_plan data must have rendering_sha256, got {d.data.keys()}"
    assert "plan_sha256" in d.data, f"present_plan data must have plan_sha256, got {d.data.keys()}"
    # Both hashes must be 64-char hex (SHA256)
    assert len(d.data["rendering_sha256"]) == 64, f"rendering_sha256 wrong length: {d.data['rendering_sha256']!r}"
    assert len(d.data["plan_sha256"]) == 64, f"plan_sha256 wrong length: {d.data['plan_sha256']!r}"

    # plan_review: legacy Namespace, exactly the pre-D9 field set
    # State is still VERIFYING after present_plan.
    d = cli.cmd_plan_review(ns(
        session=sid, target=None, scope=None, verdict="pass", reviewer="thinker",
        concerns=None, note="", plan_digest=_sha256_file(plan),
        findings_blocking=None, findings_nonblocking=None,
    ), store=store)
    assert d.ok is True, d.detail
    assert d.node in ("VERIFYING", "EXECUTING"), f"plan_review node: expected 'VERIFYING' or 'EXECUTING', got {d.node!r}"
    assert d.action == "continue", f"plan_review action: expected 'continue', got {d.action!r}"
    assert d.marker is None, f"plan_review marker: expected None, got {d.marker!r}"
    assert "thinker review recorded" in d.detail, f"plan_review detail: expected 'thinker review recorded' in {d.detail!r}"
    assert "verdict=pass" in d.detail, f"plan_review detail: expected 'verdict=pass' in {d.detail!r}"
