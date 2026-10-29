"""A standing mandate approves the norm of the item its running cycle took, and nothing else.

`approve --by mandate:<id>` is the engine side of ADR-0008's debt cycle run as an ordinary
linked task: the cycle opens a parent session whose plan is the deterministic item norm
(`mandate.item_norm_toml`), approves it on the mandate's authority, and spawns a linked
developer for stage 1. The basis holds exactly when the plan's bytes are the norm re-rendered
from the inputs recorded beside it, the cycle's lock is held, the cycle's start and the item's
taken events name this plan's issue and directory, the item's worktree lies under the cycle's
worktree root in the cycle's repo, and the mandate's gate admits a start. It skips the
presentation, grants-approval and plan-review blockers and nothing else, and it neither clears
the since-user counters nor stamps the order ledger.

Tests drive the engine through `cli.main` argv against a real `FileStateStore`, the mandate
through the real `mandate_store` under `AGENTCTL_MANDATE_DIR`, and a linked child through the
`agentctl-own.py` channel.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from argparse import Namespace
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from agentctl import cli, cli_common, mandate as rules, mandate_store as store, order_approvals, protected_actions, task_accumulator
from agentctl.plan import load_plan, order_digest
from agentctl.stage_norm import StageNorm
from agentctl.state import Node, PlanPresentation
from agentctl.store import FileStateStore
from conftest import declare_venue
from mandate_cycle import driver
from test_child_approval import _spliced, _with_stage_verify
from test_child_contour_e2e import CHILD_CHECK, Scene
from test_reject_and_resubmit import _to_resolution
from test_child_contour_gate import (  # noqa: F401 -- _isolated_env is the shared autouse fixture
    PARENT, _cli, _isolated_env, _state_dir,
)

MANDATE_ID = "core-debt"
APPROVER = rules.mandate_approver(MANDATE_ID)
CYCLE_ID = "20260101T000000Z"
ISSUE = 42
BASE_SHA = "a" * 40


class World:
    """A mandate, a running cycle (its lock held) that took one item, and the item directory
    holding the norm and its inputs, exactly as the driver writes them."""

    def __init__(self, tmp_path: Path, monkeypatch, *, take_item: bool = True, record_cycle: bool = True,
                 repo_is_engine: bool = False):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.agent_home = self.home / ".claude"
        self.agent_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.agent_home))
        monkeypatch.setenv("CLAUDE_AGENT_HOME", str(self.agent_home))
        monkeypatch.setenv("AGENTCTL_MANDATE_DIR", str(tmp_path / "mandates"))
        monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
        monkeypatch.setenv("AGENTCTL_PLAN_PRESENTATION", "1")
        self.monkeypatch = monkeypatch
        self.root = _state_dir(self.home)
        if repo_is_engine:
            self.repo = str(cli.REPO_ROOT)
        else:
            self.repo = str(tmp_path / "repo")
            (tmp_path / "repo").mkdir()
            monkeypatch.setattr(cli, "REPO_ROOT", Path(self.repo))
            monkeypatch.setattr(cli_common, "REPO_ROOT", Path(self.repo))
        self.worktree_root = tmp_path / "worktrees"
        self.worktree = self.worktree_root / f"{CYCLE_ID}-{ISSUE}"
        self.worktree.mkdir(parents=True)
        self.item_dir = tmp_path / "cycles" / CYCLE_ID / "items" / str(ISSUE)
        self.item_dir.mkdir(parents=True)
        self.now = store.utcnow()
        self.mandate = rules.new_mandate(MANDATE_ID, "user", self.now, rules.MandateLimits())
        store.save_mandate(self.mandate)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(store.cycle_lock(MANDATE_ID))
        if record_cycle:
            store.append_event(MANDATE_ID, "cycle-start", {
                "cycle_id": CYCLE_ID, "repo": self.repo, "worktree_root": str(self.worktree_root)})
        if take_item:
            self.take()
        self.inputs = self.default_inputs()
        self.write_norm()

    def close(self) -> None:
        self.stack.close()

    def take(self, *, issue: int = ISSUE, item_dir: Path | None = None, worktree: Path | None = None) -> None:
        store.append_event(MANDATE_ID, "item-taken", {
            "cycle_id": CYCLE_ID, "issue": issue, "item_dir": str(item_dir or self.item_dir),
            "worktree": str(worktree or self.worktree)})

    def default_inputs(self) -> dict:
        return {
            "mandate_id": MANDATE_ID, "issue": ISSUE, "title": "a debt issue", "body": "what is wrong",
            "cycle_id": CYCLE_ID, "base_sha": BASE_SHA, "bound": 200, "worktree": str(self.worktree),
            "repo": self.repo, "item_dir": str(self.item_dir),
        }

    def norm_text(self, inputs: dict | None = None) -> str:
        rest = dict(inputs or self.inputs)
        rest.pop("mandate_id")
        return rules.item_norm_toml(**rest, mandate=self.mandate)

    def write_norm(self, text: str | None = None, inputs: dict | None = None) -> Path:
        plan = self.item_dir / "norm.toml"
        plan.write_text(text if text is not None else self.norm_text(inputs), encoding="utf-8")
        (self.item_dir / "norm.inputs.json").write_text(json.dumps(inputs or self.inputs), encoding="utf-8")
        return plan

    def parent_to_plan_ready(self, plan: Path | None = None, session: str = PARENT) -> dict:
        plan = plan or self.item_dir / "norm.toml"
        _cli(self.root, "start", session=session, task="item", goal="g", done_criterion="dc")
        _cli(self.root, "classify", session=session, architectural=True, changed_lines=200, wall_clock_min=60)
        _cli(self.root, "plan", session=session)
        return _cli(self.root, "submit-plan", session=session, plan=str(plan))

    def approve(self, by: str = APPROVER, session: str = PARENT) -> dict:
        return _cli(self.root, "approve", session=session, by=by)

    def parent(self, session: str = PARENT):
        return FileStateStore(self.root).load(session)


@pytest.fixture(autouse=True)
def _claude_runtime_host(_isolated_env, monkeypatch):
    """Restore the in-process host signal _isolated_env drops (conftest's default), so classify
    does not fall back to detecting a Cursor CLI that only some machines have; the channel's
    subprocess env still strips it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "agentctl-test-session")


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = World(tmp_path, monkeypatch)
    yield built
    built.close()


LIVE_ARMED_KNOBS = (
    "AGENTCTL_PREMISE", "AGENTCTL_CODE_REVIEW",
    "AGENTCTL_STAGE_REVIEW", "AGENTCTL_ACCEPTANCE", "AGENTCTL_LANDING_GATE", "AGENTCTL_RESOLUTION_ASK_GATE",
    "AGENTCTL_WORKSPACE_CLEANUP",
)


@pytest.fixture
def engine_world(tmp_path, monkeypatch):
    """A world whose cycle repo is the repo the real engine subprocess runs from, with the gates a
    live session meets armed: conftest's force-off knobs are dropped (World itself forces plan review
    and presentation on). AGENTCTL_ADVISOR stays '0':
    unset, submit-plan and approve would run a real model call, and the advisor never blocks."""
    for knob in LIVE_ARMED_KNOBS:
        monkeypatch.delenv(knob, raising=False)
    built = World(tmp_path, monkeypatch, repo_is_engine=True)
    yield built
    built.close()


def _blockers(result: dict) -> list[str]:
    return (result.get("data") or {}).get("blockers") or []


def _refused(result: dict, world: World, *needles: str) -> None:
    text = " | ".join(_blockers(result))
    assert result["ok"] is False and world.parent().node == "PLAN_READY", f"the approval was not refused: {result}"
    for needle in needles:
        assert needle in text, f"the refusal does not name {needle!r}: {text}"


def _approved_by_mandate(world: World) -> None:
    approved = world.approve()
    assert approved["ok"] is True, f"the mandate's approval was refused: {_blockers(approved)}"
    assert world.parent().node == "APPROVED"


def test_a_mandate_approval_of_the_exact_item_norm_skips_presentation_grants_and_review(world):
    world.parent_to_plan_ready()
    state = world.parent()
    assert not state.plan_review_passes and state.plan_review is None, "setup: a review is already on record"
    _approved_by_mandate(world)
    rows = [h for h in world.parent().history if h.get("event") == "approve"]
    assert rows and rows[-1].get("by") == APPROVER and rows[-1].get("basis") == "mandate", rows


def test_a_mandate_approval_of_an_edited_norm_is_refused(world):
    edited = world.write_norm(text=world.norm_text() + "\n# one more line\n")
    world.parent_to_plan_ready(edited)
    _refused(world.approve(), world, "not the item norm")


def test_a_mandate_approval_with_a_paused_expired_or_breaker_open_mandate_is_refused(world):
    world.parent_to_plan_ready()
    variants = {
        "paused": rules.stopped(world.mandate),
        "breaker-open": rules.breaker_opened(world.mandate, "tests"),
        "expired": replace(world.mandate, expires_at=rules.format_ts(world.now - timedelta(days=1))),
    }
    for reason, mandate in variants.items():
        store.save_mandate(mandate)
        _refused(world.approve(), world, f"gate: {reason}")
    store.save_mandate(world.mandate)
    _approved_by_mandate(world)


def test_a_mandate_approval_does_not_clear_the_since_user_counters_or_stamp_the_order_ledger(world, monkeypatch):
    monkeypatch.setenv("AGENTCTL_TASK_ACCUMULATOR_DIR", str(world.tmp / "accumulator"))
    monkeypatch.setenv("AGENTCTL_ORDER_APPROVALS_DIR", str(world.tmp / "ledger"))
    world.parent_to_plan_ready()
    task_id = world.parent().task_id
    axis = task_accumulator.SINCE_USER_AXES[0]
    task_accumulator.add(task_id, axis, 3)
    _approved_by_mandate(world)
    assert task_accumulator.get(task_id)["per_axis_totals"][axis] == 3, "the approval cleared a since-user counter"
    doc = load_plan(str(world.item_dir / "norm.toml"))
    assert order_approvals.latest_user_approved_record(order_digest(doc)) is None, (
        "the approval stamped the order ledger with the mandate as the customer"
    )


def test_a_mandate_approval_still_applies_the_protected_grant_blocker(world, monkeypatch):
    monkeypatch.setattr(protected_actions, "protected_grant_hits",
                        lambda doc: [(1, "Bash(python3 -m pytest:*)", "some-protected-action")])
    world.parent_to_plan_ready()
    _refused(world.approve(), world, "some-protected-action")


def test_the_item_norm_passes_submit_plan_and_its_grants_are_the_fixed_template(world):
    submitted = world.parent_to_plan_ready()
    assert submitted["ok"] is True, submitted
    doc = load_plan(str(world.item_dir / "norm.toml"))
    stage = doc.stages[0]
    thinker = f"Bash(python3 {world.repo}/scripts/spawn-specialist.py --kind thinker:*)"
    assert [g.rule for g in stage.grants.allow] == ["Bash(python3 -m pytest:*)", thinker], stage.grants
    assert [(d.path, d.mode) for d in stage.grants.add_dirs] == [(str(world.worktree), "write")]


def test_a_non_mandate_approval_without_a_review_is_still_blocked(world):
    world.parent_to_plan_ready()
    _refused(world.approve(by="reviewer"), world, "review")


def test_a_mandate_approval_of_a_norm_for_no_taken_item_is_refused(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, take_item=False)
    try:
        world.parent_to_plan_ready()
        _refused(world.approve(), world, "no item taken")
        world.take(issue=ISSUE + 1)
        _refused(world.approve(), world, "no item taken")
        world.take(item_dir=world.tmp / "elsewhere")
        _refused(world.approve(), world, "no item taken")
        world.take(worktree=world.worktree_root / "a-sibling-item")
        _refused(world.approve(), world, "no item taken")
        world.take()
        _approved_by_mandate(world)
    finally:
        world.close()


def _unbound_world(tmp_path, monkeypatch, name, *, record_cycle=True, inputs=None, engine_repo=None, release_lock=False):
    world = World(tmp_path / name, monkeypatch, record_cycle=record_cycle)
    if inputs:
        world.inputs = {**world.inputs, **inputs}
        world.write_norm()
    if engine_repo:
        monkeypatch.setattr(cli, "REPO_ROOT", Path(engine_repo))
        monkeypatch.setattr(cli_common, "REPO_ROOT", Path(engine_repo))
    world.parent_to_plan_ready()
    if release_lock:
        world.close()
    return world


@pytest.mark.parametrize("name, needle, setup", [
    ("no-start", "recorded no start", {"record_cycle": False}),
    ("no-lock", "no cycle holds the mandate's lock", {"release_lock": True}),
    ("stale-cycle", "not the running one", {"inputs": {"cycle_id": "20200101T000000Z"}}),
    ("outside-root", "not under the cycle's worktree root", {"inputs": {"worktree": "/outside/root/item"}}),
    ("other-repo", "not the cycle's repo", {"inputs": {"repo": "/another/repo"}}),
    ("not-engine-repo", "not the repo this engine runs from", {"engine_repo": "/another/repo"}),
], ids=["no-start", "no-lock", "stale-cycle", "outside-root", "other-repo", "not-engine-repo"])
def test_a_mandate_approval_outside_the_running_cycles_binding_is_refused(tmp_path, monkeypatch, name, needle, setup):
    world = _unbound_world(tmp_path, monkeypatch, name, **setup)
    try:
        _refused(world.approve(), world, needle)
    finally:
        world.close()


def _review_passed_and_presented_with_stale_grants(world: World) -> None:
    """The plan review passed, and a presentation receipt bound to the plan's current bytes
    carries a grants digest that is not the plan's own -- so the only blockers left are the
    presentation family's two, each with its own message."""
    plan = world.item_dir / "norm.toml"
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    _cli(world.root, "plan-review", session=PARENT, verdict="pass", reviewer="thinker",
         target=str(plan), plan_digest=digest)
    state = world.parent()
    state.plan_presentations.append(PlanPresentation(
        plan_path=state.plan_path, kind="essence", plan_sha256=digest, rendering_sha256="r" * 64,
        rendering_text="essence", presented_ts=store.utcnow().timestamp(), grants_sha256="0" * 64))
    FileStateStore(world.root).save(state)


@pytest.mark.parametrize("by", ["user", "agent"])
def test_a_non_mandate_approval_still_requires_presentation_and_grants_approval(world, by):
    world.parent_to_plan_ready()
    _review_passed_and_presented_with_stale_grants(world)
    result = world.approve(by=by)
    text = " | ".join((result.get("data") or {}).get("blockers") or [])
    assert "plan review" not in text, text
    if by == "agent":
        assert "delivery proof" not in text and "grant set changed" not in text, text
        return
    _refused(result, world, "delivery", "the plan's grant set changed since it was presented")


class MandateScene(Scene):
    """A linked spawn for stage 1 of a parent whose plan is the item norm, approved by the
    mandate: the parent at EXECUTING, the child's engine session started under the handoff."""

    def _start_parent(self) -> str:
        self.world = World(self.tmp, self.monkeypatch)
        self.world.parent_to_plan_ready()
        _approved_by_mandate(self.world)
        _cli(self.root, "partition", session=PARENT)
        _cli(self.root, "next-stage", session=PARENT)
        self.norm = StageNorm.from_stage(self.world.parent().stage(1))
        (self.world.worktree / "tests").mkdir()
        (self.world.worktree / "tests" / "test_mod.py").write_text("", encoding="utf-8")
        return str(self.world.item_dir / "norm.toml")

    def plan_text(self, stage_check: str, *, final_check: str) -> str:
        text = _spliced(parent_norm=self.norm.norm_digest(), final_checks=((final_check, 0),))
        head, _, rest = text.partition("[[stage]]\nindex = 2")
        tail = rest[rest.index("\n[[final_check]]"):]
        text = _with_stage_verify(head + tail, stage_check).replace('["mod.py"]', '["notes.md"]')
        return declare_venue(text, self.world.worktree)


@pytest.fixture
def mandate_scene(tmp_path, monkeypatch):
    built = MandateScene(tmp_path, monkeypatch)
    yield built
    built.world.close()


def _child_submits(scene: MandateScene, final_check: str) -> dict:
    plan = scene.tmp / "child.toml"
    plan.write_text(scene.plan_text(CHILD_CHECK, final_check=final_check), encoding="utf-8")
    scene.to_planning()
    submitted = scene.submit_and_review(str(plan))
    return (submitted.get("data") or {}).get("autonomy") or {}


def test_a_linked_child_plan_carrying_the_item_check_self_approves_on_parent_norm(mandate_scene):
    item_check = mandate_scene.norm.verify_command
    autonomy = _child_submits(mandate_scene, item_check)
    assert autonomy.get("action") == "self_approve" and autonomy.get("basis") == "parent_norm", (
        autonomy.get("reasons"), [e["name"] for e in autonomy.get("extra_resources") or []],
        [c["source"] for c in autonomy.get("unresolved_changed_commands") or []])
    approved = mandate_scene.channel("approve", "--by", "agent")
    assert approved.returncode == 0 and json.loads(approved.stdout).get("ok") is True, approved.stdout[:400]
    child = FileStateStore(mandate_scene.root).load(mandate_scene.own_session)
    rows = [h for h in child.history if h.get("event") == "approve"]
    assert rows and rows[-1].get("basis") == "parent_norm" and rows[-1].get("by") == "agent", rows
    assert mandate_scene.parent_bytes() == mandate_scene.parent_before, "the parent's state changed"


def test_a_linked_child_plan_without_the_item_check_is_not_self_approved(mandate_scene):
    autonomy = _child_submits(mandate_scene, CHILD_CHECK)
    assert autonomy.get("eligible") is False and "parent-check-dropped" in (autonomy.get("reasons") or []), autonomy
    refused = json.loads(mandate_scene.channel("approve", "--by", "agent").stdout)
    blockers = (refused.get("data") or {}).get("blockers") or []
    assert refused.get("ok") is False and any("parent-check-dropped" in b for b in blockers), refused
    assert FileStateStore(mandate_scene.root).load(mandate_scene.own_session).node != "EXECUTING"


def test_reject_by_mandate_moves_the_child_to_diagnosing_and_records_the_actor(store, venued_fixtures_dir):
    reject = ["reject", "--session", "s", "--reason", "check-item red"]
    assert cli.build_parser().parse_args(reject + ["--by", APPROVER]).by == APPROVER
    assert cli.build_parser().parse_args(reject).by is None
    sid = "rejected-by-mandate"
    _to_resolution(store, sid, str(venued_fixtures_dir / "plan_two_stage.toml"))
    decision = cli.cmd_reject(
        Namespace(session=sid, reason="check-item red: 2 failures", stage=None, by=APPROVER), store=store)
    assert decision.marker == "OVERCOME-DIFFICULTY"
    state = store.load(sid)
    assert state.node == Node.DIAGNOSING.value and state.difficulty is not None
    rows = [h for h in state.history if h.get("event") == "reject"]
    assert rows and rows[-1].get("by") == APPROVER and rows[-1].get("reason") == "check-item red: 2 failures", rows


def test_the_driver_records_the_issue_as_the_order_covered_by_the_item_stage(engine_world):
    world = engine_world
    cycle = driver.Cycle(driver.Config(mandate_id=MANDATE_ID, repo=Path(world.repo)), driver.Seams(), CYCLE_ID, write=False)
    session = cycle.open_parent_session(ISSUE, world.item_dir / "norm.toml", world.worktree)
    listed = _cli(world.root, "order-list", session=session)
    elements = listed["data"]["order_elements"]
    assert [(e["id"], e["disposition"]) for e in elements] == [("issue", "covered")], elements
    assert elements[0]["element"] == f"Core issue #{ISSUE} is resolved" and elements[0]["stage"] == driver.STAGE_INDEX, elements


def test_the_driver_parent_session_reaches_stage_one_executing_with_live_gate_arming(engine_world):
    world = engine_world
    cycle = driver.Cycle(driver.Config(mandate_id=MANDATE_ID, repo=Path(world.repo)), driver.Seams(), CYCLE_ID, write=False)
    session = cycle.open_parent_session(ISSUE, world.item_dir / "norm.toml", world.worktree)
    state = world.parent(session)
    assert state.node == "EXECUTING" and state.current_stage == driver.STAGE_INDEX, state.node
    assert "premise" in state.plugins, state.plugins
    approvals = [h for h in state.history if h.get("event") == "approve"]
    assert approvals and approvals[-1].get("by") == APPROVER and approvals[-1].get("basis") == "mandate", approvals

    listed = _cli(world.root, "stage-grants", session=session, stage=driver.STAGE_INDEX, json=True)
    entries = listed["data"]["grants"]
    declared = [e["rule"] for e in entries if e["provenance"] == "declared" and "rule" in e]
    thinker = f"Bash(python3 {world.repo}/scripts/spawn-specialist.py --kind thinker:*)"
    assert declared == ["Bash(python3 -m pytest:*)", thinker], entries
    check = f"python3 {world.repo}/scripts/mandate-cycle.py check-item --item-dir {world.item_dir}"
    derived = [e["rule"] for e in entries if e["provenance"] != "declared"]
    edits = [rule for rule in derived if rule.startswith("Edit(")]
    assert derived == [f"Bash({check}:*)", *edits] and len(edits) <= 1, derived
