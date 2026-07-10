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
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from lib import config_root

from . import advisor, continuations, cost, gates, permissions, plugins
from .classify import TRACKER_KEY_RE, Signals, classify
from .config import Thresholds
from .partition import render_section, render_units, verdict
from .directive import Directive
from .dispatch import Runner, dispatch_stage, parse_marker, subprocess_runner
from .machine import transition
from .plan import load_plan
from .state import (
    _EXECUTION_NODES,
    _MAX_PLAN_STACK,
    Actor,
    Critique,
    Criterion,
    CriterionType,
    Declaration,
    Difficulty,
    FinalCheck,
    Investigation,
    JudgeBypass,
    Partition,
    PartitionUnit,
    PARTITION_UNIT_MODES,
    GateRecord,
    Means,
    Node,
    PermissionRequest,
    PlanFrame,
    PlanReview,
    Route,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
    Subject,
    WeightClass,
)
from .store import FileStateStore, StateStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VERIFY_PLAN_CLI = REPO_ROOT / "scripts" / "verify-plan-file.py"
GATE_LOG = config_root.agentctl_gate_log()
# Per-task quality ledger (quality-regression-tracking): one row per resolved
# task, stamped with the instructions-repo HEAD so a quality drop can be
# correlated back to an instruction-commit range. Same fixed-path/append-only
# idiom as ~/.local/log/claude-spawn-costs.jsonl (spawn-specialist.py).
TASK_QUALITY_LOG = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"
_GIT_HEAD_TIMEOUT_S = 5
_VALID_QUALITY_RATINGS = (1, 2, 3, 4, 5)


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _plan_file_sha256(target: str | None) -> str:
    """sha256 of a plan file's bytes, or '' when there is no readable file (#16).

    Best-effort by design: an unreadable/absent target yields '' so the plan-review
    gate degrades to path-only binding rather than wedging on a transient I/O error.
    cmd_plan_review records this over the reviewed bytes; gates.plan_review_blockers
    inlines the same sha256-of-bytes recompute (it cannot import cli — circular)."""
    if not target:
        return ""
    try:
        return hashlib.sha256(Path(target).read_bytes()).hexdigest()
    except OSError:
        return ""


def _observation_sha256(observation: str) -> str:
    """sha256 of an acceptance observation's bytes — the binding key the acceptance
    gate recomputes over the observation being recorded. Mirrors _plan_file_sha256 but
    over an in-memory string (the observation is never a file)."""
    return hashlib.sha256((observation or "").encode("utf-8")).hexdigest()


def _record_stage_review(state: SessionState, review: StageReview, *, from_judge: bool) -> None:
    """Store a StageReview, one per stage_index (last-wins). A judge verdict
    (from_judge=True) NEVER clobbers a human/manual review already present for the
    stage (e.g. an override): the automated cognition must not silently overwrite the
    user's explicit escape. A manual record (from_judge=False, via cmd_stage_review)
    always replaces."""
    existing = [r for r in state.stage_reviews if r.stage_index == review.stage_index]
    if from_judge and existing and any(r.reviewer != advisor.JUDGE_REVIEWER for r in existing):
        return
    state.stage_reviews = [r for r in state.stage_reviews if r.stage_index != review.stage_index]
    state.stage_reviews.append(review)


def _judge_bypassed_surface(state: SessionState) -> list[dict]:
    """The recorded acceptance-judge bypasses as plain dicts, for verify-final and the
    resolution summary to surface verbatim ([] when none)."""
    return [
        {"stage_index": b.stage_index, "kind": b.kind, "reviewer": b.reviewer, "note": b.note}
        for b in state.judge_bypassed
    ]


def _record_bypass(state: SessionState, bypass: JudgeBypass) -> None:
    """Append a JudgeBypass (never cleared by a later passing review) so verify-final
    and the resolution summary can surface every acceptance pass that skipped a genuine
    judge verdict. Deduplicated on (stage_index, kind) so a re-run of the same passed
    record does not multiply entries."""
    if any(b.stage_index == bypass.stage_index and b.kind == bypass.kind for b in state.judge_bypassed):
        return
    state.judge_bypassed.append(bypass)


def _snapshot_approved_plan(store: StateStore, state: SessionState) -> tuple[str, str] | None:
    """Copy the plan AS APPROVED into the state dir and return (snapshot_path, hash).

    #8: cmd_replan must diff the corrected plan against what was APPROVED — not
    against state.plan_path, which the coordinator may edit in place (an in-place
    edit would else self-diff to no_change and silently drop the correction). Taken
    at every approve so a substantive-replan re-approval refreshes the baseline.

    Best-effort: returns None (leaving cmd_replan to fall back to plan_path, the
    prior behaviour) when there is no plan, the store exposes no on-disk path, or
    the plan file is unreadable. Content-hash-named so identical plans share one
    file; the per-session snapshot_path recorded on the state is the source of
    truth for which snapshot to diff against."""
    if not state.plan_path:
        return None
    src = Path(state.plan_path)
    path_fn = getattr(store, "path", None)
    if path_fn is None or not src.exists():
        return None
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    snap = path_fn(state.session_id).parent / f"plan-approved-{digest[:16]}.toml"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(data)
    return str(snap), digest


def _apply_refined_stage_fields(cur, refined) -> None:
    """Copy the prose+verify definition fields of a freshly-loaded stage onto the
    matching live stage. Shared by both replan branches that re-materialize from a
    corrected plan (refinement, and the no_change refresh) so the two never drift.
    Outcome/status is NOT touched — re-arm logic stays with each caller."""
    cur.title = refined.title
    cur.subject.result = refined.subject.result
    cur.means.means = refined.means.means
    cur.means.method = refined.means.method
    cur.subject.invariants = refined.subject.invariants
    cur.conditions = refined.conditions
    cur.criterion.verify_command = refined.criterion.verify_command
    cur.criterion.expected_exit = refined.criterion.expected_exit


def _refresh_caches_from_plan_path(state: SessionState) -> None:
    """Re-load state.plan_path and refresh state.final_check plus each live
    stage's prose/criterion fields from those bytes.

    Approve snapshots and hashes plan_path, but the plan-review cycle answers a
    REVISE verdict by editing plan_path IN PLACE at PLAN_READY (deliberately
    plan-mutable — see hook-state-gate.py's PLAN_MUTABLE_NODES), so the copy
    cached at submit-plan can drift from the file approve is about to attest.
    Without this refresh, plan_snapshot_hash matches the edited bytes while
    dispatch/verify-final keep running the stale pre-review cache — the gate
    attests to a plan it never actually executes.

    Mutates each live stage IN PLACE via `_apply_refined_stage_fields`, which
    never touches `outcome` — an unchanged stage's Outcome therefore survives
    with no extra logic. A stage whose full definition (`stage_carry_key`)
    DID change is a different case: its recorded PASSED outcome no longer
    attests to the stage's current criterion, so it is reset to PENDING for
    re-verification. The carry-key must be read from `cur` BEFORE
    `_apply_refined_stage_fields` mutates it — comparing after would compare
    `cur` against itself post-copy, which always matches and would let a
    genuinely stale PASSED outcome survive unnoticed.

    Best-effort like `_snapshot_approved_plan`: an absent plan_path or a plan
    file that fails to load leaves the existing cache untouched rather than
    raising out of approve."""
    if not state.plan_path:
        return
    from .plan import PlanError, load_plan as _load, stage_carry_key
    try:
        refreshed = _load(state.plan_path)
    except (OSError, PlanError):
        return
    for rs in refreshed.stages:
        try:
            cur = state.stage(rs.index)
        except KeyError:
            continue
        unchanged = stage_carry_key(cur) == stage_carry_key(rs)
        _apply_refined_stage_fields(cur, rs)
        if not unchanged and cur.outcome.status == StageStatus.PASSED.value:
            cur.outcome.status = StageStatus.PENDING.value
    state.final_check = refreshed.meta.final_check


def _log_gate(state: SessionState, gate: str, blockers: list[str], *, passed: bool) -> None:
    """Append one {ts, session, node, gate, blockers, passed} line to GATE_LOG.

    Fail-open: any I/O error is swallowed so telemetry never blocks a gate
    transition. Mirrors cost.py's tolerant append-only JSONL-ledger idiom. Reads
    GATE_LOG as a module global (not a captured default) so tests can monkeypatch
    cli.GATE_LOG and have this pick it up on the next call."""
    row = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": state.session_id,
        "node": state.node,
        "gate": gate,
        "blockers": list(blockers),
        "passed": passed,
    }
    try:
        GATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GATE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _instructions_head() -> str | None:
    """Best-effort `git -C REPO_ROOT rev-parse HEAD` -> stripped stdout, or None
    on any failure (git absent, not a repo, timeout). Never blocks resolution —
    the ledger row simply carries instructions_head=null."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=_GIT_HEAD_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _write_quality_row(row: dict) -> None:
    """Append one task-quality row to TASK_QUALITY_LOG. Fail-open like _log_gate:
    an I/O error never blocks the resolve transition that already happened."""
    try:
        TASK_QUALITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TASK_QUALITY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _attach_advisories(d: Directive, kind: str, payload: dict, runner: Runner | None,
                       *, weight_class: str | None = None) -> None:
    """Attach warn-only advisory strings to d.data['advisories']. Never changes d.ok or d.node.

    Single chokepoint for the enabled resolution: env override, else config-mode +
    weight_class (advisor.resolve_enabled) — every call site threads its session's
    weight_class through here rather than re-deriving the rule per site."""
    enabled = advisor.resolve_enabled(weight_class)
    advisories = advisor.judge(kind, payload, runner, enabled=enabled)
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)


def _run_check(command: str, expected_exit: int, runner: Runner | None, cwd: str | None = None):
    """Run `command` via the injected runner; return (ok, result).

    When `cwd` is set, prefixes `cd <cwd> && ` so the Runner protocol
    (argv -> RunResult) and every injected fake stay unchanged. With cwd None the
    string is byte-identical to the pre-repo_root behaviour. A non-existent cwd
    makes `cd` fail and `&&` short-circuit, surfacing as a verify failure."""
    run = runner or subprocess_runner
    cmd = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
    result = run(["bash", "-c", cmd])
    return result.returncode == expected_exit, result


def _verify_command_result(stage, runner: Runner | None, cwd: str | None = None):
    """Execute a measurable stage's `verify_command`, if it has one.

    Returns (ok, result). When the stage carries no command, or its criterion is
    not measurable, returns (True, None) — there is nothing executable to gate on,
    so the engine keeps its flag-only behaviour. Otherwise delegates to _run_check,
    which is also used for typed final_check entries at verify-final."""
    crit = stage.criterion
    if not crit.verify_command or crit.criterion_type != CriterionType.MEASURABLE.value:
        return True, None
    return _run_check(crit.verify_command, crit.expected_exit, runner, cwd)


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
    if sig.tracker_key and TRACKER_KEY_RE.match(sig.tracker_key):
        state.tracker_key = sig.tracker_key
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
    d = Directive(True, state.node, action, detail, data={"reasons": result.reasons})
    _attach_advisories(d, "weight_classification",
                       {"goal": state.goal, "weight_class": state.weight_class, "route": state.route},
                       runner, weight_class=state.weight_class)
    return d


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
    # #15: a resubmission — the coordinator revised the plan at PLAN_READY (after a
    # thinker `revise` verdict, or the user's own pre-approval edit) and re-runs
    # submit-plan without a reset --force. Distinguished by the source node; drives
    # the `revise_plan` edge (PLAN_READY -> PLAN_READY) instead of `submit_plan`.
    resubmitting = state.node == Node.PLAN_READY.value
    if state.weight_class == WeightClass.SUBSTANTIVE.value and not plan_path.endswith(".toml"):
        state.log("submit_plan", plan=plan_path, verified=False)
        store.save(state)
        return Directive(
            False, state.node, "fix_plan",
            "substantive plan must be TOML (markdown is the prose mirror only and cannot track typed stages)",
            data={"problems": ["substantive plan must be a .toml file; rewrite as TOML with typed stages"]},
        )
    if plan_path.endswith(".toml"):
        doc = load_plan(plan_path)
        state.stages = doc.stages
        state.repo_root = doc.meta.repo_root
        state.final_check = doc.meta.final_check
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

    state.node = transition(state.node, "revise_plan" if resubmitting else "submit_plan")
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    if resubmitting:
        # The plan changed, so any recorded thinker review examined a now-stale
        # version — clear it unconditionally so the plan-review gate re-arms for the
        # new plan (a same-path in-place edit would slip a plan_path-bound check).
        state.plan_review = None
    state.plan_submitted_ts = time.time()
    state.log("submit_plan", plan=plan_path, verified=True, revised=resubmitting)
    store.save(state)
    d = Directive(
        True, state.node, "await_user_approval",
        "plan ready; HARD GATE — get explicit user approval before approve",
        marker="PLAN-READY",
    )
    _attach_advisories(d, "plan_completeness",
                       {"plan": plan_path, "stage_count": len(state.stages),
                        "titles": [s.title for s in state.stages]},
                       runner, weight_class=state.weight_class)
    return d


def cmd_plan_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a thinker review of a plan version, backing the plan-review gate.

    The COGNITION (the thinker's reasoning) happens in the thinker leaf; this only
    records its verdict, bound to the plan file it examined (`--target`, defaulting
    to the session's current plan_path — pass the NEW plan for a replan-time review).
    Purely a recorder, mirroring declare/investigate/critique: gates.
    plan_review_blockers enforces bind/verdict at approve/replan, so an incomplete
    override recorded here simply fails to clear the gate rather than erroring."""
    state = _require(store, args.session)
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to review: submit a plan first, or pass --target <plan.toml>",
        )
    # An override is the USER's escape from a reviewer's `revise` deadlock — the
    # reviewer who issued the blocking verdict cannot override themselves. Checked
    # here, before the record is overwritten and the prior reviewer's identity lost.
    if args.verdict == gates._PLAN_REVIEW_OVERRIDE:
        prev = state.plan_review
        new_reviewer = (getattr(args, "reviewer", "") or "").strip()
        if (
            prev is not None
            and prev.plan_path == target
            and prev.verdict == gates._PLAN_REVIEW_REVISE
            and new_reviewer
            and new_reviewer == (prev.reviewer or "").strip()
        ):
            return Directive(
                False, state.node, "noop",
                f"override must come from a distinct reviewer: {new_reviewer!r} is the "
                "reviewer whose 'revise' verdict it would override (the user is the "
                "expected override author)",
            )
    state.plan_review = PlanReview(
        plan_path=target,
        verdict=args.verdict,
        reviewer=getattr(args, "reviewer", "") or "",
        concerns=list(getattr(args, "concerns", None) or []),
        note=getattr(args, "note", "") or "",
        plan_sha256=_plan_file_sha256(target),
    )
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("plan_review", target=target, verdict=args.verdict,
              reviewer=state.plan_review.reviewer)
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "thinker review recorded but does not clear the gate",
            data={"blockers": blockers},
        )
    return Directive(
        True, state.node, "continue",
        f"thinker review recorded for {target} (verdict={args.verdict}); "
        "the plan-review gate is now satisfied for this plan version",
    )


def cmd_stage_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a manual review of the active acceptance_review stage's observation,
    backing the acceptance-review gate. Mirrors cmd_plan_review — purely a recorder; the
    COGNITION (a human judging, or authoring an override) happens outside. The verdict is
    bound to the observation bytes passed via --observation (defaulting to the stage's
    current observation), so gates.acceptance_review_blockers can reject a drift. The
    automated cheap judge writes an equivalent record inline in record-result; this
    command is the human path (chiefly the override deadlock escape)."""
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to review")
    if stage.criterion.criterion_type != CriterionType.ACCEPTANCE_REVIEW.value:
        return Directive(
            False, state.node, "noop",
            f"stage {stage.index} is not acceptance_review; stage-review applies only to "
            "acceptance stages",
        )
    observation = getattr(args, "observation", None)
    if observation is None:
        observation = stage.criterion.observation or ""
    _record_stage_review(
        state,
        StageReview(
            stage_index=stage.index,
            verdict=args.verdict,
            reviewer=getattr(args, "reviewer", "") or "",
            concerns=list(getattr(args, "concerns", None) or []),
            note=getattr(args, "note", "") or "",
            observation_sha256=_observation_sha256(observation),
        ),
        from_judge=False,
    )
    state.log("stage_review", stage=stage.index, verdict=args.verdict,
              reviewer=getattr(args, "reviewer", "") or "")
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"stage review recorded for stage {stage.index} (verdict={args.verdict}); "
        "record-result --status passed will re-check the acceptance gate against it",
    )


def cmd_approve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    blockers = (
        gates.blockers(state, "plan_approval")
        + plugins.plugin_gate_blockers(state, "plan_approval")
        + gates.plan_review_blockers(state, state.plan_path)
    )
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty approver: --by must name who approved"]
    _log_gate(state, "plan_approval", blockers, passed=not blockers)
    if blockers:
        return Directive(False, state.node, "fix_plan", "cannot approve", data={"blockers": blockers})
    _refresh_caches_from_plan_path(state)
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "approve")
    snap = _snapshot_approved_plan(store, state)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    state.log("approve", by=args.by)
    store.save(state)
    return Directive(
        True, state.node, "partition",
        "approved; assess partition (M1–M4) before execution",
    )


def _parse_partition_units(
    raw_units: list[str], known_indices: set[int]
) -> tuple[list[PartitionUnit], list[str]]:
    """Parse repeatable ``--unit '<mode>|<stages csv>|<title>[|<ref>]'`` specs into
    typed PartitionUnit objects, validating against the loaded plan. Returns
    ``(units, errors)`` — a non-empty ``errors`` list means the caller must reject
    with a failing Directive and record nothing.

    Validation: mode ∈ PARTITION_UNIT_MODES; a non-empty title; ≥1 integer stage
    index; every stage index exists in ``known_indices``; stage sets pairwise
    disjoint across units (a stage belongs to at most one delivery unit — stages
    left uncovered stay on the default single-PR path). ``ref`` is optional and
    org-neutral (tracker key / issue URL / child session id, assigned at
    materialization)."""
    units: list[PartitionUnit] = []
    errors: list[str] = []
    seen_stages: dict[int, int] = {}  # stage index -> owning unit (1-based)
    for pos, spec in enumerate(raw_units, start=1):
        parts = spec.split("|")
        if len(parts) < 3:
            errors.append(
                f"unit {pos}: expected '<mode>|<stages csv>|<title>[|<ref>]', got {spec!r}"
            )
            continue
        mode = parts[0].strip()
        stages_field = parts[1].strip()
        title = parts[2].strip()
        ref = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else None
        if mode not in PARTITION_UNIT_MODES:
            errors.append(
                f"unit {pos}: unknown mode {mode!r} (expected one of {', '.join(PARTITION_UNIT_MODES)})"
            )
        if not title:
            errors.append(f"unit {pos}: empty title")
        stages: list[int] = []
        for tok in [t.strip() for t in stages_field.split(",") if t.strip()]:
            try:
                stages.append(int(tok))
            except ValueError:
                errors.append(f"unit {pos}: non-integer stage index {tok!r}")
        if not stages:
            errors.append(f"unit {pos}: no stage indices given")
        for s in stages:
            if s not in known_indices:
                errors.append(f"unit {pos}: stage index {s} does not exist in the plan")
            elif s in seen_stages:
                errors.append(
                    f"unit {pos}: stage index {s} already assigned to unit {seen_stages[s]} "
                    "(units must be disjoint)"
                )
            else:
                seen_stages[s] = pos
        units.append(PartitionUnit(title=title, stages=stages, mode=mode, ref=ref))
    return units, errors


def cmd_partition(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    if state.node != Node.APPROVED.value:
        return Directive(
            False, state.node, "noop",
            f"partition runs after approval, before execution; node={state.node} is not APPROVED",
        )
    known = {s.index for s in state.stages}
    units, unit_errors = _parse_partition_units(getattr(args, "unit", None) or [], known)
    if unit_errors:
        return Directive(
            False, state.node, "fix_units",
            "invalid partition units — nothing recorded", data={"errors": unit_errors},
        )
    m1 = bool(getattr(args, "m1", False))
    m2 = bool(getattr(args, "m2", False))
    m3 = bool(getattr(args, "m3", False))
    m4 = bool(getattr(args, "m4", False))
    m3_severe = bool(getattr(args, "m3_severe", False))
    m4_severe = bool(getattr(args, "m4_severe", False))
    v = verdict(m1, m2, m3, m4, m3_severe, m4_severe)
    state.partition = Partition(
        m1=m1, m2=m2, m3=m3, m4=m4, m3_severe=m3_severe, m4_severe=m4_severe,
        verdict=v, units=units,
    )
    stage_depends = {s.index: s.depends_on for s in state.stages}
    section = render_section(m1, m2, m3, m4, m3_severe, m4_severe, v,
                            units=units, stage_depends=stage_depends)
    state.node = transition(state.node, "partition")
    state.log("partition", verdict=v, m1=m1, m2=m2, m3=m3, m4=m4, units=len(units))
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


def cmd_partition_units(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-record) the per-unit delivery routing AFTER the verdict is
    surfaced — the user's structure decision (subtickets vs several PRs vs one)
    arrives once they have seen the M1–M4 verdict. Allowed only at PARTITIONED (just
    after `partition`) or EXECUTING (mid-flight structure change); replaces the whole
    units list with the parsed `--unit` specs and leaves the verdict + node
    untouched.

    Re-recording at EXECUTING replaces the list WITHOUT validating against
    already-PASSED stages — a documented limitation, not a check."""
    state = _require(store, args.session)
    if state.node not in (Node.PARTITIONED.value, Node.EXECUTING.value):
        return Directive(
            False, state.node, "noop",
            "partition-units runs after the partition verdict is surfaced; "
            f"node={state.node} is neither PARTITIONED nor EXECUTING",
        )
    if state.partition is None:
        return Directive(
            False, state.node, "partition",
            "no partition assessment recorded yet — run `partition` first",
        )
    known = {s.index for s in state.stages}
    units, unit_errors = _parse_partition_units(getattr(args, "unit", None) or [], known)
    if unit_errors:
        return Directive(
            False, state.node, "fix_units",
            "invalid partition units — the recorded list is unchanged",
            data={"errors": unit_errors},
        )
    state.partition.units = units
    stage_depends = {s.index: s.depends_on for s in state.stages}
    block = render_units(units, stage_depends)
    state.log("partition_units", units=len(units))
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"recorded {len(units)} delivery unit(s)",
        data={"units_block": block},
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
    if action == "dispatch":
        # #13: the directive must be unambiguous that `agentctl dispatch` IS the
        # spawn (synchronous, blocking) — a generic detail invited coordinators
        # to spawn manually and then feed dispatch a second, duplicate spawn.
        detail = (
            f"stage {stage.index} active: {stage.title} — spawning "
            f"{stage.actor.executor} now via agentctl dispatch (synchronous, "
            "blocking); do NOT spawn manually with spawn-specialist.py or claude -p"
        )
    else:
        detail = f"stage {stage.index} active: {stage.title}"
    return Directive(
        True, state.node, action, detail,
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
    dry_run = bool(getattr(args, "dry_run", False))
    result = dispatch_stage(
        stage, state.plan_path or "",
        runner=runner,
        budget=getattr(args, "budget", "medium"),
        complexity=getattr(args, "complexity", "medium"),
        dry_run=dry_run,
    )
    if dry_run:
        # #10: a dry-run is a pure preview — no event log, no state save, no
        # marker routing. The echoed command is the whole result.
        return Directive(
            True, state.node, "preview",
            f"stage {stage.index} dry-run preview (no state change)",
            data={"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
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

    # Acceptance-review observation gate: recording a PASSED acceptance stage requires
    # a non-empty observation that differs (normalized) from the expected image.
    # An echoed target ("I saw the expected result") is no observation at all.
    observation = getattr(args, "observation", None) or ""
    if passed and stage.criterion.criterion_type == CriterionType.ACCEPTANCE_REVIEW.value:
        norm_obs = gates._normalize_string(observation)
        norm_img = gates._normalize_string(stage.subject.result)
        if not norm_obs:
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} is acceptance_review; acceptance pass requires "
                "recording WHAT you observed, distinct from the expected image "
                "(supply: record-result --observation '<what you observed>')",
            )
        if norm_obs == norm_img:
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} is acceptance_review; acceptance pass requires "
                "recording WHAT you observed, distinct from the expected image — "
                "echoing the target does not count "
                "(supply: record-result --observation '<what you observed>')",
            )

        # Cheap-judge COGNITION + PURE gate. When the acceptance-review gate is active
        # (substantive session / AGENTCTL_STAGE_REVIEW=1), run the fail-open haiku judge
        # over the observation, record its verdict as a StageReview bound to the
        # observation bytes, then block the pass on gates.acceptance_review_blockers
        # (which reads ONLY that record). The judge fails open (no verdict on
        # timeout/error) and the gate fails closed (no verdict blocks), so an
        # unavailable judge stalls the pass rather than waving it through.
        # bind the observation to the stage now so the gate's sha recompute sees it.
        stage.criterion.observation = observation
        if gates.stage_review_active(state):
            judge_runner = runner if runner is not None else advisor.subprocess_runner
            verdict, reason = advisor.acceptance_judge(
                observation, stage.subject.result, judge_runner, enabled=True)
            if verdict is not None:
                _record_stage_review(
                    state,
                    StageReview(
                        stage_index=stage.index, verdict=verdict,
                        reviewer=advisor.JUDGE_REVIEWER, note=reason,
                        observation_sha256=_observation_sha256(observation),
                    ),
                    from_judge=True,
                )
            ab = gates.acceptance_review_blockers(state, stage)
            if ab:
                store.save(state)
                return Directive(
                    False, state.node, "attest_observation",
                    f"stage {stage.index} acceptance pass blocked by the judge gate",
                    data={"blockers": ab},
                )
            # Cleared: if it cleared via an override, that is a bypass of a genuine
            # passing verdict — record it visibly (never cleared by a later review).
            rev = gates._stage_review_for(state, stage.index)
            if rev is not None and rev.verdict == gates._STAGE_REVIEW_OVERRIDE:
                _record_bypass(state, JudgeBypass(
                    stage_index=stage.index, kind="override",
                    reviewer=rev.reviewer, note=rev.note))
        elif (os.environ.get("AGENTCTL_STAGE_REVIEW") == "0"
              and state.weight_class == WeightClass.SUBSTANTIVE.value):
            # The gate WOULD apply to this substantive session but the kill switch
            # disabled it: the acceptance pass proceeds WITHOUT a judge verdict — record
            # the bypass so verify-final/resolve surface that this pass was unjudged.
            _record_bypass(state, JudgeBypass(
                stage_index=stage.index, kind="killswitch", reviewer="",
                note="AGENTCTL_STAGE_REVIEW=0"))

    # Machine-executed verification: for a measurable stage carrying a verify_command,
    # the engine runs it and OVERRIDES a 'passed' claim the command contradicts. A
    # contradicted pass becomes a real failure (digest + DIAGNOSING), so "report
    # honestly" is an invariant for the measurable subset, not a discipline.
    if passed:
        ok, result = _verify_command_result(stage, runner, cwd=state.repo_root)
        if not ok:
            passed = False
            note = (
                f"verify_command exit {result.returncode} != expected "
                f"{stage.criterion.expected_exit}: {stage.criterion.verify_command}"
            )
            actual = (actual + "\n" + note) if actual else note
            stage.outcome.actual = actual

    # Attribute cost for spawn stages from the cost log. In-thread stages leave
    # None — cost splitting per in-thread stage is out of scope for this attribution.
    if stage.is_spawn():
        _cost_log = getattr(args, "cost_log", None)
        _log_path = Path(_cost_log) if _cost_log else cost.COST_LOG
        _rows = cost.read_rows(_log_path)
        _attr = cost.attribute_stage(_rows, state.plan_path, stage.index)
        stage.outcome.cost_usd = _attr["cost_usd"]
        stage.outcome.duration_ms = _attr["duration_ms"]
        stage.outcome.spawn_count = _attr["spawn_count"]

    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING

    if passed:
        stage.outcome.status = StageStatus.PASSED.value
        if observation:
            stage.criterion.observation = observation
        state.current_stage = None
        state.log("record_result", stage=stage.index, status="passed")
        store.save(state)
        if state.all_stages_passed():
            d = Directive(True, state.node, "verify_final", f"stage {stage.index} passed; all stages passed")
        else:
            d = Directive(True, state.node, "next_stage", f"stage {stage.index} passed; more stages ready")
        # Warn-only advisory: kept ONLY for the non-gated acceptance path. When the
        # judge gate is active it already paid a cheap judge over this same observation,
        # so re-running the sonnet advisory here would pay for the judgement twice; the
        # advisory survives as the fallback cognition when the gate is off (kill switch).
        if (stage.criterion.criterion_type == CriterionType.ACCEPTANCE_REVIEW.value
                and not gates.stage_review_active(state)):
            _attach_advisories(d, "acceptance_observation",
                               {"expected": stage.subject.result, "observation": observation},
                               runner, weight_class=state.weight_class)
        return d

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
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        return Directive(False, state.node, "fix_stages", "not ready for resolution", data={"blockers": blockers})
    # Final-gate execution (defense in depth): re-run every measurable stage's
    # verify_command — a later stage may have regressed an earlier one. Any
    # non-match refuses RESOLUTION rather than trusting the recorded PASSED flags.
    failures: list[str] = []
    for stage in state.stages:
        ok, result = _verify_command_result(stage, runner, cwd=state.repo_root)
        if not ok:
            failures.append(
                f"stage {stage.index}: exit {result.returncode} != "
                f"{stage.criterion.expected_exit} ({stage.criterion.verify_command})"
            )
    for fc in state.final_check:
        ok, result = _run_check(fc.command, fc.expected_exit, runner, cwd=state.repo_root)
        if not ok:
            label = fc.label or fc.command
            failures.append(
                f"final_check '{label}': exit {result.returncode} != {fc.expected_exit}"
            )
    if failures:
        # A failing final_check is a difficulty (actual result diverges from the
        # plan's declared image) exactly like a failed stage — route into the same
        # DIAGNOSING cycle (record_result's failed-stage path, above) rather than
        # stranding the session at VERIFYING with no reachable resolution: from
        # VERIFYING, declare/investigate/critique all refuse ("difficulty commands
        # run only in the DIAGNOSING cycle"), and only `reset --force` escaped.
        state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
        state.difficulty = Difficulty()
        store.save(state)
        return Directive(
            False, state.node, "declare",
            "final verification command(s) failed; run overcome-difficulty — declare "
            "the divergence, then investigate, then critique; replan is blocked until "
            "the cycle is complete",
            data={"failures": failures},
        )
    # Compute whole-plan cost rollup from already-attributed stage outcomes.
    # No second log read — record-result already stored the costs on each Outcome.
    rollup = cost.rollup_plan([], state.plan_path, state.stages)
    state.cost = rollup
    state.node = transition(state.node, "final")  # VERIFYING -> RESOLUTION
    state.resolution = GateRecord("resolution", armed=True, passed=False)
    state.log("verify_final")
    store.save(state)
    kind = "run the measurable check" if state.overall_criterion_type == CriterionType.MEASURABLE.value \
        else "ask the user to accept on review"
    data = {
        "cost": {
            "total_cost_usd": rollup.total_cost_usd,
            "total_duration_ms": rollup.total_duration_ms,
            "spawn_count": rollup.spawn_count,
            "attributed_stages": rollup.attributed_stages,
            "note": rollup.note,
        }
    }
    # Bypass visibility: verify-final never returns a clean bill while any acceptance
    # pass skipped a genuine judge verdict (kill switch / override) — the bypasses are
    # surfaced verbatim so the resolution decision is made with them in view, never
    # silently. A later passing review does not clear them.
    detail = f"all stages passed; resolution gate armed — {kind}"
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f"; WARNING: {len(bypasses)} acceptance judge bypass(es) recorded (see judge_bypassed)"
    return Directive(True, state.node, "await_user_confirmation", detail, data=data)


def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    # plugin gates fold into resolve (not verify_final) so a plugin can let the
    # session reach RESOLUTION — where its publish-directive fires — yet still
    # block the final resolve until its sub-condition is met.
    blockers = gates.blockers(state, "resolution") + plugins.plugin_gate_blockers(state, "resolution")
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty confirmer: --by must name who confirmed resolution"]
    quality = getattr(args, "quality", None)
    if quality is None:
        blockers = blockers + [
            "missing --quality: resolve requires a 1-5 rating (agent-proposed from the "
            "rubric with an eye on this task's in-flight signals, confirmed or adjusted "
            "by the user inside the same resolution AskUserQuestion — see the "
            "quality-regression-investigation runbook)"
        ]
    elif quality not in _VALID_QUALITY_RATINGS:
        blockers = blockers + [f"invalid --quality {quality!r}: must be an integer 1-5"]
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        return Directive(False, state.node, "fix_stages", "cannot resolve", data={"blockers": blockers})
    state.resolution = GateRecord("resolution", armed=True, passed=True, by=args.by)
    state.node = transition(state.node, "resolve")  # RESOLUTION -> RESOLVED
    cost_surface: dict = {}
    if state.cost is not None:
        cost_surface = {
            "total_cost_usd": state.cost.total_cost_usd,
            "total_duration_ms": state.cost.total_duration_ms,
            "spawn_count": state.cost.spawn_count,
            "attributed_stages": state.cost.attributed_stages,
            "note": state.cost.note,
        }
        state.log("cost", **{k: v for k, v in cost_surface.items() if k != "note"})
    quality_by = getattr(args, "quality_by", None) or "user-confirmed"
    quality_note = getattr(args, "quality_note", None)
    state.log("resolve", by=args.by, quality=quality, quality_by=quality_by)
    store.save(state)
    quality_row = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "task_id": state.task_id,
        "session": state.session_id,
        "quality": quality,
        "quality_by": quality_by,
        "quality_note": quality_note,
        "resolved_by": args.by,
        "instructions_head": _instructions_head(),
        "n_stages": len(state.stages),
        "n_failed_stage_results": sum(
            1 for h in state.history
            if h.get("event") == "record_result" and h.get("status") == "failed"
        ),
        "n_replans": sum(1 for h in state.history if h.get("event") == "replan"),
        "n_difficulty_records": sum(1 for h in state.history if h.get("event") == "declare"),
        "spawn_count": cost_surface.get("spawn_count", 0),
        "total_cost_usd": cost_surface.get("total_cost_usd"),
    }
    _write_quality_row(quality_row)
    detail = "task resolved"
    data = {"cost": cost_surface, "quality": quality_row}
    # Bypass visibility: the resolution summary surfaces every acceptance judge bypass
    # verbatim, so a resolved task's record shows which acceptance passes were unjudged
    # (kill switch) or overridden — never hidden behind a clean COMPLETED.
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f" (with {len(bypasses)} acceptance judge bypass(es); see judge_bypassed)"
    return Directive(True, state.node, "done", detail, marker="COMPLETED", data=data)


def cmd_reject(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The resolution gate's negative exit (#14): the user rejects the delivery as
    not matching intent. RESOLUTION previously exited ONLY via resolve, so a rejected
    delivery had no engine-tracked edge and stranded the session at the gate.

    reject re-opens the difficulty cycle: it seeds the difficulty record with the
    user's rejection reason AND marks the named stage(s) FAILED — default the final
    stage — so the subsequent replan always has concrete rework to route (a reject is
    never a structural no-op). It then hands off to overcome-difficulty exactly like
    a stage failure: declare -> investigate -> critique -> replan."""
    state = _require(store, args.session)
    if state.node != Node.RESOLUTION.value:
        return Directive(
            False, state.node, "noop",
            f"reject runs only at the resolution gate (node=RESOLUTION); node={state.node}",
        )
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        return Directive(
            False, state.node, "noop",
            "reject requires a non-empty --reason (the intent mismatch the user named)",
        )
    raw = getattr(args, "stage", None) or []
    if raw:
        targets: list[Stage] = []
        for idx in raw:
            try:
                targets.append(state.stage(int(idx)))
            except KeyError:
                return Directive(
                    False, state.node, "noop",
                    f"reject --stage {idx} does not exist in the plan",
                )
    elif state.stages:
        targets = [max(state.stages, key=lambda s: s.index)]  # default: the final stage
    else:
        return Directive(False, state.node, "noop", "reject has no stages to re-open")
    state.node = transition(state.node, "reject")  # RESOLUTION -> DIAGNOSING
    for s in targets:
        s.outcome.status = StageStatus.FAILED.value
    state.current_stage = None
    # Seed the difficulty record with the rejection so the reason is durably
    # captured; the coordinator refines it through declare -> investigate -> critique
    # (which the difficulty_blockers gate still requires complete before replan).
    state.difficulty = Difficulty(declaration=Declaration(
        expected=state.overall_done_criterion or "delivery matches the user's approved intent",
        actual=reason,
        mismatch="user rejected the delivery at the resolution gate (delivered != approved intent)",
    ))
    idxs = [s.index for s in targets]
    state.log("reject", reason=reason, stages=idxs)
    store.save(state)
    return Directive(
        False, state.node, "declare",
        f"delivery rejected: {reason}; stage(s) {idxs} re-opened as FAILED. Work the "
        "difficulty (declare -> investigate -> critique), then replan.",
        marker="OVERCOME-DIFFICULTY",
        data={"rejected_stages": idxs, "reason": reason},
    )


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
        # getattr-with-default: in-process Namespace callers (test_replan.py builds
        # one by hand with only the two required fields) must keep working.
        invariants_to_preserve=list(getattr(args, "invariants_to_preserve", None) or []),
        differences_to_remove=list(getattr(args, "differences_to_remove", None) or []),
    )
    state.log("critique")
    store.save(state)
    # Consult (never fire) the same gate cmd_replan enforces: the record now has all
    # three sections, but the gate also shape-checks them (>=2 distinct hypotheses,
    # non-placeholder declaration fields). Announcing "replan unblocked" without
    # reading the gate drifts the moment either side changes shape.
    blockers = gates.difficulty_blockers(state)
    if blockers:
        action = "investigate" if any("investigation" in b for b in blockers) else "declare"
        return Directive(False, state.node, action, "; ".join(blockers), data={"blockers": blockers})
    d = Directive(True, state.node, "replan",
                  "difficulty cycle complete; replan is now unblocked")
    inv = state.difficulty.investigation
    decl = state.difficulty.declaration
    _attach_advisories(d, "hypothesis_distinctness", {
        "hypotheses": inv.hypotheses if inv else [],
        "declaration": {"expected": decl.expected, "actual": decl.actual, "mismatch": decl.mismatch}
        if decl else {},
    }, runner, weight_class=state.weight_class)
    return d


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    from .plan import diff_plans, load_plan as _load, stage_carry_key

    # precondition: inside the DIAGNOSING cycle, the difficulty record must be
    # complete before a plan may be re-normed (variant (b) — internal command
    # precondition, not a tool-hook gate). [] outside DIAGNOSING.
    dblock = gates.difficulty_blockers(state)
    _log_gate(state, "difficulty_blockers", dblock, passed=not dblock)
    if dblock:
        return Directive(False, state.node, "declare", "replan blocked by incomplete difficulty record",
                         data={"blockers": dblock})

    if not state.plan_path:
        return Directive(False, state.node, "submit_plan", "no current plan to replan against")

    # plan-review gate: the corrected plan (args.plan) must carry a thinker review
    # with a passing/overridden verdict BOUND to it before it may be applied. Gates
    # EVERY replan kind (refinement and substantive alike, per the user decision),
    # inactive for non-substantive sessions. A genuine no-op replan (args.plan ==
    # the already-reviewed plan_path) passes on the existing review.
    prblock = gates.plan_review_blockers(state, args.plan)
    _log_gate(state, "plan_review", prblock, passed=not prblock)
    if prblock:
        return Directive(False, state.node, "plan_review",
                         "replan blocked: the corrected plan needs a thinker review "
                         "(run: plan-review --target " + args.plan + ")",
                         data={"blockers": prblock})
    # #8: diff against the plan AS APPROVED (the immutable snapshot), not plan_path —
    # which the coordinator may have edited in place. Absent a snapshot (legacy
    # session, or an approve that predates the field) fall back to plan_path.
    snap = state.plan_snapshot_path
    old_path = snap if (snap and Path(snap).exists()) else state.plan_path
    # OLD side is a read-only comparison baseline: plans approved before the
    # executor vocabulary existed (#7) may carry free-text executors and must
    # stay diffable — only the NEW side (and submit-plan) is strict.
    old = _load(old_path, strict_executor=False)
    new = _load(args.plan)

    # coverage gate: inside the difficulty flow, the corrected plan must CARRY the
    # critique's similarities into conditions/invariants and CHANGE a means/method
    # for the declared differences. Empty split -> [] -> behaves exactly as before.
    if state.difficulty and state.difficulty.critique:
        cov = gates.replan_coverage_blockers(old, new, state.difficulty.critique)
        _log_gate(state, "replan_coverage", cov, passed=not cov)
        if cov:
            waiver = getattr(args, "coverage_waiver", None)
            if waiver is None:
                return Directive(False, state.node, "declare", "replan blocked: critique coverage",
                                 data={"coverage_blockers": cov})
            if not waiver.strip():
                return Directive(False, state.node, "declare",
                                 "coverage waiver reason must not be empty",
                                 data={"coverage_blockers": cov})
            # a conscious, recorded bypass — only the coverage gate, never the
            # difficulty-record completeness precondition checked above.
            state.log("replan_coverage_waived", reason=waiver, blockers=list(cov))
            _log_gate(state, "replan_coverage_waiver", cov, passed=True)

    kind = diff_plans(old, new)

    # if we are exiting the DIAGNOSING cycle (difficulty complete), the failed
    # stage is re-armed and we leave the cycle back to VERIFYING so next_stage can
    # retry it; the difficulty record is cleared so a later failure starts fresh.
    diagnosing = state.node == Node.DIAGNOSING.value

    if kind == "no_change":
        # A legacy session with no approved-plan snapshot (plan_snapshot_path=None)
        # diffs plan_path against itself, so an in-place edit self-diffs to no_change
        # (issue #8, one branch deeper). Re-materialize each live stage's prose+verify
        # fields from the freshly-loaded plan BEFORE re-arming, or record-result runs
        # the STALE verify_command still held in state. Idempotent when the plan is
        # genuinely unchanged (copies identical values).
        for ns in new.stages:
            try:
                cur = state.stage(ns.index)
            except KeyError:
                continue
            _apply_refined_stage_fields(cur, ns)
        # final_check is meta-level (not per-stage), so it needs its own refresh
        # next to the stage loop above — a self-diffed no_change still means the
        # FILE changed relative to what was cached at submit-plan/last replan.
        state.final_check = new.meta.final_check
        # Backfill a snapshot for a legacy (pre-snapshot) session so the NEXT replan
        # diffs against real approved bytes instead of self-diffing plan_path.
        if not (state.plan_snapshot_path and Path(state.plan_snapshot_path).exists()):
            snap = _snapshot_approved_plan(store, state)
            if snap:
                state.plan_snapshot_path, state.plan_snapshot_hash = snap
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
        store.save(state)
        return Directive(True, state.node, "continue", "replan is a no-op; plan unchanged")

    if kind == "refinement":
        # apply prose refinements and re-arm any FAILED stage for another attempt
        for ns in new.stages:
            try:
                cur = state.stage(ns.index)
            except KeyError:
                continue
            # carry the corrected prose+means/method/conditions/invariants/verify into
            # state so a difficulty-driven refinement actually re-selects the means
            # (not just prose).
            _apply_refined_stage_fields(cur, ns)
            if cur.outcome.status == StageStatus.FAILED.value:
                cur.outcome.status = StageStatus.PENDING.value
        state.plan_path = args.plan
        state.repo_root = new.meta.repo_root
        state.final_check = new.meta.final_check
        if diagnosing:
            state.difficulty = None
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
        state.log("replan", kind="refinement", exited_diagnosing=diagnosing)
        store.save(state)
        if state.node == Node.VERIFYING.value and state.ready_stages():
            return Directive(True, state.node, "next_stage", "refinement applied; retry the ready stage")
        return Directive(True, state.node, "continue", "refinement applied; resume execution")

    # substantive: re-arm the plan-approval gate, reload stages, return to PLAN_READY.
    # #12: carry PASSED status forward for any stage whose FULL definition is
    # unchanged by the diff, so a substantive replan doesn't reset already-delivered
    # work to PENDING and force needless re-verification. Compare each new stage
    # against the LIVE stage (what actually ran) by the full-fidelity carry key; an
    # unchanged, previously-PASSED stage keeps its recorded Outcome intact.
    live_by_index = {s.index: s for s in state.stages}
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if (prev is not None
                and prev.outcome.status == StageStatus.PASSED.value
                and stage_carry_key(prev) == stage_carry_key(ns)):
            ns.outcome = prev.outcome
    state.stages = new.stages
    state.repo_root = new.meta.repo_root
    state.final_check = new.meta.final_check
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


# --- sub-plan stack: push_subplan / pop_subplan --------------------------------

def cmd_push_subplan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Start a service sub-plan: snapshot the parent into plan_stack, then reset
    the live state to a fresh child CLASSIFIED cycle. The child runs its normal
    classify->...->resolve spine; pop-subplan restores the parent on resolution."""
    state = _require(store, args.session)
    if state.node != Node.EXECUTING.value:
        return Directive(
            False, state.node, "noop",
            f"push-subplan requires node=EXECUTING; current node={state.node}",
        )
    originating = int(getattr(args, "originating_stage", None) or state.current_stage or 0)
    if not originating:
        return Directive(False, state.node, "noop", "cannot determine originating stage; pass --originating-stage")
    child_plan = args.plan
    child_task = getattr(args, "task", None) or f"sub:{Path(child_plan).stem}"

    frame = PlanFrame(
        plan_path=state.plan_path,
        node=state.node,
        task_id=state.task_id,
        goal=state.goal,
        overall_done_criterion=state.overall_done_criterion,
        overall_criterion_type=state.overall_criterion_type,
        weight_class=state.weight_class,
        route=state.route,
        repo_root=state.repo_root,
        final_check=list(state.final_check),
        partition=state.partition,
        approval=state.approval,
        resolution=state.resolution,
        stages=list(state.stages),
        current_stage=state.current_stage,
        originating_stage=originating,
    )
    state.plan_stack.append(frame)
    # Reset to a fresh child cycle — the child re-classifies and plans normally.
    state.node = transition(Node.EXECUTING.value, "push_subplan")  # EXECUTING -> CLASSIFIED
    state.task_id = child_task
    state.plan_path = child_plan
    state.plan_verified = False
    state.goal = ""
    state.overall_done_criterion = ""
    state.overall_criterion_type = CriterionType.MEASURABLE.value
    state.weight_class = None
    state.route = None
    state.repo_root = None
    state.final_check = []
    state.partition = None
    state.approval = GateRecord("plan_approval")
    state.resolution = GateRecord("resolution")
    state.stages = []
    state.current_stage = None
    state.difficulty = None
    state.permission_request = None
    state.blocked_from = None
    state.log("push_subplan", child_plan=child_plan, originating_stage=originating, depth=len(state.plan_stack))
    store.save(state)
    return Directive(
        True, state.node, "classify",
        f"sub-plan pushed (depth={len(state.plan_stack)}); child at CLASSIFIED — run classify next",
        data={"child_plan": child_plan, "originating_stage": originating,
              "parent_task": frame.task_id, "stack_depth": len(state.plan_stack)},
    )


def cmd_pop_subplan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Restore the parent after a service sub-plan resolves. Requires node=RESOLVED;
    the RESOLVED-frm structural guarantee enforces 'no auto-pop across an unresolved
    child' — check_invariants already mandates resolution.passed at RESOLVED."""
    state = _require(store, args.session)
    if not state.plan_stack:
        return Directive(False, state.node, "noop", "plan_stack is empty; nothing to pop")
    if state.node != Node.RESOLVED.value:
        return Directive(
            False, state.node, "noop",
            f"pop-subplan requires node=RESOLVED (child must fully resolve first); current node={state.node}",
        )
    child_task_id = state.task_id
    new_node = transition(state.node, "pop_subplan")  # RESOLVED -> EXECUTING
    frame = state.plan_stack.pop()
    # Restore all parent plan-level fields from the frame.
    state.plan_path = frame.plan_path
    state.task_id = frame.task_id
    state.goal = frame.goal
    state.overall_done_criterion = frame.overall_done_criterion
    state.overall_criterion_type = frame.overall_criterion_type
    state.weight_class = frame.weight_class
    state.route = frame.route
    state.repo_root = frame.repo_root
    state.final_check = frame.final_check
    state.partition = frame.partition
    state.approval = frame.approval
    state.resolution = frame.resolution
    state.stages = frame.stages
    state.node = new_node
    # Mark the originating stage as satisfied and clear the active-stage pointer.
    try:
        orig = state.stage(frame.originating_stage)
        orig.outcome.status = StageStatus.PASSED.value
        orig.control = f"satisfied by sub-plan {child_task_id}"
    except KeyError:
        pass
    state.current_stage = None
    state.log("pop_subplan", child_task_id=child_task_id, originating_stage=frame.originating_stage,
              depth=len(state.plan_stack))
    store.save(state)
    return Directive(
        True, state.node, "next_stage",
        f"sub-plan {child_task_id!r} resolved; parent restored at EXECUTING; "
        f"stage {frame.originating_stage} satisfied — run next-stage to continue",
        data={"originating_stage": frame.originating_stage, "child_task_id": child_task_id,
              "stack_depth": len(state.plan_stack)},
    )


# --- spine orchestrators: collapse the deterministic ceremony into one call -----
# `drive` (opening) and `close` (closing) are THIN orchestrators: they sequence the
# existing cmd_* functions and branch on the Directives those return. They add no
# Node, no machine edge, and no gate of their own — every state mutation is performed
# by a delegated cmd_*, so the engine's invariants hold by construction. Their ONE
# rule beyond sequencing: never auto-cross a human gate. `drive` stops at PLAN_READY
# unless given --approved-by; `close` stops at the resolution gate unless given
# --confirmed-by, and it surfaces resolution blockers (core + plugin-phase) by
# delegating to cmd_resolve, which already aggregates and refuses an empty --by.

def _run_step(fn, args, *, store: StateStore, runner: Runner | None, trace: list) -> Directive:
    """Call a cmd_* function, append a compact crumb to `trace`, return its Directive."""
    d = fn(args, store=store, runner=runner)
    trace.append({"command": fn.__name__.removeprefix("cmd_"), "node": d.node,
                  "action": d.action, "ok": d.ok})
    return d


def cmd_drive(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Walk the OPENING spine from the session's current node, firing only legal
    forward edges, and STOP at the plan-approval gate (PLAN_READY) by default.
    Cross it only when given --approved-by <who>, threaded into cmd_approve --by:
    the flag does not make approval implicit — it is the human token authorizing the
    wrapper to collapse the post-approval ceremony (approve -> partition -> next-stage
    -> EXECUTING) into the same call. Idempotent: re-running at/after EXECUTING is a
    no-op that reports the node."""
    state = _require(store, args.session)
    trace: list = []
    node = state.node

    # idempotency / guard: opening spine is done (or the session is parked elsewhere)
    if node in _EXECUTION_NODES:
        return Directive(True, node, "noop",
                         f"drive: session already at {node}; opening spine complete",
                         data={"trace": trace})
    if node in (Node.BLOCKED.value, Node.DIAGNOSING.value):
        return Directive(False, node, "noop",
                         f"drive: session at {node}; resolve that before driving",
                         data={"trace": trace})

    # --- classify (at CLASSIFIED) ---
    if node == Node.CLASSIFIED.value:
        d = _run_step(cmd_classify, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(d.ok, d.node, d.action, f"drive: classify failed: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node

    state = _require(store, args.session)
    wc = state.weight_class

    # --- route on weight class (at ROUTED) ---
    if node == Node.ROUTED.value:
        if wc == WeightClass.CHAT.value:
            return Directive(True, node, "answer_in_thread",
                             "drive: chat — answer in-thread (terminal at ROUTED)",
                             data={"trace": trace})
        if wc == WeightClass.SMALL_CHANGE.value:
            d = _run_step(cmd_next_stage, args, store=store, runner=runner, trace=trace)
            return Directive(d.ok, d.node, d.action,
                             f"drive: small change to EXECUTING — {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        # substantive: plan -> submit_plan
        d = _run_step(cmd_plan, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(d.ok, d.node, d.action, f"drive: plan failed: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node

    # --- submit plan (at PLANNING) ---
    if node == Node.PLANNING.value:
        if not getattr(args, "plan", None):
            return Directive(False, node, "fix_plan",
                             "drive: at PLANNING but no --plan provided",
                             data={"trace": trace})
        d = _run_step(cmd_submit_plan, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action,
                             f"drive: plan failed verification: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- the plan-approval GATE-STOP (at PLAN_READY) ---
    if node == Node.PLAN_READY.value:
        approver = getattr(args, "approved_by", None)
        if not (approver and approver.strip()):
            return Directive(True, node, "await_user_approval",
                             "drive: plan ready — HARD GATE; get explicit user approval, then "
                             "re-run drive with --approved-by <who>",
                             marker="PLAN-READY", data={"trace": trace})
        ap = argparse.Namespace(session=args.session, by=approver)
        d = _run_step(cmd_approve, ap, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"drive: approve failed: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- partition (at APPROVED) ---
    if node == Node.APPROVED.value:
        pa = argparse.Namespace(
            session=args.session,
            m1=getattr(args, "m1", False), m2=getattr(args, "m2", False),
            m3=getattr(args, "m3", False), m4=getattr(args, "m4", False),
            m3_severe=getattr(args, "m3_severe", False),
            m4_severe=getattr(args, "m4_severe", False),
        )
        d = _run_step(cmd_partition, pa, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"drive: partition failed: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node
        if d.action == "surface_partition":
            # a split is suggested — STOP for the user; do not auto-advance (not a gate,
            # but the M1–M4 verdict is cognition the wrapper must not paper over)
            return Directive(True, node, "surface_partition",
                             f"drive: {d.detail}",
                             data={**d.data, "trace": trace})

    # --- enter the first stage (at PARTITIONED) ---
    if node == Node.PARTITIONED.value:
        d = _run_step(cmd_next_stage, args, store=store, runner=runner, trace=trace)
        return Directive(d.ok, d.node, d.action,
                         f"drive: first stage active — {d.detail}",
                         marker=d.marker, data={**d.data, "trace": trace})

    return Directive(True, node, "inspect", f"drive: stopped at {node}", data={"trace": trace})


def cmd_close(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Walk the CLOSING spine and STOP at the resolution gate. record-result for the
    active stage (only with an explicit --status; a failed result routes to DIAGNOSING
    and is surfaced, never swallowed) -> verify-final (when all stages passed) -> a
    read-only PROBE of cmd_resolve. With no --confirmed-by the probe leaves the session
    at RESOLUTION and close reports what still blocks resolve (core + experience-plugin-
    phase blockers); with --confirmed-by it resolves. Idempotent at RESOLVED.

    Note: plugin OBSERVER nudges (PluginDirectives, e.g. the experience plugin's
    record_experience nudge) are not emitted through this path — they require the
    main()/_fire_plugins wiring. Resolution gates still hold, because cmd_resolve reads
    plugin_gate_blockers directly; only the advisory nudge directives are silent here."""
    state = _require(store, args.session)
    trace: list = []
    node = state.node

    if node == Node.RESOLVED.value:
        return Directive(True, node, "noop", "close: already RESOLVED", data={"trace": trace})
    if node not in (Node.EXECUTING.value, Node.VERIFYING.value, Node.RESOLUTION.value):
        return Directive(False, node, "noop",
                         f"close: nothing to close yet (node={node}); drive to EXECUTING first",
                         data={"trace": trace})

    # --- record the active stage's result (only with an explicit status) ---
    if node == Node.EXECUTING.value:
        status = getattr(args, "status", None)
        if not status:
            return Directive(False, node, "record_result",
                             "close: stage is EXECUTING — supply --status passed|failed "
                             "(and --control for a spawn:developer stage)",
                             data={"trace": trace})
        rr = argparse.Namespace(
            session=args.session, status=status,
            actual=getattr(args, "actual", "") or "",
            control=getattr(args, "control", None),
            observation=getattr(args, "observation", "") or "",
        )
        d = _run_step(cmd_record_result, rr, store=store, runner=runner, trace=trace)
        if not d.ok:
            # failed result -> DIAGNOSING (overcome-difficulty), or attest_control needed
            return Directive(False, d.node, d.action, f"close: {d.detail}",
                             marker=d.marker, data={**d.data, "trace": trace})
        node = d.node
        if d.action == "next_stage":
            return Directive(True, node, "next_stage",
                             "close: stage recorded; more stages remain — execute them, then "
                             "close again (close does not auto-run remaining stages)",
                             data={"trace": trace})

    # --- final verification (at VERIFYING, all stages passed) ---
    if node == Node.VERIFYING.value:
        d = _run_step(cmd_verify_final, args, store=store, runner=runner, trace=trace)
        if not d.ok:
            return Directive(False, d.node, d.action, f"close: {d.detail}",
                             data={**d.data, "trace": trace})
        node = d.node

    # --- resolution GATE-STOP: probe cmd_resolve (constraints 1 + 3) ---
    if node == Node.RESOLUTION.value:
        confirmer = getattr(args, "confirmed_by", None)
        rs = argparse.Namespace(session=args.session, by=(confirmer or ""),
                                quality=getattr(args, "quality", None),
                                quality_by=getattr(args, "quality_by", None),
                                quality_note=getattr(args, "quality_note", None))
        d = _run_step(cmd_resolve, rs, store=store, runner=runner, trace=trace)
        if d.ok:
            return Directive(True, d.node, d.action, "close: task resolved",
                             marker=d.marker, data={"trace": trace})
        # blocked: separate the gate-stop sentinels (confirmer + rating, both
        # supplied by the confirmed re-run itself) from real blockers
        blockers = d.data.get("blockers", [])
        real = [b for b in blockers
                if "empty confirmer" not in b and "missing --quality" not in b]
        if real:
            detail = ("close: confirmer given but resolution still blocked"
                      if confirmer and confirmer.strip() else "close: resolution blocked")
            return Directive(False, node, "fix_stages", detail,
                             data={"blockers": real, "trace": trace})
        return Directive(True, node, "await_user_confirmation",
                         "close: ready to resolve — get explicit user confirmation, then "
                         "re-run close with --confirmed-by <who> --quality <1-5>",
                         data={"trace": trace})

    return Directive(True, node, "inspect", f"close: stopped at {node}", data={"trace": trace})


COMMANDS = {
    "start": cmd_start,
    "reset": cmd_reset,
    "plugin-activate": cmd_plugin_activate,
    "plugin-deactivate": cmd_plugin_deactivate,
    "plugin-record": cmd_plugin_record,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "submit-plan": cmd_submit_plan,
    "plan-review": cmd_plan_review,
    "stage-review": cmd_stage_review,
    "approve": cmd_approve,
    "partition": cmd_partition,
    "partition-units": cmd_partition_units,
    "next-stage": cmd_next_stage,
    "dispatch": cmd_dispatch,
    "resolve-permission": cmd_resolve_permission,
    "record-result": cmd_record_result,
    "declare": cmd_declare,
    "investigate": cmd_investigate,
    "critique": cmd_critique,
    "verify-final": cmd_verify_final,
    "resolve": cmd_resolve,
    "reject": cmd_reject,
    "replan": cmd_replan,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "status": cmd_status,
    "drive": cmd_drive,
    "close": cmd_close,
    "push-subplan": cmd_push_subplan,
    "pop-subplan": cmd_pop_subplan,
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
    sp = add("plan-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.PLAN_REVIEW_VERDICTS), required=True,
                    help="pass = clears the gate; revise = blocks; override = user's "
                         "explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (the user, for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a blocking concern the thinker raised (repeatable; audit trail)")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--target", default=None,
                    help="plan file reviewed (defaults to the session's current plan_path; "
                         "pass the NEW plan for a replan-time review)")
    sp = add("stage-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.STAGE_REVIEW_VERDICTS), required=True,
                    help="pass = clears the acceptance gate; revise = blocks; override = "
                         "user's explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (the user, for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a blocking concern the reviewer raised (repeatable; audit trail)")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--observation", default=None,
                    help="the observation being reviewed (defaults to the stage's current "
                         "observation); binds the verdict to these exact bytes")
    sp = add("approve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    _UNIT_HELP = ("delivery unit as '<mode>|<stages csv>|<title>[|<ref>]' "
                  "(mode: inline|spawn|subtask); repeatable")
    sp = add("partition"); sp.add_argument("--session", required=True)
    sp.add_argument("--m1", action="store_true"); sp.add_argument("--m2", action="store_true")
    sp.add_argument("--m3", action="store_true"); sp.add_argument("--m4", action="store_true")
    sp.add_argument("--m3-severe", dest="m3_severe", action="store_true")
    sp.add_argument("--m4-severe", dest="m4_severe", action="store_true")
    sp.add_argument("--unit", dest="unit", action="append", default=None, help=_UNIT_HELP)
    sp = add("partition-units"); sp.add_argument("--session", required=True)
    sp.add_argument("--unit", dest="unit", action="append", default=None, help=_UNIT_HELP)
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
    sp.add_argument("--observation", default="",
                    help="for acceptance_review stages: what you actually observed "
                         "(required when recording passed; must differ from the expected image)")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
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
    sp.add_argument("--invariant-to-preserve", dest="invariants_to_preserve",
                    action="append", default=None,
                    help="a similarity the corrected plan must PRESERVE as a condition/"
                         "invariant (repeatable); the engine verifies coverage on replan")
    sp.add_argument("--difference-to-remove", dest="differences_to_remove",
                    action="append", default=None,
                    help="a difference whose removal requires a CHANGED means/method "
                         "(repeatable); the engine verifies a means/method changed on replan")
    sp = add("verify-final"); sp.add_argument("--session", required=True)
    sp = add("resolve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--quality", type=int, choices=list(_VALID_QUALITY_RATINGS), default=None,
                    help="1-5 rating, agent-proposed and user-confirmed/adjusted in the "
                         "resolution AskUserQuestion; refused if absent")
    sp.add_argument("--quality-by", dest="quality_by", default="user-confirmed",
                    help="'user-confirmed' (default), 'user-adjusted', or 'user-other' "
                         "(free-text answer)")
    sp.add_argument("--quality-note", dest="quality_note", default=None)
    sp = add("reject"); sp.add_argument("--session", required=True)
    sp.add_argument("--reason", required=True,
                    help="the intent mismatch the user named when rejecting the delivery "
                         "(seeds the difficulty record)")
    sp.add_argument("--stage", dest="stage", action="append", default=None, type=int,
                    help="plan stage index to re-open as FAILED (repeatable; "
                         "defaults to the final stage so a reject is never a no-op)")
    sp = add("replan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp.add_argument("--coverage-waiver", dest="coverage_waiver", default=None,
                    help="bypass a failing coverage gate with a recorded reason (refused if empty); "
                         "never bypasses the difficulty-record completeness precondition")
    sp = add("block"); sp.add_argument("--session", required=True); sp.add_argument("--reason", default="")
    sp = add("unblock"); sp.add_argument("--session", required=True)
    sp = add("status"); sp.add_argument("--session", required=False)

    # drive: opening-spine orchestrator — union of classify signals + --plan +
    # --approved-by (the gate-cross token) + the M1–M4 partition markers.
    sp = add("drive"); sp.add_argument("--session", required=True)
    sp.add_argument("--chat", action="store_true")
    sp.add_argument("--changed-lines", dest="changed_lines", type=int, default=0)
    sp.add_argument("--files", type=int, default=1)
    sp.add_argument("--wall-clock-min", dest="wall_clock_min", type=int, default=0)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp.add_argument("--architectural", action="store_true")
    sp.add_argument("--external-effect", dest="external_effect", action="store_true")
    sp.add_argument("--new-dependency", dest="new_dependency", action="store_true")
    sp.add_argument("--public-api-change", dest="public_api_change", action="store_true")
    sp.add_argument("--plan", default=None)
    sp.add_argument("--approved-by", dest="approved_by", default=None,
                    help="human token authorizing the wrapper to cross the plan-approval "
                         "gate; pass ONLY after a real user-approval round")
    sp.add_argument("--m1", action="store_true"); sp.add_argument("--m2", action="store_true")
    sp.add_argument("--m3", action="store_true"); sp.add_argument("--m4", action="store_true")
    sp.add_argument("--m3-severe", dest="m3_severe", action="store_true")
    sp.add_argument("--m4-severe", dest="m4_severe", action="store_true")

    # close: closing-spine orchestrator — record-result inputs + --confirmed-by
    # (the resolution-gate-cross token).
    sp = add("close"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], default=None)
    sp.add_argument("--actual", default="")
    sp.add_argument("--control", default=None)
    sp.add_argument("--observation", default="",
                    help="for acceptance_review stages: what you actually observed "
                         "(threaded to record-result)")
    sp.add_argument("--confirmed-by", dest="confirmed_by", default=None,
                    help="human token authorizing the wrapper to cross the resolution "
                         "gate; pass ONLY after explicit user confirmation")
    sp.add_argument("--quality", type=int, choices=list(_VALID_QUALITY_RATINGS), default=None,
                    help="1-5 rating threaded to resolve (see resolve --quality)")
    sp.add_argument("--quality-by", dest="quality_by", default="user-confirmed")
    sp.add_argument("--quality-note", dest="quality_note", default=None)

    sp = add("push-subplan"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True, help="path to the child service sub-plan TOML")
    sp.add_argument("--task", default=None, help="task_id for the child (defaults to sub:<plan-stem>)")
    sp.add_argument("--originating-stage", dest="originating_stage", type=int, default=None,
                    help="parent stage whose missing element the sub-plan supplies "
                         "(defaults to state.current_stage)")

    sp = add("pop-subplan"); sp.add_argument("--session", required=True)
    return p


def _inject_default_session(argv: list[str], harness: str | None) -> list[str]:
    """Return a copy of ``argv`` with ``--session <harness>`` appended when the
    harness session id is known and the caller passed no --session of its own.

    hook-state-gate.py authorizes production edits by the HARNESS conversation
    session_id (payload["session_id"] == $CLAUDE_CODE_SESSION_ID). A self-chosen
    --session silently drives a different engine state file than the gate reads,
    so an omitted --session must default to the harness id — not stay unset and
    fail the 30 required=True subcommands. Appending places the flag inside the
    subparser's argument region (--session is a subcommand option); it is a
    no-op when --session (either '--session X' or '--session=X') is already
    present, or when the harness id is empty/None."""
    if not harness:
        return list(argv)
    for tok in argv:
        if tok == "--session" or tok.startswith("--session="):
            return list(argv)
    return list(argv) + ["--session", harness]


def main(argv: list[str] | None = None) -> int:
    harness = os.environ.get("CLAUDE_CODE_SESSION_ID")
    raw = _inject_default_session(
        list(sys.argv[1:] if argv is None else argv), harness
    )
    args = build_parser().parse_args(raw)
    if harness and getattr(args, "session", None) and args.session != harness:
        print(
            f"agentctl: warning: --session {args.session!r} differs from "
            f"CLAUDE_CODE_SESSION_ID {harness!r}; the production-edit gate "
            f"authorizes by the harness id, so gated edits may be denied.",
            file=sys.stderr,
        )
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
