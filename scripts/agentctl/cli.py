"""Subcommands: each maps one coordination step to a Directive.

This is the composition layer — it loads state (via the injected StateStore),
fires a machine transition, mutates state under the gate guardians, persists, and
returns a Directive. machine.py and classify.py stay pure; the side effects live
here behind two injectable seams (store, runner) so the whole CLASSIFIED..RESOLVED
cycle runs in tests with no filesystem-of-record and no `claude -p` spend.

Every command function has the signature cmd_x(args, *, store, runner) -> Directive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import gates
from .classify import Signals, classify
from .config import Thresholds
from .directive import Directive
from .dispatch import Runner, dispatch_stage
from .machine import transition
from .plan import load_plan
from .state import (
    CriterionType,
    GateRecord,
    Node,
    Route,
    SessionState,
    Stage,
    StageStatus,
    WeightClass,
)
from .store import FileStateStore, StateStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFY_PLAN_CLI = REPO_ROOT / "scripts" / "verify-plan-file.py"


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _require(store: StateStore, session_id: str) -> SessionState:
    state = store.load(session_id)
    if state is None:
        raise KeyError(f"no session {session_id!r}")
    return state


# --- commands -------------------------------------------------------------

def cmd_start(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = SessionState(
        session_id=args.session,
        task_id=args.task,
        goal=getattr(args, "goal", "") or "",
        overall_done_criterion=getattr(args, "done_criterion", "") or "",
        overall_criterion_type=getattr(args, "criterion_type", CriterionType.MEASURABLE.value),
        recursion_depth=int(getattr(args, "recursion_depth", 0) or 0),
    )
    state.log("start", task=state.task_id)
    store.save(state)
    return Directive(True, state.node, "classify", "session registered; run classify next")


def cmd_classify(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    thr = Thresholds()
    sig = Signals(
        is_chat=bool(getattr(args, "chat", False)),
        changed_lines=int(getattr(args, "changed_lines", 0) or 0),
        files=int(getattr(args, "files", 1) or 1),
        wall_clock_min=int(getattr(args, "wall_clock_min", 0) or 0),
        tracker_key=getattr(args, "tracker_key", None),
        architectural=bool(getattr(args, "architectural", False)),
        external_effect=bool(getattr(args, "external_effect", False)),
        new_dependency=bool(getattr(args, "new_dependency", False)),
        public_api_change=bool(getattr(args, "public_api_change", False)),
    )
    result = classify(sig, thr)
    state.weight_class = result.weight_class
    state.route = result.route
    state.node = transition(state.node, "classify")
    state.log("classify", weight_class=result.weight_class, route=result.route, reasons=result.reasons)

    if result.weight_class == WeightClass.SMALL_CHANGE.value:
        # carve-out: no plan-approval gate; auto-pass so ROUTED->EXECUTING is legal
        state.approval = GateRecord("plan_approval", armed=True, passed=True, by="small-change-carve-out")
        state.stages = [
            Stage(
                index=1,
                title=state.goal or "small change",
                executor="in_thread",
                expected_result_image=state.goal or "change applied",
                criterion_type=state.overall_criterion_type,
                done_criterion=state.overall_done_criterion or "change applied and self-checked",
            )
        ]
        action, detail = "execute_in_thread", "small change: execute in-thread, then record-result"
    elif result.weight_class == WeightClass.CHAT.value:
        action, detail = "answer_in_thread", "chat: answer directly; terminal at ROUTED"
    else:
        action, detail = "plan", "substantive: route to planner, then submit-plan"

    store.save(state)
    return Directive(True, state.node, action, detail, data={"reasons": result.reasons})


def cmd_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    state.node = transition(state.node, "plan")
    state.log("plan")
    store.save(state)
    return Directive(True, state.node, "await_plan", "planner working; submit-plan when ready")


def _verify_markdown_plan(path: str) -> list[str]:
    proc = subprocess.run(
        ["python3", str(VERIFY_PLAN_CLI), path], capture_output=True, text=True
    )
    if proc.returncode == 0:
        return []
    return [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()]


def cmd_submit_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    plan_path = args.plan
    if plan_path.endswith(".toml"):
        doc = load_plan(plan_path)
        state.stages = doc.stages
        if not state.goal:
            state.goal = doc.meta.goal
        if not state.overall_done_criterion:
            state.overall_done_criterion = doc.meta.done_criterion
        state.plan_verified = True
        problems: list[str] = []
    else:
        # markdown fallback: reuse verify-plan-file.py for structure-only check
        problems = _verify_markdown_plan(plan_path)
        state.plan_verified = not problems

    state.plan_path = plan_path
    state.node = transition(state.node, "submit_plan")
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.log("submit_plan", plan=plan_path, verified=state.plan_verified)
    store.save(state)

    if not state.plan_verified:
        return Directive(False, state.node, "fix_plan", "plan failed verification", data={"problems": problems})
    return Directive(
        True, state.node, "await_user_approval",
        "plan ready; HARD GATE — get explicit user approval before approve",
        marker="PLAN-READY",
    )


def cmd_approve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = gates.blockers(state, "plan_approval")
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty approver: --by must name who approved"]
    if blockers:
        return Directive(False, state.node, "fix_plan", "cannot approve", data={"blockers": blockers})
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "approve")
    state.log("approve", by=args.by)
    store.save(state)
    return Directive(True, state.node, "next_stage", "approved; advance to first ready stage")


def cmd_next_stage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    ready = state.ready_stages()
    if not ready:
        return Directive(False, state.node, "verify_final", "no ready stages; run verify-final if all passed")
    stage = ready[0]
    # pick the entry edge into EXECUTING from the current node
    if state.node == Node.APPROVED.value:
        event = "execute_approved"
    elif state.node == Node.ROUTED.value:
        event = "execute_small"
    elif state.node == Node.VERIFYING.value:
        event = "next_stage"
    else:
        return Directive(False, state.node, "blocked", f"cannot start a stage from node={state.node}")
    state.node = transition(state.node, event)
    stage.status = StageStatus.ACTIVE.value
    state.current_stage = stage.index
    state.log("next_stage", stage=stage.index, executor=stage.executor)
    store.save(state)
    action = "dispatch" if stage.is_spawn() else "execute_in_thread"
    return Directive(
        True, state.node, action,
        f"stage {stage.index} active: {stage.title}",
        data={"stage": stage.index, "executor": stage.executor,
              "expected_result_image": stage.expected_result_image},
    )


def cmd_dispatch(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to dispatch")
    if not stage.is_spawn():
        return Directive(True, state.node, "execute_in_thread", f"stage {stage.index} is in-thread; no spawn")
    result = dispatch_stage(
        stage, state.plan_path or "",
        runner=runner,
        budget=getattr(args, "budget", "medium"),
        complexity=getattr(args, "complexity", "medium"),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    state.log("dispatch", stage=stage.index, kind=stage.spawn_kind(), returncode=result.returncode)
    store.save(state)
    ok = result.returncode == 0
    return Directive(
        ok, state.node, "record_result" if ok else "handle_spawn_failure",
        f"dispatched stage {stage.index} -> {stage.spawn_kind()}",
        data={"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
    )


def cmd_record_result(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to record")
    actual = args.actual or ""
    stage.actual = actual
    passed = args.status == "passed"
    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING

    if passed:
        stage.status = StageStatus.PASSED.value
        state.current_stage = None
        state.log("record_result", stage=stage.index, status="passed")
        store.save(state)
        if state.all_stages_passed():
            return Directive(True, state.node, "verify_final", f"stage {stage.index} passed; all stages passed")
        return Directive(True, state.node, "next_stage", f"stage {stage.index} passed; more stages ready")

    # failed: loop guard — same stage failing twice on the same actual digest -> escalate
    dig = _digest(actual)
    repeat = dig in stage.fail_digests
    stage.fail_digests.append(dig)
    stage.status = StageStatus.FAILED.value
    state.log("record_result", stage=stage.index, status="failed", repeat=repeat)
    store.save(state)
    if repeat:
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} failed twice with same result digest; stop retrying",
            marker="ESCALATE",
        )
    return Directive(False, state.node, "replan", f"stage {stage.index} failed; run overcome-difficulty / replan")


def cmd_verify_final(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = gates.blockers(state, "resolution")
    if blockers:
        return Directive(False, state.node, "fix_stages", "not ready for resolution", data={"blockers": blockers})
    state.node = transition(state.node, "final")  # VERIFYING -> RESOLUTION
    state.resolution = GateRecord("resolution", armed=True, passed=False)
    state.log("verify_final")
    store.save(state)
    kind = "run the measurable check" if state.overall_criterion_type == CriterionType.MEASURABLE.value \
        else "ask the user to accept on review"
    return Directive(
        True, state.node, "await_user_confirmation",
        f"all stages passed; resolution gate armed — {kind}",
    )


def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = gates.blockers(state, "resolution")
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty confirmer: --by must name who confirmed resolution"]
    if blockers:
        return Directive(False, state.node, "fix_stages", "cannot resolve", data={"blockers": blockers})
    state.resolution = GateRecord("resolution", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "resolve")  # RESOLUTION -> RESOLVED
    state.log("resolve", by=args.by)
    store.save(state)
    return Directive(True, state.node, "done", "task resolved", marker="COMPLETED")


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    from .plan import diff_plans, load_plan as _load

    if not state.plan_path:
        return Directive(False, state.node, "submit_plan", "no current plan to replan against")
    old = _load(state.plan_path)
    new = _load(args.plan)
    kind = diff_plans(old, new)

    if kind == "no_change":
        return Directive(True, state.node, "continue", "replan is a no-op; plan unchanged")

    if kind == "refinement":
        # apply prose refinements and re-arm any FAILED stage for another attempt
        for ns in new.stages:
            try:
                cur = state.stage(ns.index)
            except KeyError:
                continue
            cur.title = ns.title
            cur.expected_result_image = ns.expected_result_image
            if cur.status == StageStatus.FAILED.value:
                cur.status = StageStatus.PENDING.value
        state.plan_path = args.plan
        state.log("replan", kind="refinement")
        store.save(state)
        if state.node == Node.VERIFYING.value and state.ready_stages():
            return Directive(True, state.node, "next_stage", "refinement applied; retry the ready stage")
        return Directive(True, state.node, "continue", "refinement applied; resume execution")

    # substantive: re-arm the plan-approval gate, reload stages, return to PLAN_READY
    state.stages = new.stages
    state.plan_path = args.plan
    state.plan_verified = True
    state.overall_done_criterion = new.meta.done_criterion or state.overall_done_criterion
    state.current_stage = None
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.node = Node.PLAN_READY.value
    state.log("replan", kind="substantive")
    store.save(state)
    return Directive(
        True, state.node, "await_user_approval",
        "substantive replan; HARD GATE — re-approval required",
        marker="PLAN-READY",
    )


def cmd_block(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node in (Node.RESOLVED.value, Node.BLOCKED.value):
        return Directive(False, state.node, "noop", f"cannot block from node={state.node}")
    state.blocked_from = state.node
    state.node = Node.BLOCKED.value
    state.log("block", reason=getattr(args, "reason", None))
    store.save(state)
    return Directive(True, state.node, "unblock", "blocked; resolve the blocker then unblock", marker="ESCALATE")


def cmd_unblock(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node != Node.BLOCKED.value or not state.blocked_from:
        return Directive(False, state.node, "noop", "not blocked")
    state.node = state.blocked_from
    state.blocked_from = None
    state.log("unblock")
    store.save(state)
    return Directive(True, state.node, "continue", "unblocked; resume from prior node")


def cmd_status(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = store.load(args.session) if getattr(args, "session", None) else None
    if state is None:
        return Directive(True, "(none)", "start", "no session state; run start")
    return Directive(
        True, state.node, "inspect",
        f"{state.task_id}: {state.weight_class}/{state.route}",
        data={
            "weight_class": state.weight_class,
            "route": state.route,
            "current_stage": state.current_stage,
            "stages": [{"index": s.index, "status": s.status, "title": s.title} for s in state.stages],
            "approval_passed": state.approval.passed,
            "resolution_passed": state.resolution.passed,
        },
    )


COMMANDS = {
    "start": cmd_start,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "submit-plan": cmd_submit_plan,
    "approve": cmd_approve,
    "next-stage": cmd_next_stage,
    "dispatch": cmd_dispatch,
    "record-result": cmd_record_result,
    "verify-final": cmd_verify_final,
    "resolve": cmd_resolve,
    "replan": cmd_replan,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "status": cmd_status,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentctl", description="deterministic coordination state machine")
    p.add_argument("--state-root", help="override state directory (tests/inspection)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, **kw):
        return sub.add_parser(name, **kw)

    sp = add("start"); sp.add_argument("--session", required=True); sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default=""); sp.add_argument("--done-criterion", dest="done_criterion", default="")
    sp.add_argument("--criterion-type", dest="criterion_type", default=CriterionType.MEASURABLE.value)
    sp.add_argument("--recursion-depth", dest="recursion_depth", type=int, default=0)

    sp = add("classify"); sp.add_argument("--session", required=True)
    sp.add_argument("--chat", action="store_true")
    sp.add_argument("--changed-lines", dest="changed_lines", type=int, default=0)
    sp.add_argument("--files", type=int, default=1)
    sp.add_argument("--wall-clock-min", dest="wall_clock_min", type=int, default=0)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp.add_argument("--architectural", action="store_true")
    sp.add_argument("--external-effect", dest="external_effect", action="store_true")
    sp.add_argument("--new-dependency", dest="new_dependency", action="store_true")
    sp.add_argument("--public-api-change", dest="public_api_change", action="store_true")

    sp = add("plan"); sp.add_argument("--session", required=True)
    sp = add("submit-plan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp = add("approve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp = add("next-stage"); sp.add_argument("--session", required=True)
    sp = add("dispatch"); sp.add_argument("--session", required=True)
    sp.add_argument("--budget", default="medium"); sp.add_argument("--complexity", default="medium")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("record-result"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], required=True)
    sp.add_argument("--actual", default="")
    sp = add("verify-final"); sp.add_argument("--session", required=True)
    sp = add("resolve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp = add("replan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp = add("block"); sp.add_argument("--session", required=True); sp.add_argument("--reason", default="")
    sp = add("unblock"); sp.add_argument("--session", required=True)
    sp = add("status"); sp.add_argument("--session", required=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = FileStateStore(Path(args.state_root) if args.state_root else None)
    fn = COMMANDS[args.command]
    try:
        directive = fn(args, store=store)
    except Exception as exc:  # surface as a failed directive, not a traceback
        directive = Directive(False, "(error)", "error", str(exc))
    print(json.dumps(directive.to_dict(), ensure_ascii=False, indent=2))
    return 0 if directive.ok else 1


if __name__ == "__main__":
    sys.exit(main())
