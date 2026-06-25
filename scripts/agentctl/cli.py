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

from . import continuations, gates, permissions, plugins
from .classify import Signals, classify
from .config import Thresholds
from .partition import render_section, verdict
from .directive import Directive
from .dispatch import Runner, dispatch_stage, parse_marker, subprocess_runner
from .machine import transition
from .plan import load_plan
from .state import (
    Actor,
    Critique,
    Criterion,
    CriterionType,
    Declaration,
    Difficulty,
    Investigation,
    Partition,
    GateRecord,
    Means,
    Node,
    PermissionRequest,
    Route,
    SessionState,
    Stage,
    StageStatus,
    Subject,
    WeightClass,
)
from .store import FileStateStore, StateStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFY_PLAN_CLI = REPO_ROOT / "scripts" / "verify-plan-file.py"


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _verify_command_result(stage, runner: Runner | None):
    """Execute a measurable stage's `verify_command`, if it has one.

    Returns (ok, result). When the stage carries no command, or its criterion is
    not measurable, returns (True, None) — there is nothing executable to gate on,
    so the engine keeps its flag-only behaviour. Otherwise runs the command via the
    injected runner (tests pass a fake; the default shells out) and reports whether
    the exit code matched `expected_exit`. This is the seam that removes the model
    from the trust path for the measurable subset."""
    crit = stage.criterion
    if not crit.verify_command or crit.criterion_type != CriterionType.MEASURABLE.value:
        return True, None
    run = runner or subprocess_runner
    result = run(["bash", "-c", crit.verify_command])
    return result.returncode == crit.expected_exit, result


def _is_recursion_refusal(result) -> bool:
    """spawn-specialist refuses at the recursion cap with returncode 3 and a
    'max-recursion-depth=' stderr line (see spawn-specialist.py)."""
    return getattr(result, "returncode", None) == 3 or (
        "max-recursion-depth" in (getattr(result, "stderr", "") or "")
    )


def _require(store: StateStore, session_id: str) -> SessionState:
    state = store.load(session_id)
    if state is None:
        raise KeyError(f"no session {session_id!r}")
    return state


def _park_blocked(state: SessionState, store: StateStore, stage, marker, base: dict) -> Directive:
    """Park the session at BLOCKED and escalate — for a spawn whose output is
    malformed/unroutable (no marker, or a marker the engine cannot resolve)."""
    state.blocked_from = state.node
    state.node = Node.BLOCKED.value
    reason = "malformed spawn output" if marker in (None, "MALFORMED") else f"marker {marker}"
    state.log("dispatch_escalate", stage=stage.index, marker=marker)
    store.save(state)
    return Directive(
        False, state.node, "escalate",
        f"stage {stage.index} -> escalate ({reason})",
        marker="ESCALATE", data=base,
    )


# --- commands -------------------------------------------------------------

def cmd_start(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    if getattr(args, "if_absent", False):
        existing = store.load(args.session)
        if existing is not None and existing.node != Node.RESOLVED.value:
            return Directive(
                True, existing.node, "continue",
                f"session live (task={existing.task_id}, node={existing.node}); start is a no-op",
            )
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


def cmd_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Re-arm a session for a NEW task once its prior task is closed. Refuses to
    discard a live prior task (not RESOLVED/ROUTED/BLOCKED) unless --force, so a
    new prompt cannot silently wipe in-flight work. Otherwise builds a fresh
    CLASSIFIED SessionState from the same args cmd_start uses."""
    prior = store.load(args.session)
    if (
        prior is not None
        and prior.node not in (Node.RESOLVED.value, Node.ROUTED.value, Node.BLOCKED.value)
        and not getattr(args, "force", False)
    ):
        return Directive(
            False, prior.node, "noop",
            f"prior task '{prior.task_id}' is live at node={prior.node}; "
            "resolve/block it or pass --force to discard",
        )
    new = SessionState(
        session_id=args.session,
        task_id=args.task,
        goal=getattr(args, "goal", "") or "",
        overall_done_criterion=getattr(args, "done_criterion", "") or "",
        overall_criterion_type=getattr(args, "criterion_type", CriterionType.MEASURABLE.value),
        recursion_depth=int(getattr(args, "recursion_depth", 0) or 0),
    )
    new.log("reset", task=new.task_id, prior_task=(prior.task_id if prior else None))
    store.save(new)
    return Directive(
        True, new.node, "classify", "session re-armed for new task; run classify",
    )


def cmd_plugin_activate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Attach a registered plugin to THIS session (the per-session counterpart of
    import-time registration). The owning skill runs this on invocation. Plugin-
    specific kwargs (e.g. --tracker-key) are stashed in the seeded bag. Idempotent
    — safe to re-run on resume; merges new kwargs into the existing bag."""
    state = _require(store, args.session)
    name = args.plugin
    if name not in plugins.REGISTRY:
        return Directive(
            False, state.node, "noop",
            f"unknown plugin {name!r}; registered: {sorted(plugins.REGISTRY)}",
        )
    seed = dict(getattr(args, "seed", None) or {})
    if getattr(args, "tracker_key", None):
        seed["tracker_key"] = args.tracker_key
    plugins.activate(state, name, seed)
    state.log("plugin_activate", plugin=name)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"plugin {name!r} activated for this session",
        data={"active": sorted(state.plugins)},
    )


def cmd_plugin_deactivate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Manual retire (escape hatch: 'stop touching the tracker'). Engine-driven
    auto-retire (terminal) is the normal path; this is for a lapsed trigger or a
    user change of mind. Archives the bag for audit."""
    state = _require(store, args.session)
    ok = plugins.deactivate(state, args.plugin)
    state.log("plugin_deactivate", plugin=args.plugin, was_active=ok)
    store.save(state)
    detail = (
        f"plugin {args.plugin!r} deactivated (archived)" if ok
        else f"plugin {args.plugin!r} was not active"
    )
    return Directive(ok, state.node, "continue", detail, data={"active": sorted(state.plugins)})


def cmd_plugin_record(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record that a plugin-side publication actually happened: marks
    bag['published_phases'][phase]=True. The coordinator runs this AFTER the
    comment lands, so a publish gate (e.g. tracker's) reflects a real post rather
    than an intention. Generic — any publish-style plugin shares the convention;
    it does NOT fire a plugin event (recording a publish must not re-trigger
    observers). No-op-with-error if the plugin is not active."""
    state = _require(store, args.session)
    bag = state.plugins.get(args.plugin)
    if bag is None:
        return Directive(False, state.node, "noop", f"plugin {args.plugin!r} is not active")
    phase = args.phase
    note = getattr(args, "note", None)
    if phase == "skipped" and not (note and note.strip()):
        return Directive(False, state.node, "noop", "a skip must carry a reason: pass --note")
    published = bag.setdefault("published_phases", {})
    published[phase] = True
    # top-level bool-flag convention: a plugin whose bag seeds a bool keyed by the
    # phase name (e.g. experience: searched/recorded/skipped) reads those flags in
    # its gate; flip it true. Tracker's bag has no such keys, so it is untouched.
    if isinstance(bag.get(phase), bool):
        bag[phase] = True
    if note:
        if phase == "skipped":
            bag["skip_reason"] = note
        elif phase == "searched":
            bag["decision"] = note
    state.log("plugin_record", plugin=args.plugin, phase=phase)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"plugin {args.plugin!r}: phase {phase!r} recorded as published",
        data={"published_phases": sorted(published)},
    )


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
                subject=Subject(
                    material=state.goal or "target",
                    result=state.goal or "change applied",
                ),
                means=Means(means="Edit tool", method="apply the small change in-thread"),
                actor=Actor(executor="in_thread"),
                criterion=Criterion(
                    criterion_type=state.overall_criterion_type,
                    done_criterion=state.overall_done_criterion or "change applied and self-checked",
                ),
            )
        ]
        action, detail = "execute_in_thread", "small change: execute in-thread, then record-result"
    elif result.weight_class == WeightClass.CHAT.value:
        action, detail = "answer_in_thread", "chat: answer directly; terminal at ROUTED"
    else:
        action, detail = "plan", "substantive: route to planner, then submit-plan"

    if result.weight_class == WeightClass.SUBSTANTIVE.value:
        plugins.auto_activate_for(state)

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

    if not state.plan_verified:
        # Stay at PLANNING — do NOT transition or arm the gate. Advancing to
        # PLAN_READY on a failed structure check strands the session there with
        # an armed gate and no recovery edge back (every retry bounced; had to
        # be unstuck by hand via `reset --force`). The agent fixes the plan and
        # re-runs submit-plan in place from PLANNING.
        state.log("submit_plan", plan=plan_path, verified=False)
        store.save(state)
        return Directive(False, state.node, "fix_plan", "plan failed verification", data={"problems": problems})

    state.node = transition(state.node, "submit_plan")
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.log("submit_plan", plan=plan_path, verified=True)
    store.save(state)
    return Directive(
        True, state.node, "await_user_approval",
        "plan ready; HARD GATE — get explicit user approval before approve",
        marker="PLAN-READY",
    )


def cmd_approve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = gates.blockers(state, "plan_approval") + plugins.plugin_gate_blockers(state, "plan_approval")
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty approver: --by must name who approved"]
    if blockers:
        return Directive(False, state.node, "fix_plan", "cannot approve", data={"blockers": blockers})
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "approve")
    state.log("approve", by=args.by)
    store.save(state)
    return Directive(
        True, state.node, "partition",
        "approved; assess partition (M1–M4) before execution",
    )


def cmd_partition(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node != Node.APPROVED.value:
        return Directive(
            False, state.node, "noop",
            f"partition runs after approval, before execution; node={state.node} is not APPROVED",
        )
    m1 = bool(getattr(args, "m1", False))
    m2 = bool(getattr(args, "m2", False))
    m3 = bool(getattr(args, "m3", False))
    m4 = bool(getattr(args, "m4", False))
    m3_severe = bool(getattr(args, "m3_severe", False))
    m4_severe = bool(getattr(args, "m4_severe", False))
    v = verdict(m1, m2, m3, m4, m3_severe, m4_severe)
    state.partition = Partition(
        m1=m1, m2=m2, m3=m3, m4=m4, m3_severe=m3_severe, m4_severe=m4_severe, verdict=v
    )
    section = render_section(m1, m2, m3, m4, m3_severe, m4_severe, v)
    state.node = transition(state.node, "partition")
    state.log("partition", verdict=v, m1=m1, m2=m2, m3=m3, m4=m4)
    store.save(state)
    action = "surface_partition" if v in ("recommended", "possible") else "next_stage"
    detail = (
        f"partition verdict: {v}; surface to the user before implementation"
        if v in ("recommended", "possible")
        else f"partition verdict: {v}; ship as one PR — advance to first stage"
    )
    return Directive(
        True, state.node, action, detail,
        data={"verdict": v, "section": section},
    )


def cmd_next_stage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    ready = state.ready_stages()
    if not ready:
        return Directive(False, state.node, "verify_final", "no ready stages; run verify-final if all passed")
    stage = ready[0]
    # pick the entry edge into EXECUTING from the current node
    if state.node == Node.PARTITIONED.value:
        event = "execute_approved"
    elif state.node == Node.ROUTED.value:
        event = "execute_small"
    elif state.node == Node.VERIFYING.value:
        event = "next_stage"
    else:
        return Directive(False, state.node, "blocked", f"cannot start a stage from node={state.node}")
    state.node = transition(state.node, event)
    stage.outcome.status = StageStatus.ACTIVE.value
    state.current_stage = stage.index
    state.log("next_stage", stage=stage.index, executor=stage.actor.executor)
    store.save(state)
    action = "dispatch" if stage.is_spawn() else "execute_in_thread"
    return Directive(
        True, state.node, action,
        f"stage {stage.index} active: {stage.title}",
        data={"stage": stage.index, "executor": stage.actor.executor,
              "expected_result_image": stage.subject.result},
    )


def cmd_dispatch(args, *, store: StateStore, runner: Runner | None = None,
                 perm_checker=None) -> Directive:
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
    if result.returncode != 0 and _is_recursion_refusal(result):
        # spawn-specialist refused at the recursion cap — a structural blocker, not
        # a stage result. Park at BLOCKED and escalate; never report success. This
        # must win before marker routing (a refusal carries no valid marker).
        state.blocked_from = state.node
        state.node = Node.BLOCKED.value
        state.log("dispatch_refused", stage=stage.index, reason="recursion-cap")
        store.save(state)
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} spawn refused: recursion cap reached — escalate to the user",
            marker="ESCALATE",
            data={"returncode": result.returncode, "stderr": result.stderr},
        )

    # The marker wins over the returncode: a specialist may exit 0 with CLARIFY, or
    # non-zero with a valid escalation marker. spawn-specialist.py has already parsed
    # and (if needed) MALFORMED-wrapped the marker onto stdout.
    marker, body = parse_marker(result.stdout)
    base = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    if marker == "COMPLETED":
        store.save(state)
        return Directive(
            True, state.node, "record_result",
            f"stage {stage.index} returned COMPLETED — diff delivery vs approved intent before recording",
            marker="COMPLETED", data={**base, "intent_diff_required": True},
        )
    if marker == "CLARIFY":
        store.save(state)
        return Directive(
            True, state.node, "answer_clarify",
            f"stage {stage.index} needs a clarification answered before it can continue",
            marker="CLARIFY",
            data={**base, "question": body, "continuation": continuations.clarify(body)},
        )
    if marker == "REPLAN":
        store.save(state)
        return Directive(
            False, state.node, "replan",
            f"stage {stage.index} proposes a plan-level revision",
            marker="REPLAN", data={**base, "reason": body},
        )
    if marker == "INCOMPLETE":
        store.save(state)
        return Directive(
            False, state.node, "decide_incomplete",
            f"stage {stage.index} returned INCOMPLETE — re-spawn / ask / accept",
            marker="INCOMPLETE", data={**base, "reason": body},
        )
    if marker == "PLAN-READY":
        store.save(state)
        return Directive(
            True, state.node, "await_plan_approval",
            f"stage {stage.index} returned a fresh plan — HARD GATE, get explicit user approval",
            marker="PLAN-READY", data=base,
        )
    if marker == "PERMISSION-REQUEST":
        action = body
        checker = perm_checker or permissions.check_permission
        if checker(action):
            # already granted — skip the user ask, re-spawn with the granted note
            store.save(state)
            return Directive(
                True, state.node, "continue_spawn",
                f"stage {stage.index} requested permission already granted: {action}",
                marker="PERMISSION-REQUEST",
                data={**base, "action": action,
                      "continuation": continuations.permission_granted(action, "global")},
            )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=body
        )
        state.log("permission_request", stage=stage.index, action=action)
        store.save(state)
        return Directive(
            True, state.node, "ask_user_permission",
            f"stage {stage.index} requests permission: {action}",
            marker="PERMISSION-REQUEST",
            data={**base, "action": action, "options": ["once", "project", "global", "deny"]},
        )
    if marker is None and result.returncode != 0:
        store.save(state)
        return Directive(
            False, state.node, "handle_spawn_failure",
            f"stage {stage.index} spawn failed (rc={result.returncode}) with no marker",
            data=base,
        )
    # ESCALATE / MALFORMED / marker-less success (rc==0, no marker) -> park BLOCKED.
    return _park_blocked(state, store, stage, marker, base)


def cmd_resolve_permission(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Resume a session parked on a PERMISSION-REQUEST once the manager has the
    user's decision. The user ask is cognitive; this only records the outcome,
    clears the parked request, and hands back the continuation to re-spawn with."""
    state = _require(store, args.session)
    req = state.permission_request
    if req is None:
        return Directive(False, state.node, "noop", "no pending permission request to resolve")
    if args.decision == "granted":
        cont = continuations.permission_granted(req.action, getattr(args, "scope", "once"))
        detail = f"permission granted for {req.action}; re-spawn the stage"
    else:
        cont = continuations.permission_denied(req.action)
        detail = f"permission denied for {req.action}; re-spawn with the fallback"
    state.permission_request = None
    state.log("resolve_permission", action=req.action, decision=args.decision)
    store.save(state)
    return Directive(
        True, state.node, "continue_spawn", detail,
        data={"action": req.action, "decision": args.decision, "continuation": cont},
    )


def cmd_record_result(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to record")
    actual = args.actual or ""
    stage.outcome.actual = actual
    passed = args.status == "passed"

    # General control-criterion attestation: optional on any stage, but required
    # non-empty for spawn:developer + passed (review is the control criterion of a
    # developer-actor stage; reviewer ⊂ controller, developer ⊂ executor).
    control = getattr(args, "control", None) or None
    if control:
        stage.control = control
    if passed and stage.needs_control() and not stage.has_control():
        return Directive(
            False, state.node, "attest_control",
            f"stage {stage.index} is a spawn:developer stage; the control criterion of a "
            "developer-produced result is review — supply it via: "
            "record-result --control '<how the code was reviewed>'",
        )

    # Machine-executed verification: for a measurable stage carrying a verify_command,
    # the engine runs it and OVERRIDES a 'passed' claim the command contradicts. A
    # contradicted pass becomes a real failure (digest + DIAGNOSING), so "report
    # honestly" is an invariant for the measurable subset, not a discipline.
    if passed:
        ok, result = _verify_command_result(stage, runner)
        if not ok:
            passed = False
            note = (
                f"verify_command exit {result.returncode} != expected "
                f"{stage.criterion.expected_exit}: {stage.criterion.verify_command}"
            )
            actual = (actual + "\n" + note) if actual else note
            stage.outcome.actual = actual

    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING

    if passed:
        stage.outcome.status = StageStatus.PASSED.value
        state.current_stage = None
        state.log("record_result", stage=stage.index, status="passed")
        store.save(state)
        if state.all_stages_passed():
            return Directive(True, state.node, "verify_final", f"stage {stage.index} passed; all stages passed")
        return Directive(True, state.node, "next_stage", f"stage {stage.index} passed; more stages ready")

    # failed: loop guard — same stage failing twice on the same actual digest -> escalate
    dig = _digest(actual)
    repeat = dig in stage.outcome.fail_digests
    stage.outcome.fail_digests.append(dig)
    stage.outcome.status = StageStatus.FAILED.value
    state.log("record_result", stage=stage.index, status="failed", repeat=repeat)
    if repeat:
        store.save(state)
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} failed twice with same result digest; stop retrying",
            marker="ESCALATE",
        )
    # enter the overcome-difficulty sub-spine: a fresh Difficulty record must be
    # worked through (declare -> investigate -> critique) before replan is allowed.
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty()
    store.save(state)
    return Directive(
        False, state.node, "declare",
        f"stage {stage.index} failed; run overcome-difficulty — declare the divergence, "
        "then investigate, then critique; replan is blocked until the cycle is complete",
        marker="OVERCOME-DIFFICULTY",
    )


def cmd_verify_final(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = gates.blockers(state, "resolution")
    if blockers:
        return Directive(False, state.node, "fix_stages", "not ready for resolution", data={"blockers": blockers})
    # Final-gate execution (defense in depth): re-run every measurable stage's
    # verify_command — a later stage may have regressed an earlier one. Any
    # non-match refuses RESOLUTION rather than trusting the recorded PASSED flags.
    failures: list[str] = []
    for stage in state.stages:
        ok, result = _verify_command_result(stage, runner)
        if not ok:
            failures.append(
                f"stage {stage.index}: exit {result.returncode} != "
                f"{stage.criterion.expected_exit} ({stage.criterion.verify_command})"
            )
    if failures:
        return Directive(
            False, state.node, "fix_stages",
            "final verification command(s) failed", data={"failures": failures},
        )
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
    # plugin gates fold into resolve (not verify_final) so a plugin can let the
    # session reach RESOLUTION — where its publish-directive fires — yet still
    # block the final resolve until its sub-condition is met.
    blockers = gates.blockers(state, "resolution") + plugins.plugin_gate_blockers(state, "resolution")
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty confirmer: --by must name who confirmed resolution"]
    if blockers:
        return Directive(False, state.node, "fix_stages", "cannot resolve", data={"blockers": blockers})
    state.resolution = GateRecord("resolution", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "resolve")  # RESOLUTION -> RESOLVED
    state.log("resolve", by=args.by)
    store.save(state)
    return Directive(True, state.node, "done", "task resolved", marker="COMPLETED")


# --- overcome-difficulty sub-spine: declare -> investigate -> critique --------
# Each command fills one section of the active Difficulty record in order. The
# engine enforces the ORDERING and that each section's artifact exists; the
# CONTENT (what the divergence is, the >=2 hypotheses, the functional ground) is
# the cognition the overcome-difficulty skill supplies.

def _require_diagnosing(state: SessionState) -> Directive | None:
    if state.node != Node.DIAGNOSING.value:
        return Directive(
            False, state.node, "noop",
            f"difficulty commands run only in the DIAGNOSING cycle; node={state.node}",
        )
    if state.difficulty is None:
        state.difficulty = Difficulty()
    return None


def cmd_declare(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    state.difficulty.declaration = Declaration(
        expected=args.expected, actual=args.actual, mismatch=args.mismatch
    )
    state.log("declare")
    store.save(state)
    return Directive(True, state.node, "investigate",
                     "declaration recorded; localize the divergence next (investigate)")


def cmd_investigate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    if state.difficulty.declaration is None:
        return Directive(False, state.node, "declare",
                         "investigate is out of order: declare the divergence first")
    state.difficulty.investigation = Investigation(
        localized_expectation=args.localized_expectation,
        localized_actual=args.localized_actual,
        hypotheses=list(getattr(args, "hypotheses", None) or []),
    )
    state.log("investigate")
    store.save(state)
    return Directive(True, state.node, "critique",
                     "investigation recorded; state the functional ground + replanning task (critique)")


def cmd_critique(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    if state.difficulty.declaration is None or state.difficulty.investigation is None:
        return Directive(False, state.node, "declare",
                         "critique is out of order: declaration and investigation must come first")
    state.difficulty.critique = Critique(
        functional_ground=args.functional_ground,
        replanning_task=args.replanning_task,
    )
    state.log("critique")
    store.save(state)
    return Directive(True, state.node, "replan",
                     "difficulty cycle complete; replan is now unblocked")


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    from .plan import diff_plans, load_plan as _load

    # precondition: inside the DIAGNOSING cycle, the difficulty record must be
    # complete before a plan may be re-normed (variant (b) — internal command
    # precondition, not a tool-hook gate). [] outside DIAGNOSING.
    dblock = gates.difficulty_blockers(state)
    if dblock:
        return Directive(False, state.node, "declare", "replan blocked by incomplete difficulty record",
                         data={"blockers": dblock})

    if not state.plan_path:
        return Directive(False, state.node, "submit_plan", "no current plan to replan against")
    old = _load(state.plan_path)
    new = _load(args.plan)
    kind = diff_plans(old, new)

    # if we are exiting the DIAGNOSING cycle (difficulty complete), the failed
    # stage is re-armed and we leave the cycle back to VERIFYING so next_stage can
    # retry it; the difficulty record is cleared so a later failure starts fresh.
    diagnosing = state.node == Node.DIAGNOSING.value

    if kind == "no_change":
        if diagnosing:
            for s in state.stages:
                if s.outcome.status == StageStatus.FAILED.value:
                    s.outcome.status = StageStatus.PENDING.value
            state.difficulty = None
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
            state.log("replan", kind="no_change", exited_diagnosing=True)
            store.save(state)
            if state.ready_stages():
                return Directive(True, state.node, "next_stage",
                                 "difficulty worked through; plan unchanged — retry the re-armed stage")
            return Directive(True, state.node, "continue", "difficulty worked through; resume execution")
        return Directive(True, state.node, "continue", "replan is a no-op; plan unchanged")

    if kind == "refinement":
        # apply prose refinements and re-arm any FAILED stage for another attempt
        for ns in new.stages:
            try:
                cur = state.stage(ns.index)
            except KeyError:
                continue
            cur.title = ns.title
            cur.subject.result = ns.subject.result
            if cur.outcome.status == StageStatus.FAILED.value:
                cur.outcome.status = StageStatus.PENDING.value
        state.plan_path = args.plan
        if diagnosing:
            state.difficulty = None
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
        state.log("replan", kind="refinement", exited_diagnosing=diagnosing)
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
    state.difficulty = None
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
            "stages": [{"index": s.index, "status": s.outcome.status, "title": s.title} for s in state.stages],
            "approval_passed": state.approval.passed,
            "resolution_passed": state.resolution.passed,
        },
    )


COMMANDS = {
    "start": cmd_start,
    "reset": cmd_reset,
    "plugin-activate": cmd_plugin_activate,
    "plugin-deactivate": cmd_plugin_deactivate,
    "plugin-record": cmd_plugin_record,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "submit-plan": cmd_submit_plan,
    "approve": cmd_approve,
    "partition": cmd_partition,
    "next-stage": cmd_next_stage,
    "dispatch": cmd_dispatch,
    "resolve-permission": cmd_resolve_permission,
    "record-result": cmd_record_result,
    "declare": cmd_declare,
    "investigate": cmd_investigate,
    "critique": cmd_critique,
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
    sp.add_argument("--if-absent", dest="if_absent", action="store_true")

    sp = add("reset"); sp.add_argument("--session", required=True); sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default=""); sp.add_argument("--done-criterion", dest="done_criterion", default="")
    sp.add_argument("--criterion-type", dest="criterion_type", default=CriterionType.MEASURABLE.value)
    sp.add_argument("--recursion-depth", dest="recursion_depth", type=int, default=0)
    sp.add_argument("--force", action="store_true")

    sp = add("plugin-activate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp = add("plugin-deactivate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp = add("plugin-record"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True); sp.add_argument("--phase", required=True)
    sp.add_argument("--note", default=None)

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
    sp = add("partition"); sp.add_argument("--session", required=True)
    sp.add_argument("--m1", action="store_true"); sp.add_argument("--m2", action="store_true")
    sp.add_argument("--m3", action="store_true"); sp.add_argument("--m4", action="store_true")
    sp.add_argument("--m3-severe", dest="m3_severe", action="store_true")
    sp.add_argument("--m4-severe", dest="m4_severe", action="store_true")
    sp = add("next-stage"); sp.add_argument("--session", required=True)
    sp = add("dispatch"); sp.add_argument("--session", required=True)
    sp.add_argument("--budget", default="medium"); sp.add_argument("--complexity", default="medium")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("resolve-permission"); sp.add_argument("--session", required=True)
    sp.add_argument("--decision", choices=["granted", "denied"], required=True)
    sp.add_argument("--scope", choices=["once", "project", "global"], default="once")
    sp = add("record-result"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], required=True)
    sp.add_argument("--actual", default="")
    sp.add_argument("--control", default=None,
                    help="control-criterion attestation (required for spawn:developer stages "
                         "when recording passed; accepted on any stage)")
    sp = add("declare"); sp.add_argument("--session", required=True)
    sp.add_argument("--expected", required=True); sp.add_argument("--actual", required=True)
    sp.add_argument("--mismatch", required=True)
    sp = add("investigate"); sp.add_argument("--session", required=True)
    sp.add_argument("--localized-expectation", dest="localized_expectation", required=True)
    sp.add_argument("--localized-actual", dest="localized_actual", required=True)
    sp.add_argument("--hypothesis", dest="hypotheses", action="append", default=None,
                    help="a candidate hypothesis (repeatable; >=2 required for a complete record)")
    sp = add("critique"); sp.add_argument("--session", required=True)
    sp.add_argument("--functional-ground", dest="functional_ground", required=True)
    sp.add_argument("--replanning-task", dest="replanning_task", required=True)
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
    else:
        _fire_plugins(args, store, directive)
    print(json.dumps(directive.to_dict(), ensure_ascii=False, indent=2))
    return 0 if directive.ok else 1


def _fire_plugins(args, store: StateStore, directive: Directive) -> None:
    """After a command runs, fire the matching plugin event on the (just-saved)
    state so active plugins can observe, gate, and auto-retire. Central wiring —
    the command bodies stay plugin-agnostic. Fires regardless of directive.ok
    (a blocked resolve must still surface its publish nudge). A plugin-less session
    skips entirely (no reload, no behavior change). Plugin faults never crash the
    engine."""
    event = plugins.event_for(args.command)
    if event is None:
        return
    session = getattr(args, "session", None)
    if not session:
        return
    try:
        state = store.load(session)
        if state is None or not plugins.active(state):
            return
        plugins.fire(event, state, directive)
        store.save(state)
    except Exception as exc:  # observability without aborting the directive
        directive.data.setdefault("plugin_errors", []).append(str(exc))


if __name__ == "__main__":
    sys.exit(main())
