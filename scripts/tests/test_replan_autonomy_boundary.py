"""Unit 2: inside the boundary of an order the user already approved, the coordinator
self-approves further plans, replans and permission requests; outside it, the user decides.

Every test drives the engine through `agentctl` CLI argv (`cli.main`) against a real
`FileStateStore` and the real order-approvals ledger (isolated per test by conftest),
asserting on the Directive JSON, the persisted state and the ledger file. The only
state the tests write directly is what the CLI cannot reach: a parked permission
request, the accumulated replan history, and the stage-level cost log the engine's own
`--cost-log` flag reads.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentctl import cli, effort, order_approvals as oa
from agentctl.config import Thresholds
from agentctl.plan import load_plan, order_digest
from agentctl.state import Node, PermissionRequest
from agentctl.store import FileStateStore
from conftest import SUBSTANTIVE_ORDER

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE_GOAL = 'goal = "Demonstrate the full two-stage coordination cycle"'
BASE_REQ = 'text = "the fixture plan meets the substantive grade"'
OBSERVATIONS = (
    "mod.py is on disk and importing it in a fresh interpreter raised nothing",
    "ran pytest over tests/test_mod.py: every case passed, none failed",
)
STAGE2_TAIL = 'output_artifacts = ["tests/test_mod.py"]\n'


def _with_order(text: str, order: str = SUBSTANTIVE_ORDER) -> str:
    head, sep, tail = text.partition("\n[[stage]]")
    return head + "\n" + order + sep + tail


def plan_text(*, goal=None, req=None, place=None, customer=None, traceability=False,
              extra_outputs=(), executor2=None, stage2_extra="", stage3=False,
              verify1=None, repo_root=None, negative1=None) -> str:
    """The two-stage fixture with a `[meta.order]`, varied on exactly one axis per test."""
    order = SUBSTANTIVE_ORDER
    if req:
        order = order.replace("the fixture plan meets the substantive grade", req)
    if place:
        order = order.replace("the norm governing an act of activity, in a test", place)
    if customer:
        order = order.replace('customer_id = "user"', f'customer_id = "{customer}"')
    if traceability:
        order = order.replace('functional_place =', 'requires_traceability = true\nfunctional_place =')
    text = _with_order((FIXTURES / "plan_two_stage.toml").read_text(encoding="utf-8"), order)
    if goal:
        text = text.replace(BASE_GOAL, f'goal = "{goal}"')
    if extra_outputs:
        listed = ", ".join(f'"{p}"' for p in ("tests/test_mod.py", *extra_outputs))
        text = text.replace(STAGE2_TAIL, f"output_artifacts = [{listed}]\n")
    if verify1:
        extra = f'verify_command = "{verify1}"\n' + (f'negative_control = "{negative1}"\n' if negative1 else "")
        text = text.replace("depends_on = []\n", "depends_on = []\n" + extra, 1)
    if repo_root:
        text = text.replace('criterion_type = "measurable"\n', f'criterion_type = "measurable"\nrepo_root = "{repo_root}"\n', 1)
    if executor2:
        head, sep, tail = text.rpartition('executor = "spawn:developer"')
        text = head + f'executor = "{executor2}"' + tail
    if stage2_extra:
        text = text.rstrip("\n") + "\n" + stage2_extra
    if stage3:
        text = text.rstrip("\n") + (
            '\n\n[[stage]]\nindex = 3\ntitle = "Wire CI"\nexecutor = "spawn:developer"\n'
            'expected_result_image = "CI config runs the suite"\ncriterion_type = "measurable"\n'
            'done_criterion = "the CI config names test_mod"\ndepends_on = [2]\n'
        )
    return text


def grant_block(*rules: str) -> str:
    quoted = ", ".join(f'"{r}"' for r in rules)
    return f"\n[stage.grants]\nallow = [{quoted}]\n"


LAND_MAIN = ('verify_kind = "landed"\n\n[stage.landed]\ntarget = "main"\nremote = "origin"\n'
             'delivered_stage = 1\n')
PUSH_MAIN = "Bash(git push origin HEAD:main)"


class Eng:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "state"
        self.store = FileStateStore(self.root)
        self._n = 0

    def run(self, cmd: str, **flags):
        argv = ["--state-root", str(self.root), cmd.replace("_", "-")]
        for name, value in flags.items():
            flag = "--" + name.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    argv.extend([flag, str(item)])
            elif value is not None and value is not False:
                argv.extend([flag, str(value)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(argv)
        out = json.loads(buf.getvalue())
        out["rc"] = rc
        return out

    def write(self, text: str, name: str | None = None) -> str:
        self._n += 1
        path = self.tmp / (name or f"plan{self._n}.toml")
        path.write_text(text, encoding="utf-8")
        return str(path)

    def state(self, sid: str):
        return self.store.load(sid)

    def open(self, sid: str, plan: str, *, task: str | None = None):
        self.run("start", session=sid, task=task or f"task-{sid}", goal="g", done_criterion="dc")
        self.run("classify", session=sid, architectural=True, files=5, changed_lines=200,
                 wall_clock_min=60)
        self.run("plan", session=sid)
        return self.run("submit_plan", session=sid, plan=plan)

    def approve_as_user(self, sid: str, plan: str, *, task: str | None = None):
        d = self.open(sid, plan, task=task)
        assert d["marker"] == "PLAN-READY", d
        d = self.run("approve", session=sid, by="user")
        assert d["ok"], d
        return d

    def execute(self, sid: str):
        self.run("partition", session=sid)
        return self.run("next_stage", session=sid)

    def fail_stage(self, sid: str):
        self.execute(sid)
        d = self.run("record_result", session=sid, status="failed", actual="boom")
        assert self.state(sid).node == Node.DIAGNOSING.value, d
        return d

    def diagnose(self, sid: str, tag: str = "a", *, difference: bool = True, normalize: bool = True):
        self.run("declare", session=sid, expected=f"e-{tag}", actual=f"a-{tag}", mismatch=f"m-{tag}")
        self.run("investigate", session=sid, localized_expectation=f"le-{tag}",
                 localized_actual=f"la-{tag}", hypothesis=[f"h1-{tag}", f"h2-{tag}"])
        self.run("critique", session=sid, functional_ground=f"fg-{tag}", replanning_task=f"rt-{tag}",
                 failure_address="нормативное",
                 difference_to_remove=[f"d-{tag}"] if difference else None)
        if normalize:
            self.run("normalize", session=sid, factor=f"factor-{tag}", level="note")

    def park_permission(self, sid: str, action: str = "need a grant"):
        state = self.state(sid)
        state.permission_request = PermissionRequest(action=action, stage_index=1, raw=action)
        self.store.save(state)

    def seed_replans(self, sid: str, count: int):
        state = self.state(sid)
        state.history.extend({"event": "replan"} for _ in range(count))
        self.store.save(state)

    def cost_log(self, plan: str, usd: float) -> str:
        path = self.tmp / "costs.jsonl"
        path.write_text(json.dumps({"plan_path": plan, "stage_index": 1, "cost_usd": usd}) + "\n",
                        encoding="utf-8")
        return str(path)

    def spend_fire(self, sid: str, plan: str, usd: float = 100.0):
        d = self.run("record_result", session=sid, status="passed", actual="ok",
                     control="reviewed: ok", observation=OBSERVATIONS[0],
                     cost_log=self.cost_log(plan, usd))
        assert self.state(sid).effort_fires, d
        return d

    def ledger(self, plan: str) -> dict:
        return oa.get(order_digest(load_plan(plan, strict=False)))


@pytest.fixture
def eng(tmp_path):
    return Eng(tmp_path)


@pytest.fixture(autouse=True)
def _no_ambient_harness_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def action(d: dict):
    return (d.get("data") or {}).get("autonomy", {}).get("action")


def autonomy(d: dict) -> dict:
    return (d.get("data") or {}).get("autonomy", {})


def approved_order(eng: Eng, text: str | None = None, sid: str = "u1"):
    plan = eng.write(text or plan_text(), "approved.toml")
    eng.approve_as_user(sid, plan)
    return plan


# --- the threshold and the ledger -----------------------------------------------------


def test_threshold_value_is_five():
    assert Thresholds().effort_replan_absolute() == 5
    config = (SCRIPTS.parent / "config.md").read_text(encoding="utf-8")
    assert "| `effort-replan-absolute` | `5` |" in config


def test_resolved_reopen_needs_user_decision_only_from_fifth(eng):
    from agentctl import task_accumulator

    def reopen(sid, task, **extra):
        eng.run("start", session=sid, task=task, goal="g", done_criterion="dc")
        state = eng.state(sid)
        state.resolution.passed = True
        state.node = Node.RESOLVED.value
        eng.store.save(state)
        return eng.run("reset", session=sid, task=task, reopen_reason="the last lap missed a case", **extra)

    task_accumulator.add("t4", "resolved_reentry", 4, session_id="x", now=None)
    assert reopen("s4", "t4")["ok"] is True
    task_accumulator.add("t5", "resolved_reentry", 5, session_id="x", now=None)
    refused = reopen("s5", "t5")
    assert refused["ok"] is False
    assert reopen("s5", "t5", reopen_user_decision="the user said continue")["ok"] is True


def test_customer_approval_stamps_effort_estimate(eng):
    plan = approved_order(eng)
    record = eng.ledger(plan)["records"][-1]
    estimate = record.get("effort_estimate") or {}
    assert set(estimate) == {"spend", "wall_clock"}
    assert estimate["spend"] > 0 and estimate["wall_clock"] > 0


# --- who approves a plan -------------------------------------------------------------


def test_continuation_session_within_ledger_self_approved(eng):
    plan = approved_order(eng)
    d = eng.open("c1", plan)
    assert action(d) == "self_approve"
    assert d["marker"] is None
    assert eng.run("approve", session="c1", by="agent")["ok"] is True
    assert eng.state("c1").approval.passed is True


def test_fresh_order_initial_approval_goes_to_user(eng):
    plan = eng.write(plan_text(), "fresh.toml")
    d = eng.open("f1", plan)
    assert action(d) == "await_user_approval"
    assert d["marker"] == "PLAN-READY"
    refused = eng.run("approve", session="f1", by="agent")
    assert refused["ok"] is False


def test_substantive_replan_within_boundary_self_approved(eng):
    plan = approved_order(eng)
    eng.open("r1", plan)
    eng.run("approve", session="r1", by="agent")
    eng.fail_stage("r1")
    eng.diagnose("r1")
    d = eng.run("replan", session="r1", plan=eng.write(plan_text(stage3=True)))
    assert action(d) == "self_approve", d
    assert eng.run("approve", session="r1", by="agent")["ok"] is True


@pytest.mark.parametrize("variant,kwargs", [
    ("new_file", dict(extra_outputs=("brand_new_module.py",))),
    ("new_specialist", dict(executor2="spawn:tech-writer")),
])
def test_new_resource_goes_to_user(eng, variant, kwargs):
    plan = approved_order(eng)
    d = eng.open("n1", eng.write(plan_text(**kwargs)))
    assert action(d) == "await_user_approval"
    assert d["marker"] == "PLAN-READY"
    assert autonomy(d)["extra_resources"]


def test_new_file_resource_goes_to_user(eng):
    test_new_resource_goes_to_user(eng, "new_file", dict(extra_outputs=("brand_new_module.py",)))


def test_new_specialist_kind_goes_to_user(eng):
    test_new_resource_goes_to_user(eng, "new_specialist", dict(executor2="spawn:tech-writer"))


def test_new_landing_target_goes_to_user(eng):
    approved_order(eng, plan_text(stage2_extra=LAND_MAIN))
    other = LAND_MAIN.replace('target = "main"', 'target = "ticket/other"')
    d = eng.open("l1", eng.write(plan_text(stage2_extra=other)))
    assert action(d) == "await_user_approval"
    assert any("ticket/other" in json.dumps(r) for r in autonomy(d)["extra_resources"])


@pytest.mark.parametrize("kwargs", [
    dict(goal="Demonstrate a different goal entirely"),
    dict(req="the fixture plan meets a different requirement"),
    dict(place="a different functional place"),
    dict(customer="someone-else"),
    dict(traceability=True),
])
def test_order_change_goes_to_user(eng, kwargs):
    approved_order(eng)
    d = eng.open("o1", eng.write(plan_text(**kwargs)))
    assert action(d) == "await_user_approval"
    assert autonomy(d)["reason"]


def test_goal_wording_change_goes_to_user(eng):
    test_order_change_goes_to_user(eng, dict(goal="Demonstrate a different goal entirely"))


def test_requirement_text_change_goes_to_user(eng):
    test_order_change_goes_to_user(eng, dict(req="the fixture plan meets a different requirement"))


def test_functional_place_traceability_customer_change_goes_to_user(eng):
    test_order_change_goes_to_user(eng, dict(place="a different functional place"))
    for i, kwargs in enumerate((dict(customer="someone-else"), dict(traceability=True))):
        d = eng.open(f"o-{i}", eng.write(plan_text(**kwargs)))
        assert action(d) == "await_user_approval"


def test_refinement_moving_order_digest_goes_to_user(eng):
    plan = approved_order(eng)
    eng.open("m1", plan)
    eng.run("approve", session="m1", by="agent")
    eng.fail_stage("m1")
    eng.diagnose("m1", difference=False)
    moved = eng.write(plan_text(goal="Demonstrate the cycle, re-scoped by the coordinator"))
    d = eng.run("replan", session="m1", plan=moved)
    assert action(d) == "await_user_approval", d
    assert d["marker"] == "PLAN-READY"
    assert autonomy(d)["order_changed"] is True


# --- runtime grants -----------------------------------------------------------------


def _customer_grant(eng, sid, plan, scope, rule):
    eng.open(sid, plan)
    eng.run("approve", session=sid, by="agent")
    eng.execute(sid)
    eng.park_permission(sid)
    d = eng.run("resolve_permission", session=sid, decision="granted", by="user", scope=scope, rule=[rule])
    assert d["ok"], d


def test_stage_scoped_runtime_grant_reused_is_self_approved(eng):
    plan = approved_order(eng)
    _customer_grant(eng, "g1", plan, "stage", PUSH_MAIN.replace("main", "ticket/x"))
    wider = eng.write(plan_text(stage2_extra=grant_block(PUSH_MAIN.replace("main", "ticket/x"))))
    assert action(eng.open("g2", wider)) == "self_approve"


def test_once_scoped_runtime_grant_does_not_widen_boundary(eng):
    plan = approved_order(eng)
    rule = PUSH_MAIN.replace("main", "ticket/x")
    _customer_grant(eng, "g1", plan, "once", rule)
    assert action(eng.open("g2", eng.write(plan_text(stage2_extra=grant_block(rule))))) == "await_user_approval"


def test_earlier_session_grant_same_order_counts(eng):
    plan = approved_order(eng)
    rule = PUSH_MAIN.replace("main", "ticket/x")
    _customer_grant(eng, "g1", plan, "stage", rule)
    eng.run("block", session="g1", reason="done with this session")
    later = eng.open("g3", eng.write(plan_text(stage2_extra=grant_block(rule))))
    assert action(later) == "self_approve"


def test_runtime_grant_under_other_order_does_not_widen_boundary(eng):
    plan = approved_order(eng)
    other = eng.write(plan_text(goal="An unrelated order altogether"), "other.toml")
    eng.approve_as_user("o1", other)
    rule = PUSH_MAIN.replace("main", "ticket/x")
    _customer_grant(eng, "g1", other, "stage", rule)
    d = eng.open("g2", eng.write(plan_text(stage2_extra=grant_block(rule))))
    assert action(d) == "await_user_approval"
    assert plan


# --- changed commands of unknown effect ------------------------------------------------


@pytest.fixture
def venue(tmp_path):
    root = tmp_path / "venue"
    root.mkdir()
    return root


def cmd_plan(venue: Path, verify: str = "frobnicate --fast", **kwargs) -> str:
    return plan_text(verify1=verify, repo_root=str(venue), **kwargs)


def thinker_review(eng: Eng, sid: str, plan: str, verdict: str = "pass", reviewer: str = "thinker"):
    digest = hashlib.sha256(Path(plan).read_bytes()).hexdigest()
    return eng.run("plan_review", session=sid, verdict=verdict, reviewer=reviewer, target=plan,
                   plan_digest=digest)


def changed(d: dict) -> list[dict]:
    return autonomy(d)["unresolved_changed_commands"]


def test_unresolved_changed_engine_command_needs_first_thinker_pass(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    d = eng.open("t1", slow)
    assert action(d) == "await_user_approval"
    assert [c["source"] for c in changed(d)] == ["frobnicate --slow"]
    assert autonomy(d)["thinker_pass"] is False
    assert eng.run("approve", session="t1", by="agent")["ok"] is False
    assert thinker_review(eng, "t1", slow)["ok"] is True
    later = eng.open("t2", slow)
    assert action(later) == "self_approve"
    assert autonomy(later)["thinker_pass"] is True


def test_first_thinker_verdict_per_identity_set_survives_byte_edit(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    thinker_review(eng, "t1", slow)
    retitled = eng.write(cmd_plan(venue, "frobnicate --slow").replace('title = "', 'title = "Edited ', 1))
    assert action(eng.open("t2", retitled)) == "self_approve"
    other = eng.write(cmd_plan(venue, "frobnicate --medium"))
    assert action(eng.open("t3", other)) == "await_user_approval"


def test_declared_unresolved_wildcard_rule_goes_to_user_despite_thinker_pass(eng, venue):
    approved_order(eng, cmd_plan(venue))
    wild = eng.write(cmd_plan(venue, stage2_extra=grant_block("Bash(python3 scripts/foo.py:*)")))
    d = eng.open("t1", wild)
    assert action(d) == "await_user_approval"
    declared = [c for c in changed(d) if c["origin"] == "declared"]
    assert declared and all(c["needs_user"] for c in declared)
    thinker_review(eng, "t1", wild)
    assert action(eng.open("t2", wild)) == "await_user_approval"


def test_derived_rule_judged_by_its_source_command(eng, venue):
    approved_order(eng, cmd_plan(venue))
    d = eng.open("t1", eng.write(cmd_plan(venue, "frobnicate --slow")))
    derived = [c for c in changed(d) if c["origin"] == "verify_command"]
    assert [(c["source"], c["needs_user"]) for c in derived] == [("frobnicate --slow", False)]


def test_helper_edit_under_same_command_text_goes_to_user(eng, venue, tmp_path):
    helper = tmp_path / "outside_helper.py"
    helper.write_text("print(1)\n", encoding="utf-8")
    command = f"frobnicate {helper}"
    plan = approved_order(eng, cmd_plan(venue, command))
    assert action(eng.open("t1", plan)) == "self_approve"
    helper.write_text("print(2)\n", encoding="utf-8")
    d = eng.open("t2", plan)
    assert action(d) == "await_user_approval"
    assert [c["source"] for c in changed(d)] == [command]
    assert autonomy(d)["thinker_pass"] is False


def release_review_rounds(eng: Eng, sid: str) -> None:
    state = eng.state(sid)
    state.plan_review_rounds = Thresholds().effort_replan_absolute()
    eng.store.save(state)


def test_agent_override_does_not_satisfy_unresolved_command_condition(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    release_review_rounds(eng, "t1")
    assert thinker_review(eng, "t1", slow, "override", "agent")["ok"] is True
    assert eng.state("t1").plan_review.verdict == "override"
    assert not (eng.ledger(slow).get("first_thinker_verdicts") or {})
    assert action(eng.open("t2", slow)) == "await_user_approval"


def test_autonomy_data_and_audit_log_list_changed_commands(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    thinker_review(eng, "t1", slow)
    d = eng.run("approve", session="t1", by="agent")
    assert d["ok"] is True, d
    entry = [e for e in eng.state("t1").history if e.get("event") == "approve"][-1]
    assert entry["by"] == "agent"
    assert entry["unresolved_changed_commands"] == ["frobnicate --slow"]
    assert entry["extra_resources"] == []


def test_first_thinker_verdict_not_carried_across_user_reapproval(eng, venue):
    approved_order(eng, cmd_plan(venue))
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    thinker_review(eng, "t1", slow)
    assert action(eng.open("t2", slow)) == "self_approve"
    reapproved = eng.write(cmd_plan(venue, stage3=True))
    eng.open("u2", reapproved)
    assert eng.run("approve", session="u2", by="user")["ok"] is True
    d = eng.open("t3", slow)
    assert action(d) == "await_user_approval"
    assert autonomy(d)["thinker_pass"] is False


# --- the coordinator's plan-review override -------------------------------------------


def test_agent_plan_review_override_after_round_release(eng):
    plan = approved_order(eng)
    eng.open("o1", plan)
    release_review_rounds(eng, "o1")
    d = thinker_review(eng, "o1", plan, "override", "agent")
    assert d["ok"] is True, d
    review = eng.state("o1").plan_review
    assert (review.verdict, review.reviewer) == ("override", "agent")


def test_agent_plan_review_override_refused_before_round_release(eng):
    plan = approved_order(eng)
    eng.open("o1", plan)
    d = thinker_review(eng, "o1", plan, "override", "agent")
    assert d["ok"] is False
    assert "release" in d["detail"]
    assert eng.state("o1").plan_review is None


def test_present_directive_discloses_agent_review_override(eng, tmp_path):
    plan = approved_order(eng)
    eng.open("o1", plan)
    rendering = tmp_path / "essence.md"
    rendering.write_text("Summary of the plan.", encoding="utf-8")
    refusal = eng.run("present_plan", session="o1", kind="essence", rendering_file=str(rendering))
    rendering.write_text("Summary of the plan.\n" + refusal["data"]["grants_block"], encoding="utf-8")
    before = eng.run("present_plan", session="o1", kind="essence", rendering_file=str(rendering))
    assert before["ok"] is True, before
    assert "agent_review_override" not in before["data"]
    release_review_rounds(eng, "o1")
    thinker_review(eng, "o1", plan, "override", "agent")
    after = eng.run("present_plan", session="o1", kind="essence", rendering_file=str(rendering))
    assert after["data"]["agent_review_override"]["reviewer"] == "agent"


