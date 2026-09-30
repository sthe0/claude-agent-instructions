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
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exit_:
                return {"ok": False, "rc": exit_.code, "marker": None, "detail": err.getvalue()}
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


def assert_new_resource_goes_to_user(eng: Eng, **plan_kwargs):
    approved_order(eng)
    d = eng.open("n1", eng.write(plan_text(**plan_kwargs)))
    assert action(d) == "await_user_approval"
    assert d["marker"] == "PLAN-READY"
    assert autonomy(d)["extra_resources"]


def test_new_file_resource_goes_to_user(eng):
    assert_new_resource_goes_to_user(eng, extra_outputs=("brand_new_module.py",))


def test_new_specialist_kind_goes_to_user(eng):
    assert_new_resource_goes_to_user(eng, executor2="spawn:tech-writer")


def test_new_landing_target_goes_to_user(eng):
    approved_order(eng, plan_text(stage2_extra=LAND_MAIN))
    other = LAND_MAIN.replace('target = "main"', 'target = "ticket/other"')
    d = eng.open("l1", eng.write(plan_text(stage2_extra=other)))
    assert action(d) == "await_user_approval"
    assert any("ticket/other" in json.dumps(r) for r in autonomy(d)["extra_resources"])


def assert_order_change_goes_to_user(eng: Eng, *variants: dict):
    approved_order(eng)
    for i, plan_kwargs in enumerate(variants):
        d = eng.open(f"o{i}", eng.write(plan_text(**plan_kwargs), f"order-variant-{i}.toml"))
        assert action(d) == "await_user_approval", plan_kwargs
        assert autonomy(d)["reason"]


def test_goal_wording_change_goes_to_user(eng):
    assert_order_change_goes_to_user(eng, dict(goal="Demonstrate a different goal entirely"))


def test_requirement_text_change_goes_to_user(eng):
    assert_order_change_goes_to_user(eng, dict(req="the fixture plan meets a different requirement"))


def test_functional_place_traceability_customer_change_goes_to_user(eng):
    assert_order_change_goes_to_user(
        eng, dict(place="a different functional place"), dict(customer="someone-else"),
        dict(traceability=True),
    )


def test_resource_dropped_at_user_reapproval_is_no_longer_approved(eng):
    approved_order(eng, plan_text(extra_outputs=("brand_new_module.py",)))
    narrowed = eng.write(plan_text(), "narrowed.toml")
    eng.open("r1", narrowed)
    assert eng.run("approve", session="r1", by="user")["ok"] is True
    d = eng.open("r2", eng.write(plan_text(extra_outputs=("brand_new_module.py",)), "readded.toml"))
    assert action(d) == "await_user_approval"
    assert any("brand_new_module.py" in json.dumps(r) for r in autonomy(d)["extra_resources"])


def narrowed_after_user_dropped_push(eng: Eng, sid: str) -> str:
    """The user approves an order that includes a push to ticket/x, then re-approves it
    without that push; returns the narrowed plan's path with an agent session executing it."""
    rule = PUSH_MAIN.replace("main", "ticket/x")
    wide = approved_order(eng, plan_text(stage2_extra=grant_block(rule)))
    assert "ticket/x" in json.dumps(eng.ledger(wide)["records"][0]["resources"])
    narrowed = eng.write(plan_text(), "narrowed-grant.toml")
    eng.open("re", narrowed)
    assert eng.run("approve", session="re", by="user")["ok"] is True
    agent_session(eng, sid, narrowed)
    return narrowed


def test_resource_dropped_at_user_reapproval_is_refused_by_agent_resolve_permission(eng):
    narrowed_after_user_dropped_push(eng, "a1")
    eng.park_permission("a1", PUSH_MAIN)
    refused = eng.run("resolve_permission", session="a1", decision="granted", by="agent",
                      scope="stage", rule=[PUSH_MAIN.replace("main", "ticket/x")])
    assert refused["ok"] is False
    assert "not covered by any resource the customer approved" in refused["detail"]


def test_resource_dropped_at_user_reapproval_routes_dispatched_request_to_user(eng):
    from argparse import Namespace

    from agentctl.dispatch import RunResult

    narrowed_after_user_dropped_push(eng, "a1")
    rule = PUSH_MAIN.replace("main", "ticket/x")
    reply = f"PERMISSION-REQUEST:\nAction: push\nWhy: needed\nFallback if denied: stop\nRule: {rule}\n"
    d = cli.cmd_dispatch(
        Namespace(session="a1", budget="medium", complexity="medium", dry_run=False),
        store=eng.store, runner=lambda argv: RunResult(0, stdout=reply), perm_checker=lambda a: False,
    )
    assert d.action == "ask_user_permission", d.detail
    assert d.data["reason_class"] == "not-approved", d.data


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
    return autonomy(d).get("unresolved_changed_commands") or []


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


def test_first_thinker_verdict_not_carried_across_user_reapproval(eng, venue):
    base = cmd_plan(venue)
    approved_order(eng, base)
    slow = eng.write(cmd_plan(venue, "frobnicate --slow"))
    eng.open("t1", slow)
    thinker_review(eng, "t1", slow)
    assert action(eng.open("t2", slow)) == "self_approve"
    retitled = eng.write(base.replace('title = "', 'title = "Reapproved ', 1), "reapproved.toml")
    eng.open("t3", retitled)
    assert eng.run("approve", session="t3", by="user")["ok"] is True
    assert [r["by"] for r in eng.ledger(retitled)["records"]] == ["user", "user"]
    d = eng.open("t4", slow)
    assert action(d) == "await_user_approval"
    assert autonomy(d)["thinker_pass"] is False


def test_declared_unresolved_wildcard_rule_goes_to_user_despite_thinker_pass(eng, venue):
    approved_order(eng, cmd_plan(venue))
    wild = eng.write(cmd_plan(venue, stage2_extra=grant_block("Bash(python3 scripts/foo.py:*)")))
    d = eng.open("t1", wild)
    assert action(d) == "await_user_approval"
    declared = [c for c in changed(d) if c["origin"] == "declared"]
    assert declared and all(c["needs_user"] for c in declared)
    thinker_review(eng, "t1", wild)
    assert action(eng.open("t2", wild)) == "await_user_approval"


@pytest.mark.parametrize("command", ["frobnicate $(id)", "frobnicate --fast > /dev/null"])
def test_verify_command_the_grant_derivation_cannot_read_still_needs_first_thinker_pass(eng, venue, command):
    approved_order(eng, cmd_plan(venue))
    d = eng.open("t1", eng.write(cmd_plan(venue, command)))
    assert action(d) == "await_user_approval"
    assert [(c["source"], c["origin"]) for c in changed(d)] == [(command, "verify_command")]
    assert autonomy(d)["thinker_pass"] is False
    assert "first passing thinker review" in autonomy(d)["reason"]


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


def test_first_thinker_verdict_not_carried_across_reapproval_with_new_stage(eng, venue):
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
    assert (after["data"].get("agent_review_override") or {}).get("reviewer") == "agent"


# --- effort custody across sessions of one order --------------------------------------


def window(eng: Eng, plan: str) -> dict:
    return eng.ledger(plan)["effort_since_user_approval"]


def agent_session(eng: Eng, sid: str, plan: str, *, execute: bool = True):
    d = eng.open(sid, plan)
    assert action(d) == "self_approve", d
    assert eng.run("approve", session=sid, by="agent")["ok"] is True
    if execute:
        eng.execute(sid)


def book_spend(eng: Eng, sid: str, plan: str, usd: float):
    return eng.run("record_result", session=sid, status="passed", actual="ok",
                   control="reviewed: ok", observation=OBSERVATIONS[0],
                   cost_log=eng.cost_log(plan, usd))


def test_self_approval_does_not_restamp_user_ledger(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan, execute=False)
    records = eng.ledger(plan)["records"]
    assert [r["by"] for r in records] == ["user"]


def test_effort_accumulates_across_agent_approved_sessions(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    book_spend(eng, "a1", plan, 2.0)
    second = eng.write(plan_text(), "second.toml")
    agent_session(eng, "a2", second)
    book_spend(eng, "a2", second, 3.0)
    assert window(eng, plan)["spend"] == pytest.approx(5.0)


def test_effort_flushed_at_approve_replan_submit(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    assert getattr(eng.state("a1"), "order_effort_flushed", None) is not None
    eng.run("record_result", session="a1", status="failed", actual="boom",
            cost_log=eng.cost_log(plan, 2.0))
    eng.diagnose("a1")
    replanned = eng.write(plan_text(stage3=True))
    eng.run("replan", session="a1", plan=replanned, cost_log=eng.cost_log(plan, 4.0))
    assert window(eng, plan)["spend"] == pytest.approx(4.0)
    resubmitted = eng.write(plan_text(stage3=True), "resubmitted.toml")
    eng.run("submit_plan", session="a1", plan=resubmitted, cost_log=eng.cost_log(replanned, 6.0))
    assert window(eng, plan)["spend"] == pytest.approx(4.0 + 6.0)


def test_agent_approval_keeps_user_approved_effort_estimate(eng):
    plan = approved_order(eng)
    approved = eng.ledger(plan)["records"][-1].get("effort_estimate")
    assert approved
    agent_session(eng, "a1", eng.write(plan_text(stage3=True), "bigger.toml"), execute=False)
    estimate = eng.state("a1").effort_estimate
    assert {scale: estimate[scale] for scale in approved} == approved


def test_task_reset_by_agent_refused(eng):
    from agentctl import task_accumulator

    task_accumulator.add("t-reset", "replan_count", 5, session_id="x", now=None)
    refused = eng.run("task_reset", task="t-reset", reason="loop broke", by="agent")
    assert refused["ok"] is False
    assert refused["marker"] == "ESCALATE_TO_USER"
    assert task_accumulator.get("t-reset")["per_axis_totals"]["replan_count"] == 5
    assert eng.run("task_reset", task="t-reset", reason="the user renegotiated", by="user")["ok"] is True
    assert task_accumulator.get("t-reset")["per_axis_totals"].get("replan_count", 0) == 0


# --- spend / wall-clock fires belong to the user ---------------------------------------

SELF_RULE = PUSH_MAIN.replace("main", "ticket/x")


def agent_grant(eng: Eng, sid: str):
    eng.park_permission(sid)
    return eng.run("resolve_permission", session=sid, decision="granted", by="agent",
                   scope="stage", rule=[SELF_RULE])


def test_open_spend_fire_blocks_self_approval_in_new_session(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    eng.spend_fire("a1", plan)
    assert [f["scale"] for f in eng.ledger(plan)["open_effort_fires"]] == ["spend"]
    d = eng.open("b1", eng.write(plan_text(), "b.toml"))
    assert action(d) == "await_user_approval"
    assert autonomy(d)["open_effort_fire"] is True
    assert eng.run("approve", session="b1", by="agent")["ok"] is False


def test_open_spend_fire_blocks_agent_self_grant(eng):
    plan = approved_order(eng, plan_text(stage2_extra=grant_block(SELF_RULE)))
    agent_session(eng, "a1", plan)
    assert agent_grant(eng, "a1")["ok"] is True
    eng.spend_fire("a1", plan)
    refused = agent_grant(eng, "a1")
    assert refused["ok"] is False
    assert refused["data"]["reason_class"] == "open-effort-fire"


def test_spend_and_wall_clock_fires_go_to_user(eng):
    plan = approved_order(eng)
    agent_session(eng, "a1", plan)
    eng.spend_fire("a1", plan)
    spend = eng.run("fire_acknowledge", session="a1", decision="revise", by="agent")
    assert spend["ok"] is False
    assert "owed to the user" in spend["detail"]
    state = eng.state("a1")
    state.effort_fires[-1]["scale"] = effort.SCALE_WALL_CLOCK
    eng.store.save(state)
    wall = eng.run("fire_acknowledge", session="a1", decision="revise", by="agent")
    assert wall["ok"] is False
    assert "owed to the user" in wall["detail"]


# --- the replans-scale fire: the agent's own planning-difficulty cycle -----------------


def replans_fire(eng: Eng, sid: str = "a1", *, usd: float | None = None):
    plan = approved_order(eng)
    agent_session(eng, sid, plan)
    eng.seed_replans(sid, Thresholds().effort_replan_absolute())
    flags = {"cost_log": eng.cost_log(plan, usd)} if usd is not None else {}
    d = eng.run("record_result", session=sid, status="passed", actual="ok",
                control="reviewed: ok", observation=OBSERVATIONS[0], **flags)
    assert eng.state(sid).effort_fires[-1]["scale"] == effort.SCALE_REPLANS, d
    return d


def ack(eng: Eng, sid: str = "a1", decision: str = "revise"):
    return eng.run("fire_acknowledge", session=sid, decision=decision, by="agent")


def test_replans_fire_without_difficulty_record_cannot_be_agent_acknowledged(eng):
    fired = replans_fire(eng)
    assert fired["data"]["agent_route"]["route"] == "agent_planning_difficulty_cycle"
    refused = ack(eng)
    assert refused["ok"] is False
    assert refused["marker"] == "ESCALATE_TO_USER"
    assert "no complete planning-difficulty record" in refused["detail"]


def test_agent_acknowledge_requires_difference_to_remove(eng):
    replans_fire(eng)
    eng.diagnose("a1", difference=False)
    refused = ack(eng)
    assert refused["ok"] is False
    assert "differences_to_remove" in refused["detail"]


def test_agent_acknowledge_requires_normalize_record(eng):
    replans_fire(eng)
    eng.diagnose("a1", normalize=False)
    refused = ack(eng)
    assert refused["ok"] is False
    assert "normalization" in refused["detail"]


def test_agent_acknowledge_refuses_record_declared_before_fire(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    state = eng.state("a1")
    state.effort_fires[-1]["difficulty_id_at_fire"] = cli._difficulty_id(state.difficulty)
    eng.store.save(state)
    refused = ack(eng)
    assert refused["ok"] is False
    assert "predates the fire" in refused["detail"]


def test_agent_replan_after_agent_ack_refuses_coverage_waiver(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    assert ack(eng)["ok"] is True
    untouched = eng.write(plan_text().replace('title = "', 'title = "Edited ', 1))
    refused = eng.run("replan", session="a1", plan=untouched, coverage_waiver="one-off")
    assert refused["ok"] is False
    assert "coverage-waiver is not accepted" in refused["detail"]


def test_agent_cannot_acknowledge_with_continue_or_abandon(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    for decision in ("continue", "abandon"):
        refused = ack(eng, decision=decision)
        assert refused["ok"] is False
        assert "the agent may only revise" in refused["detail"]
    assert eng.state("a1").effort_fires[-1].get("ack") is None


def test_replans_fire_with_complete_planning_record_proceeds_without_user(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    assert ack(eng)["ok"] is True
    assert eng.state("a1").effort_fires[-1]["ack"]["by"] == "agent"
    d = eng.run("replan", session="a1", plan=eng.write(plan_text(stage3=True)))
    assert action(d) == "self_approve", d
    assert eng.run("approve", session="a1", by="agent")["ok"] is True
    approvers = [e.get("by") for e in eng.state("a1").history if e.get("event") == "approve"]
    assert approvers == ["agent", "agent"]


def test_agent_replan_after_agent_ack_refuses_normalization_waiver(eng):
    replans_fire(eng)
    eng.diagnose("a1")
    assert ack(eng)["ok"] is True
    refused = eng.run("replan", session="a1", plan=eng.write(plan_text(stage3=True)),
                      normalization_waiver="one-off")
    assert refused["ok"] is False
    assert "normalization-waiver is not accepted" in refused["detail"]


def test_agent_acknowledge_keeps_spend_baseline(eng):
    replans_fire(eng, usd=2.0)
    assert eng.state("a1").effort_baseline["spend"] == pytest.approx(2.0)
    eng.diagnose("a1")
    assert ack(eng)["ok"] is True
    assert eng.state("a1").effort_baseline["spend"] == pytest.approx(0.0)


# --- renegotiation at the diagnosing-replan ceiling --------------------------------------


def at_ceiling(eng: Eng, sid: str = "a1", **first_record) -> str:
    """An agent session failed into DIAGNOSING with the task at the replan ceiling and a
    complete record `first_tag` that predates the (recorded) bare-replan refusal."""
    from agentctl import task_accumulator

    plan = approved_order(eng)
    agent_session(eng, sid, plan, execute=False)
    eng.fail_stage(sid)
    task_accumulator.add(f"task-{sid}", "replan_count", Thresholds().effort_replan_absolute(),
                         session_id="seed", now=None)
    eng.diagnose(sid, "a", **first_record)
    return plan


def renegotiate(eng: Eng, sid: str, replanned: str, decision: str = "continue", **flags):
    return eng.run("replan", session=sid, plan=replanned, renegotiation_decision=decision,
                   renegotiated_by="agent", renegotiation_note="within the boundary", **flags)


def ceiling_refusal(eng: Eng, sid: str, replanned: str):
    d = eng.run("replan", session=sid, plan=replanned)
    assert d["marker"] == "ESCALATE_TO_USER" and d["action"] == "renegotiate", d
    return d


def test_agent_renegotiation_continue_after_planning_cycle_within_boundary_accepted(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    d = renegotiate(eng, "a1", replanned)
    assert d["ok"] is True, d
    assert action(d) == "self_approve"
    assert eng.state("a1").renegotiations[-1]["by"] == "agent"


@pytest.mark.parametrize("gap, needle", [
    (dict(difference=False), "differences_to_remove"),
    (dict(normalize=False), "normalization"),
])
def test_agent_renegotiation_continue_requires_planning_difficulty_record(eng, gap, needle):
    at_ceiling(eng, **gap)
    replanned = eng.write(plan_text(stage3=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b", **gap)
    refused = renegotiate(eng, "a1", replanned)
    assert refused["ok"] is False
    assert needle in refused["detail"]
    assert not eng.state("a1").renegotiations


def test_agent_renegotiation_continue_refuses_record_declared_before_ceiling(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True))
    ceiling_refusal(eng, "a1", replanned)
    refused = renegotiate(eng, "a1", replanned)
    assert refused["ok"] is False
    assert "predates the ceiling event" in refused["detail"]


def test_agent_renegotiation_refused_when_resources_widen(eng):
    at_ceiling(eng)
    widened = eng.write(plan_text(stage3=True, extra_outputs=("brand_new_module.py",)))
    ceiling_refusal(eng, "a1", widened)
    eng.diagnose("a1", "b")
    refused = renegotiate(eng, "a1", widened)
    assert refused["ok"] is False
    assert "outside the approved boundary" in refused["detail"]


def test_agent_renegotiation_refused_when_order_changes(eng):
    at_ceiling(eng)
    moved = eng.write(plan_text(goal="Demonstrate a different goal entirely"))
    ceiling_refusal(eng, "a1", moved)
    eng.diagnose("a1", "b")
    refused = renegotiate(eng, "a1", moved)
    assert refused["ok"] is False
    assert "outside the approved boundary" in refused["detail"]


def test_agent_cannot_rescope_or_abandon(eng):
    at_ceiling(eng)
    replanned = eng.write(plan_text(stage3=True))
    ceiling_refusal(eng, "a1", replanned)
    eng.diagnose("a1", "b")
    for decision in ("rescope", "abandon"):
        refused = renegotiate(eng, "a1", replanned, decision)
        assert refused["ok"] is False
        assert "that is the user's decision" in refused["detail"]
    assert eng.state("a1").node == Node.DIAGNOSING.value


# --- pushing to trunk is never self-granted ---------------------------------------------


def test_trunk_push_self_grant_goes_to_user(eng):
    plan = approved_order(eng, plan_text(stage2_extra=LAND_MAIN))
    agent_session(eng, "a1", plan, execute=False)
    eng.fail_stage("a1")
    eng.diagnose("a1")
    pushing = eng.write(plan_text(stage2_extra=LAND_MAIN + grant_block(PUSH_MAIN), stage3=True))
    d = eng.run("replan", session="a1", plan=pushing)
    assert d["marker"] == "PLAN-READY", d
    assert action(d) == "await_user_approval"
    extra = autonomy(d)["extra_resources"]
    assert any("vcs_ref" in json.dumps(r) and "origin" in json.dumps(r) and "main" in json.dumps(r)
               and "push" in json.dumps(r) for r in extra), extra

    eng.park_permission("a1", PUSH_MAIN)
    granted = eng.run("resolve_permission", session="a1", decision="granted", by="agent",
                      scope="stage", rule=[PUSH_MAIN])
    assert granted["ok"] is False
    assert "not covered by any resource the customer approved" in granted["detail"]


# --- the whole cycle through the real entry point ---------------------------------------


def sub(eng: Eng, cmd: str, **flags) -> dict:
    argv = [sys.executable, str(SCRIPTS / "agentctl-cli.py"), "--state-root", str(eng.root),
            cmd.replace("_", "-")]
    for name, value in flags.items():
        flag = "--" + name.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([flag, str(item)])
        elif value is not None and value is not False:
            argv.extend([flag, str(value)])
    done = subprocess.run(argv, capture_output=True, text=True, env=os.environ.copy(), timeout=120)
    return json.loads(done.stdout)


def test_e2e_agent_replan_cycle_without_user(eng):
    plan = approved_order(eng)
    for step in ("start", "classify", "plan", "submit_plan"):
        flags = {
            "start": dict(task="task-e2e", goal="g", done_criterion="dc"),
            "classify": dict(architectural=True, files=5, changed_lines=200, wall_clock_min=60),
            "plan": {},
            "submit_plan": dict(plan=plan),
        }[step]
        d = sub(eng, step, session="e1", **flags)
    assert action(d) == "self_approve", d
    assert sub(eng, "approve", session="e1", by="agent")["ok"] is True
    sub(eng, "partition", session="e1")
    sub(eng, "next_stage", session="e1")
    eng.seed_replans("e1", Thresholds().effort_replan_absolute())
    fired = sub(eng, "record_result", session="e1", status="passed", actual="ok",
                control="reviewed: ok", observation=OBSERVATIONS[0])
    assert fired["data"]["agent_route"]["route"] == "agent_planning_difficulty_cycle", fired
    for step, flags in (
        ("declare", dict(expected="e", actual="a", mismatch="m")),
        ("investigate", dict(localized_expectation="le", localized_actual="la",
                             hypothesis=["h1", "h2"])),
        ("critique", dict(functional_ground="fg", replanning_task="rt",
                          failure_address="нормативное", difference_to_remove="d")),
        ("normalize", dict(factor="f", level="note")),
    ):
        sub(eng, step, session="e1", **flags)
    assert sub(eng, "fire_acknowledge", session="e1", decision="revise", by="agent")["ok"] is True
    replanned = eng.write(plan_text(stage3=True))
    d = sub(eng, "replan", session="e1", plan=replanned)
    assert action(d) == "self_approve", d
    assert sub(eng, "approve", session="e1", by="agent")["ok"] is True
    assert [e.get("by") for e in eng.state("e1").history if e.get("event") == "approve"] == ["agent", "agent"]
    assert [r["by"] for r in eng.ledger(plan)["records"]] == ["user"]


