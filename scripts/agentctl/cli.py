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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import proc_tree
from lib import argv_text, config_root, kind_baselines, transcript_stops, widening_targets

from . import advisor, continuations, controls, cost, delivery, effort, enumerate_sidecar, exempt_paths, gates, grant_shadow, grants as _grants, ledger, order_approvals, permissions, plan_resources, plugins, plugins_ledger, plugins_premise, premise, resources as _resources, runtime_host, solved_marker, task_accumulator
from .checkrun import NEGATIVE_CONTROL_REFUSED_EXIT_CODES, format_observations, observe_stage_checks
from .classify import TRACKER_KEY_RE, Signals, classify
from .config import Thresholds
from .partition import render_section, render_units, verdict
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import (
    CHILD_EXHAUSTED,
    CHILD_INFRA_FAILURE,
    Runner,
    dispatch_stage,
    parse_marker,
    subprocess_runner,
)
from .machine import transition
from .plan import (
    CONDITION_MARKERS,
    META_PART,
    ConcernFormatError,
    PlanDoc,
    PlanError,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    changed_parts,
    check_venue_warnings,
    grants_sha256,
    load_plan,
    load_plan_with_digest,
    order_digest,
    pair_binding,
    pair_part_tokens,
    parse_concern,
    parse_pair,
    part_digest_map,
    plan_has_any_grants,
    plan_meta_digest,
    plan_meta_element_key,
    plan_meta_element_keys,
    plan_stage_digests,
    review_pairs,
    stage_element_keys,
    stage_part,
    stage_question_key,
    stage_reattest_digest,
    verify_command_reachability_blockers,
    verify_command_scope_warnings,
    _venue_for,
)
from .text_shape import WHOLE_STAGE_ELEMENT
from .render import (
    cmd_plan_grants,
    cmd_plan_render,
    plan_render_stage_arg_type,
    render_plan_grants,
    render_plan_md,
    render_stages_md,
)
from .submission import submission_advice, submission_violations
from .state import (
    _EXECUTION_NODES,
    _MAX_PLAN_STACK,
    Actor,
    AcceptanceBypass,
    AcceptanceReview,
    AGENT_ACTOR,
    AUTHORIZE_REPLAN_MARKER,
    CheckKind,
    CheckVenue,
    CodeReview,
    CONCERN_FIXED,
    CONCERN_OPEN,
    CONCERN_RECORDED,
    CONCERN_SUPERSEDED,
    ConcernRecord,
    Critique,
    Criterion,
    CriterionType,
    Declaration,
    Difficulty,
    FAILURE_ADDRESS_VALUES,
    FinalCheck,
    Investigation,
    JudgeBypass,
    LANDED_GIT_ERROR_EXIT,
    Partition,
    PartitionUnit,
    PARTITION_UNIT_MODES,
    GateRecord,
    Means,
    Node,
    Normalization,
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
    Outcome,
    PermissionRequest,
    PLAN_PRESENTATION_KIND_ESSENCE,
    PLAN_PRESENTATION_KIND_FULL,
    PLAN_PRESENTATION_KIND_REPLAN_DIFF,
    PLAN_PRESENTATION_KINDS,
    PLAN_PRESENTATION_RENDERING_CAP_BYTES,
    PlanFrame,
    PlanPairReview,
    PlanPresentation,
    PlanReview,
    plan_review_concern_ids,
    plan_review_pair_scope,
    plan_review_scope_for_pair,
    plan_review_scope_for_stage,
    plan_review_scope_stage_index,
    ReattestStash,
    RequirementVerdict,
    RiskAcceptance,
    Route,
    SessionState,
    SHOW_FULL_PLAN_MARKER,
    Stage,
    StageReview,
    StageStatus,
    Subject,
    WeightClass,
)
from .store import FileStateStore, StateStore, safe_session_id as _safe_session_id

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE_LOG = config_root.agentctl_gate_log()
# Per-task quality ledger (quality-regression-tracking): one row per resolved
# task, stamped with the instructions-repo HEAD so a quality drop can be
# correlated back to an instruction-commit range. Same fixed-path/append-only
# idiom as ~/.local/log/claude-spawn-costs.jsonl (spawn-specialist.py).
TASK_QUALITY_LOG = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"
ESCALATION_LEDGER_ENV = "AGENTCTL_ESCALATION_LEDGER"
DEFAULT_ESCALATION_LEDGER = Path.home() / ".local" / "log" / "claude-plan-review-escalations.jsonl"
_GIT_HEAD_TIMEOUT_S = 5
_VALID_QUALITY_RATINGS = (1, 2, 3, 4, 5)

# The required observation shape, stated once and referenced everywhere an
# observation is authored or its rejection explained (GitHub issue #95):
# a rubric's success form must live at the authoring point, not only at the
# refusal point. Kept short and length-bounded because the acceptance-judge
# leaf's empirical finding is that long, cumulative observations are what
# drives the fail-open rate up, not just the revise rate.
OBSERVATION_CONTRACT = (
    "attest in the present tense what you observed: name the artifact "
    "(file, command, output) and state what reading it showed. Do not "
    "narrate what had been wrong or how it was fixed — a defect history is "
    "not an observation. Keep it short and targeted (~400-500 chars); a "
    "long cumulative observation makes the judge both more likely to move "
    "the goalposts and more likely to time out."
)


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _bash_rule_command_from_call(call: tuple[str, dict] | None) -> str | None:
    """The parsed Bash rule's command text from a self-reported
    `Rule: Bash(<command>:*)` call, or `None` for a non-Bash call (a
    transcript denial row is Bash-only — see `_classify_transcript_
    denials` — so a non-Bash call can never match one). Used to match a
    self-reported PERMISSION-REQUEST against the specific transcript row
    for the SAME denial, by checking that the row's own stored raw
    command starts with this rule text — mirroring `_segment_covered`'s
    wildcard-prefix semantics. Deliberately a PREFIX check, not a digest
    equality check: the rule a child names is typically a narrower
    stand-in for the actual invoked command (extra redirects, arguments a
    `:*` wildcard was meant to admit), so a byte-identical match between
    the rule's command and the transcript's raw command would almost
    never hold even for the correct row."""
    if call is None or call[0] != "Bash":
        return None
    command = call[1].get("command") if isinstance(call[1], dict) else None
    return command if isinstance(command, str) and command.strip() else None


def _plan_file_sha256(target: str | None) -> str:
    """sha256 of a plan file's bytes, or '' when there is no readable file (#16).

    Best-effort by design: an unreadable/absent target yields '' so the plan-review
    gate degrades to path-only binding rather than wedging on a transient I/O error.
    That degradation is the NOTHING-ATTESTED case only — cmd_plan_review turns the
    empty result into a refusal when the caller did supply a --plan-digest, since a
    digest it cannot compare is not evidence the reviewer read anything.
    cmd_plan_review records this over the reviewed bytes; gates.plan_review_blockers
    inlines the same sha256-of-bytes recompute (it cannot import cli — circular)."""
    if not target:
        return ""
    try:
        return hashlib.sha256(Path(target).read_bytes()).hexdigest()
    except OSError:
        return ""


def _plan_file_bytes(target: str | None) -> int | None:
    """Byte size of a plan file, or None when there is no readable file.

    Mirrors _plan_file_sha256's best-effort contract. cmd_plan_review logs this on
    the plan_review history event so a round's fix size is computable as a byte
    delta between consecutive events of the same family, without re-reading the
    plan archive after the fact."""
    if not target:
        return None
    try:
        return Path(target).stat().st_size
    except OSError:
        return None


def _observation_sha256(observation: str) -> str:
    """sha256 of an acceptance observation's bytes — the binding key the acceptance
    gate recomputes over the observation being recorded. Mirrors _plan_file_sha256 but
    over an in-memory string (the observation is never a file)."""
    return hashlib.sha256((observation or "").encode("utf-8")).hexdigest()


# `--reviewer` is free text (30+ observed spellings of "thinker" alone), which makes a
# reviewer-by-role query over history unanswerable without re-parsing every event by
# hand. D9 capture: a best-effort canonical token alongside the untouched raw string —
# never REPLACING it, since a normalization that destroys its input cannot be
# re-derived when the mapping below turns out wrong (e.g. a new role appears).
_REVIEWER_CANONICAL_TOKENS = (
    "thinker", "developer", "code-reviewer", "planner", "tech-writer", "coordinator", "user",
)


def _normalize_reviewer_token(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return ""
    for token in _REVIEWER_CANONICAL_TOKENS:
        if token in s:
            return token
    return "other"


_AUTOMATED_JUDGE_REVIEWERS = (advisor.JUDGE_REVIEWER, "judge:haiku")


def _record_stage_review(state: SessionState, review: StageReview, *, from_judge: bool) -> None:
    """Store a StageReview, one per stage_index (last-wins). A judge verdict
    (from_judge=True) NEVER clobbers a human/manual review already present for the
    stage (e.g. an override): the automated cognition must not silently overwrite the
    user's explicit escape. A manual record (from_judge=False, via cmd_stage_review)
    always replaces."""
    existing = [r for r in state.stage_reviews if r.stage_index == review.stage_index]
    # Not a bare `!= advisor.JUDGE_REVIEWER`: persisted reviews keep the tag they were
    # written under, so a judge-tier rename would otherwise freeze old automated verdicts as "human".
    if from_judge and existing and any(r.reviewer not in _AUTOMATED_JUDGE_REVIEWERS for r in existing):
        return
    state.stage_reviews = [r for r in state.stage_reviews if r.stage_index != review.stage_index]
    state.stage_reviews.append(review)


def _record_code_review(state: SessionState, review: CodeReview) -> None:
    """Store a CodeReview, one per stage_index (last-wins). Unlike
    _record_stage_review there is no automated judge path to protect a human
    override from — every code-review record is code-reviewer/human-authored via
    cmd_code_review, so a later record for the same stage always replaces the
    prior one."""
    state.code_reviews = [r for r in state.code_reviews if r.stage_index != review.stage_index]
    state.code_reviews.append(review)


def _judge_bypassed_surface(state: SessionState) -> list[dict]:
    """The recorded acceptance-judge bypasses as plain dicts, for verify-final and the
    resolution summary to surface verbatim ([] when none)."""
    return [
        {"stage_index": b.stage_index, "kind": b.kind, "reviewer": b.reviewer, "note": b.note}
        for b in state.judge_bypassed
    ]


def _acceptance_bypass_surface(state: SessionState) -> dict | None:
    """The recorded plan-level acceptance bypass as a plain dict, for verify-final and
    the resolution summary to surface verbatim (None when no bypass is in force).
    Mirrors _judge_bypassed_surface's role but for the singular AcceptanceBypass —
    see AcceptanceBypass's docstring for why resolution_blockers itself never reads
    this field."""
    b = state.acceptance_bypass
    if b is None:
        return None
    return {"reason": b.reason, "reviewer": b.reviewer, "note": b.note}


def _record_bypass(state: SessionState, bypass: JudgeBypass) -> None:
    """Append a JudgeBypass (never cleared by a later passing review) so verify-final
    and the resolution summary can surface every acceptance pass that skipped a genuine
    judge verdict. Deduplicated on (stage_index, kind, observation_sha256) — the third
    key matters only for kind="fail_open" (override/killswitch always carry "" and so
    dedupe exactly as before): a fail_open bypass is scoped to the OBSERVATION it waved
    through (see gates.acceptance_review_blockers), so a second fail-open on a
    DIFFERENT observation for the same stage records its own entry rather than being
    silently absorbed by the first."""
    if any(
        b.stage_index == bypass.stage_index and b.kind == bypass.kind
        and b.observation_sha256 == bypass.observation_sha256
        for b in state.judge_bypassed
    ):
        return
    state.judge_bypassed.append(bypass)


def _write_plan_snapshot(store: StateStore, state: SessionState, data: bytes, digest: str) -> tuple[str, str] | None:
    """Write an ALREADY-READ plan buffer to the session's snapshot dir and return
    (snapshot_path, hash) -- the write-only half of `_snapshot_approved_plan`,
    factored out so a caller that already holds `(data, digest)` from a single
    read (`load_plan_with_digest`) never has to re-read the file just to take its
    approved-plan snapshot. Content-hash-named so identical plans share one file.

    Best-effort like its caller: None when the store exposes no on-disk path."""
    path_fn = getattr(store, "path", None)
    if path_fn is None:
        return None
    snap = path_fn(state.session_id).parent / f"plan-approved-{digest[:16]}.toml"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(data)
    return str(snap), digest


def _write_plan_version_snapshot(store: StateStore, state: SessionState, data: bytes, digest: str) -> str | None:
    """Content-address EVERY submitted plan version (submit_plan and every replan
    kind), not just the approved ones `_write_plan_snapshot` covers — every
    `plan_sha256`/`prev_plan_sha256` logged on a history event must resolve to an
    actual file on disk, including a rejected or refinement/no_change submission the
    approved-plan snapshot never touches.

    A distinct filename prefix (`plan-version-` vs `plan-approved-`) so this never
    collides with, or is mistaken for, the approved-plan snapshot machinery above —
    that snapshot's own name and readers stay untouched. Content-hash-named so an
    identical resubmission shares one file. Best-effort like its sibling: None when
    the store exposes no on-disk path."""
    path_fn = getattr(store, "path", None)
    if path_fn is None:
        return None
    snap = path_fn(state.session_id).parent / f"plan-version-{digest[:16]}.toml"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(data)
    return str(snap)


def _snapshot_plan_version(store: StateStore, state: SessionState, plan_path: str) -> str | None:
    """Read `plan_path`'s current bytes, content-address them via
    `_write_plan_version_snapshot`, and return the sha256 digest -- the read+write pair
    `submit_plan` and every `replan` kind call so EVERY submitted plan version (not just
    approved ones) resolves to an actual file on disk. Best-effort: None on an unreadable
    file, leaving the caller to log the event without a digest rather than raise out of a
    command that has already decided to accept/apply these bytes."""
    try:
        data = Path(plan_path).read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    _write_plan_version_snapshot(store, state, data, digest)
    return digest


def _snapshot_approved_plan(store: StateStore, state: SessionState) -> tuple[str, str] | None:
    """Copy the plan AS APPROVED into the state dir and return (snapshot_path, hash).

    #8: cmd_replan must diff the corrected plan against what was APPROVED — not
    against state.plan_path, which the coordinator may edit in place (an in-place
    edit would else self-diff to no_change and silently drop the correction). Taken
    at every approve so a substantive-replan re-approval refreshes the baseline.

    Best-effort: returns None (leaving cmd_replan to fall back to plan_path, the
    prior behaviour) when there is no plan, the store exposes no on-disk path, or
    the plan file is unreadable. Reads state.plan_path itself -- callers that
    already hold the bytes from a single upstream read (`load_plan_with_digest`)
    should call `_write_plan_snapshot` directly instead, to avoid a second read of
    a file that could have changed between the two."""
    if not state.plan_path:
        return None
    src = Path(state.plan_path)
    if not src.exists():
        return None
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return _write_plan_snapshot(store, state, data, digest)


def _refresh_approved_grant_snapshot(
    state: SessionState, store: StateStore, doc, data: bytes, digest: str,
) -> None:
    """Re-snapshot the just-applied plan bytes and rebind `approved_grants_sha256` to
    them, mirroring `cmd_approve`'s own snapshot+hash binding (see its "Bind the
    approved grant set" comment) -- called from the `no_change` and `refinement`
    branches of `cmd_replan`, which apply corrected bytes to an ALREADY-APPROVED
    session in place, without requiring a fresh `approve`. Without this, those two
    branches update `state.plan_path` but leave `plan_snapshot_path`/
    `plan_snapshot_hash`/`approved_grants_sha256` pointed at the PRIOR approval's
    bytes, so `_stage_grant_entries` either hash-mismatches on a corrected snapshot
    that no longer matches the stale stamped hash, or silently keeps re-deriving
    grants from plan content the session no longer runs.

    `doc`, `data` and `digest` are the ONE read `cmd_replan` took of `args.plan`
    (`load_plan_with_digest`) -- passed through rather than re-read here, so this
    function can never bind a snapshot to bytes different from the ones `doc` was
    parsed from or `state.accepted_plan_digest` was stamped from. Belt and braces:
    refuses to rebind (raising, rather than silently keeping the stale snapshot)
    when `digest` does not match `state.accepted_plan_digest` -- the caller must
    always stamp that field from this SAME buffer first (both `cmd_replan`
    branches do, immediately before calling this)."""
    if digest != state.accepted_plan_digest:
        raise ValueError(
            "refusing to rebind the approved-grant snapshot: the plan bytes' "
            f"digest {digest!r} does not match state.accepted_plan_digest "
            f"{state.accepted_plan_digest!r} -- stamp accepted_plan_digest from "
            "this same buffer before refreshing the snapshot"
        )
    snap = _write_plan_snapshot(store, state, data, digest)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    state.approved_grants_sha256 = grants_sha256(doc)


def _replan_baseline_path(state: SessionState) -> str | None:
    """The comparison baseline every replan-family diff is taken against: the
    approved-plan snapshot when one exists on disk, else state.plan_path (the
    legacy, pre-snapshot fallback — see _snapshot_approved_plan). Extracted from
    the three call sites that repeated this derivation (cmd_replan,
    _renormalize_replan, cmd_check_coverage) so they cannot drift apart; carries
    ONLY the derivation, not _renormalize_replan's snapshot backfill, which that
    path performs deliberately and the other two do not."""
    snap = state.plan_snapshot_path
    return snap if (snap and Path(snap).exists()) else state.plan_path


_ORDER_EFFORT_SCALES = (
    (effort.SCALE_SPEND, effort.ORDER_SPEND_KEY),
    (effort.SCALE_WALL_CLOCK, effort.ORDER_WALL_CLOCK_KEY),
)


def _ledgered_order_key(state: SessionState) -> str | None:
    """The order-approvals ledger key of this session's order, iff the order has a
    user-approved version there — the condition every order-keyed effort and
    autonomy path is gated on. None for an order-less or never-user-approved plan,
    which keeps today's per-session behavior."""
    path = _replan_baseline_path(state) if state.plan_path else None
    if not path:
        return None
    try:
        doc = load_plan(path, strict=False)
    except (OSError, PlanError):
        return None
    key = order_digest(doc)
    return key if order_approvals.latest_user_approved_record(key) is not None else None


def _effort_cross_totals(state: SessionState) -> dict:
    """`task_accumulator`'s per_axis_totals, plus — for a ledgered order — the order's
    spend/wall-clock total since the last user approval (ledger window + this
    session's unflushed delta, minus the total at this session's last fire), under the
    keys `effort.effective_deltas` reads."""
    totals = dict(task_accumulator.get(state.task_id)["per_axis_totals"])
    key = _ledgered_order_key(state)
    if key is None:
        return totals
    window = order_approvals.get(key)["effort_since_user_approval"]
    flushed = state.order_effort_flushed or {}
    actual = effort.actual(state)
    base = state.order_effort_base or {}
    for scale, total_key in _ORDER_EFFORT_SCALES:
        unflushed = max(0.0, actual[scale] - float(flushed.get(scale, actual[scale])))
        total = float(window.get(scale) or 0.0) + unflushed
        floor = float(base.get(scale) or 0.0)
        # The floor is subtracted only while the window has not been reset below it (a fresh user approval).
        totals[total_key] = total - (floor if total >= floor else 0.0)
    return totals


def _flush_order_effort(state: SessionState, key: str | None = None) -> None:
    """Move this session's unflushed spend/wall-clock into its order's ledger window."""
    key = key or _ledgered_order_key(state)
    if key is None:
        return
    actual = effort.actual(state)
    flushed = state.order_effort_flushed
    if flushed is not None:
        spend = max(0.0, actual[effort.SCALE_SPEND] - float(flushed.get(effort.SCALE_SPEND) or 0.0))
        wall = max(0.0, actual[effort.SCALE_WALL_CLOCK] - float(flushed.get(effort.SCALE_WALL_CLOCK) or 0.0))
        if spend or wall:
            order_approvals.flush_effort(key, spend_delta=spend, wall_clock_delta=wall)
    state.order_effort_flushed = {
        effort.SCALE_SPEND: actual[effort.SCALE_SPEND],
        effort.SCALE_WALL_CLOCK: actual[effort.SCALE_WALL_CLOCK],
    }


def _order_customer_id(state: SessionState) -> str:
    path = _replan_baseline_path(state) if state.plan_path else None
    if not path:
        return ""
    try:
        order = load_plan(path, strict=False).meta.order
    except (OSError, PlanError):
        return ""
    return order.customer_id if order is not None else ""


def _clear_open_fires_if_customer(state: SessionState, by: str) -> None:
    """A customer-authored acknowledgement is the only act that discharges the open
    user-owed fire marker of the order (an agent-authored one never does)."""
    key = _ledgered_order_key(state)
    customer_id = _order_customer_id(state)
    if key is not None and customer_id and by.strip().casefold() == customer_id.casefold():
        order_approvals.clear_effort_fires(key)


def _open_effort_fire(state: SessionState) -> list[dict]:
    """The open spend/wall-clock fire markers owed to the user for this session's order."""
    key = _ledgered_order_key(state)
    return list(order_approvals.get(key)["open_effort_fires"]) if key else []


def _record_effort_fire(state: SessionState, div: "effort.Divergence", *, now: float) -> dict:
    """`effort.record_fire` plus, on a spend/wall-clock fire of a ledgered order, the
    open user-owed marker and the order-level baseline rebase."""
    fire = effort.record_fire(state, div, now=now)
    fire["difficulty_id_at_fire"] = _difficulty_id(state.difficulty)
    if div.scale in effort.RATIO_SCALES:
        key = _ledgered_order_key(state)
        if key is not None:
            _flush_order_effort(state, key)
            order_approvals.record_effort_fire(
                key, scale=div.scale, detail={"actual": div.actual, "estimate": div.estimate,
                                               "multiple": div.multiple},
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            window = order_approvals.get(key)["effort_since_user_approval"]
            state.order_effort_base = {
                scale: float(window.get(scale) or 0.0) for scale, _ in _ORDER_EFFORT_SCALES
            }
    return fire


def _difficulty_id(difficulty) -> str | None:
    """Content identity of a difficulty record's declared/investigated/critiqued sections
    — None until it is declared. Two records with the same sections are one record, so an
    agent cannot re-spend a consumed one under a fresh acknowledgement."""
    if difficulty is None or difficulty.declaration is None:
        return None
    payload = {
        "declaration": asdict(difficulty.declaration),
        "investigation": asdict(difficulty.investigation) if difficulty.investigation else None,
        "critique": asdict(difficulty.critique) if difficulty.critique else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _agent_replans_fire_route(state: SessionState) -> dict | None:
    """The agent's own route for an unacknowledged `replans`-scale fire of a ledgered
    order: a planning-difficulty cycle, not a question to the user. None for every other
    fire (spend/wall-clock stay the user's) and for a session with no user-approved order."""
    if _ledgered_order_key(state) is None or not state.effort_fires:
        return None
    last = state.effort_fires[-1]
    if last.get("ack") is not None or last.get("scale") != effort.SCALE_REPLANS:
        return None
    return {
        "route": "agent_planning_difficulty_cycle",
        "steps": ["declare", "investigate", "critique (differences_to_remove)", "normalize",
                  "fire-acknowledge --decision revise --by agent", "replan"],
    }


def _agent_fire_ack_refusal(state: SessionState, last: dict, decision: str) -> str | None:
    """Why an agent-authored fire-acknowledge is refused, or None when it may proceed."""
    if _ledgered_order_key(state) is None:
        return "the order has no user-approved version; the fire is the user's decision"
    if last.get("scale") != effort.SCALE_REPLANS:
        return "an open spend/wall-clock fire is owed to the user, not the agent"
    if decision != "revise":
        return f"decision {decision!r} on a fire is the user's; the agent may only revise"
    diff = state.difficulty
    did = _difficulty_id(diff)
    if did is None or not diff.complete():
        return "no complete planning-difficulty record (declare, investigate, critique) is open"
    if did == last.get("difficulty_id_at_fire"):
        return "the difficulty record predates the fire; declare one after it"
    if did in state.agent_ack_difficulty_ids:
        return "this difficulty record already discharged an earlier fire"
    if not diff.critique.differences_to_remove:
        return "the critique names no differences_to_remove"
    if diff.normalization is None or not diff.normalization.factor.strip():
        return "no real normalization record (a waiver does not discharge an agent fire)"
    return None


def _agent_renegotiation_refusal(state: SessionState, decision: str, doc) -> str | None:
    """Why an agent-authored renegotiation at the diagnosing-replan ceiling is refused,
    or None when a `continue` may proceed."""
    if decision != "continue":
        return f"{decision!r} changes the order's scope or ends it; that is the user's decision"
    if doc is None or _ledgered_order_key(state) is None:
        return "the order has no user-approved version to continue within"
    diff = state.difficulty
    did = _difficulty_id(diff)
    if did is None or not diff.complete():
        return "no complete planning-difficulty record (declare, investigate, critique) is open"
    if state.renegotiation_ceiling_difficulty_id is None:
        return "no ceiling refusal is recorded; run the replan without a decision first"
    if did == state.renegotiation_ceiling_difficulty_id:
        return "the difficulty record predates the ceiling event; declare one after it"
    if did in state.agent_ack_difficulty_ids:
        return "this difficulty record already discharged an earlier decision"
    if not diff.critique.differences_to_remove:
        return "the critique names no differences_to_remove"
    if diff.normalization is None or not diff.normalization.factor.strip():
        return "no real normalization record (a waiver does not discharge an agent decision)"
    boundary = _autonomy_for(state, doc)
    if not boundary["eligible"]:
        return f"the plan is outside the approved boundary: {boundary['reason']}"
    return None


def _autonomy_for(state: SessionState, doc) -> dict:
    """`gates.autonomy_boundary` for `doc`, against the ledger of the order this session
    was user-approved under (the baseline plan's order key), so a changed order reads as
    `order_changed` rather than as an empty ledger of its own."""
    key = _ledgered_order_key(state) or order_digest(doc)
    ledger = order_approvals.get(key)
    protected = _resources.protected_permission_surfaces(
        repo_root=state.repo_root, delivery_worktree=state.delivery_worktree,
        ledger_dir=str(order_approvals._root(None)),
    )
    view = plan_resources.compute_boundary_view(doc, order_digest(doc), venue=_venue_for(doc))
    return gates.autonomy_boundary(
        ledger, view, ledger.get("first_thinker_verdicts"), protected=protected,
    )


def _record_customer_stage_grants(state: SessionState, by: str, scope: str, entries: list[dict]) -> None:
    """A customer's `--scope stage` grant widens the order's approved set for later
    replans; a `once` grant is a single re-launch and never does."""
    customer_id = _order_customer_id(state)
    if scope != "stage" or not customer_id or by.strip().casefold() != customer_id.casefold():
        return
    try:
        doc = load_plan(state.plan_path)
    except (OSError, PlanError):
        return
    venue = _venue_for(doc)
    key = order_digest(doc)
    for entry in entries:
        if "rule" in entry:
            resolution = plan_resources.resolve_rule_grant(entry["rule"], venue)
        else:
            resolution = plan_resources.resolve_add_dir_grant(entry["path"], entry["mode"])
        if resolution.status != "resolved":
            continue
        for resource in resolution.resources:
            order_approvals.record_customer_grant(
                key, resource=resource, by=by, at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )


def _agent_review_override(state: SessionState) -> dict | None:
    """The coordinator's own plan-review override on record, which the user must see
    named when the plan reaches them: the review was waived, not passed."""
    reviews = [(review, review.scope)
               for review in (state.plan_review, *state.plan_stage_reviews.values())
               if review is not None]
    reviews += [(review, plan_review_scope_for_pair(pair))
                for pair, review in state.plan_pair_reviews.items()]
    for review, scope in reviews:
        if (review.verdict == gates._PLAN_REVIEW_OVERRIDE
                and (review.reviewer or "").strip().casefold() == AGENT_ACTOR):
            return {"reviewer": review.reviewer, "scope": scope, "note": review.note,
                    "plan_path": review.plan_path}
    return None


def _plan_review_override_refusal(state: SessionState, prev, reviewer: str, order) -> str | None:
    """Why an override by `reviewer` is refused, or None. `prev` is the prior record
    of the SAME scope and plan path (or None). An override is the customer's escape
    from a reviewer's `revise` deadlock: never by that reviewer itself, by the
    coordinator only while a round/friction release names the decision as its own,
    and by the order's customer_id when one is declared."""
    if (prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE
            and reviewer and reviewer == (prev.reviewer or "").strip()):
        return (
            f"override must come from a distinct reviewer: {reviewer!r} is the "
            "reviewer whose 'revise' verdict it would override (the user is the "
            "expected override author)"
        )
    if reviewer.casefold() == AGENT_ACTOR:
        # The override never counts as a passing first verdict for the autonomy
        # boundary (it is not written to the ledger).
        if not (gates.plan_review_round_release_active(state)
                or gates.cross_axis_friction_release_active(state)):
            return (
                "an agent-authored override is refused: it is available only once the "
                "review-round or cross-axis friction release is active"
            )
    elif order is not None and order.customer_id and reviewer != order.customer_id:
        return (
            f"override reviewer {reviewer!r} does not match order customer_id "
            f"{order.customer_id!r}; record it as the customer of record, or correct --reviewer"
        )
    return None


def _record_first_thinker_verdict(state: SessionState, doc, review, scope: str) -> None:
    """Write the thinker's first whole-plan verdict on the changed unresolved commands
    into the order's ledger, once per (approved version, changed identities) key. An
    override, an unattested review or a stage-scoped one is never that verdict."""
    if doc is None or scope or not review.plan_sha256:
        return
    if review.verdict not in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_REVISE):
        return
    key = _ledgered_order_key(state)
    if key is None:
        return
    verdict_key = _autonomy_for(state, doc)["first_verdict_key"]
    if verdict_key is None:
        return
    order_approvals.record_first_thinker_verdict(key, verdict_key, {
        "verdict": review.verdict, "plan_sha256": review.plan_sha256, "reviewer": review.reviewer,
    })


def _kind_within_boundary(state: SessionState, kind: str, doc) -> str:
    """A refinement that moves the order digest or adds a resource beyond the user's
    approved set is not a refinement: it re-enters the approval gate as substantive."""
    if kind != "refinement" or _ledgered_order_key(state) is None:
        return kind
    verdict = _autonomy_for(state, doc)
    if verdict["order_changed"] or verdict["extra_resources"]:
        return "substantive"
    return kind


def _autonomy_directive_data(state: SessionState, doc) -> dict:
    """The `data['autonomy']` block a plan-approval-bound Directive carries: the
    boundary verdict plus the action it routes to (self_approve inside, the user's
    approval outside)."""
    verdict = _autonomy_for(state, doc)
    return {**verdict, "action": "self_approve" if verdict["eligible"] else "await_user_approval"}


_CRITERION_ENGINE_WRITTEN_FIELDS = frozenset({"observation"})


def _apply_refined_stage_fields(cur, refined) -> None:
    """Copy the definition fields of a freshly-loaded stage onto the matching live
    stage. Shared by both replan branches that re-materialize from a corrected plan
    (refinement, and the no_change refresh) and by `_refresh_caches_from_plan_path`
    at approve, so the three never drift. Outcome/status is NOT touched — re-arm
    logic stays with each caller.

    The copied set must COVER every field `plan.stage_carry_key` reads. That
    coupling is the contract: the carry key decides whether a substantive replan
    keeps a stage's PASSED outcome, so a field this function fails to copy leaves
    the live stage stale against the plan bytes and re-arms a stage whose plan text
    never changed. `test_refresh_covers_every_carry_key_field` pins the relation, so
    a field added to the key but not here fails there rather than as an unexplained
    re-arm later. COVER is a lower bound, not an equality: `subject.material` is
    copied although no key reads it, because the live stage is also what `status`
    and every stage-reading report render — leaving one prose field pinned to the
    pre-edit bytes while its siblings track the file is a discrepancy with no
    reason behind it.

    Some of the copied fields (executor, supplies, done_criterion, criterion_type)
    are `_structural_signature`'s per-stage tuple, so a change to one classifies the
    replan substantive: copying them is a no-op for the two replan callers and
    load-bearing only for the approve-time refresh, which absorbs an in-place edit
    made at plan-mutable PLAN_READY."""

    cur.title = refined.title
    cur.subject.material = refined.subject.material
    cur.subject.result = refined.subject.result
    cur.means.means = refined.means.means
    cur.means.method = refined.means.method
    cur.means.procedure = refined.means.procedure
    cur.subject.invariants = refined.subject.invariants
    cur.subject.material_refs = list(refined.subject.material_refs)
    cur.subject.knowledge_refs = list(refined.subject.knowledge_refs)
    cur.knowledge = refined.knowledge
    cur.conditions = refined.conditions
    cur.preconditions = refined.preconditions
    cur.ephemeral_artifacts_waiver = refined.ephemeral_artifacts_waiver
    for field in fields(Criterion):
        if field.name not in _CRITERION_ENGINE_WRITTEN_FIELDS:
            setattr(cur.criterion, field.name, getattr(refined.criterion, field.name))
    cur.actor.executor = refined.actor.executor
    cur.actor.cost_tier = refined.actor.cost_tier
    cur.actor.guard_exempt_paths = list(refined.actor.guard_exempt_paths)
    cur.supplies = list(refined.supplies)


def _sync_venue_from_plan(state: SessionState, doc: "PlanDoc | None" = None) -> None:
    """Derive state.repo_root and state.delivery_worktree from a plan's [meta].

    The ONE place that answers "which trees is this session executing in", called
    from every route that establishes, re-establishes or re-reads which plan the
    session is running. The pair is set TOGETHER: a route that refreshed only
    repo_root left `resolve_check_venue` falling back to the canonical checkout
    for a plan that declares a delivery worktree, so dispatch launched the
    executor in a tree it is forbidden to write — the venue asymmetry that
    resolver exists to remove, reintroduced one route down.

    `doc` is the already-loaded plan when the caller has one; otherwise
    state.plan_path is loaded here. The load is lenient: only [meta] is read, and
    one caller (pop-subplan) is a restore path against a parent plan that may
    predate a later schema tightening.

    Fail-safe by contract: an absent, unreadable or unparseable plan leaves both
    existing values untouched and never raises. Two callers depend on that — the
    pop-subplan restore, which falls back to the frame's snapshotted venue, and
    the approve-time refresh, whose whole contract is best-effort."""
    if doc is None:
        if not state.plan_path:
            return
        try:
            doc = load_plan(state.plan_path, strict=False)
        except (OSError, PlanError):
            return
    state.repo_root = doc.meta.repo_root
    state.delivery_worktree = doc.meta.delivery_worktree


def _plan_venue_pair(path: str | None) -> tuple[str, str] | None:
    """Read exactly the two [meta] fields _sync_venue_from_plan reads off a plan
    file, as a plain pair — None on any read failure (absent path, unreadable or
    unparseable file), distinct from a successful read of two empty fields.

    Exists so cmd_push_subplan and cmd_pop_subplan compare the SAME two values
    the venue-resolution seam actually uses, and can tell "the file could not be
    read" apart from "the file has no venue declared" — the latter is the
    majority plan shape, not an edge case, so collapsing it into None would make
    the venue-substitution guard blind exactly where a plan is least specified."""
    if not path:
        return None
    try:
        doc = load_plan(path, strict=False)
    except (OSError, PlanError):
        return None
    return (doc.meta.repo_root or "", doc.meta.delivery_worktree or "")


def _restore_current_stage(state: SessionState) -> None:
    """Derive state.current_stage from the restored stages' own status, rather
    than trusting whatever the frame snapshotted or hardcoding None.

    Mirrors _sync_venue_from_plan's derive-don't-trust reasoning: a pop can
    restore a stage the earlier push left ACTIVE, mid-flight, while the frame's
    own current_stage snapshot may already be None — a live wedge one buggy pop
    can leave behind. Restoring that snapshot verbatim would keep the pointer
    lost; the per-stage ACTIVE status is the authoritative record of what's in
    flight, so the pointer is always recomputed from it instead. Fail-safe in
    the ambiguous cases — zero or more than one ACTIVE stage — both yield None,
    matching today's behaviour."""
    active = [s for s in state.stages if s.outcome.status == StageStatus.ACTIVE.value]
    state.current_stage = active[0].index if len(active) == 1 else None


def _stamp_accepted_plan_digest(state: SessionState, plan_path: str) -> None:
    """Record the sha256 of the ACCEPTED plan bytes on the session.

    Called from the three submission seams' commands and nowhere else, and only past every
    refusal path of the command that stamps — at approve that includes the plan_approval
    gate itself, which sits BELOW the seam — so state.accepted_plan_digest always names
    bytes the session actually took. Best-effort like its neighbours: an unreadable file
    leaves the previous digest in place rather than raising out of a command that has
    already decided to accept."""
    try:
        state.accepted_plan_digest = hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()
    except OSError:
        return


def _refresh_caches_from_plan_path(
    state: SessionState,
    *,
    runner: Runner | None = None,
    advice: list[str] | None = None,
) -> list[str]:
    """SUBMISSION SEAM (c). Re-load state.plan_path, validate it at submission grade, and
    — only if it is clean — refresh state.final_check plus each live stage's
    prose/criterion fields from those bytes. Returns the violation list; [] == refreshed.

    The seam's non-refusing channel is an OUT-PARAMETER rather than a second return value:
    the violation list is what this function's caller branches on, and advice must never
    become part of that decision. Pass a list to collect it; pass nothing (every caller
    that has no Directive to hang it on) and the seam behaves exactly as before.

    The seam is here rather than in `cmd_approve` directly because this is already the one
    place that re-reads plan_path at approve time, and a second read would let the two
    disagree. Nothing is mutated when the plan is dirty: the caller refuses with a
    Directive and the session keeps the state it had, so a rejected approve is not also a
    half-applied edit. The refusal must NOT be a raised PlanError — approve is where the
    plan_approval gate is armed, and an exception escaping there strands the session at
    PLAN_READY with no edge back.

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

    An absent plan_path still returns [] — "there is nothing to refresh" is not a
    submission violation, and the plan_approval gate already refuses a session with no
    plan artifact. A plan_path that is set but no longer LOADS is different: it is a
    session whose approve is about to attest to bytes nobody can read, so it is reported
    as a violation rather than swallowed. The old silent return let approve pass on the
    stale pre-edit cache — the very "attests to a plan it never actually executes" failure
    this function's own docstring names."""
    if not state.plan_path:
        return []
    from .plan import PlanError, load_plan as _load, stage_carry_key
    try:
        refreshed = _load(state.plan_path)
    except (OSError, PlanError) as exc:
        return [f"cannot load the plan at {state.plan_path!r}: {exc}"]
    violations = _submission_problems(refreshed, runner, state.weight_class)
    if violations:
        return violations
    if advice is not None:
        advice.extend(_submission_advice(refreshed, runner, state.weight_class))
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
    _sync_venue_from_plan(state, refreshed)
    # The digest is NOT stamped here. A clean submission is not yet an accepted plan at this
    # seam: `cmd_approve` still has the plan_approval gate to compose, and a blocked approve
    # must not leave the session naming bytes it refused. The stamp is the caller's, placed
    # past that refusal.
    return []


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
                       *, weight_class: str | None = None,
                       runtime_host_: str = runtime_host.HOST_CLAUDE) -> None:
    """Attach warn-only advisory strings to d.data['advisories']. Never changes d.ok or d.node.

    Single chokepoint for the enabled resolution: env override, else config-mode +
    weight_class (advisor.resolve_enabled) — every call site threads its session's
    weight_class through here rather than re-deriving the rule per site."""
    enabled = advisor.resolve_enabled(weight_class)
    advisories = advisor.judge(kind, payload, runner, enabled=enabled, runtime_host=runtime_host_)
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)


def _with_advisories(d: Directive, advisories: list[str]) -> Directive:
    """Attach warn-only strings to a Directive on its way out. Never touches d.ok/d.node."""
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)
    return d


def _submission_advice(doc, runner: Runner | None, weight_class: str | None) -> list[str]:
    """The submission seam's non-refusing channel, resolved the same way advisories are.

    Warn-only by construction — `submission_advice` never refuses — so every seam attaches
    these to d.data['advisories'] and leaves d.ok alone. Same enabled rule and same runner
    handling as `_attach_advisories`: `advisor.resolve_enabled` decides, and the caller's
    runner is passed STRAIGHT THROUGH.

    Pass-through matters more here than at the other advisory sites, because these seams
    are `submit_plan`, `approve` and `replan` — the commands most of this engine's tests
    drive. `advisor.subprocess_runner`'s own docstring reserves itself for "a caller that
    wants a live advisor", precisely so `runner=None` stays byte-identical to advisor-
    absent; substituting it here would put a real `claude -p` behind every substantive
    session's plan submission. Injecting it is an ENTRY-POINT decision (the shape
    hook-turn-end-gate.py and hook-escalation-diagnosis-gate.py use at their `__main__`),
    not one to take inside a helper.

    Fail-open at the HELPER boundary, not only inside `judge_echo`. `resolve_enabled`
    builds `Thresholds()` -> `parse_config_md()` -> `read_text()`, and that read sits
    outside its own `except KeyError`: an unreadable config.md would otherwise raise out
    of `cmd_approve`, a path that never called `resolve_enabled` before this seam existed,
    and strand the session at PLAN_READY with no edge back. A warn-only channel must never
    be able to refuse a command by exception."""
    try:
        return submission_advice(
            doc,
            judge_runner=runner,
            judge_enabled=advisor.resolve_enabled(weight_class),
        )
    except Exception:
        return []


def _submission_problems(doc, runner: Runner | None, weight_class: str | None) -> list[str]:
    """The submission seam's REFUSING channel, with its judge resolved the way the advice
    channel's is — one place per channel, so no seam re-derives the enabled rule.

    Unlike `_submission_advice` this may NOT swallow its result: these strings refuse a
    plan, and dropping them on an unrelated error would pass bytes nobody validated. Only
    the ENABLED resolution is caught, and only because `advisor.resolve_enabled` reaches
    config.md through `parse_config_md` -> `read_text` outside its own `except KeyError` —
    an unreadable config.md must not raise out of `cmd_approve` and strand the session at
    PLAN_READY with an armed gate and no edge back. Falling back to enabled=False keeps
    every violation the plan's own bytes support and drops only the one that needed a
    judge, which is the direction that judge already fails in."""
    try:
        enabled = advisor.resolve_enabled(weight_class)
    except Exception:
        enabled = False
    return submission_violations(
        doc,
        session_weight_class=weight_class,
        judge_runner=runner,
        judge_enabled=enabled,
    )


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
    if not plan_resources.runs_verify_command(crit):
        return True, None
    return _run_check(crit.verify_command, crit.expected_exit, runner, cwd)


_VENUE_IDENTITY_GIT_TIMEOUT_S = 30


def _venue_tree_identity(cwd: str | None, runner: Runner | None) -> str | None:
    """Digest identifying the current state of a verify_command's venue: HEAD sha
    plus the tree sha `git write-tree` would produce from a full `git add -A` of
    the working tree — computed entirely by git itself, in a disposable INDEX
    copy, never the venue's real one OR its real object store.

    Rationale for going through git rather than hand-rolling `diff`/`ls-files`/
    `read_bytes`: git's own content addressing already normalizes every path
    representation git itself accepts (non-ASCII bytes, leading/trailing spaces,
    embedded newlines) the way its own commands do, so the identity cannot drift
    from what `git add -A` would actually stage — a hand-rolled walk has to
    reinvent that decoding and can disagree with it at the edges.

    Mechanics: resolve the venue's REAL index path (`git rev-parse --git-path
    index` — worktree-aware, so a linked worktree's own index is used, not the
    main checkout's), copy its bytes into a throwaway temp file (seeding it from
    the real index preserves git's stat cache, so `add -A` need not re-hash every
    unchanged file), then run `add -A` and `write-tree` against that temp file via
    `GIT_INDEX_FILE` — passed through the `env` argv prefix, since the Runner
    protocol is argv-only and carries no environment. The real index and the
    working tree are never touched: `GIT_INDEX_FILE` redirects every read and
    write git would otherwise do against `.git/index` to the temp copy, and `add
    -A` never writes to tracked files regardless.

    `add -A`/`write-tree` also WRITE new blob/tree objects for any new or changed
    content — by default straight into the venue's real `.git/objects`, which
    would permanently pollute the real object store with throwaway objects from a
    tree identity nobody ever commits. `GIT_OBJECT_DIRECTORY` redirects every
    object git writes to a second disposable directory alongside the temp index;
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`, pointed at the venue's REAL objects dir
    (`git rev-parse --git-path objects` — shared across a main checkout and its
    linked worktrees, so this resolves to the one common store from either), lets
    git still READ every object already committed, so `write-tree` can reuse an
    existing blob's sha without rewriting it. Only genuinely new/changed content
    ends up as a loose object, and it lands in the disposable directory, removed
    with the rest of `tmp_dir` in the `finally` block below — the real store never
    gains a byte. Worktree-aware for the same reason `index` is: a linked
    worktree's `--git-path objects` still resolves to the shared common objects
    dir, not a private one, so this isolation covers a linked-worktree venue too.

    Every git call in this function carries a bounded timeout
    (`_VENUE_IDENTITY_GIT_TIMEOUT_S`); a hang or an expiry is treated exactly like
    any other git failure below — no caching, always re-run.

    Known limitation, not a bug: a `.gitignore`d file's content is invisible to
    `add -A` exactly as it is to a real commit, so an edit confined to an ignored
    file does not move the identity. A gitlink (an embedded repo, mode 160000) is
    the other known gap — git cannot see inside its working tree without
    recursing into that repo — so its PRESENCE in the temp index, changed or not,
    makes this function return None rather than risk a false cache hit.

    Returns None (never caches; the check always re-runs) whenever: the venue has
    no git repo or `HEAD` is unborn; any git call in the sequence fails, times out,
    or a call whose value is used (`rev-parse`, `write-tree`) prints nothing; or
    the temp index ends up containing a gitlink. `cwd=None` mirrors
    _verify_command_result's own default: the venue is plain repo_root.

    One consequence of the gitlink refusal worth naming: in a checkout whose
    untracked tree itself contains linked worktrees or embedded repos (e.g. the
    canonical checkout with its `.claude/worktrees/*` subtrees), `add -A` stages
    each one as a gitlink the moment it is reached, so the identity always
    resolves to None there — by design, not a bug the cache is missing out on."""
    # The Runner protocol takes argv only; the timeout bound applies to the real
    # subprocess runner, never to an injected one.
    if runner is None:
        def run(argv):
            return subprocess_runner(argv, timeout=_VENUE_IDENTITY_GIT_TIMEOUT_S)
    else:
        run = runner
    base = Path(cwd) if cwd else REPO_ROOT
    prefix = ["git", "-C", str(base)]
    head = run(prefix + ["rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        return None
    index_path = run(prefix + ["rev-parse", "--git-path", "index"])
    if index_path.returncode != 0 or not index_path.stdout.strip():
        return None
    real_index = base / index_path.stdout.strip()
    objects_path = run(prefix + ["rev-parse", "--git-path", "objects"])
    if objects_path.returncode != 0 or not objects_path.stdout.strip():
        return None
    # Resolved to absolute: passed to the subprocess as an env var value below,
    # which git reads before honouring `-C str(base)` -- a relative alternates
    # path would be interpreted against this process's own cwd, not the venue.
    real_objects = (base / objects_path.stdout.strip()).resolve()
    tmp_dir = tempfile.mkdtemp(prefix="agentctl-venue-index-")
    try:
        tmp_index = os.path.join(tmp_dir, "index")
        if real_index.is_file():
            shutil.copyfile(real_index, tmp_index)
        tmp_objects = os.path.join(tmp_dir, "objects")
        os.mkdir(tmp_objects)
        env_argv = [
            "env",
            f"GIT_INDEX_FILE={tmp_index}",
            f"GIT_OBJECT_DIRECTORY={tmp_objects}",
            f"GIT_ALTERNATE_OBJECT_DIRECTORIES={real_objects}",
        ] + prefix
        added = run(env_argv + ["add", "-A"])
        if added.returncode != 0:
            return None
        listed = run(env_argv + ["ls-files", "-s"])
        if listed.returncode != 0:
            return None
        if any(line.startswith("160000") for line in (listed.stdout or "").splitlines() if line.strip()):
            return None
        tree = run(env_argv + ["write-tree"])
        if tree.returncode != 0 or not tree.stdout.strip():
            return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    digest = hashlib.sha256()
    digest.update(head.stdout.strip().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(tree.stdout.strip().encode("utf-8"))
    return digest.hexdigest()


def _check_definition_digest(crit) -> str:
    """Digest of the fields that decide what the check itself DOES —
    verify_command, expected_exit, negative_control, negative_control_waiver —
    folded into the cache identity alongside `_venue_tree_identity` (R2 review
    B). The tree identity alone answers "has the venue moved"; it says nothing
    about "did a replan change the check we're pointing at the venue", so a
    tree-only key would let an edited control (or command) inherit a stale
    green-at-the-old-definition verdict on an otherwise-unmoved tree. Folded in
    unconditionally — an old recorded `checked_tree_identity` (from before this
    field existed) simply stops matching, so it degrades to a cache miss (one
    extra re-run) rather than a crash or a silent wrong hit."""
    digest = hashlib.sha256()
    for part in (
        crit.verify_command or "",
        str(crit.expected_exit),
        crit.negative_control or "",
        crit.negative_control_waiver or "",
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _cached_check_hit(stage, identity: str | None) -> bool:
    """True iff `stage.outcome` already recorded a GREEN check result for this exact
    venue tree identity: a repeated record-result call whose tree hasn't moved
    since the last green run skips re-running the check and goes straight to the
    judge. A red result is never cached (see the call site), so a genuinely broken
    check is always re-run rather than trusted to still be broken. `identity=None`
    (no resolvable git tree) never hits."""
    return (
        identity is not None
        and stage.outcome.checked_tree_identity == identity
        and stage.outcome.checked_tree_ok is True
    )


def _resolve_or_refuse(state: SessionState, venue: str) -> tuple[str | None, str | None]:
    """Resolve a declared CheckVenue via state.resolve_check_venue; return
    (cwd, refusal). refusal is None when the venue is usable — unset (cwd is
    None, the pre-venue behaviour), no delivery_worktree ever declared (the
    resolved cwd is plain repo_root, exactly as pre-fix — a missing repo_root
    surfaces as a shell `cd` failure -> check FAILED, unchanged), or an
    existing directory. Once a delivery_worktree IS declared, a resolved venue
    that does not exist on disk (e.g. a worktree never created, or cleaned up
    after the session that made it ended) cannot be run: the engine has no way
    to know whether the check would have passed there, so this is a REFUSAL
    distinct from a check failure — the caller must surface it without
    recording a stage FAILED or entering DIAGNOSING."""
    cwd = state.resolve_check_venue(venue)
    return cwd, _refuse_missing_cwd(state, cwd)


def _refuse_missing_cwd(state: SessionState, cwd: str | None) -> str | None:
    """The existence check shared by _resolve_or_refuse and its verify-final
    counterpart _resolve_final_or_refuse: given an already-resolved cwd,
    return a refusal message when the venue is declared but missing on disk,
    else None. Kept as the ONE place that turns "missing directory" into a
    refusal, so the two call sites cannot drift on what counts as refusable."""
    if not cwd or state.delivery_worktree is None or Path(cwd).is_dir():
        return None
    return (
        f"check venue {cwd!r} does not exist (declared by [meta] delivery_worktree "
        f"or repo_root); create the venue, or set venue = \"repo_root\" if a "
        f"delivery venue is no longer needed"
    )


def _resolve_final_or_refuse(state: SessionState, criterion: Criterion) -> tuple[str | None, str | None]:
    """verify-final's counterpart to _resolve_or_refuse: resolves the
    criterion's FINAL venue (SessionState.resolve_final_check_venue —
    verify_venue_at_final when declared, else verify_venue) instead of its
    execution venue, then applies the identical existence-refusal rule. Used
    ONLY by cmd_verify_final for stage checks; cmd_dispatch and
    cmd_record_result keep resolving crit.verify_venue via _resolve_or_refuse,
    since during execution the delivery venue is the only tree the change
    exists in at all."""
    cwd = state.resolve_final_check_venue(criterion)
    return cwd, _refuse_missing_cwd(state, cwd)


def _diagnose_venue_refusal(
    state: SessionState, store: StateStore, message: str,
    div: effort.Divergence | None,
) -> Directive:
    """Route a verify-final venue refusal into the ordinary DIAGNOSING cycle
    instead of stranding the session at VERIFYING — from VERIFYING, declare/
    investigate/critique all refuse ("difficulty commands run only in the
    DIAGNOSING cycle"), and only `reset --force` escaped. Mirrors the
    final_check FAILURE branch below verbatim, `next` included: like a
    stage/final_check FAILURE it directs the coordinator to `declare` and work
    the difficulty through declare -> investigate -> critique -> replan. A venue
    refusal is corrected by the replan (fixing the venue declaration), NOT by a
    bare "recreate the venue and re-run verify-final" — that shortcut re-entered
    verify-final from DIAGNOSING and crashed on the illegal transition, which is
    why the token names the difficulty cycle, not a direct venue edit. No stage
    is marked FAILED — `_resolve_or_refuse`'s refusal/failure split is preserved;
    only the destination node changes. Only ever reached from VERIFYING
    (cmd_verify_final's entry guard returns before here for any other node), so
    the `diagnose` transition is always legal. `div` is the divergence already
    computed by the caller (fire site 2's hoisted refresh_spend/divergence call) —
    attached here exactly like record_result's failed branch, so a venue refusal
    doesn't silently drop a live effort divergence just because it returns before
    the plan's own end-of-function fire check."""
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty()
    data = {}
    if div is not None and gates.effort_active(state):
        now = _utcnow()
        data["effort_divergence"] = _record_effort_fire(state, div, now=now)
    store.save(state)
    return Directive(
        False, state.node, "declare", message,
        marker="OVERCOME-DIFFICULTY",
        data=data,
    )


def _diagnose_acceptance_rejection(
    state: SessionState, store: StateStore, failing_ids: list[str],
) -> Directive:
    """Route a verify-final resolution refusal caused by a FAILING AcceptanceReview
    verdict into the ordinary DIAGNOSING cycle, instead of stranding the session at
    VERIFYING with no reachable difficulty cycle (declare/investigate/critique all
    require DIAGNOSING; only `reject` reaches it, and only from RESOLUTION — a node
    this session never gets to when acceptance already failed before every stage
    finished being verified). Mirrors _diagnose_venue_refusal's transition/save/
    Directive shape, and cmd_reject's declaration pre-seed (state.py Difficulty):
    a customer rejection recorded after every stage already PASSED is categorically
    the same event reject already handles at RESOLUTION — expected==pass, actual==
    the failing verdict(s), mismatch==the order's customer rejected the delivery —
    just reached one gate earlier, before an AcceptanceReview was ever able to pass.
    No effort-divergence data is attached here (see this stage's Method note: doing
    so would require hoisting refresh_spend/divergence earlier in cmd_verify_final
    for a defense-in-depth nicety, not the core fix)."""
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty(declaration=Declaration(
        expected="every [meta.order] requirement accepted (verdict pass)",
        actual=f"requirement id(s) {failing_ids} recorded as fail",
        mismatch="the order's customer rejected part of the delivery after every "
                  "stage already PASSED",
    ))
    store.save(state)
    return Directive(
        False, state.node, "declare",
        f"AcceptanceReview carries a non-pass verdict on requirement id(s) "
        f"{failing_ids}; run overcome-difficulty — declare the divergence, then "
        "investigate, then critique; replan is blocked until the cycle is complete",
        marker="OVERCOME-DIFFICULTY",
    )


def _diagnose_effort_divergence(
    state: SessionState, store: StateStore, div: "effort.Divergence", fire: dict,
) -> Directive:
    """Route a PASSING record-result or verify-final into DIAGNOSING instead of the
    normal next_stage/verify_final/resolve Directive, because this session's actual
    effort has diverged past its current plan's estimate (effort.py's divergence()).
    No stage is marked FAILED — the work genuinely passed — and no user question is
    ever asked at the fire: that would reinstate exactly the supervisory burden this
    trigger exists to remove (effort.py's module docstring, opening paragraph). Mirrors
    _diagnose_venue_refusal's shape (destination node + message only); the caller must
    already have called effort.record_fire(state, div) — divergence()'s CALLER
    OBLIGATION — and passes its return value through as `fire` so the pre-framed
    declaration carries the numbers that triggered it, not just the scale name."""
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty()
    store.save(state)
    data = {"effort_divergence": fire}
    route = _agent_replans_fire_route(state)
    if route is not None:
        data["agent_route"] = route
    return Directive(
        False, state.node, "declare", div.framing,
        marker="OVERCOME-DIFFICULTY",
        data=data,
    )


def _effort_fire_escalation_data(state: SessionState) -> dict:
    """The fire-context payload attached to every gates.effort_fire_blockers refusal
    (dispatch/replan/submit_plan) — the last (unacknowledged) entry of
    state.effort_fires plus the identifiers a coordinator needs to call
    `agentctl fire-acknowledge` against the right session. Shared by all three call
    sites so the payload shape never drifts between them."""
    fire = state.effort_fires[-1] if state.effort_fires else {}
    return {
        "task_id": state.task_id,
        "session_id": state.session_id,
        "scale": fire.get("scale"),
        "kind": fire.get("kind"),
        "actual": fire.get("actual"),
        "estimate": fire.get("estimate"),
        "multiple": fire.get("multiple"),
        "ts": fire.get("ts"),
    }


def _effort_fire_refusal(state: SessionState, what: str, efblock: list[str]) -> Directive:
    """The refusal `gates.effort_fire_blockers` produces at dispatch/replan/submit_plan.
    A replans-scale fire of a ledgered order names the agent's planning-difficulty cycle;
    every other fire is the user's decision."""
    data = {"blockers": efblock, "effort_fire": _effort_fire_escalation_data(state)}
    route = _agent_replans_fire_route(state)
    if route is None:
        return Directive(
            False, state.node, "fire_acknowledge",
            f"{what} blocked by an unacknowledged effort-divergence fire",
            marker=DIRECTIVE_ESCALATE_TO_USER, data=data,
        )
    data["agent_route"] = route
    return Directive(
        False, state.node, "planning_difficulty",
        f"{what} blocked by an unacknowledged replans-scale effort-divergence fire; "
        "run the planning-difficulty cycle (" + " → ".join(route["steps"]) + ")",
        marker="OVERCOME-DIFFICULTY", data=data,
    )


def _freeze_delivered_head(state: SessionState, stage, runner: Runner | None) -> None:
    """Best-effort `git rev-parse HEAD` at this stage's resolved verify venue,
    stamped onto `stage.outcome.delivered_head` — the delivered-head freeze
    `cmd_record_result` performs for every recorded stage, BEFORE that same
    call dispatches any verification, so a `kind = "landed"` check whose
    `delivered_stage` names THIS stage (a self-reference) already finds its
    own frozen commit in place. Re-stamps on every record-result call for the
    stage (a retried, previously-FAILED stage may deliver new commits) — but
    once stamped, no landed check ever re-derives it: only this call site
    writes the field, everyone else only reads it (SessionState.render_landed_command).
    Fails open: an unresolvable venue or a git error leaves whatever was
    already stamped (if anything) untouched, rather than clobbering it."""
    cwd = state.resolve_check_venue(stage.criterion.verify_venue)
    if not cwd:
        return
    run = runner or subprocess_runner
    result = run(["git", "-C", cwd, "rev-parse", "HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        stage.outcome.delivered_head = result.stdout.strip()


def _needs_delivered_head_freeze(state: SessionState, stage_index: int) -> bool:
    """True when SOME landed check in this plan — a stage's own criterion (a
    self-reference) or a final_check — names `stage_index` as its
    `delivered_stage`. Freezing is the only case that matters; skipping it
    otherwise keeps every plan with no landed check byte-identical to before
    (no runner call record-result did not already make) — the regression an
    unconditional freeze would otherwise introduce for every ordinary stage."""
    for s in state.stages:
        crit = s.criterion
        if (crit.verify_kind == CheckKind.LANDED.value and crit.landed
                and crit.landed.delivered_stage == stage_index):
            return True
    for fc in state.final_check:
        if (fc.kind == CheckKind.LANDED.value and fc.landed
                and fc.landed.delivered_stage == stage_index):
            return True
    return False


def _landed_check_result(
    state: SessionState, spec, runner: Runner | None
) -> tuple[bool, str | None, object]:
    """Dispatch one `kind = "landed"` check: render + run + classify. Returns
    (ok, refusal, result). `refusal` is set only when the check could not be
    attempted at all — SessionState.render_landed_command's own refusal
    (no repo_root / unknown delivered_stage / no frozen delivered_head yet),
    or the synthesized script exiting LANDED_GIT_ERROR_EXIT (git itself could
    not resolve `target`/`remote` — a ref that doesn't exist locally or on the
    remote). Only a genuine exit 0 (contained) or exit 1 (git's honest "not an
    ancestor") is a real pass/fail; a refusal must never be recorded as a
    stage FAILED or routed into DIAGNOSING — the shared rationale behind
    `_resolve_or_refuse`'s refusal/failure split, applied to the landed axis."""
    command, refusal = state.render_landed_command(spec)
    if refusal:
        return False, refusal, None
    run = runner or subprocess_runner
    result = run(["bash", "-c", command])
    if result.returncode == LANDED_GIT_ERROR_EXIT:
        return False, (
            f"landed check could not resolve {spec.target!r} (or "
            f"{spec.remote}/{spec.target!r}) against the delivered commit — "
            f"git could not answer (exit {LANDED_GIT_ERROR_EXIT})"
        ), result
    return result.returncode == 0, None, result


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
    state.review_rounds = task_accumulator.get(state.task_id)["per_axis_totals"]["review_rounds"]
    return state


def _count_review_round(state: SessionState, plan_sha256: str, *, thinker_record: bool = False) -> None:
    """Count one review round of the task and mirror the new total into `state`.

    `thinker_record=False` is a pairwise record: the accumulator adds a round only
    when `plan_sha256` is not the version it last counted (one pairwise run over N
    pairs, or a rerun on the same version, is one round). `thinker_record=True` is a
    whole-plan or stage thinker spawn, a round in itself. The compare-and-increment
    is the accumulator's own critical section, so this never reads the digest first.
    An empty digest (unreadable plan) counts nothing — same fail-direction as
    `_count_plan_review_round`."""
    if not plan_sha256:
        return
    digest = {"force_digest" if thinker_record else "count_if_digest_differs": plan_sha256}
    totals = task_accumulator.add(
        state.task_id, "review_rounds", 1, session_id=state.session_id, now=_utcnow(), **digest,
    )
    state.review_rounds = totals["per_axis_totals"]["review_rounds"]


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
    runtime_host.bind_runtime_host(state, getattr(args, "host", None), require=False)
    state.log("start", task=state.task_id)
    store.save(state)
    return Directive(True, state.node, "classify", "session registered; run classify next")


def cmd_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Re-arm a session for a NEW task once its prior task is closed. Refuses to
    discard a live prior task (not RESOLVED/ROUTED/BLOCKED) unless --force, so a
    new prompt cannot silently wipe in-flight work. Otherwise builds a fresh
    CLASSIFIED SessionState from the same args cmd_start uses.

    Second refusal, for the opposite case: re-entering a task that already reached
    RESOLVED. RESOLVED has no outgoing edge but `pop_subplan`, so this command is the
    only way back into a closed order — and the fresh SessionState it builds zeroes the
    effort baseline, the replan count and every round-release counter, so an unbounded
    reopen loop pays nothing and nobody is ever asked whether the order still stands.
    `gates.resolved_reentry_blockers` requires a recorded reason, and past
    `effort-replan-absolute` reopens a recorded user decision; the count it reads lives
    in the cross-session accumulator precisely because this command discards state."""
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
    reopen_reason = (getattr(args, "reopen_reason", None) or "").strip()
    reopen_decision = (getattr(args, "reopen_user_decision", None) or "").strip()
    reopening = prior is not None and prior.node == Node.RESOLVED.value and prior.task_id == args.task
    reentry_count = (
        int(task_accumulator.get(prior.task_id)["per_axis_totals"].get("resolved_reentry", 0) or 0)
        if reopening else 0
    )
    reentry_blockers = gates.resolved_reentry_blockers(
        prior.node if prior is not None else None,
        task_id=args.task,
        same_task=reopening,
        reopen_count=reentry_count,
        reason=reopen_reason,
        user_decision=reopen_decision,
    )
    if reentry_blockers:
        return Directive(
            False, prior.node, "noop", "; ".join(reentry_blockers),
            data={"blockers": reentry_blockers, "resolved_reentry_count": reentry_count},
        )
    if prior is not None:
        effort.refresh_spend(prior, _cost_rows(args), prior.plan_path)
        _flush_order_effort(prior)
    new = SessionState(
        session_id=args.session,
        task_id=args.task,
        goal=getattr(args, "goal", "") or "",
        overall_done_criterion=getattr(args, "done_criterion", "") or "",
        overall_criterion_type=getattr(args, "criterion_type", CriterionType.MEASURABLE.value),
        recursion_depth=int(getattr(args, "recursion_depth", 0) or 0),
    )
    runtime_host.bind_runtime_host(new, getattr(args, "host", None), require=False)
    new.log("reset", task=new.task_id, prior_task=(prior.task_id if prior else None))
    if reopening:
        # Counted in the accumulator, which survives this command; logged on the new
        # state, which is what a reader of THIS task's history will open. Neither
        # substitutes for the other: the log carries the reason, the accumulator
        # carries the count the ceiling compares.
        task_accumulator.add(
            new.task_id, "resolved_reentry", 1,
            session_id=new.session_id, now=_utcnow(),
        )
        new.log(
            "resolved_reentry", reason=reopen_reason,
            user_decision=reopen_decision or None, prior_reopens=reentry_count,
        )
    store.save(new)
    # Hygiene, not a security boundary (delivery.delete_stamp's own docstring):
    # a stale stamp cannot silently clear a later task's gate since the gate
    # binds on plan_sha256/rendering_sha256 and a reset task starts with no
    # plan, but leaving the sidecar around would confuse status/debugging
    # output with a stamp for a task that no longer exists.
    state_file = config_root.resolve_agentctl_state_file(args.session)
    if state_file is not None:
        delivery.delete_stamp(state_file)
    return Directive(
        True, new.node, "classify",
        (f"resolved task re-opened (reopen #{reentry_count + 1}); run classify"
         if reopening else "session re-armed for new task; run classify"),
        data={"resolved_reentry_count": reentry_count + 1} if reopening else {},
    )


def cmd_task_reset(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Explicit renegotiation: zero the cross-session task accumulator (item B)
    for `--task`. `cmd_reset` deliberately does NOT clear it, since the
    accumulator is task-scoped, not session-scoped: a fresh session re-armed
    on the same stuck task must inherit its prior friction, not silently
    forgive it (that would defeat the accumulator's entire purpose).
    Session-independent by design (no `--session`, no state load) — the
    accumulator lives outside any single session's state file — and requires
    `--reason` so this is never a casual one-flag habit; a user genuinely
    renegotiating a task's scope states why. A second, in-session path exists
    for the same explicit-renegotiation act: `cmd_replan`'s
    `--renegotiation-decision continue|rescope` (see
    `task_accumulator.reset`'s own docstring) — that path folds the reset into
    an already-required customer decision instead of a separate command."""
    by = (args.by or "").strip()
    if not by or by.casefold() == AGENT_ACTOR:
        return Directive(
            False, "(task-scoped)", "noop",
            f"task-reset is a user decision: --by must name the customer, not "
            f"{AGENT_ACTOR!r}",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"task": args.task, "by": by},
        )
    task_accumulator.reset(args.task)
    return Directive(
        True, "(task-scoped)", "noop",
        f"cross-session task accumulator reset for task {args.task!r} by {by}: {args.reason}",
        data={"task": args.task, "reason": args.reason, "by": by},
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
    bag['published_phases'][key]=True. The coordinator runs this AFTER the
    comment lands, so a publish gate (e.g. tracker's) reflects a real post rather
    than an intention. Generic — any publish-style plugin shares the convention;
    it does NOT fire a plugin event (recording a publish must not re-trigger
    observers). No-op-with-error if the plugin is not active.

    `--stage <n>` composes the bag key as f"{phase}:{stage}" instead of the bare
    phase name — the tracker plugin's per-stage journal gate (_journal_gate)
    tests membership of "progress:<index>" for each PASSED stage that declared
    output_artifacts, so a per-stage progress entry must be distinguishable from
    every other stage's. Omitting --stage keeps the plain phase key, unchanged
    for every other caller of this command (plan/result/status/searched/…)."""
    state = _require(store, args.session)
    bag = state.plugins.get(args.plugin)
    if bag is None:
        return Directive(False, state.node, "noop", f"plugin {args.plugin!r} is not active")
    phase = args.phase
    stage = getattr(args, "stage", None)
    key = f"{phase}:{stage}" if stage is not None else phase
    note = getattr(args, "note", None)
    skipped = bool(getattr(args, "skipped", False))
    if skipped:
        # An HONEST degrade: the transport for this phase was unavailable (e.g. the
        # tracker backend defines no tracker_publish_plan). Store a SKIP MARKER under
        # the phase key — the gate tests MEMBERSHIP, so the marker discharges a
        # mandatory phase without wedging resolution, while the reason stays visible
        # in the bag and the returned directive. Never a silent `recorded`.
        if not (note and note.strip()):
            return Directive(False, state.node, "noop",
                             "a skipped publication must carry a reason: pass --note")
        published = bag.setdefault("published_phases", {})
        published[key] = {"skipped": note}
        state.log("plugin_record", plugin=args.plugin, phase=phase, stage=stage, skipped=True)
        store.save(state)
        return Directive(
            True, state.node, "continue",
            f"plugin {args.plugin!r}: phase {key!r} publication skipped: {note}",
            data={"published_phases": sorted(published)},
        )
    if phase == "skipped" and not (note and note.strip()):
        return Directive(False, state.node, "noop", "a skip must carry a reason: pass --note")
    published = bag.setdefault("published_phases", {})
    published[key] = True
    # top-level bool-flag convention: a plugin whose bag seeds a bool keyed by the
    # phase name (e.g. experience: searched/recorded/skipped) reads those flags in
    # its gate; flip it true. Tracker's bag has no such keys, so it is untouched.
    # (A composite --stage key never collides with this convention's bare names.)
    if isinstance(bag.get(key), bool):
        bag[key] = True
    if note:
        if phase == "skipped":
            bag["skip_reason"] = note
        elif phase == "searched":
            bag["decision"] = note
    state.log("plugin_record", plugin=args.plugin, phase=phase, stage=stage)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"plugin {args.plugin!r}: phase {key!r} recorded as published",
        data={"published_phases": sorted(published)},
    )


def cmd_ledger_add(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Declare (or re-declare) one claim in the active ledger bag. Permissive by
    design — an ungrounded axiom/derivation/assumption is stored as-is; only the
    resolution gate (ledger.validate_ledger, via plugins_ledger._ledger_gate)
    rejects it. Grounding a claim is re-adding the same --id with --source/
    --premise/--basis filled in: UPSERT, last write wins. Fires no plugin event —
    recording a claim must not re-trigger the resolve nudge."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    claims = bag.setdefault("claims", [])
    entry = {
        "id": args.id,
        "status": args.status,
        "statement": getattr(args, "statement", "") or "",
        "source": getattr(args, "source", "") or "",
        "premises": list(getattr(args, "premises", None) or []),
        "basis": getattr(args, "basis", "") or "",
        "load_bearing": bool(getattr(args, "load_bearing", True)),
    }
    for i, c in enumerate(claims):
        if c.get("id") == entry["id"]:
            claims[i] = entry
            break
    else:
        claims.append(entry)
    state.log("ledger_add", claim=entry["id"], status=entry["status"])
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"claim {entry['id']!r} recorded as {entry['status']!r}",
        data={"claims": [c["id"] for c in claims]},
    )


def cmd_ledger_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: report the active ledger's blockers via the SAME
    plugins_ledger.ledger_blockers the resolution gate uses, so check and gate never
    diverge — claim closure (validate_ledger) AND candidate disposition-completeness
    (validate_candidates) AND the enumeration cross-check having RUN (bag
    ['enumerated']). Green (ok=True, no blockers) iff every load-bearing claim is
    grounded/derived/marked, every candidate is recorded/dismissed, and
    ledger-enumerate has run. Does not mutate state."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    blockers = plugins_ledger.ledger_blockers(bag)
    detail = "ledger closed" if not blockers else f"ledger not closed: {'; '.join(blockers)}"
    return Directive(not blockers, state.node, "inspect", detail, data={"blockers": blockers})


def cmd_ledger_candidate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Declare (or re-declare) one enumeration candidate — a load-bearing
    decision/judgment the coordinator (or ledger-enumerate's independent advisor
    pass, stage 5) has surfaced. Permissive by design, mirroring ledger-add:
    stored as 'raised' (undispositioned); only the resolution gate (ledger.
    validate_candidates) rejects a candidate left raised. UPSERT, last write
    wins — re-raising the same --id resets any prior disposition, so grounding a
    candidate is done via ledger-dispose, not by re-raising it. Fires no plugin
    event."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    candidates = bag.setdefault("candidates", [])
    entry = {
        "id": args.id,
        "statement": getattr(args, "statement", "") or "",
        "disposition": "raised",
        "reason": "",
        "claim": "",
    }
    for i, c in enumerate(candidates):
        if c.get("id") == entry["id"]:
            candidates[i] = entry
            break
    else:
        candidates.append(entry)
    state.log("ledger_candidate", candidate=entry["id"])
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {entry['id']!r} raised",
        data={"candidates": [c["id"] for c in candidates]},
    )


def cmd_ledger_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one enumeration candidate: 'recorded' (linked to an existing
    load-bearing claim via --claim) or 'dismissed' (with --reason). Refuses early
    when the flag its own --as needs is missing, rather than landing a
    disposition the resolution gate (ledger.validate_candidates) would reject
    anyway — the gate still owns whether the --claim id is real/load-bearing,
    this command only owns whether the required flag was passed at all."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    candidates = bag.setdefault("candidates", [])
    match = next((c for c in candidates if c.get("id") == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such candidate {args.id!r}")
    if args.as_ == "recorded" and not args.claim:
        return Directive(False, state.node, "noop", "--claim is required for --as recorded")
    if args.as_ == "dismissed" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as dismissed")
    match["disposition"] = args.as_
    match["reason"] = args.reason or ""
    match["claim"] = args.claim or ""
    state.log("ledger_dispose", candidate=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {args.id!r} dispositioned as {args.as_!r}",
        data={"candidate": args.id, "disposition": args.as_},
    )


def cmd_ledger_enumerate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Run the independent semantic enumeration cross-check over an outgoing
    deliverable: read --artifact, ask an independent advisor pass (advisor.
    enumerate_claims, `claude -p --model sonnet`, cost-bounded) to RAISE the
    load-bearing decisions/judgments/claims it detects, UPSERT each as a 'raised'
    candidate (last-wins by a deterministic `enum-N` id), then flip bag
    ['enumerated']=True so the mandatory-cross-check blocker is discharged.

    It only RAISES — disposition stays the coordinator's act (ledger-dispose), so
    it is the disposition + enumerated blockers, not this call, that make the
    cross-check advisory-BLOCKING. A recall-widener, never authoritative: an empty
    enumeration still flips the flag (the pass RAN), and the residual missed claim
    is Layer B discipline's to catch. Fires no plugin event."""
    state = _require(store, args.session)
    bag = state.plugins.get("ledger")
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'ledger' is not active")
    try:
        text = Path(args.artifact).read_text(encoding="utf-8")
    except OSError as exc:
        return Directive(False, state.node, "noop",
                         f"cannot read artifact {args.artifact!r}: {exc}")
    run = runner if runner is not None else advisor.enumerate_subprocess_runner
    statements = advisor.enumerate_claims(
        text, run, runtime_host=state.runtime_host or runtime_host.HOST_CLAUDE
    )
    candidates = bag.setdefault("candidates", [])
    raised: list[str] = []
    for i, statement in enumerate(statements):
        cid = f"enum-{i + 1}"
        entry = {"id": cid, "statement": statement, "disposition": "raised",
                 "reason": "", "claim": ""}
        for j, c in enumerate(candidates):
            if c.get("id") == cid:
                candidates[j] = entry
                break
        else:
            candidates.append(entry)
        raised.append(cid)
    bag["enumerated"] = True
    state.log("ledger_enumerate", raised=len(raised))
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"enumeration cross-check ran; raised {len(raised)} candidate(s) — "
        "disposition each with `agentctl ledger-dispose`",
        data={"raised": raised, "enumerated": True},
    )


def _question_bag(store: StateStore, session_id: str):
    """(state, bag) for the active premise bag, or (state, None) when the plugin is
    not armed — the caller returns the not-active Directive, mirroring the ledger
    verbs' `if bag is None` guard."""
    state = _require(store, session_id)
    return state, state.plugins.get("premise")


def _enumeration_escape_counts(state, doc: "PlanDoc | None" = None) -> dict | None:
    """plugins_premise.escape_counts for `state`, tolerant of the two states every
    surface reporting them must survive: NO PREMISE BAG (the plugin is not armed —
    most sessions, and the whole-surface not-applicable this function owns) and NO
    LOADABLE PLAN (nothing submitted yet, or a path that no longer parses — the
    per-plan-axis one escape_counts owns). Neither raises, and neither produces a
    zero: the not-applicable None propagates, at the whole-surface level for the
    first and at `this_plan` for the second, so a reader can tell 'no escapes were
    taken' from 'this axis does not apply here'.

    `doc` is an already-loaded PlanDoc when the caller has one (cmd_approve does), so
    the digest is derived from the same bytes that caller's other checks used."""
    bag = state.plugins.get("premise") if state is not None else None
    if bag is None:
        return None
    digest = None
    plan_path = getattr(state, "plan_path", None)
    if plan_path:
        if doc is None:
            try:
                doc = load_plan(plan_path)
            except (OSError, PlanError):
                doc = None
        if doc is not None:
            digest = plugins_premise._plan_content_digest(doc)
    return plugins_premise.escape_counts(bag, digest)


def _bound_stage_key(state, question: "premise.Question", plan_path: str | None = None) -> str:
    """The current stage_question_key of the ELEMENT a Question is bound to — the
    value dispose/rebind stamp into `disposed_at_key`. Scoped to the element rather
    than the whole stage so that editing one place of a stage's definition leaves the
    questions answered against its other places dispositioned. For a `plan.goal` /
    `plan.done_criterion` target, returns `plan_meta_element_key(doc, kind)` instead
    (#123) — the plan-level twin of the per-stage key. Returns "" for an
    unparseable target and when no plan has been submitted yet (`state.plan_path`
    empty) — exactly the cases premise.validate_questions exempts from the
    key-mismatch check. Reads only; the WRITE lives in the two disposing verbs so
    the package-wide single-writer scan stays exact.

    `plan_path`, when given, is read INSTEAD of `state.plan_path` — for the
    CORRECTED plan of a replan, which is not `state.plan_path` until that replan
    succeeds: `cmd_replan` evaluates the plan_approval gate against `args.plan`,
    and premise.validate_questions blocks a disposed question whose stamped
    `disposed_at_key` no longer matches its bound stage's key under THAT plan.
    Without a way to stamp against the corrected plan, dispose/rebind could only
    ever re-stamp the OLD plan's key and the gate blocked the very replan that
    would clear it — the enumeration channel's identical defect (#48(b)),
    mirrored here for the other two writers of `disposed_at_key`."""
    parsed = premise.parse_target(question.target)
    if parsed is None:
        return ""
    kind, stage_index, element = parsed
    if plan_path is None:
        plan_path = getattr(state, "plan_path", None)
    if not plan_path:
        return ""
    doc = load_plan(plan_path)
    if kind in ("goal", "done_criterion"):
        return plan_meta_element_key(doc, kind)
    if kind != "stage":
        return ""
    keys = {s.index: stage_question_key(s, element) for s in doc.stages}
    return keys.get(stage_index, "")


def _bound_order_stage_key(
    state, element: "premise.OrderElement", plan_path: str | None = None
) -> str:
    """The current whole-stage key of the stage an OrderElement is marked 'covered'
    by — the value cmd_order_dispose stamps into `content_digest` (#123), the
    order-coverage twin of `_bound_stage_key`. Whole-stage rather than per-element:
    an order element cites a stage's OUTCOME, not one of its named fields, so any
    edit to that stage should be visible as coverage drift. Returns "" when
    `element.stage` is None or no plan has been submitted yet — the cases
    premise.validate_order_elements exempts from the key-mismatch check.

    `plan_path`, when given, is read INSTEAD of `state.plan_path` — the same
    CORRECTED-plan escape `_bound_stage_key` documents: re-covering during a
    blocked replan must stamp against `args.plan`, not the stale `state.plan_path`,
    or the staleness check just added for OrderElement would deadlock replan with
    no route out, the same defect #48(b) fixed for questions."""
    if element.stage is None:
        return ""
    if plan_path is None:
        plan_path = getattr(state, "plan_path", None)
    if not plan_path:
        return ""
    doc = load_plan(plan_path)
    keys = {s.index: stage_element_keys(s) for s in doc.stages}
    stage_keys = keys.get(element.stage)
    if not stage_keys:
        return ""
    return stage_keys.get(WHOLE_STAGE_ELEMENT, "")


def _materiality_doc(state, named_plan) -> "tuple[PlanDoc | None, str]":
    """(plan a raised question's control is resolved against, refusal). Both empty
    when no plan exists yet: 'does this control exist in this plan' is undecidable
    before there is a plan, and refusing every pre-submission question would close
    the channel exactly where a plan's construction raises the most of them.

    A NAMED --plan that cannot be loaded refuses instead of skipping — otherwise
    naming any unreadable path is a one-flag bypass of the whole check."""
    if named_plan:
        try:
            return load_plan(named_plan), ""
        except (OSError, PlanError) as exc:
            return None, f"cannot load the plan named by --plan ({named_plan!r}): {exc}"
    plan_path = getattr(state, "plan_path", None)
    if not plan_path:
        return None, ""
    try:
        return load_plan(plan_path), ""
    except (OSError, PlanError):
        return None, ""


def _materiality_advisories(control: str, question: str, doc, state, runner) -> list[str]:
    """The PERCEPTION half of the materiality check, warn-only. The engine has
    already decided the rule half — the control resolves against this plan — and a
    judge may not reopen it; all that is left is whether the answer could MOVE the
    control, which no document decides.

    A judged NO is surfaced; a fail-open False is not. The reason field is what
    separates them, and here that distinction is the whole safety property: the
    advisory asserts the plan's own controls are indifferent to this question, and
    a False produced by a killed subprocess asserts nothing."""
    try:
        enabled = advisor.resolve_enabled(getattr(state, "weight_class", None))
    except Exception:
        return []
    run = runner if runner is not None else advisor.subprocess_runner
    try:
        verdict, reason = advisor.judge_question_materiality(
            control, question, run, enabled=enabled,
            control_text=controls.control_text(
                control, doc, grammars=controls.MATERIALITY_GRAMMARS),
        )
    except Exception:
        return []
    if verdict or reason:
        return []
    return [
        f"advisory (never blocking): the judge reads {control!r} as unable to change "
        f"its verdict on this question's answer — re-check that this is the control "
        f"the question really bears on"
    ]


def cmd_question_raise(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-declare) one OPEN question arising during plan construction.
    Permissive exactly like ledger-add about its TARGET: a malformed one is stored
    as-is and the GATE (premise.validate_questions) reports it, so the moment-of-
    arising record is never lost to an argparse rejection. UPSERT by --id, last
    write wins — re-raising resets the entry to open. state.log stamps the act; that
    timestamp IS the moment-of-arising record and is why questions live in state,
    not the plan file.

    NOT permissive about --control, and this is the one seam that is not: a question
    must name the control of this plan its answer could flip, and a name that
    resolves to nothing here is refused. The refusal lives at this WRITE seam rather
    than at the gate on purpose — every question persisted before the requirement
    existed carries no control name, and a gate demanding one would convert each of
    them into a blocker on a session that can no longer go back and answer it.
    Enforced here, the requirement binds every question raised from now on and none
    raised before.

    `--plan` names the plan the control is resolved against, defaulting to
    `state.plan_path`, for the CORRECTED plan of a replan — the same deadlock
    `question-dispose`/`question-rebind` carry the flag for (#48(b)): without it a
    question about a stage that exists only in the correction could never be
    raised."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to raise "
                         "against the session's own plan, or name a real one")
    control = getattr(args, "control", None)
    if control is not None:
        control = str(control).strip()
        if not control:
            return Directive(False, state.node, "noop",
                             "--control was given an empty name; name the control of "
                             "this plan whose verdict the answer could change")
    doc, refusal = _materiality_doc(state, named_plan)
    if refusal:
        return Directive(False, state.node, "noop", refusal)
    if control and doc is not None:
        problem = controls.resolve_control(
            control, doc, grammars=controls.MATERIALITY_GRAMMARS)
        if problem:
            return Directive(
                False, state.node, "noop",
                f"--control names no control of this plan — {problem}",
                data={"control": control},
            )
    questions = premise.questions_from_dicts(bag.get("questions", []))
    questions = [q for q in questions if q.id != args.id]
    questions.append(premise.Question(id=args.id, target=args.target,
                                      question=args.question or "", control=control or ""))
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_raise", question=args.id, target=args.target)
    store.save(state)
    advisories = (
        _materiality_advisories(control, args.question or "", doc, state, runner)
        if control and doc is not None else []
    )
    return _with_advisories(Directive(
        True, state.node, "continue",
        f"question {args.id!r} raised (open) against {args.target!r}",
        data={"questions": [q.id for q in questions], "control": control or ""},
    ), advisories)


def cmd_question_research(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the own-research attempt for a question — a SEPARATE act from
    disposing, because 'research precedes escalation' is only evidenced by two
    independently-logged acts. Sets own_research; does not disposition. Writes no
    disposed_at_key (only dispose/rebind may)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.own_research = args.attempted
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_research", question=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"own research recorded for question {args.id!r}",
        data={"question": args.id},
    )


def cmd_question_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition a question: researched | escalated | assumed. Stamps
    `disposed_at_key` from the BOUND STAGE's current stage_question_key ("" for
    plan.goal / plan.done_criterion targets and when no plan exists yet) — PER
    ENTRY, at disposition time, on THIS entry only. There is NO bag-level
    plan_sha256 to restamp; v1's laundering defect was exactly such a shared field,
    so one add carried a whole stale bag onto a new plan version. A per-entry stamp
    written only here (and in cmd_question_rebind) has no such path by construction.

    DEVIATES from the permissive-add idiom for ONE case: refuses `--to escalated`
    with empty own_research, naming `question-research` as the route out. This is a
    UX fast-fail, NOT the authority — the authority is premise.validate_questions
    rule 5 at the plan_approval gate (both exist on purpose; the CLI refusal is a
    courtesy, the gate refusal is the control). Other required fields per
    disposition stay permissive here and are enforced only at the gate.

    `--plan` names the plan `disposed_at_key` is stamped against, defaulting to
    `state.plan_path` so every pre-existing invocation is byte-identical — see
    `_bound_stage_key`. An empty `--plan` is rejected rather than silently falling
    back, mirroring `question-enumerate --plan`'s `0fc6313` fix (#48(b))."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to disposition "
                         "against the session's own plan, or name a real one")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    if args.to == "escalated" and not match.own_research:
        return Directive(
            False, state.node, "noop",
            "refusing --to escalated: own research must precede escalation to the "
            "user — record it with `agentctl question-research --id "
            f"{args.id} --attempted ...` first",
        )
    match.disposition = args.to
    if args.answer:
        match.answer = args.answer
    if args.source:
        match.source = args.source
    if args.derivation:
        match.derivation = args.derivation
    if args.basis:
        match.basis = args.basis
    if args.risk:
        match.risk = args.risk
    match.disposed_at_key = _bound_stage_key(state, match, named_plan)
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_dispose", question=args.id, disposition=args.to)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} dispositioned as {args.to!r}",
        data={"question": args.id, "disposition": args.to,
              "disposed_at_key": match.disposed_at_key},
    )


def cmd_question_rebind(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Re-stamp a disposed question's `disposed_at_key` to its bound stage's CURRENT
    key, after the coordinator has RE-READ the question against the changed stage
    (--confirm-still-valid carries that reason). This is the reachable route out of
    blocker 12 (bound-stage-definition-changed); without it a refusal with no
    resolution path trains a bypass. Requires a non-empty reason (argparse-enforced).
    The second — and only other — writer of disposed_at_key.

    `--plan` names the plan the re-stamp is read against, defaulting to
    `state.plan_path` so every pre-existing invocation is byte-identical — see
    `_bound_stage_key`. It exists for the CORRECTED plan of a replan (not yet
    `state.plan_path`): `cmd_replan` evaluates the plan_approval gate against
    `args.plan`, so without a way to name it here, a rebind could only ever
    re-stamp the OLD plan's key and blocker 12 would keep firing against the
    corrected one — the very deadlock this verb exists to make unreachable
    (#48(b), the enumeration channel's `752969e`/`0fc6313` fix mirrored here). An
    empty `--plan` is rejected rather than silently falling back."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to rebind "
                         "against the session's own plan, or name a real one")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.disposed_at_key = _bound_stage_key(state, match, named_plan)
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_rebind", question=args.id, reason=args.confirm_still_valid)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} rebound to its stage's current key",
        data={"question": args.id, "disposed_at_key": match.disposed_at_key},
    )


def cmd_question_retire(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Retire a question whose target stage no longer exists — the route out of the
    dangling-edge blocker (rule 2). Sets disposition='retired' + reason; writes NO
    disposed_at_key (retired is not key-bound — it has walked away from the target)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    match = next((q for q in questions if q.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such question {args.id!r}")
    match.disposition = "retired"
    match.reason = args.reason
    bag["questions"] = premise.questions_to_dicts(questions)
    state.log("question_retire", question=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"question {args.id!r} retired",
        data={"question": args.id},
    )


def cmd_question_list(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only render of the question bag. `--format md` is the THINKER'S read
    surface (state is canonical and so invisible to a reviewer who reads only the
    plan): a markdown table of target | control | question | disposition |
    own_research | source | derivation. A PROJECTION, exactly like the plan-render —
    never a second source of truth. Does not mutate state.

    `control` is rendered because it is the column a reviewer can DISAGREE with: it
    claims which of the plan's own controls the answer moves, and the engine only
    checked that the control exists."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    questions = premise.questions_from_dicts(bag.get("questions", []))
    if getattr(args, "format", None) == "md":
        rows = [
            "| target | control | question | disposition | own_research | source | derivation |",
            "|---|---|---|---|---|---|---|",
        ]
        for q in questions:
            disp = q.disposition + (f" — {q.stale_note}" if q.stale_note else "")
            rows.append(
                f"| {q.target} | {q.control} | {q.question} | {disp} | "
                f"{q.own_research} | {q.source} | {q.derivation} |"
            )
        if bag.get("enumeration_refused_oversize"):
            rows.append(
                "\n**enumeration refused (oversize)** — plan too large for judge "
                "subprocess argv (E2BIG); split the plan or record "
                "`agentctl question-enumerate-escape --reason advisor_oversize --note <text>`"
            )
        detail = "\n".join(rows)
    else:
        detail = "; ".join(
            f"{q.id}={q.disposition}" + (" [stale]" if q.stale_note else "")
            for q in questions
        ) or "no questions"
    return Directive(
        True, state.node, "inspect", detail,
        data={"questions": premise.questions_to_dicts(questions)},
    )


def cmd_order_raise(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record (or re-declare) one element of the ORDER the plan answers. Permissive
    UPSERT by --id exactly like question-raise, last write wins — re-raising resets
    the element to 'raised'. state.log stamps the act, and that timestamp is what
    evidences enumeration-before-plan: an element first raised after submit-plan was
    read off the plan, not off the order."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    elements = [e for e in elements if e.id != args.id]
    elements.append(premise.OrderElement(id=args.id, element=args.element))
    bag["order_elements"] = premise.order_elements_to_dicts(elements)
    state.log("order_raise", element=args.id)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order element {args.id!r} raised (undispositioned)",
        data={"order_elements": [e.id for e in elements]},
    )


def cmd_order_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one order element: covered (by --stage) or cut (with --reason).
    Fast-fails on the missing field for the named disposition — a courtesy, NOT the
    authority: premise.validate_order_elements at the plan_approval gate is the
    control, and it re-checks both (the dangling-stage case is only decidable there,
    against the plan actually submitted)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    if args.as_ == "covered" and args.stage is None:
        return Directive(False, state.node, "noop", "--stage is required for --as covered")
    if args.as_ == "cut" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as cut")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    match = next((e for e in elements if e.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such order element {args.id!r}")
    match.disposition = args.as_
    match.stage = args.stage if args.as_ == "covered" else None
    match.reason = args.reason if args.as_ == "cut" else ""
    match.content_digest = (
        _bound_order_stage_key(state, match, plan_path=getattr(args, "plan", None))
        if args.as_ == "covered" else ""
    )
    match.stale_note = ""
    bag["order_elements"] = premise.order_elements_to_dicts(elements)
    state.log("order_dispose", element=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"order element {args.id!r} dispositioned as {args.as_!r}",
        data={"element": args.id, "disposition": args.as_, "stage": match.stage},
    )


def cmd_order_list(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only render of the order bag. `--format md` IS the gate's own block,
    via the same plugins_premise.coverage_block the essence check re-derives — so
    the coordinator pastes what the gate will check rather than composing a second
    rendering of its own. Calling render_coverage_block directly here would be that
    second rendering: it would silently omit the live risk acceptances the gate
    demands, and the pasted essence would be rejected for lines this command never
    showed. A PROJECTION, never a source of truth. Does not mutate."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    elements = premise.order_elements_from_dicts(bag.get("order_elements", []))
    plan_path = getattr(state, "plan_path", None)
    stage_count = len(load_plan(plan_path).stages) if plan_path else 0
    if getattr(args, "format", None) == "md":
        detail = plugins_premise.coverage_block(state, bag) or premise.render_coverage_block(
            elements, stage_count)
    else:
        detail = "; ".join(
            f"{e.id}={e.disposition}" + (" [stale]" if e.stale_note else "")
            for e in elements
        ) or "no order elements"
    return Directive(
        True, state.node, "inspect", detail,
        data={"order_elements": premise.order_elements_to_dicts(elements),
              "stage_count": stage_count},
    )


def cmd_question_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: report the active premise bag's blockers via the SAME
    plugins_premise.premise_blockers the plan_approval gate uses, so check and gate
    never diverge (the ledger-check precedent). Green (ok=True) iff every raised
    question is closed, every enumeration candidate dispositioned, and the
    enumeration cross-check has run against the CURRENT plan content and either its
    runner did NOT fail or a typed escape is on record for the failure (`agentctl
    question-enumerate-escape --reason <closed-set value>`) — a run that ran but
    FAILED, unescaped, is red here exactly as it is at the gate. Does not mutate."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    blockers = plugins_premise.premise_blockers(state, bag)
    detail = "questions closed" if not blockers else f"questions not closed: {'; '.join(blockers)}"
    return Directive(not blockers, state.node, "inspect", detail, data={"blockers": blockers})


def cmd_question_candidate_dispose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Disposition one question-enumeration candidate (bag['candidates'], written
    by question-enumerate): 'recorded' (linked to an existing Question via
    --question) or 'dismissed' (with --reason). Mirrors cmd_ledger_dispose's
    shape but operates on the premise bag's OWN candidate store, never
    bag['questions'] — the two stores have different entry shapes and
    disposition referents (a QuestionCandidate points at a Question id, not a
    ledger claim), so this is a dedicated verb rather than an overload of
    question-dispose. Refuses early when --question for --as recorded is
    missing or does not resolve to an existing bag['questions'] entry —
    premise.validate_question_candidates enforces the same resolvability at the
    plan_approval gate; this command owns the CLI-level fast-fail."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    candidates = premise.question_candidates_from_dicts(bag.get("candidates", []))
    match = next((c for c in candidates if c.id == args.id), None)
    if match is None:
        return Directive(False, state.node, "noop", f"no such candidate {args.id!r}")
    if args.as_ == "recorded":
        if not args.question:
            return Directive(False, state.node, "noop", "--question is required for --as recorded")
        questions = premise.questions_from_dicts(bag.get("questions", []))
        if not any(q.id == args.question for q in questions):
            return Directive(
                False, state.node, "noop",
                f"--question {args.question!r} does not resolve to an existing "
                "question (dangling edge)",
            )
    if args.as_ == "dismissed" and not args.reason:
        return Directive(False, state.node, "noop", "--reason is required for --as dismissed")
    match.disposition = args.as_
    match.reason = args.reason or ""
    match.question = args.question or ""
    bag["candidates"] = premise.question_candidates_to_dicts(candidates)
    content_hash = premise.dismissal_hash(match.statement)
    if args.as_ == "dismissed":
        # A genuine COORDINATOR dismissal — the only source dismissed_hashes has
        # (the engine's own automatic immaterial dismissals and out-of-scope
        # listings never write here; see plugins_premise.py's state_factory).
        premise.record_dismissed_hash(
            bag.setdefault("dismissed_hashes", {}), content_hash,
            reason=match.reason, from_id=match.id, target=match.target)
    else:
        # The latest ruling countermands any earlier dismissal of this exact
        # (text, target) — a later pass must not keep silently carrying it.
        premise.forget_dismissed_hash(bag.get("dismissed_hashes") or {}, content_hash, match.target)
    state.log("question_candidate_dispose", candidate=args.id, disposition=args.as_)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"candidate {args.id!r} dispositioned as {args.as_!r}",
        data={"candidate": args.id, "disposition": args.as_},
    )


_LEGACY_ENUMERATION_ID = re.compile(r"^qenum-\d+$")


def _enumeration_part(target: str) -> str:
    """Which part of the plan a raised pair belongs to. A target that does not parse
    as a stage address belongs to the plan-level part — including a malformed one,
    which is the safe direction: `meta` is covered by every whole-plan pass, so a
    question the advisor addressed badly is still raised somewhere rather than
    dropped."""
    parsed = premise.parse_target(target)
    if parsed is not None and parsed[0] == "stage":
        return stage_part(parsed[1])
    return META_PART


def _candidate_immateriality(target: str, doc) -> str:
    """The reason to record an enumerated candidate as already dismissed, "" to
    raise it. A candidate the engine can see is addressed to no control of this
    plan is not a question the coordinator has to sit down and disposition: it
    cannot move any verdict this plan will reach.

    Only a STAGE target carries a derivable control — that stage's own
    done_criterion. A plan-level or unparseable target is raised, the same safe
    direction `_enumeration_part` takes on the same input and for the same reason:
    a badly-addressed question is still a question, and dismissing it on the
    strength of an address WE could not parse would discard it silently."""
    parsed = premise.parse_target(target)
    if parsed is None or parsed[0] != "stage":
        return ""
    control = f"stage {parsed[1]} done_criterion"
    unresolved = controls.resolve_control(
        control, doc, grammars=controls.MATERIALITY_GRAMMARS)
    return premise.CANDIDATE_IMMATERIAL if unresolved else ""


def _inherit_disposition(existing: dict, entry: dict, preserve: bool) -> dict:
    # Matched on the statement text, never the (coarser) target: two passes can
    # both address "goal" (same target, same id-slot) with different wording, and
    # only the statement tells them apart. `statement` is populated on every
    # candidate dict regardless of whether "target" is present, so there is no
    # legacy row this would fail to match — see _apply_enumeration_result's own
    # docstring ("Preservation is keyed on the statement being IDENTICAL, not on
    # the id alone").
    match = preserve and existing.get("statement") == entry.get("statement")
    if match and existing.get("disposition") != "raised":
        return dict(existing)
    return entry


def _unread_part_slot(candidates: list, entry: dict) -> str:
    """The id a plan-level pair from a pass that did NOT read the meta part lands
    under. Such a pass has no standing to renumber the meta part, so it must not
    take over a `qenum-meta-N` slot holding a different question: the row whose
    statement is identical keeps its own id (an in-place upsert that the caller runs
    with preserve_disposition, so its recorded/dismissed disposition survives), and a
    new statement gets the next unused `qenum-meta-N`, leaving every existing meta
    candidate — and the disposition recorded against it — exactly as it stands."""
    for existing in candidates:
        if existing.get("statement") == entry["statement"]:
            return existing["id"]
    used = {c.get("id") for c in candidates}
    n = 1
    while f"qenum-{META_PART}-{n}" in used:
        n += 1
    return f"qenum-{META_PART}-{n}"


def _upsert_candidate(
    candidates: list, entry: dict, *, preserve_disposition: bool, carry: dict | None = None,
) -> str:
    """Upsert `entry` into `candidates`, returning the id it actually lands under.

    `carry` is a dismissed_hashes record ({reason, from_id, target}) when `entry`'s
    own (statement hash, target) matches one on file — the SAME-TARGET requirement
    is what makes this a silent carry rather than a mere hint (see
    `premise.dismissed_hash_lookup`); checked by the caller, not here, since the
    caller already holds the bag's dismissed_hashes map. When present it takes
    priority over the ordinary id/legacy-id matching below and writes `entry` as
    'dismissed' with a carried reason, by a 3-tier resolution: (1) an existing row
    at entry['id'] with the SAME statement — an ordinary in-place update, unless
    it is already 'recorded' (a recorded disposition is never converted by a
    carry); (2) failing that, any existing DISMISSED row addressed to the SAME
    target whose own statement hash matches — reused in place, under ITS id, so
    the carry never overwrites an OPEN candidate merely because the two happen to
    share a part/index slot, and never reaches across to a same-text row
    addressed to a DIFFERENT target (that cross-target case is a hint, not a
    carry, and never reaches this function at all); (3) failing both, a fresh row
    under a non-colliding id. This is what keeps the invariant "a carried entry
    never overwrites an open candidate whose text differs, nor a dismissed row
    addressed to a different target" — the ordinary same-id branch below would
    happily overwrite an unrelated open row at the same id, which is exactly the
    case tier (2)/(3) exist to avoid."""
    if carry is not None:
        carried_entry = {
            **entry,
            "disposition": "dismissed",
            "reason": f"carried: {carry.get('reason', '')} (from {carry.get('from_id', '')})",
        }
        for j, existing in enumerate(candidates):
            if existing.get("id") == entry["id"] and existing.get("statement") == entry["statement"]:
                if existing.get("disposition") == "recorded":
                    return existing["id"]
                candidates[j] = carried_entry
                return entry["id"]
        target_hash = premise.dismissal_hash(entry["statement"])
        for j, existing in enumerate(candidates):
            if (existing.get("disposition") == "dismissed"
                    and existing.get("target") == entry.get("target")
                    and premise.dismissal_hash(existing.get("statement", "")) == target_hash):
                candidates[j] = {**carried_entry, "id": existing["id"]}
                return existing["id"]
        existing_ids = {c.get("id") for c in candidates}
        fresh_id = entry["id"]
        suffix = 2
        while fresh_id in existing_ids:
            fresh_id = f"{entry['id']}-carry{suffix}"
            suffix += 1
        candidates.append({**carried_entry, "id": fresh_id})
        return fresh_id

    for j, existing in enumerate(candidates):
        if existing.get("id") == entry["id"]:
            candidates[j] = _inherit_disposition(existing, entry, preserve_disposition)
            return entry["id"]
    # A candidate raised under the pre-part id scheme is the SAME question when its
    # statement is identical, so it is taken over rather than left standing beside its
    # own successor — otherwise a session carried across the change meets both, and the
    # disposition it already recorded protects neither.
    for j, existing in enumerate(candidates):
        if (_LEGACY_ENUMERATION_ID.match(existing.get("id") or "")
                and existing.get("statement") == entry["statement"]):
            taken_over = _inherit_disposition(existing, entry, preserve_disposition)
            candidates[j] = {**taken_over, "id": entry["id"]}
            return entry["id"]
    candidates.append(entry)
    return entry["id"]


def _target_stage_index(target: str) -> int | None:
    """The stage index a target addresses, or None for a plan-level/unparseable one —
    the same parse _enumeration_part and _candidate_immateriality already run, named
    for the one extra caller that needs just the index (the out-of-scope check)."""
    parsed = premise.parse_target(target)
    if parsed is not None and parsed[0] == "stage":
        return parsed[1]
    return None


@dataclass
class EnumerationApplyResult:
    """`_apply_enumeration_result`'s return value: the candidate ids it upserted as
    'raised' or freshly 'dismissed' (`raised`), the stage-addressed pairs a narrowed
    pass declined to disposition because their stage was outside its scope
    (`out_of_scope`, each `{"target", "question"}`), the ids a (text, target) match
    silently carried forward from an earlier coordinator dismissal of that SAME
    target instead of freshly raising (`carried`, a subset of `raised` since a
    carried candidate still lands in bag['candidates']), and the ids left OPEN but
    annotated with a `dismissal_hint_note` because their text matches an earlier
    dismissal recorded against a DIFFERENT (or unknown/legacy) target — a pointer
    for the coordinator to weigh, never a silent disposition (`carried_hint`, a
    subset of `raised`, disjoint from `carried`)."""
    raised: list[str]
    out_of_scope: list[dict]
    carried: list[str]
    carried_hint: list[str]


def _apply_enumeration_result(
    bag: dict, doc: PlanDoc, plan_path, pairs: list[tuple[str, str]], runner_ok: bool | None,
    *, parts: tuple[bool, set[int]] | None = None,
    preserve_disposition: bool = False, stderr: str = "",
    honor_dismissed_hashes: bool = True,
) -> EnumerationApplyResult:
    """Upsert `pairs` as QuestionCandidates (last-wins by a deterministic
    `qenum-<part>-N` id) — 'raised', except that a pair the engine can see is
    addressed to no control of this plan is written 'dismissed' with the one
    countable immateriality reason (see `_candidate_immateriality`), because a
    candidate that cannot move any verdict is not work for the coordinator —
    and stamp the bag's enumerated/enumerated_at/enumerated_plan/
    enumerated_runner_ok/enumerated_runner_stderr/enumerated_count fields plus the
    per-part digests the pass covered, from ONE enumeration pass's result. `stderr` is
    the failed pass's own diagnostic: the runner-failure blocker reads it back to
    pre-select an escape reason, so it must travel with the runner_ok it explains and
    not be re-derived later from a run nobody kept.
    Shared by the synchronous cmd_question_enumerate path and the detached-worker
    sidecar fold (cmd_approve/cmd_replan) so both apply identical upsert semantics
    to the SAME bag shape regardless of which path produced the pairs.

    `parts` is what the pass actually read — `(whole_plan, {stage indices})` from
    plugins_premise.enumeration_run_scope, None for a whole-plan pass. Only those
    parts' digests are refreshed, so a stage nobody re-read stays recorded against
    the bytes it WAS read at, and only those parts' candidate ids are renumbered:
    another part's candidates, and the dispositions recorded against them, are left
    exactly as they stand. On a narrowed pass, a pair whose target parses to a STAGE
    outside `stage_scope` is not upserted at all — the pass never read that stage, so
    it has no standing to disposition a question about it — and is listed in
    `EnumerationApplyResult.out_of_scope` instead, re-evaluated fresh on every pass
    (never carried, never counted against the plan-content digest it did not cover).
    A plan-level/unparseable target is exempt from this check and still upserted,
    the same safe direction `_enumeration_part` and `_candidate_immateriality` take —
    but, the meta part being unread, into a slot that never displaces a different
    existing meta question (`_unread_part_slot`).

    `preserve_disposition` is what separates the two callers. A human running
    `question-enumerate` ASKED for a fresh pass, so re-raising a candidate they had
    already dismissed is the point (False, the default — behaviour unchanged). The
    fold is INVOLUNTARY: it happens inside cmd_approve/cmd_replan, and resetting a
    recorded `dismissed`+reason or `recorded`+question link there would discard the
    user's own disposition and refuse the approve that disposition existed to
    unblock. Preservation is keyed on the statement being IDENTICAL, not on the id
    alone: `qenum-s1-3` of a later pass is a different question than `qenum-s1-3` of
    an earlier one unless its text says otherwise, and inheriting a disposition
    across a changed statement would silently discharge a question nobody read.

    `honor_dismissed_hashes` (default True, both callers) is what
    `question-enumerate --reopen-dismissed` flips to False for one pass: a
    candidate whose (text, target) matches a bag['dismissed_hashes'] record is
    normally carried forward dismissed under the ORIGINAL reason (see
    _upsert_candidate) rather than freshly raised, regardless of which id or part
    produces it this time. False both skips that carry for THIS pass (re-raising
    it the old fresh-pass way) AND forgets the matching record via
    `premise.forget_dismissed_hash`, so the very next ORDINARY pass does not
    silently re-carry a dismissal the coordinator just reopened — forgetting only
    at lookup time, never speculatively, keeps an unrelated (hash, target) pair
    that never matched untouched. A same-hash record filed against a DIFFERENT (or
    unknown/legacy) target is never a carry candidate at all, honored or not — it
    surfaces as a hint (`premise.dismissal_hint_note`, stamped into the raised
    entry's `reason`) so the coordinator sees the pointer without an unrelated
    stage silently reusing someone else's ruling. dismissed_hashes is otherwise
    untouched here — new dismissals are written only by a genuine coordinator
    dismissal (cmd_question_candidate_dispose)."""
    live_stages = plan_stage_digests(doc)
    meta_covered, stage_scope = parts if parts is not None else (True, set(live_stages))
    all_dismissed_hashes = bag.get("dismissed_hashes") or {}

    out_of_scope: list[dict] = []
    by_part: dict[str, list[tuple[str, str]]] = {}
    for target, question in pairs:
        if not meta_covered:
            stage_index = _target_stage_index(target)
            if stage_index is not None and stage_index not in stage_scope:
                out_of_scope.append({"target": target, "question": question,
                                    "reason": premise.CANDIDATE_OUT_OF_EDIT_SCOPE})
                continue
        by_part.setdefault(_enumeration_part(target), []).append((target, question))

    candidates = bag.setdefault("candidates", [])
    raised: list[str] = []
    carried: list[str] = []
    carried_hint: list[str] = []
    for part, part_pairs in by_part.items():
        for i, (target, question) in enumerate(part_pairs):
            immaterial = _candidate_immateriality(target, doc)
            entry = {"id": f"qenum-{part}-{i + 1}",
                     "statement": f"[{target}] {question}",
                     "disposition": "dismissed" if immaterial else "raised",
                     "reason": immaterial, "question": "", "target": target}
            carry = hint = None
            if not immaterial and all_dismissed_hashes:
                content_hash = premise.dismissal_hash(entry["statement"])
                found_carry, hint = premise.dismissed_hash_lookup(
                    all_dismissed_hashes, content_hash, target)
                if found_carry is not None:
                    if honor_dismissed_hashes:
                        carry = found_carry
                    else:
                        premise.forget_dismissed_hash(
                            bag.setdefault("dismissed_hashes", {}), content_hash, target)
            if hint is not None and carry is None:
                entry["reason"] = premise.dismissal_hint_note(hint)
            unread_meta = carry is None and part == META_PART and not meta_covered
            if unread_meta:
                entry["id"] = _unread_part_slot(candidates, entry)
            final_id = _upsert_candidate(
                candidates, entry,
                preserve_disposition=preserve_disposition or (unread_meta and honor_dismissed_hashes),
                carry=carry)
            raised.append(final_id)
            if carry is not None:
                carried.append(final_id)
            elif hint is not None:
                carried_hint.append(final_id)

    bag["enumerated"] = True
    bag["enumerated_at"] = plugins_premise._plan_content_digest(doc)
    if meta_covered:
        bag["enumerated_meta_at"] = plan_meta_digest(doc)
    recorded = bag.get("enumerated_stage_at") or {}
    bag["enumerated_stage_at"] = {
        str(index): (digest if index in stage_scope else recorded[str(index)])
        for index, digest in live_stages.items()
        if index in stage_scope or str(index) in recorded
    }
    bag["enumerated_plan"] = str(plan_path)
    bag["enumerated_runner_ok"] = runner_ok
    bag["enumerated_runner_stderr"] = stderr
    bag["enumerated_count"] = len(pairs)
    bag["enumerate_pass"] = int(bag.get("enumerate_pass") or 0) + 1
    return EnumerationApplyResult(
        raised=raised, out_of_scope=out_of_scope, carried=carried, carried_hint=carried_hint)


def cmd_question_enumerate(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Run the independent question-enumeration cross-check over the WHOLE plan: ONE
    bounded advisor pass (advisor.enumerate_questions_health, `claude -p --model sonnet`,
    cost-bounded) re-reads goal + done_criterion + the full plan text and RAISES the
    questions the plan's construction should have provoked but left implicit, each UPSERT
    as a 'raised' QuestionCandidate (last-wins by a deterministic `qenum-<part>-N` id), then
    flips bag['enumerated']=True and stamps bag['enumerated_at'] with the CURRENT plan
    content digest so a later content change re-blocks approve (the staleness check).

    ONE call, not one per element: the questions worth raising are overwhelmingly
    cross-element, and per-element fan-out would multiply cost by the element count for
    no recall gain (argued in enumerate_questions_health). That one call reads the whole
    plan unless a landed pass already covers every part but a few moved STAGES, in which
    case it reads those stages (plugins_premise.enumeration_run_scope) and leaves the
    other parts' candidates and dispositions untouched.

    The flag is flipped REGARDLESS of the pair count — never gated on a non-empty
    result. A count-gate is the tempting inversion and it is WRONG: a genuinely
    question-free plan is a HEALTHY pass, and gating on the count would leave
    `enumerated` False forever, wedging approve with no route out.

    Runner health is a different matter, and no longer discharges silently. A pass
    whose runner FAILED (enumerated_runner_ok False) is recorded as such and BLOCKS
    approve in plugins_premise.premise_blockers until a typed escape is on record —
    this command reports that in its advisory rather than pretending the cross-check
    was met. An ABSENT advisor (None) still discharges on the flag with the older
    non-blocking advisory (F3b), because refusing a check the fleet cannot run would
    be a wedge, not a gate. Fires no plugin event; records runner health
    (enumerated_runner_ok), its stderr and the pair count (enumerated_count)."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")
    # --plan names the plan to enumerate, defaulting to the session's current one so
    # every pre-existing invocation is byte-identical. It exists for the CORRECTED
    # plan of a replan, which is not state.plan_path until that replan succeeds:
    # premise_blockers stamps staleness by comparing bag['enumerated_at'] against the
    # digest of the plan under evaluation, and cmd_replan evaluates that gate against
    # args.plan, so without a way to name the corrected plan the gate can only ever be
    # left stale and blocks the replan that would clear it.
    #
    # The flag changes which plan is READ, never what the gate accepts: the digest is
    # still computed from the bytes actually enumerated and still has to equal the
    # digest of the plan being approved, so naming a THIRD plan stamps a digest
    # matching neither and blocks exactly as before.
    #
    # One failure mode IS new. An ABANDONED --plan pass leaves its candidates behind:
    # ids are upserted, so if a later pass over a different plan raises FEWER, the
    # surplus survive as `raised` and validate_question_candidates blocks approve on
    # candidates belonging to a plan no longer under evaluation. Rather than silently
    # truncating (which would discard a genuine candidate whenever a pass legitimately
    # shrinks), the bag records WHICH plan the standing candidates came from, so an
    # operator meeting an unexplained one can see it is an orphan and dispose of it.
    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to enumerate the "
                         "session's own plan, or name a real one")
    plan_path = named_plan or getattr(state, "plan_path", None)
    if not plan_path:
        return Directive(False, state.node, "noop",
                         "no plan submitted yet — run submit-plan before question-enumerate")
    try:
        plan_text = Path(plan_path).read_text(encoding="utf-8")
    except OSError as exc:
        return Directive(False, state.node, "noop",
                         f"cannot read plan {plan_path!r}: {exc}")
    # --plan accepts a path from the caller, so an unparseable one is ordinary bad
    # input and gets a Directive; state.plan_path is only ever set after a successful
    # load, so this branch is reachable only for the flag.
    try:
        doc = load_plan(plan_path)
    except PlanError as exc:
        return Directive(False, state.node, "noop",
                         f"cannot parse plan {plan_path!r}: {exc}")

    whole_plan, stage_scope = plugins_premise.enumeration_run_scope(bag, doc)
    if not whole_plan:
        plan_text = render_stages_md(doc, stage_scope)
    scope_source = "whole_plan" if whole_plan else "enumeration_baseline"

    run = runner if runner is not None else advisor.enumerate_subprocess_runner
    runner_ok, pairs, stderr = advisor.enumerate_questions_health(
        doc.meta.goal, doc.meta.done_criterion, plan_text, run)

    result = _apply_enumeration_result(
        bag, doc, plan_path, pairs, runner_ok, stderr=stderr,
        parts=(whole_plan, stage_scope),
        honor_dismissed_hashes=not getattr(args, "reopen_dismissed", False))
    state.log("question_enumerate", raised=len(result.raised), runner_ok=runner_ok, via="command",
              stages=sorted(stage_scope) if not whole_plan else None,
              scope_source=scope_source, out_of_scope=len(result.out_of_scope),
              carried=len(result.carried), carried_hint=len(result.carried_hint))
    store.save(state)

    scope_note = "" if whole_plan else (
        " (narrowed to stage(s) "
        + ", ".join(str(index) for index in sorted(stage_scope))
        + " — the only parts whose content moved since the last pass)")
    # Carried/hinted/out-of-scope are reported separately, not folded into "raised",
    # because they need three different coordinator reactions: a carried candidate
    # needs none (already dismissed on file), a hinted one is open and wants a
    # look before disposing, and an out-of-scope one was never dispositioned this
    # pass at all.
    breakdown = []
    if result.carried:
        breakdown.append(f"{len(result.carried)} carried (already dismissed on file)")
    if result.carried_hint:
        breakdown.append(f"{len(result.carried_hint)} hinted (similar to a dismissal for a different target)")
    if result.out_of_scope:
        breakdown.append(f"{len(result.out_of_scope)} out of this pass's scope")
    breakdown_note = f" ({'; '.join(breakdown)})" if breakdown else ""
    d = Directive(
        True, state.node, "continue",
        f"question enumeration cross-check ran; raised {len(result.raised)} candidate(s)"
        f"{scope_note}{breakdown_note} — "
        "disposition each with `agentctl question-candidate-dispose --id qenum-<part>-N "
        "--as recorded --question <qid> | --as dismissed --reason <text>`",
        data={"raised": result.raised, "enumerated": True, "runner_ok": runner_ok,
              "whole_plan": whole_plan, "stages": sorted(stage_scope),
              "scope_source": scope_source, "out_of_scope": result.out_of_scope,
              "carried": result.carried, "carried_hint": result.carried_hint},
    )
    # THREE arms, because runner_ok is three-valued and the three states now have
    # three different truths. `False` no longer discharges anything — the gate
    # blocks on it — so the old discharge wording became FALSE for that arm the
    # moment the blocker landed. `None` (advisor absent) still discharges, because
    # the gate deliberately does not block on it. Folding None into either
    # neighbour would print "blocked" at a session that is not blocked, or
    # "discharged" at one that is; nothing in the suite reads advisory text, so
    # such an error ships green — hence the arms are spelled out and each is tested.
    if runner_ok is False:
        pre_selected = advisor.classify_runner_failure(stderr)
        d.data.setdefault("advisories", []).append(
            "question enumeration RAN but its runner FAILED — the mandatory cross-check is "
            "now BLOCKED pending a typed escape, not discharged: either re-run this command "
            "once the advisor is healthy, or record "
            f"`agentctl question-enumerate-escape --reason {pre_selected} --note <text>` "
            "(the reason is pre-selected from this run's own stderr)"
        )
    elif runner_ok is None or not pairs:
        why = ("the advisor runner was unavailable"
               if runner_ok is None else "the pass raised no questions")
        d.data.setdefault("advisories", []).append(
            f"question enumeration discharged the mandatory cross-check on the flag alone "
            f"({why}) — the enumeration added no candidates, so re-read goal + "
            "done_criterion + every stage by hand for smuggled premises before approving"
        )
    return d


def _parse_stage_scope(raw) -> set[int] | None:
    """`--stages 3,7` -> {3, 7}; absent, empty or unreadable -> None, meaning the whole
    plan. A hand-typed nonsense value widens the reading rather than narrowing it to
    nothing, so the worst a bad value costs is the cross-check the engine ran before
    scoping existed."""
    tokens = [token.strip() for token in str(raw or "").split(",") if token.strip()]
    if not tokens or not all(token.isdigit() for token in tokens):
        return None
    return {int(token) for token in tokens}


def cmd_question_enumerate_worker(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Detached-child entry point for background whole-plan enumeration.

    Launched (never invoked by a human) via proc_tree.launch_supervised from
    cmd_submit_plan / cmd_replan, and NEVER wired into plugins.EVENT_FOR_COMMAND —
    so main()'s _fire_plugins returns before touching the state store. This
    function mirrors that by construction: it takes `store` (every cmd_* does, by
    the module's own calling convention) but never calls store.load() or
    store.save() on it. The parent process is typically already gone by the time
    this runs, so there is no session state to safely read or write — see
    enumerate_sidecar.py's module docstring for why a sidecar file, not the
    state store, is this process's only output.

    --plan and --digest are supplied by the launcher, but --digest is VERIFIED here
    rather than trusted: it is re-derived from the doc this process actually loaded,
    and a disagreement refuses the write. Handing the key down verbatim bought key
    agreement by giving up content agreement — the plan can be edited during the
    child's flight (up to advisor.ENUMERATE_TIMEOUT_S of it), and a sidecar keyed by
    the launcher's promise while carrying an enumeration of other bytes is folded as
    healthy. It is also what made this verb, which is in COMMANDS and the parser,
    a hand-callable way to write any sidecar the next `approve` would trust."""
    try:
        doc = load_plan(args.plan)
    except PlanError as exc:
        return Directive(False, "worker", "noop", f"cannot parse plan {args.plan!r}: {exc}")
    try:
        plan_text = Path(args.plan).read_text(encoding="utf-8")
    except OSError as exc:
        return Directive(False, "worker", "noop", f"cannot read plan {args.plan!r}: {exc}")

    # Refuse rather than re-key to the recomputed digest: the launcher stamped a
    # deadline against the digest it promised, and a sidecar under a different key is
    # a result for a plan version nobody is waiting on. Refusing lets that deadline
    # expire into its escape, which is the designed route out.
    recomputed = plugins_premise._plan_content_digest(doc)
    if recomputed != getattr(args, "digest", None):
        return Directive(
            False, "worker", "noop",
            f"refusing to write a sidecar: --digest {str(getattr(args, 'digest', None))[:12]}… "
            f"does not match the content digest of {args.plan!r} ({recomputed[:12]}…) — the "
            "plan changed after the launch, or this worker was invoked by hand")

    stage_scope = _parse_stage_scope(getattr(args, "stages", None))
    if stage_scope is not None:
        plan_text = render_stages_md(doc, stage_scope)

    run = runner if runner is not None else advisor.enumerate_subprocess_runner
    runner_ok, pairs, stderr_text = advisor.enumerate_questions_health(
        doc.meta.goal, doc.meta.done_criterion, plan_text, run)

    enumerate_sidecar.write(args.session, args.digest, {
        "runner_ok": runner_ok,
        "pairs": [list(pair) for pair in pairs],
        "stderr": stderr_text,
        "content_digest": args.digest,
        "plan_path": str(args.plan),
        # Absent (None) means the whole plan, which is also what a sidecar written
        # before the scope existed says by saying nothing.
        "stages": sorted(stage_scope) if stage_scope is not None else None,
    })
    return Directive(True, "worker", "noop",
                      f"enumeration worker finished; {len(pairs)} pair(s) written to sidecar")


def _question_raised_since_the_failed_enumeration(history: list[dict]) -> bool | None:
    """Whether a `question_raise` appears AFTER the last failed `question_enumerate`.

    The admissibility check behind `manual_enumeration_done`, and the only closed-set
    reason that asserts work was DONE rather than naming a failure the engine can see
    for itself. Without a precondition it is an unconditional click-through wearing a
    reason token.

    The obvious phrasing — "a question raised against the current plan digest" — is
    NOT expressible: premise.Question carries neither a content digest nor a
    timestamp, and `disposed_at_key` is a stage question key. What IS derivable is
    ordering over state.history, which is append-ordered, so "after" is index order.
    Existence alone would be worthless: every substantive plan has questions in its
    bag already, so an existence check passes for free on exactly the sessions this
    governs.

    Returns None when the history holds no failed `question_enumerate` at all — a
    distinct answer from False, because there is then nothing to order against
    rather than an ordering that came out wrong, and the caller says so.

    Honest about its own limit: a question raised SOLELY to satisfy this passes. The
    precondition raises the cost of a click-through from zero to non-zero and leaves
    a trace in the question log; it does not make gaming impossible."""
    last_failed = None
    for i, entry in enumerate(history or []):
        if entry.get("event") == "question_enumerate" and entry.get("runner_ok") is False:
            last_failed = i
    if last_failed is None:
        return None
    return any(entry.get("event") == "question_raise"
               for entry in history[last_failed + 1:])


def cmd_question_enumerate_escape(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a TYPED escape from an enumeration blocker, against the plan content
    the blocker is refusing on.

    The mandatory cross-check used to discharge itself the moment the pass RAN,
    whatever it returned — so an advisor timeout bought approve for free and left no
    trace anyone could count. The blocker now stands until an escape is on record;
    this is the only way past it, and every use is one countable row naming WHY.

    Liveness is the constraint that shapes the admissibility rules below: for every
    state in which approve is refused on the enumeration axis there must be some
    admissible reason, or the gate is a wedge rather than a gate. The two refusing
    branches are (a) a landed pass whose runner FAILED — escaped by the four
    runner-failure reasons — and (b) an enumeration that has not landed at all,
    escaped by `enumeration_not_landed` once the launch deadline has passed. A
    stale enumeration is escapable only once the round budget is spent: re-running
    the check clears staleness per step, but each re-run surfaces questions whose
    disposition edits the plan and stales the enumeration again, so per-step
    clearing is not loop termination. Below the budget, re-running is the route
    out; at or above it `plan_enumerate_round_release_active` fires and
    `enumerate_rounds_exhausted` is the additional escape.

    Admissibility is checked against the bag rather than trusted from the operator:
    a runner-failure reason offered while the last pass reports healthy (or absent —
    `None`, the advisor-not-there value) is refused, so is one offered while the
    failure on record was computed against OTHER plan content than the escape binds
    to, and `enumeration_not_landed` offered while the child still has time on its
    deadline is refused WITH the time remaining, because "wait" is the correct action
    there and the operator needs to know how long.

    `manual_enumeration_done` is the one reason asserting that WORK WAS DONE rather
    than that infrastructure failed, so it alone carries a second condition on top of
    the failed-run one: a `question_raise` must appear in state.history after the last
    failed `question_enumerate` (see _question_raised_since_the_failed_enumeration,
    including what that check cannot promise).

    `advisor_unavailable` is in the closed set but the blocker never pre-selects it:
    a live session whose advisor is missing surfaces as an ordinary error, so only a
    caller who KNOWS the runner was stubbed out or absent should reach for it.

    --plan mirrors cmd_question_enumerate's flag for the same reason: cmd_replan
    evaluates the gate against the CORRECTED plan, which is not state.plan_path
    until that replan succeeds, so an escape that could only ever bind to
    state.plan_path could never unblock the replan it exists for."""
    state, bag = _question_bag(store, args.session)
    if bag is None:
        return Directive(False, state.node, "noop", "plugin 'premise' is not active")

    # Also enforced by argparse `choices=`. Kept here because cmd_* functions are
    # called directly with a hand-built Namespace (by the suite, and by cmd_drive's
    # composition), and a closed set enforced only at the parser is not closed for
    # those callers.
    reason = getattr(args, "reason", None) or ""
    if reason not in premise.ENUMERATION_ESCAPE_REASONS:
        return Directive(False, state.node, "noop",
                         f"--reason must be one of "
                         f"{', '.join(premise.ENUMERATION_ESCAPE_REASONS)}; got {reason!r}")
    note = (getattr(args, "note", None) or "").strip()
    if not note:
        return Directive(False, state.node, "noop",
                         "--note is required and must not be empty — the reason token is what "
                         "aggregates, the note is what makes one row diagnosable")

    named_plan = getattr(args, "plan", None)
    if named_plan is not None and not str(named_plan).strip():
        return Directive(False, state.node, "noop",
                         "--plan was given an empty path; omit the flag to escape against the "
                         "session's own plan, or name a real one")
    plan_path = named_plan or getattr(state, "plan_path", None)
    if not plan_path:
        return Directive(False, state.node, "noop",
                         "no plan submitted yet — there is no enumeration blocker to escape")
    try:
        doc = load_plan(plan_path)
    except OSError as exc:
        return Directive(False, state.node, "noop", f"cannot read plan {plan_path!r}: {exc}")
    except PlanError as exc:
        return Directive(False, state.node, "noop", f"cannot load plan {plan_path!r}: {exc}")

    digest = plugins_premise._plan_content_digest(doc)
    runner_ok = bag.get("enumerated_runner_ok")
    if reason in premise.ENUMERATION_RUNNER_FAILURE_REASONS:
        if runner_ok is not False:
            healthy = "reports a HEALTHY run" if runner_ok is True else "records no run at all"
            return Directive(
                False, state.node, "noop",
                f"--reason {reason} escapes a FAILED enumeration run, but this session's "
                f"premise bag {healthy} (enumerated_runner_ok={runner_ok!r}) — nothing to "
                "escape from")
        # ...and the failure on record must be the one for the bytes being escaped. The
        # escape binds PER DIGEST while `enumerated_runner_ok` is session-global, so
        # without this a `False` left by a SUPERSEDED pass would admit an escape bound
        # to plan content whose own pass has not run yet — and when that pass later
        # lands and fails, escape_recorded finds the pre-recorded row and clears the
        # blocker. The failure would never be surfaced to anyone: the fail-open this
        # gate exists to close, one level in. premise_blockers already honours the same
        # rule (its failure branch is an elif behind the staleness check); this is the
        # escape side of it.
        enumerated_at = bag.get("enumerated_at") or ""
        if enumerated_at != digest:
            speaks_for = (
                f"a pass against different plan content (enumerated_at={enumerated_at[:12]}…)"
                if enumerated_at else "no landed pass at all")
            return Directive(
                False, state.node, "noop",
                f"--reason {reason} escapes the failed enumeration for the plan content at "
                f"{digest[:12]}…, but this session's premise bag speaks for {speaks_for} — "
                "wait for the pass against THESE bytes to land (or run `agentctl "
                "question-enumerate` to run it now) and escape the failure it reports")
        if reason == premise.ESCAPE_MANUAL_ENUMERATION_DONE:
            raised_since = _question_raised_since_the_failed_enumeration(state.history)
            if raised_since is None:
                return Directive(
                    False, state.node, "noop",
                    f"--reason {reason} asserts the cross-check was done BY HAND, but this "
                    "session's history records no failed `question_enumerate` to have done "
                    "it after — the claim has nothing to be ordered against, so it cannot "
                    "be checked; use the reason that names the failure you actually saw")
            if not raised_since:
                return Directive(
                    False, state.node, "noop",
                    f"--reason {reason} asserts the cross-check was done BY HAND, but every "
                    "`question_raise` in this session PREDATES the failed enumeration — so "
                    "nothing was raised in its place; run `agentctl question-raise` for what "
                    "the hand re-reading found (or dispose of the pass with the reason that "
                    "names the failure)")
    elif reason == premise.ESCAPE_ENUMERATE_ROUNDS_EXHAUSTED:
        if not gates.plan_enumerate_round_release_active(bag):
            passes = int(bag.get("enumerate_pass") or 0)
            threshold = Thresholds().effort_replan_absolute()
            return Directive(
                False, state.node, "noop",
                f"--reason {reason} is admissible only once the enumerate round budget is "
                f"exhausted ({passes}/{threshold} pass(es) applied so far) — run "
                "`agentctl question-enumerate` to advance the count, or re-run until the "
                "budget is spent")
    else:
        if bag.get("enumerated"):
            return Directive(
                False, state.node, "noop",
                f"--reason {premise.ESCAPE_ENUMERATION_NOT_LANDED} escapes an enumeration that "
                "never landed, but this session has one on record — re-run "
                "`agentctl question-enumerate` if it is stale")
        deadline = bag.get("enumerate_deadline")
        if deadline is None:
            return Directive(
                False, state.node, "noop",
                f"--reason {premise.ESCAPE_ENUMERATION_NOT_LANDED} names a background "
                "enumeration that missed its deadline, but none has ever been launched for "
                "this session — run `agentctl question-enumerate` instead")
        remaining = float(deadline) - time.time()
        if remaining > 0:
            return Directive(
                False, state.node, "noop",
                f"the background enumeration is still within its deadline — {remaining:.0f}s "
                "remaining; wait for it to land (or run `agentctl question-enumerate`, "
                f"which BLOCKS for up to {advisor.ENUMERATE_TIMEOUT_S}s) rather than "
                "escaping a check that may yet arrive")

    escapes = bag.setdefault("escapes", [])
    # A second escape at the same digest is RECORDED, not deduped: an escape is an
    # act, its note may differ, and dropping the row would put a hole in the audit
    # trail the whole mechanism exists to keep. What it must not do is read as the
    # thing that unblocked the gate when the gate was already clear — and "already
    # clear" is a question about the FAMILY premise_blockers discharges on, not
    # about this one reason token: premise_blockers clears the runner-failure branch
    # on ANY of ENUMERATION_RUNNER_FAILURE_REASONS, so escaping `advisor_timeout` at
    # a digest already carrying an `advisor_error` escape unblocks nothing either,
    # and must say so. `already` is therefore computed over the same family
    # premise_blockers consults for this branch, not over `(reason,)` alone.
    family = (
        premise.ENUMERATION_RUNNER_FAILURE_REASONS if reason in premise.ENUMERATION_RUNNER_FAILURE_REASONS
        else (reason,)
    )
    already = plugins_premise.escape_recorded(bag, digest, family)
    escapes.append({
        "reason": reason,
        "note": note,
        "content_digest": digest,
        # The window and the pass this escape speaks for — see plugins_premise.
        # escape_recorded for why the digest alone is not an identity.
        "enumerate_launch": int(bag.get("enumerate_launch") or 0),
        "enumerate_pass": int(bag.get("enumerate_pass") or 0),
        "runner_ok": runner_ok,
        "plan": str(plan_path),
    })
    state.log("question_enumerate_escape", reason=reason, plan=str(plan_path))
    store.save(state)
    detail = (
        f"enumeration escape recorded ({reason}) against the current plan content — the "
        "enumeration blocker is discharged for THESE plan bytes only; any edit to the plan "
        "re-blocks approve until the cross-check runs or is escaped again")
    if already:
        detail += (
            " — note that the blocker for these plan bytes was ALREADY discharged before "
            "this row (by this reason or another in the same family), so it adds to the "
            "count rather than unblocking anything")
    return Directive(
        True, state.node, "continue", detail,
        data={"reason": reason, "content_digest": digest, "escapes": len(escapes),
              "already_recorded": already},
    )


def cmd_classify(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    try:
        runtime_host.bind_runtime_host(state, getattr(args, "host", None), require=True)
    except (runtime_host.HostAmbiguousError, runtime_host.HostConflictError) as exc:
        return Directive(False, state.node, "noop", str(exc))
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
    elif sig.tracker_key and solved_marker.key_shape(sig.tracker_key) == "github":
        # a fully-qualified github ref (owner/repo#N) is not TRACKER_KEY_RE-shaped and so
        # does not force SUBSTANTIVE, but it must still reach cmd_resolve's marker stamp.
        state.tracker_key = sig.tracker_key
    state.deliverable_kind = getattr(args, "deliverable_kind", "") or ""
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
                       runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    return d


def cmd_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    state.node = transition(state.node, "plan")
    state.log("plan")
    store.save(state)
    return Directive(True, state.node, "await_plan", "planner working; submit-plan when ready")


def _spawn_enumeration_worker(argv, **kwargs):
    """The one seam between `_launch_enumeration` and `proc_tree.launch_supervised`.

    Exists so a test can suppress the real detached spawn by patching a `cli`-owned
    attribute instead of `proc_tree.launch_supervised` itself — `cli.proc_tree` is
    the SAME module object `test_proc_tree.py`/`test_kill_tree_cli.py` import
    directly, so patching the shared attribute (as a prior revision of the suite's
    autouse fixture did) stubs those tests' own subject under test too. Patching
    this wrapper instead leaves `proc_tree.launch_supervised` untouched for
    everyone but `_launch_enumeration`'s caller."""
    return proc_tree.launch_supervised(argv, **kwargs)


# The child's own ENUMERATE_TIMEOUT_S bound starts only once it has paid interpreter
# startup, imports and the plan load; the parent's deadline starts at the spawn call.
# Without a margin `enumeration_not_landed` becomes admissible a second or two before
# a healthy child's own bound expires — an escape recorded against a check that was
# still legitimately running.
_ENUMERATE_LAUNCH_MARGIN_S = 15


def _launch_enumeration(state: SessionState, bag: dict, doc: PlanDoc, plan_path) -> None:
    """Clear the premise bag's enumeration record back to not-run state, bump the
    launch counter an escape binds to, stamp `enumerate_deadline` (launch instant +
    advisor.ENUMERATE_TIMEOUT_S + _ENUMERATE_LAUNCH_MARGIN_S), and launch
    a detached background enumeration pass over `plan_path` — called from
    cmd_submit_plan and cmd_replan, the two places a NEW plan content becomes the
    one `approve` will gate-check.

    The stamp happens unconditionally, even when the launch itself fails below: a
    caller comparing premise_blockers against `time.time()` needs a real deadline
    regardless of whether the child actually started — a silently-missing deadline
    would look identical to "plenty of time left."

    Clearing enumerated/enumerated_at back to not-run (rather than leaving a
    still-True flag pinned to a now-superseded digest) routes the outstanding-child
    window onto the escapable _ENUMERATE_NOT_RUN blocker instead of the inescapable
    _ENUMERATE_STALE one — see plugins_premise.premise_blockers. The PER-PART digests
    survive that clear: a narrowed launch reads only the stages that moved, so the
    record its fold completes is the one holding what every other part was read at.

    Fire-and-forget by design: launch_supervised's child is detached
    (start_new_session=True, stdio to DEVNULL) and this process never reaps it —
    proc_tree.py's own module docstring is the precedent this mirrors. A launch
    failure (missing interpreter, fork failure, non-POSIX) is swallowed: the
    deadline is already stamped, so the outstanding-child window simply expires
    and Stage 5's escape takes over exactly as if the child had started and hung.
    Swallowed, but no longer silent: both outcomes are logged, so a wiring bug that
    breaks the launch for everyone is readable as itself instead of only as a
    fleet-wide rise in the `not_landed` escape bucket, and the success rows give that
    bucket a denominator. The log runs before the caller's store.save(), which every
    call site performs."""
    # The scope is derived from the very record the clear below destroys.
    whole_plan, stage_scope = plugins_premise.enumeration_run_scope(bag, doc)
    digest = plugins_premise._plan_content_digest(doc)
    bag["enumerated"] = False
    bag["enumerated_at"] = ""
    bag["enumerate_launch"] = int(bag.get("enumerate_launch") or 0) + 1
    bag["enumerate_launch_digest"] = digest
    bag["enumerate_deadline"] = (
        time.time() + advisor.ENUMERATE_TIMEOUT_S + _ENUMERATE_LAUNCH_MARGIN_S)
    scripts_dir = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "agentctl", "question-enumerate-worker",
            "--session", state.session_id, "--plan", str(plan_path), "--digest", digest]
    if not whole_plan:
        argv += ["--stages", ",".join(str(index) for index in sorted(stage_scope))]
    try:
        _spawn_enumeration_worker(
            argv,
            cwd=str(scripts_dir),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        state.log("enumerate_launch", ok=False, launch=bag["enumerate_launch"],
                  error=repr(exc))
    else:
        state.log("enumerate_launch", ok=True, launch=bag["enumerate_launch"])


def _fold_enumeration_sidecar(state: SessionState, doc: PlanDoc, plan_path) -> bool:
    """Fold a landed background-enumeration sidecar into the premise bag, if one is
    waiting for `doc`'s exact content digest — called from cmd_approve (before its
    blockers computation) and cmd_replan (inside the swapped-plan_path block,
    before pblock and before the digest-gated relaunch decision) so a detached
    worker's already-finished result is visible to the SAME gate evaluation that
    would otherwise see only the pre-launch not-run state, and so a fold that
    matches the proposed digest makes cmd_replan's relaunch check a no-op instead
    of firing a redundant second worker for content already enumerated.

    Returns True when the bag was actually mutated, which the caller MUST persist:
    the fold's own candidates are what the gate then refuses on, so a fold left
    unsaved names ids that exist nowhere on disk and `question-candidate-dispose`
    cannot address them.

    A no-op when the bag ALREADY records an enumeration for this exact digest: that
    result is on record (typically from a synchronous `question-enumerate` the
    coordinator ran by hand, whose candidates they have since dispositioned), and
    re-folding a sidecar carrying the same pass would cost a spurious refusal on
    every approve cycle. The sidecar is not even read in that case — it stays for
    session-end cleanup.

    A successful fold LOGS `question_enumerate` exactly as the synchronous command
    does, carrying the same `runner_ok`. That entry is not bookkeeping: since the
    detachment this is the path most enumerations actually arrive on, and
    `manual_enumeration_done`'s admissibility is an ORDERING over state.history
    (a `question_raise` after the last failed `question_enumerate`). A fold that
    logged nothing would leave that precondition with no anchor to order against on
    the very sessions it governs — silently admitting or silently refusing, either
    way for the wrong reason. The no-op returns above log nothing, so the history
    records passes, not attempts."""
    bag = state.plugins.get("premise")
    if bag is None:
        return False
    digest = plugins_premise._plan_content_digest(doc)
    if bag.get("enumerated") is True and bag.get("enumerated_at") == digest:
        return False
    payload = enumerate_sidecar.read_discarding_superseded(state.session_id, digest)
    if payload is None:
        return False
    pairs = [tuple(p) for p in payload.get("pairs", [])]
    runner_ok = payload.get("runner_ok")
    sidecar_stages = payload.get("stages")
    whole_plan = sidecar_stages is None
    parts = ((True, set(plan_stage_digests(doc))) if whole_plan
             else (False, set(sidecar_stages)))
    scope_source = "whole_plan" if whole_plan else "enumeration_baseline"
    result = _apply_enumeration_result(bag, doc, plan_path, pairs, runner_ok,
                                       parts=parts, preserve_disposition=True,
                                       stderr=payload.get("stderr", ""))
    # Surface the oversize escape explicitly so question-list --format md shows
    # "enumeration refused (oversize)" rather than a silent absence or a generic
    # advisor_error bucket entry — the split-the-plan work item is different from
    # a runner-health alarm and must be visible to the reviewer reading the bag.
    if runner_ok is False:
        _fold_escape = advisor.classify_runner_failure(payload.get("stderr", ""))
        if _fold_escape == premise.ESCAPE_ADVISOR_OVERSIZE:
            bag["enumeration_refused_oversize"] = True
    # `via` is stated on BOTH producers rather than encoded as this one's presence:
    # a distinction carried by an absent field reads as a forgotten field to the
    # next person grepping the history, and these rows now have three readers.
    state.log("question_enumerate", raised=len(result.raised), runner_ok=runner_ok, via="fold",
              scope_source=scope_source, out_of_scope=len(result.out_of_scope),
              carried=len(result.carried), carried_hint=len(result.carried_hint))
    return True


def cmd_submit_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if efblock:
        return _effort_fire_refusal(state, "submit_plan", efblock)
    effort.refresh_spend(state, _cost_rows(args), state.plan_path)
    _flush_order_effort(state)
    plan_path = args.plan
    # #15: a resubmission — the coordinator revised the plan at PLAN_READY (after a
    # thinker `revise` verdict, or the user's own pre-approval edit) and re-runs
    # submit-plan without a reset --force. Distinguished by the source node; drives
    # the `revise_plan` edge (PLAN_READY -> PLAN_READY) instead of `submit_plan`.
    resubmitting = state.node == Node.PLAN_READY.value
    if not plan_path.endswith(".toml"):
        state.log("submit_plan", plan=plan_path, verified=False)
        store.save(state)
        return Directive(
            False, state.node, "fix_plan",
            "plans are TOML-only — rewrite as TOML with typed stages",
            data={"problems": ["plans are TOML-only; rewrite as TOML with typed stages"]},
        )
    # Read plan_path's bytes ONCE and derive doc, the digest stamped below (seam (a))
    # and the plan-version snapshot below from that same buffer, so a concurrent edit
    # between separate reads can't desynchronize accepted_plan_digest from the bytes
    # that were actually parsed and verified.
    doc, _submitted_plan_bytes, _submitted_plan_digest = load_plan_with_digest(plan_path)
    state.stages = doc.stages
    _sync_venue_from_plan(state, doc)
    state.final_check = doc.meta.final_check
    if not state.goal:
        state.goal = doc.meta.goal
    if not state.overall_done_criterion:
        state.overall_done_criterion = doc.meta.done_criterion
    state.plan_verified = True
    # Submission seam (a): the first entry of these bytes into the session. The session's
    # weight class is passed in so this seam and the reachability gate immediately below
    # arm on the same condition — before, the seam keyed on the plan's own [meta] and the
    # gate on the session's, so a substantive session submitting a plan that simply omits
    # `weight_class` cleared the seam and not the gate.
    # Entry-point fallback (the run=runner-if-not-None-else-advisor.subprocess_runner idiom
    # used at the other advisor call sites): production's cmd_submit_plan is always invoked
    # with runner=None, so without this the judge is unreachable outside tests regardless of
    # advisor.resolve_enabled — which stays the actual kill switch, unaffected by this line.
    # Resolved HERE rather than beside the advice channel below because the seam's own
    # judged refusal (a `conditions` that merely restates depends_on) is part of `problems`,
    # and a refusal cannot be computed after the return that acts on it. The binding itself
    # costs nothing; only a prefilter hit spends a judge call.
    run = runner if runner is not None else advisor.subprocess_runner
    problems: list[str] = _submission_problems(doc, run, state.weight_class)
    if state.weight_class == WeightClass.SUBSTANTIVE.value:
        # Two-directional control: the scope lint (advisory, below) keeps a
        # control from being false-RED; this BLOCKS a control that can never
        # go honestly GREEN because it names a path no stage produces and
        # that does not exist. No legitimate instance -> a blocker, not a warn.
        problems.extend(
            verify_command_reachability_blockers(
                doc.stages, doc.meta.final_check, doc.meta.repo_root
            )
        )
    if problems:
        state.plan_verified = False

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

    # Past the refusal, so these bytes were ACCEPTED — which is the only thing
    # accepted_plan_digest ever records.
    state.accepted_plan_digest = _submitted_plan_digest
    # Content-address these accepted bytes as a plan version, independent of the
    # approved-plan snapshot below (that one is taken only at `approve`) -- so the
    # `submit_plan` history event's plan_sha256 always resolves to a stored file, even
    # for a submission never subsequently approved.
    _write_plan_version_snapshot(store, state, _submitted_plan_bytes, _submitted_plan_digest)
    _submitted_plan_sha256 = _submitted_plan_digest
    state.node = transition(state.node, "revise_plan" if resubmitting else "submit_plan")
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    if resubmitting:
        # The counter (read by gates.plan_review_round_release_active, reset by
        # cmd_approve) advances per resubmission made while a review record STANDS —
        # redrafts of a plan nobody reviewed are not rounds. Read before the staleness
        # clear below, which ends the round.
        if state.plan_review is not None or state.plan_stage_reviews:
            state.plan_review_rounds += 1
        # The plan changed, so any recorded thinker review that no longer covers
        # the resubmitted bytes must clear so the plan-review gate re-arms for
        # them. "No longer covers" is decided per review record via the SAME
        # plan.changed_parts a review's own recorded digests feed the coverage
        # gate with — a review recorded before this field existed (empty
        # reviewed_meta_digest/reviewed_stage_keys) compares as "everything
        # moved" against ANY doc, reproducing the old unconditional clear for
        # every legacy record without a special case.
        def _still_covers(pr: PlanReview) -> bool:
            meta_moved, moved = changed_parts(
                doc, {"meta": pr.reviewed_meta_digest, "stages": pr.reviewed_stage_keys})
            if meta_moved:
                return False
            idx = plan_review_scope_stage_index(pr.scope)
            return True if idx is None else idx not in moved

        if state.plan_review is not None and not _still_covers(state.plan_review):
            state.plan_review = None
        state.plan_stage_reviews = {
            scope: pr for scope, pr in state.plan_stage_reviews.items() if _still_covers(pr)
        }
    state.plan_submitted_ts = time.time()
    state.log("submit_plan", plan=plan_path, verified=True, revised=resubmitting,
              plan_sha256=_submitted_plan_sha256)
    bag = state.plugins.get("premise")
    if bag is not None:
        _launch_enumeration(state, bag, doc, plan_path)
    store.save(state)
    autonomy = _autonomy_directive_data(state, doc)
    override = _agent_review_override(state)
    directive_data = {"autonomy": autonomy, **({"agent_review_override": override} if override else {})}
    if autonomy["action"] == "self_approve":
        d = Directive(
            True, state.node, "self_approve",
            "plan is inside the boundary the user approved for this order; run "
            "`approve --by agent`",
            data=directive_data,
        )
    else:
        d = Directive(
            True, state.node, "await_user_approval",
            "plan ready; HARD GATE — get explicit user approval before approve",
            marker="PLAN-READY", data=directive_data,
        )
    _attach_advisories(d, "plan_completeness",
                       {"plan": plan_path, "stage_count": len(state.stages),
                        "titles": [s.title for s in state.stages]},
                       runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    # Deterministic scope lint (experience leaf 2026-06-29) — always runs,
    # independent of the optional LLM advisor above; warn-only, never blocks.
    d.data.setdefault("advisories", []).extend(
        verify_command_scope_warnings(doc.stages, doc.meta.final_check)
    )
    # Deterministic check-venue lint (#45) — same warn-only channel; fires
    # only when [meta] delivery_worktree names a venue distinct from repo_root.
    # Two triggers (see check_venue_warnings): a check whose `cd` target
    # contradicts its declared venue, and (schema 24) a bare "delivery"-venue
    # stage in a plan that asserts landing, which will refuse at verify-final.
    d.data.setdefault("advisories", []).extend(
        check_venue_warnings(doc.stages, doc.meta.final_check, doc.meta.repo_root, doc.meta.delivery_worktree)
    )
    d.data.setdefault("advisories", []).extend(grant_shadow.mixed_spelling_advisories(doc))
    # Submission seam (a)'s advice channel: a stage whose expected_result_image merely
    # restates its own check. Warn-only at all three seams — see submission.submission_advice.
    # Rides the same `run` resolved at the seam above (see its comment for why the fallback
    # exists at all). The `plan_completeness` advisory above is DELIBERATELY left on the raw
    # `runner`, i.e. inert in production, so the two adjacent advisor call sites in this
    # function behave oppositely on purpose: giving it the same fallback would add a second
    # live `claude -p` to every submit, which no stage has sized or measured. Read "is the
    # advisor reachable?" per call site, not by generalizing from either one.
    d.data.setdefault("advisories", []).extend(
        _submission_advice(doc, run, state.weight_class)
    )
    # Predsubmit check-run observation (C.2) — actually RUNS each stage's
    # verify_command in its declared venue, warn-only, same advisories channel.
    # Substantive-only, mirroring the reachability-blocker gate above: a
    # non-substantive plan carries no verify_command discipline through this
    # cycle at all, so there is nothing here for it to observe.
    if state.weight_class == WeightClass.SUBSTANTIVE.value:
        d.data.setdefault("advisories", []).extend(
            format_observations(
                observe_stage_checks(doc.stages, state.resolve_check_venue, runner)
            )
        )
    if gates.plan_presentation_active(state):
        # A NUDGE, not the enforcement — the hash-bound gate in gates.
        # plan_presentation_blockers (checked at `approve`) is what actually
        # makes presentation non-skippable. d.ok/d.node/marker stay untouched
        # so a coordinator/test relying on the PLAN-READY contract sees no
        # change; this only adds an advisory string a well-behaved caller reads.
        d.data.setdefault("advisories", []).append(
            "run `present-plan --kind essence` (and `--kind full` if the plan "
            "needs stage-by-stage detail) before requesting approval — "
            "`approve` will refuse without a bound presentation + delivery proof"
        )
    return d


# Kinds superseded by KIND ALONE, ignoring plan_path — see
# _record_plan_presentation's docstring for why replan_diff differs from
# essence/full. An explicit set, not an inline special case, so a future
# kind must choose its supersede key deliberately rather than inherit one
# by falling through an if/else.
_SUPERSEDE_BY_KIND_ALONE = frozenset({PLAN_PRESENTATION_KIND_REPLAN_DIFF})


def _record_plan_presentation(state: SessionState, presentation: PlanPresentation) -> None:
    """Store a PlanPresentation — SUPERSEDE, not append. Mirrors
    _record_stage_review's replace-then-append idiom: a later presentation
    fully replaces the prior receipt for the same key, so
    gates._plan_presentation_for's last-wins scan never has to choose between
    a stale and a fresh receipt.

    The supersede KEY splits by kind: essence/full supersede on
    (plan_path, kind), unchanged since these kinds always present
    state.plan_path — a session only ever runs one plan at a time, so the
    path component never actually discriminates for them. replan_diff
    (`_SUPERSEDE_BY_KIND_ALONE`) supersedes on kind ALONE: cmd_present_plan
    resolves its target from `--plan`, which varies across replan attempts
    against different candidate plan files, so keying on plan_path would let
    a receipt for a path the session has since moved off linger forever
    (unbounded rendering_text growth, and a stale entry surviving in
    last-wins scans across paths) instead of being replaced the moment a
    fresh diff is presented — only one proposed-diff receipt is ever
    current, and a receipt for an abandoned path is dead by definition."""
    if presentation.kind in _SUPERSEDE_BY_KIND_ALONE:
        state.plan_presentations = [
            p for p in state.plan_presentations if p.kind != presentation.kind
        ]
    else:
        state.plan_presentations = [
            p for p in state.plan_presentations
            if not (p.plan_path == presentation.plan_path and p.kind == presentation.kind)
        ]
    state.plan_presentations.append(presentation)


_PLAN_PRESENTATION_STAGE_ANCHOR_RE = re.compile(r"^\[stage (\d+)\]", re.MULTILINE)


def _plan_presentation_skeleton(stages: list[Stage]) -> str:
    """Deterministic `full`-rendering skeleton: one `[stage N] <title>` anchor
    line per stage, in plan order. present-plan's completeness check parses
    these same anchors back out of a submitted `full` rendering — the
    coordinator fills in the body under each anchor but must not drop or
    renumber one, or the completeness check rejects the rendering."""
    lines = [f"[stage {s.index}] {s.title}" for s in stages]
    return "\n".join(lines) + ("\n" if lines else "")


def cmd_present_plan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Stamp a PlanPresentation receipt: proof the coordinator rendered the plan
    for the user, bound to the exact plan version (plan_sha256) and the exact
    rendered bytes (rendering_sha256). Records the ARTIFACT and its bindings
    only — never a judgement of whether the rendering is a FAITHFUL or GOOD
    summary of the plan (that remains perception, same charter as PlanReview/
    StageReview). Orthogonal to the node graph: never calls transition() and
    never changes state.node — a plan may be presented at any point after it
    exists, independent of where the state machine currently sits.

    `--emit-skeleton` stamps nothing; it only hands back the anchor scaffold a
    `full` rendering must preserve, so the coordinator does not have to
    hand-enumerate stages itself.
    """
    state = _require(store, args.session)
    if not state.plan_path:
        return Directive(False, state.node, "noop", "no plan to present: submit a plan first")

    if getattr(args, "emit_skeleton", False):
        try:
            doc = load_plan(state.plan_path)
        except Exception as exc:
            return Directive(False, state.node, "noop", f"cannot load plan: {exc}")
        skeleton = _plan_presentation_skeleton(doc.stages)
        return Directive(
            True, state.node, "continue",
            "skeleton emitted; fill in each [stage N] section, then present-plan "
            "--kind full --rendering-file <path> with the completed rendering",
            data={"skeleton": skeleton, "stage_count": len(doc.stages)},
        )

    kind = args.kind
    if kind not in PLAN_PRESENTATION_KINDS:
        return Directive(
            False, state.node, "noop",
            f"unknown presentation kind {kind!r}; expected one of {PLAN_PRESENTATION_KINDS}",
        )
    explicit_plan = getattr(args, "plan", None)
    if explicit_plan and kind != PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # --plan is a degree of freedom only replan_diff needs (the proposed
        # plan is a different file than state.plan_path): widening it to
        # essence/full would let a receipt be stamped for a file the session
        # is not executing, which plan_presentation_blockers never checks for.
        return Directive(
            False, state.node, "noop",
            f"--plan is only accepted with --kind {PLAN_PRESENTATION_KIND_REPLAN_DIFF!r}; "
            f"essence/full always present state.plan_path ({state.plan_path!r})",
        )
    target = explicit_plan or state.plan_path
    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # essence is the receipt an approval ask is assembled from — gate it on
        # the same plan_review_blockers precondition as approve/replan, so a
        # thinker review must exist BEFORE that receipt can be stamped, not only
        # before the terminal approve. `full` is the detailed on-request view,
        # not the approval trigger, so it stays ungated.
        prblock = gates.plan_review_blockers(state, target)
        _log_gate(state, "plan_review", prblock, passed=not prblock)
        if prblock:
            return Directive(
                False, state.node, "noop", "cannot present essence",
                data={"blockers": prblock},
            )
    elif kind == PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # A proposed diff must itself have cleared thinker review before it can
        # be shown as the authorization prompt — the same precondition essence
        # pays, over the PROPOSED bytes rather than state.plan_path.
        prblock = gates.plan_review_blockers(state, target)
        _log_gate(state, "plan_review", prblock, passed=not prblock)
        if prblock:
            return Directive(
                False, state.node, "noop", "cannot present replan_diff",
                data={"blockers": prblock},
            )
    rendering_file = getattr(args, "rendering_file", None)
    if not rendering_file:
        return Directive(False, state.node, "noop", "--rendering-file is required")
    try:
        raw = Path(rendering_file).read_bytes()
    except OSError as exc:
        return Directive(False, state.node, "noop", f"cannot read rendering file: {exc}")
    if len(raw) > PLAN_PRESENTATION_RENDERING_CAP_BYTES:
        return Directive(
            False, state.node, "noop",
            f"rendering is {len(raw)} bytes, over the "
            f"{PLAN_PRESENTATION_RENDERING_CAP_BYTES}-byte cap — trim it, never "
            "truncate silently; the recorded receipt must match what the user saw",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Directive(False, state.node, "noop", "rendering file must be UTF-8 text")

    if kind == PLAN_PRESENTATION_KIND_FULL:
        # Completeness is MECHANIZABLE (every stage anchor present) — enforce it.
        # Whether the prose under each anchor is a faithful summary is
        # perception, not checked here; `essence` has no analogous check at all
        # (free-form by design — a stage-enumerated essence would just be `full`).
        try:
            doc = load_plan(target)
        except Exception as exc:
            return Directive(False, state.node, "noop", f"cannot load plan: {exc}")
        expected = {s.index for s in doc.stages}
        found = {int(m) for m in _PLAN_PRESENTATION_STAGE_ANCHOR_RE.findall(text)}
        missing = sorted(expected - found)
        extra = sorted(found - expected)
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"missing stage anchors: {missing}")
            if extra:
                problems.append(f"unknown stage anchors: {extra}")
            return Directive(
                False, state.node, "noop",
                "full rendering is incomplete; stamping nothing — " + "; ".join(problems),
                data={"missing": missing, "extra": extra},
            )
        # Same containment discipline as the anchor check just above, over a
        # DIFFERENT engine-generated block: a plan that grants anything must
        # show the reader exactly what, in the SAME rendering the approval ask
        # is assembled from (never a later, easy-to-skip detail view).
        if plan_has_any_grants(doc):
            full_grants_block = render_plan_grants(doc, fmt="full")
            if full_grants_block.strip() not in text:
                return Directive(
                    False, state.node, "noop",
                    "full rendering omits the grants block; stamping nothing — "
                    "paste this block into it verbatim and re-run",
                    data={"grants_block": full_grants_block},
                )

    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # Fold any landed enumerator sidecar BEFORE computing the coverage block,
        # so candidates are in the bag when the receipt is stamped (#60). The fold
        # is idempotent: a second call from cmd_approve at the same digest is a
        # no-op (the same-digest guard in _fold_enumeration_sidecar fires). A failed
        # plan load is swallowed — it surfaces moments later via the coverage_block
        # check below which also loads the plan.
        _fold_pres_bag = state.plugins.get("premise")
        if _fold_pres_bag is not None:
            try:
                _fold_pres_doc = load_plan(target)
                if _fold_enumeration_sidecar(state, _fold_pres_doc, target):
                    store.save(state)
            except (OSError, PlanError):
                # The same load-plan failure modes every other `load_plan` call site
                # in this file narrows to (a malformed plan, or a TOCTOU race on the
                # plan file underneath this exact race window) — swallowed here
                # because it surfaces moments later via the coverage_block check
                # below, which also loads the plan. Anything else is a bug in the
                # fold itself and must not be hidden behind it.
                pass
        # The scope-coverage block must be IN the essence — checked the same
        # mechanical way the `full` branch above checks stage anchors (containment
        # of engine-generated lines, never a read of the essence's own prose).
        # A COURTESY, not the authority: plugins_premise.premise_blockers re-checks
        # it at `approve`. It lives here because the essence is emitted as a turn's
        # FINAL text message, so discovering the omission at `approve` would cost
        # the whole present -> timer -> ask cycle — hence: stamp NOTHING, hand the
        # block back verbatim to paste. Silent when the premise plugin is not armed
        # (no order bag exists, and the gate half is equally silent) or when plan
        # presentation is inactive.
        bag = state.plugins.get("premise")
        if bag is not None and gates.plan_presentation_active(state):
            block = plugins_premise.coverage_block(state, bag)
            missing = plugins_premise.coverage_block_missing_lines(block, text)
            if missing:
                return Directive(
                    False, state.node, "noop",
                    "essence rendering omits the scope-coverage block; stamping "
                    "nothing — paste these lines into it verbatim and re-run: "
                    + "; ".join(missing),
                    data={"coverage_block": block, "missing_lines": missing},
                )
        # Same containment discipline, this time over the COMPACT grants
        # projection — the essence is the approval ask's raw material, so a
        # plan that grants anything must show the reader what, right here,
        # not only in the on-request `full`/`plan-grants` detail view.
        try:
            _grants_doc = load_plan(target)
        except (OSError, PlanError):
            _grants_doc = None  # surfaced already by the coverage/anchor checks above
        if _grants_doc is not None and plan_has_any_grants(_grants_doc):
            compact_grants_block = render_plan_grants(_grants_doc, fmt="compact")
            if compact_grants_block.strip() not in text:
                return Directive(
                    False, state.node, "noop",
                    "essence rendering omits the grants block; stamping nothing — "
                    "paste this block into it verbatim and re-run",
                    data={"grants_block": compact_grants_block},
                )

        # Refuse to stamp a receipt for a plan `approve` would refuse on premise
        # grounds — reverses the #60 "present, then discover at approve"
        # ordering (incident 2026-09-28: an essence was stamped with 12 raised
        # candidates, approve then refused). Runs after the coverage/grants
        # containment checks above so their data payloads survive on a refusal,
        # and excludes the essence-coverage half (include_essence_coverage=
        # False): that half compares this receipt against itself, which is
        # circular before it exists — approve's own gating stays unfiltered.
        if bag is not None and gates.plan_presentation_active(state):
            try:
                _premise_blk = plugins_premise.premise_blockers(
                    state, bag, include_essence_coverage=False
                )
            except (OSError, PlanError):
                _premise_blk = []  # surfaced already by the coverage/grants checks above
            if _premise_blk:
                return Directive(
                    False, state.node, "noop",
                    "cannot present essence: premise blockers stand — dispose "
                    "every open question and undispositioned order element, "
                    "same routes `approve` prints — stamping nothing",
                    data={"blockers": _premise_blk},
                )

    # grants_sha256 over whatever doc this branch already loaded (full/essence);
    # replan_diff loads its own, since neither branch above populated one for it.
    # A load failure here leaves the digest None rather than raising — the same
    # fail-closed posture PlanPresentation.grants_sha256 documents, and consistent
    # with essence's own tolerance of a failed `_grants_doc` load above.
    if kind == PLAN_PRESENTATION_KIND_FULL:
        _sha_doc = doc
    elif kind == PLAN_PRESENTATION_KIND_ESSENCE:
        _sha_doc = _grants_doc
    else:
        try:
            _sha_doc = load_plan(target)
        except (OSError, PlanError):
            _sha_doc = None
    presented_grants_sha256 = grants_sha256(_sha_doc) if _sha_doc is not None else None

    presentation = PlanPresentation(
        plan_path=target,
        kind=kind,
        plan_sha256=_plan_file_sha256(target),
        rendering_sha256=hashlib.sha256(raw).hexdigest(),
        rendering_text=text,
        presented_ts=time.time(),
        grants_sha256=presented_grants_sha256,
    )
    _record_plan_presentation(state, presentation)
    # D9 capture: what the user said when sending a PRIOR presentation of this plan
    # back (a correction or rejection) — the one thing present_plan's own receipt
    # never had anywhere to record, since the text belongs to the user's reply, not
    # to the rendering being stamped now. Optional and free-text; its absence (the
    # ordinary case — a first presentation, or a clean approval) is not a refusal.
    state.log("present_plan", plan=target, kind=kind,
              rendering_sha256=presentation.rendering_sha256,
              rejection_text=getattr(args, "rejection_text", None))
    store.save(state)

    if kind == PLAN_PRESENTATION_KIND_ESSENCE:
        # essence is the only kind that feeds an approval ask (full is
        # on-request detail, skeleton stamps nothing), so it alone gets the
        # full arm-timer/final-text/ask-next-turn choreography — spelled out
        # here, at the point the coordinator is guaranteed to read it, rather
        # than left to forgettable prose in a memory leaf it may never have
        # loaded (ask-user-question-split-turn.md).
        next_steps = [
            "arm a `sleep 2` background timer now (atomic with deferring the ask)",
            "emit THIS exact rendering as the turn's FINAL text message — zero "
            "tool calls after it",
            "next turn, open directly with the approval AskUserQuestion (zero "
            "preceding text) carrying an option whose label or description "
            f"embeds the literal marker {SHOW_FULL_PLAN_MARKER!r}",
        ]
        detail = (
            "presentation receipt recorded (kind=essence). Next: "
            + " Then, ".join(f"({i}) {step}" for i, step in enumerate(next_steps, 1))
        )
        data = {
            "rendering_sha256": presentation.rendering_sha256,
            "plan_sha256": presentation.plan_sha256,
            "show_full_plan_marker": SHOW_FULL_PLAN_MARKER,
            "next_steps": next_steps,
        }
        override = _agent_review_override(state)
        if override is not None:
            data["agent_review_override"] = override
        return Directive(True, state.node, "continue", detail, data=data)

    if kind == PLAN_PRESENTATION_KIND_REPLAN_DIFF:
        # Mirrors the essence choreography above exactly, substituting the
        # replan-authorization marker for the approval one — this is the
        # rendering a non-substantive replan's diff-authorization ask is
        # assembled from, gated by gates.replan_authorization_blockers.
        next_steps = [
            "arm a `sleep 2` background timer now (atomic with deferring the ask)",
            "emit THIS exact rendering as the turn's FINAL text message — zero "
            "tool calls after it",
            "next turn, open directly with the replan-authorization "
            "AskUserQuestion (zero preceding text) carrying an option whose "
            f"label or description embeds the literal marker {AUTHORIZE_REPLAN_MARKER!r}",
        ]
        detail = (
            "presentation receipt recorded (kind=replan_diff). Next: "
            + " Then, ".join(f"({i}) {step}" for i, step in enumerate(next_steps, 1))
        )
        data = {
            "rendering_sha256": presentation.rendering_sha256,
            "plan_sha256": presentation.plan_sha256,
            "authorize_replan_marker": AUTHORIZE_REPLAN_MARKER,
            "next_steps": next_steps,
        }
        return Directive(True, state.node, "continue", detail, data=data)

    return Directive(
        True, state.node, "continue",
        f"presentation receipt recorded (kind={kind}); emit this exact rendering "
        "as the turn's FINAL text message so the delivery hook can verify it "
        "actually reached the user",
        data={"rendering_sha256": presentation.rendering_sha256, "plan_sha256": presentation.plan_sha256},
    )


def cmd_confirm_delivery(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The HUMAN escape from the delivery-stamp half of the plan-presentation
    gate — used when the delivery hook is disabled, uninstalled, or otherwise
    cannot fire (see gates.plan_presentation_blockers's fail-CLOSED
    justification: without a reachable escape, a dead hook would brick every
    substantive session at PLAN_READY forever — the bypass-trainer shape a
    fail-closed gate must never produce).

    NOT a security boundary: anyone who can run this can already run
    `approve --by <name>` directly, exactly as with plan-review overrides — it
    exists for AUDIT-TRAIL hygiene (a named actor and a stated reason for the
    manual override), never to gate WHO may approve. cmd_present_plan and the
    delivery hook must never call this themselves; it is reachable only as a
    human-initiated command, enforced by rejecting `--by hook` outright.

    `--kind` (default essence) selects WHICH presentation receipt this stamp
    binds to — essence/full back the plan-approval gate
    (gates.plan_presentation_blockers), replan_diff backs the replan-
    authorization gate (gates.replan_authorization_blockers). Without this,
    the replan_diff gate would have no reachable escape at all: the same
    disabled/uninstalled-hook brick this command exists to prevent for
    approval would apply to every non-substantive replan on such a machine.
    """
    state = _require(store, args.session)
    kind = getattr(args, "kind", None) or PLAN_PRESENTATION_KIND_ESSENCE
    if kind not in PLAN_PRESENTATION_KINDS:
        return Directive(
            False, state.node, "noop",
            f"unknown presentation kind {kind!r}; expected one of {PLAN_PRESENTATION_KINDS}",
        )
    receipt = gates._plan_presentation_for(state, kind)
    if receipt is None:
        return Directive(
            False, state.node, "noop",
            f"no {kind} presentation receipt exists yet — run present-plan "
            f"--kind {kind} before confirm-delivery has anything to bind to",
        )
    by = (getattr(args, "by", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    escape_reason = (getattr(args, "escape_reason", "") or "").strip()
    missing = []
    if not by:
        missing.append("--by")
    if not note:
        missing.append("--note")
    if not escape_reason:
        missing.append("--escape-reason")
    if missing:
        return Directive(
            False, state.node, "noop",
            f"confirm-delivery requires {' and '.join(missing)} (a named actor, "
            "a typed reason from a closed set, and a stated explanation for the "
            "manual override)",
        )
    if escape_reason not in delivery.DELIVERY_ESCAPE_REASONS:
        # Refused in the body rather than by argparse `choices=`: the enum is
        # also enforced when a caller builds the namespace directly, and the
        # refusal joins the aggregate above instead of exiting the process.
        return Directive(
            False, state.node, "noop",
            f"--escape-reason {escape_reason!r} is not one of "
            f"{', '.join(delivery.DELIVERY_ESCAPE_REASONS)} — an untyped reason "
            "is exactly what makes escapes uncountable",
        )
    if by.lower() == delivery.SOURCE_HOOK:
        return Directive(
            False, state.node, "noop",
            "--by hook is reserved for the automated delivery hook; a human "
            "override must name an actual person",
        )
    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    if state_file is None:
        return Directive(
            False, state.node, "noop",
            "cannot resolve this session's state file — nowhere to write the "
            "delivery stamp sidecar",
        )
    stamp = delivery.DeliveryStamp(
        plan_path=receipt.plan_path,
        plan_sha256=receipt.plan_sha256,
        rendering_sha256=receipt.rendering_sha256,
        verified_ts=time.time(),
        source=delivery.SOURCE_OVERRIDE,
        by=by,
        note=note,
        escape_reason=escape_reason,
    )
    delivery.write_stamp(state_file, stamp)
    state.log("confirm_delivery", by=by, note=note, escape_reason=escape_reason, kind=kind)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"delivery override recorded by {by!r}; the {kind} presentation's gate "
        "is now satisfied for this receipt",
    )


def _note_round_release(state, review_blockers, store: StateStore) -> dict | None:
    """The round-release payload for a refusal, plus its once-per-round telemetry event.

    Shared by every command whose refusal can carry the release — approve, plan-review
    and replan — because the valve firing is only useful if the coordinator reading THAT
    refusal sees it. The log event is the metric this valve's reachability was measured
    with, so a command that surfaces the release but never logs it would leave the metric
    reading 0 after the fix. Deduped on the round number: the same round can be refused
    many times, and each refusal must not add a fresh event.

    Checks the SOLO plan-review axis OR the combined cross-axis ceiling (item A):
    `gates.plan_review_blockers` substitutes the release message into its own
    returned blockers on EITHER condition, so a guard that only checked the solo
    axis would miss a cross-axis-only release and report None here while the
    caller's blockers already carry the substituted message. The payload carries
    `rounds` (the counter the valve actually read, i.e. the larger of the two),
    `review_rounds` (the task-level spend) and the `threshold` it fired against.

    Returns None when neither valve is active, which is also the payload callers put
    on the Directive — an explicit "no release here" rather than a missing key.
    """
    if not (
        review_blockers
        and (gates.plan_review_round_release_active(state) or gates.cross_axis_friction_release_active(state))
    ):
        return None
    rounds = gates._PLAN_REVIEW_ROUND_COUNTER.value(state)
    already_logged = any(
        e.get("event") == "plan_review_round_release" and e.get("rounds") == rounds
        for e in state.history
    )
    if not already_logged:
        state.log("plan_review_round_release", rounds=rounds, review_rounds=state.review_rounds,
                  threshold=Thresholds().effort_replan_absolute())
        store.save(state)
    return {"rounds": rounds, "review_rounds": state.review_rounds,
            "threshold": Thresholds().effort_replan_absolute()}


def _append_escalation_ledger(rows: list[dict]) -> None:
    """Append condition-4 gap-ledger lines. Fail-open like _write_quality_row: a
    ledger I/O error never blocks the review record that already happened. The path
    is resolved per call so a test (or operator) override takes effect immediately."""
    if not rows:
        return
    path = Path(os.environ.get(ESCALATION_LEDGER_ENV) or DEFAULT_ESCALATION_LEDGER)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _condition4_ledger_rows(
    state: SessionState, doc: PlanDoc, target: str, review: PlanPairReview,
    concerns: list[str], outcome: str,
) -> list[dict]:
    marker = CONDITION_MARKERS[3]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    return [
        {
            "ts": now,
            "session": state.session_id,
            "plan_path": target,
            "task_id": doc.meta.task_id,
            "unit": review.pair,
            "dependency_stage": review.service,
            "condition": marker.rstrip(":"),
            "concern": concern,
            "concern_sha256": hashlib.sha256(concern.encode("utf-8")).hexdigest(),
            "outcome": outcome,
        }
        for concern in concerns
        if concern.lstrip().startswith(marker)
    ]


@dataclass
class _ConcernPlan:
    """What the severity rules made of one review record's concerns, before it is stored."""

    parsed: list
    parts: list
    effective: list
    verdict: str
    part_digests: dict
    regression_command: str

    @property
    def bodies(self) -> list[str]:
        return [pc.body for pc in self.parsed]

    @property
    def severities(self) -> list[str]:
        return [pc.severity for pc in self.parsed]

    @property
    def downgraded(self) -> bool:
        return any(
            pc.severity == SEVERITY_BLOCKING and eff != gates.EFFECTIVE_BLOCKING
            for pc, eff in zip(self.parsed, self.effective)
        )


def _ledger_scope(scope: str) -> str:
    index = plan_review_scope_stage_index(scope) if scope else None
    return plan_review_scope_for_stage(index) if index is not None else scope


def _parse_review_concerns(state: SessionState, raw: list[str], verdict: str):
    """Every `--concern` split into severity / restated id / body, or the refusal text.
    Untagged, `blocking:` on a pass, and a `re:<id>` naming no ledger concern all refuse."""
    parsed = []
    for text in raw:
        try:
            pc = parse_concern(text)
        except ConcernFormatError as e:
            return None, str(e)
        if pc.severity == SEVERITY_BLOCKING and verdict == gates._PLAN_REVIEW_PASS:
            return None, (
                f"a `pass` review cannot carry a blocking concern ({text[:80]!r}): "
                f"write it as `{SEVERITY_NOTE}: ...` or record a `revise`"
            )
        if pc.restates and pc.restates not in state.concern_ledger:
            return None, (
                f"`re:{pc.restates}` names no recorded concern: a restatement cites the "
                "stable id of an earlier concern (`data.concern_ids` of its plan-review record)"
            )
        parsed.append(pc)
    return parsed, ""


def _concern_parts(body: str, own_parts) -> list[str]:
    token = gates._concern_part_token(body)
    if token is None:
        return list(own_parts)
    index = plan_review_scope_stage_index(token)
    return [plan_review_scope_for_stage(index) if index is not None else token]


def _plan_concern_severity(
    state: SessionState, doc, ledger_scope: str, own_parts, prev, parsed, verdict: str, *,
    attested: bool, regression_command: str, evidence: bool, may_promote: bool,
) -> _ConcernPlan:
    digests = part_digest_map(doc) if doc is not None else {}
    prev_digests = prev.part_digests if prev is not None else {}
    open_parts = {
        p for e in state.concern_ledger.values()
        if e.scope == ledger_scope and e.status == CONCERN_OPEN
        and not gates._concern_discharged(e.scope, e.local_id, e.text, state, doc)
        for p in e.parts
    }
    parts_by_concern, effective = [], []
    for pc in parsed:
        parts = _concern_parts(pc.body, own_parts or tuple(digests))
        changed = {p for p in parts if digests.get(p) is None or prev_digests.get(p) != digests[p]}
        target = state.concern_ledger.get(pc.restates) if pc.restates else None
        dispositioned = target is not None and (
            target.status == CONCERN_SUPERSEDED
            or gates._concern_discharged(target.scope, target.local_id, target.text, state, doc)
        )
        differs = bool(regression_command) and (
            target is None or regression_command not in target.regression_commands
        )
        parts_by_concern.append(parts)
        effective.append(gates.effective_concern_severity(
            pc.severity, parts, has_prev=prev is not None, changed=changed,
            open_parts=open_parts, restates_dispositioned=dispositioned,
            new_evidence=evidence or differs,
        ))
    effective_verdict = verdict
    if (verdict == gates._PLAN_REVIEW_REVISE and parsed and attested and may_promote
            and gates.EFFECTIVE_BLOCKING not in effective):
        reraised = _blocking_parts(parsed, parts_by_concern)
        still_open = [
            e for e in state.concern_ledger.values()
            if e.scope == ledger_scope and e.status == CONCERN_OPEN
            and not gates._concern_discharged(e.scope, e.local_id, e.text, state, doc)
            and _entry_stays_open(e, digests, reraised)
        ]
        if not still_open:
            effective_verdict = gates._PLAN_REVIEW_PASS
    return _ConcernPlan(parsed, parts_by_concern, effective, effective_verdict, digests,
                        regression_command)


def _blocking_parts(parsed, parts_by_concern) -> set[str]:
    return {
        p for pc, parts in zip(parsed, parts_by_concern)
        if pc.severity == SEVERITY_BLOCKING for p in parts
    }


def _entry_stays_open(entry: ConcernRecord, digests: dict, reraised: set[str]) -> bool:
    """An open concern survives a record unless a part it names changed since it was
    last raised and the record does not re-raise that part as blocking."""
    moved = any(
        digests.get(p) is None or entry.raise_digests.get(p) != digests[p]
        for p in entry.parts
    )
    return not moved or bool(reraised.intersection(entry.parts))


def _stamp_concern_plan(review, cp: _ConcernPlan, raw_verdict: str) -> None:
    review.concerns = cp.bodies
    review.severities = cp.severities
    review.effective_severities = cp.effective
    review.raw_verdict = raw_verdict
    review.part_digests = cp.part_digests
    review.verdict = cp.verdict


def _commit_concern_ledger(state: SessionState, review, cp: _ConcernPlan, ledger_scope: str,
                           local_ids: list[str], seq: int, *, record_only: bool = False) -> list[str]:
    """Close the scope's open concerns this record settles, register this record's own,
    and return their stable ids. A pass/override supersedes every open concern; any other
    record fixes one whose part changed since it was raised and that this record does not
    re-raise, and refreshes the raise digests of one it does re-raise. A concern outside the
    reviewer's own scope never opens. `record_only` registers the concerns as recorded
    without touching the scope's open ones — for a record that does not become the scope's
    authoritative review."""
    settles_all = review.verdict in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_OVERRIDE)
    reraised = _blocking_parts(cp.parsed, cp.parts)
    out_of_scope = set(getattr(review, "out_of_scope_concern_ids", []))
    for entry in state.concern_ledger.values():
        if record_only or entry.scope != ledger_scope or entry.status != CONCERN_OPEN:
            continue
        if settles_all:
            entry.status = CONCERN_SUPERSEDED
        elif _entry_stays_open(entry, cp.part_digests, reraised):
            if reraised.intersection(entry.parts):
                entry.raise_digests = {p: cp.part_digests.get(p, "") for p in entry.parts}
        else:
            entry.status = CONCERN_FIXED
    prefix = plan_review_pair_scope(ledger_scope) or ledger_scope or "plan"
    ids = []
    for i, (pc, parts, eff) in enumerate(zip(cp.parsed, cp.parts, cp.effective)):
        cid = f"{prefix}#{seq}.c{i}"
        ids.append(cid)
        opens = (eff == gates.EFFECTIVE_BLOCKING and not settles_all and not record_only
                 and local_ids[i] not in out_of_scope)
        state.concern_ledger[cid] = ConcernRecord(
            id=cid, scope=ledger_scope, plan_path=review.plan_path, local_id=local_ids[i],
            text=pc.body, severity=pc.severity, effective=eff, parts=list(parts),
            raise_digests={p: cp.part_digests.get(p, "") for p in parts}, record_seq=seq,
            status=CONCERN_OPEN if opens else CONCERN_RECORDED,
            regression_commands=[cp.regression_command] if cp.regression_command else [],
        )
        target = state.concern_ledger.get(pc.restates) if pc.restates else None
        if target is not None and cp.regression_command \
                and cp.regression_command not in target.regression_commands:
            target.regression_commands.append(cp.regression_command)
    return ids


def _log_concerns_downgraded(state: SessionState, cp: _ConcernPlan, scope: str, raw_verdict: str) -> None:
    if cp.downgraded or cp.verdict != raw_verdict:
        state.log("plan_review_concerns_downgraded", scope=scope, raw_verdict=raw_verdict,
                  verdict=cp.verdict, severities=cp.severities, effective=cp.effective)


def _cmd_plan_review_pair(
    args, state: SessionState, target: str, pair: str, *,
    store: StateStore, runner: Runner | None,
) -> Directive:
    """The `--scope topo:<pair>` branch of cmd_plan_review.

    Branches off BEFORE the whole-plan / stage terminal-pass machinery on purpose: a
    pair record is judged only against a CURRENT same-pair record (gates.pair_status),
    never against the nearest whole-plan pass, and it is written only to
    state.plan_pair_reviews — never to plan_review, plan_review_passes or
    plan_stage_reviews, and it never advances plan_review_rounds (it advances the
    task's review_rounds, once per plan version — see _count_review_round). The binding is
    recomputed here from a FRESH load of the evaluated plan, never taken from node
    files materialized at spawn time or from state.stages."""
    def refuse(msg: str) -> Directive:
        return Directive(False, state.node, "noop", msg)

    unsupported = [flag for flag, dest in (("--concern-id", "concern_ids"),
                                           ("--findings-blocking", "findings_blocking"),
                                           ("--findings-nonblocking", "findings_nonblocking"))
                   if getattr(args, dest, None) is not None]
    if unsupported:
        return refuse(
            f"not recorded for a 'topo:' scope: {', '.join(unsupported)} — a pair "
            "concern is identified by its condition marker, and risk-accept does not "
            "take a pair scope"
        )
    try:
        doc, _, live = load_plan_with_digest(target)
        parse_pair(doc, pair)
    except (OSError, PlanError, ValueError) as e:
        return refuse(f"--scope 'topo:{pair}' cannot be recorded against {target}: {e}")
    verdict = args.verdict
    parsed, parse_error = _parse_review_concerns(state, list(getattr(args, "concerns", None) or []), verdict)
    if parsed is None:
        return refuse(parse_error)
    reviewer = (getattr(args, "reviewer", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    attested = (getattr(args, "plan_digest", None) or "").strip().lower()
    if verdict == gates._PLAN_REVIEW_PASS and not attested:
        return refuse(
            f"a pass for pair {pair!r} needs --plan-digest: a reviewer that cannot attest "
            "the plan bytes it read cannot bind a pair"
        )
    if attested and attested != live:
        return refuse(
            f"--plan-digest {attested!r} does not match a fresh load of {target!r} "
            f"({live!r}): the reviewer read a different or stale plan; re-read the "
            "current plan and re-run plan-review"
        )
    prev = state.plan_pair_reviews.get(pair)
    if prev is not None and prev.plan_path != target:
        prev = None
    if verdict == gates._PLAN_REVIEW_OVERRIDE:
        if not reviewer or not note:
            return refuse("a pair override needs a non-empty --reviewer and --note")
        refusal = _plan_review_override_refusal(state, prev, reviewer, doc.meta.order)
        if refusal:
            return refuse(refusal)
    regression_command = (getattr(args, "regression_command", "") or "").strip()
    regression_exit: int | None = None
    if verdict == gates._PLAN_REVIEW_REVISE and gates.pair_status(state, doc, target, pair) == "current":
        if not regression_command:
            return refuse(
                f"a current pass for pair {pair!r} is overturned only by run-demonstrated "
                "regression evidence: re-run with --regression-command that exits non-zero"
            )
        try:
            _, result = _run_check(regression_command, 0, runner, cwd=state.repo_root)
            regression_exit = result.returncode if result is not None else None
        except Exception:  # noqa: BLE001 - a broken command is no evidence
            regression_exit = None
        if not regression_exit:
            return refuse(
                f"--regression-command did not demonstrate a regression (exit "
                f"{regression_exit!r}): the current pass for pair {pair!r} stands"
            )
    base, service = parse_pair(doc, pair)
    ledger_scope = plan_review_scope_for_pair(pair)
    cp = _plan_concern_severity(
        state, doc, ledger_scope, pair_part_tokens(pair), state.plan_pair_reviews.get(pair),
        parsed, verdict, attested=bool(attested), regression_command=regression_command,
        evidence=bool(regression_exit), may_promote=True,
    )
    concerns = cp.bodies
    review = PlanPairReview(
        pair=pair, base=base, service=service, verdict=verdict, reviewer=reviewer,
        concerns=concerns, note=note, plan_path=target, plan_sha256=attested,
        record_seq=_next_record_seq(state), **pair_binding(doc, pair),
    )
    _stamp_concern_plan(review, cp, verdict)
    review.concern_ids = _commit_concern_ledger(
        state, review, cp, ledger_scope, [f"c{i}" for i in range(len(concerns))], review.record_seq)
    ledger = []
    if review.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, review, concerns, "confirmed-gap")
    elif verdict == gates._PLAN_REVIEW_OVERRIDE and prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, prev, prev.concerns, "false-alarm")
    state.plan_pair_reviews[pair] = review
    if _is_counted_review(review.verdict, reviewer):
        _count_review_round(state, live)
    _log_concerns_downgraded(state, cp, ledger_scope, verdict)
    state.log("plan_pair_review", target=target, pair=pair, verdict=review.verdict,
              raw_verdict=verdict, reviewer=reviewer,
              plan_sha256=attested, concerns=concerns, note=note,
              regression_command=regression_command, regression_exit=regression_exit,
              ledger_lines=len(ledger))
    store.save(state)
    _append_escalation_ledger(ledger)
    status = gates.pair_status(state, doc, target, pair)
    return Directive(
        True, state.node, "continue",
        f"pair review recorded for {pair!r} against {target} (verdict={verdict}); "
        f"pair status: {status}",
        data={"pair": pair, "pair_status": status, "ledger_lines": len(ledger),
              "concern_ids": review.concern_ids},
    )


TOPOLOGICAL_COMPOSITION_REVIEWER = "topological-composition"


def _is_counted_review(verdict: str, reviewer: str) -> bool:
    """A reviewer's pass/revise is a review round; an override is a decision, and the
    engine's own composition record is not a review at all."""
    return (
        verdict in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_REVISE)
        and reviewer != TOPOLOGICAL_COMPOSITION_REVIEWER
    )


def _next_record_seq(state) -> int:
    state.next_record_seq += 1
    return state.next_record_seq


def _stamp_whole_plan_baseline(state, review: PlanReview, doc) -> None:
    """Give a whole-plan record its place in the review-record order and, when its
    plan loads, the per-pair binding hashes the walk-stale set is later measured
    against. A plan whose pairs cannot be enumerated leaves the bindings unset,
    which the gate reads as every pair being stale."""
    review.record_seq = _next_record_seq(state)
    if doc is None:
        return
    try:
        review.reviewed_pair_bindings = gates.pair_baseline_bindings(doc)
    except PlanError:
        review.reviewed_pair_bindings = None


def _count_plan_review_round(state: SessionState, target: str) -> None:
    """The post-approval round count of a whole-plan review (see the comment in
    cmd_plan_review): once per plan VERSION, never for an unreadable plan."""
    if state.approval is not None and state.approval.passed:
        counted = _plan_file_sha256(target)
        if counted and counted != state.plan_review_counted_digest:
            state.plan_review_rounds += 1
            state.plan_review_counted_digest = counted


def cmd_plan_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a thinker review of a plan version, backing the plan-review gate.

    The COGNITION (the thinker's reasoning) happens in the thinker leaf; this only
    records its verdict, bound to the plan file it examined (`--target`, defaulting
    to the session's current plan_path — pass the NEW plan for a replan-time review).
    Purely a recorder, mirroring declare/investigate/critique: gates.
    plan_review_blockers enforces bind/verdict at approve/replan, so an incomplete
    override recorded here simply fails to clear the gate rather than erroring.

    The reviewer must pass --plan-digest <hex> (the sha256 of its OWN read of the
    plan); it is cross-checked against the live bytes and stored as the attested
    plan_sha256. A passing verdict does NOT bind without a matching attestation,
    and an attestation the engine cannot cross-check — because the target is
    unreadable — is REFUSED rather than stored on the caller's word.

    --scope 'stage:<n>' binds the review to one stage instead of the whole plan
    (stage 5): the record also carries the engine's OWN digests of the plan's
    meta and per-stage parts at this moment (plan_meta_digest/plan_stage_digests),
    which gates.plan_review_blockers compares against the live plan via
    plan.changed_parts to decide whether this review still covers what it once
    covered. Recording those digests requires a LOADABLE plan — a whole-plan
    review degrades gracefully to a digest-less record on a parse failure (still
    useful as a path/content-hash-bound record), but a stage-scoped review REFUSES:
    it cannot confirm the named stage even exists without parsing the plan."""
    state = _require(store, args.session)
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to review: submit a plan first, or pass --target <plan.toml>",
        )
    scope = (getattr(args, "scope", None) or "").strip()
    pair = plan_review_pair_scope(scope)
    if pair is not None:
        return _cmd_plan_review_pair(args, state, target, pair, store=store, runner=runner)
    doc = None
    parse_error = None
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        parse_error = str(e)
    if scope:
        stage_index = plan_review_scope_stage_index(scope)
        if stage_index is None:
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r} is not a recognized scope (expected 'stage:<n>')",
            )
        if doc is None:
            return Directive(
                False, state.node, "noop",
                f"cannot validate --scope {scope!r}: {target} failed to load: {parse_error}",
            )
        if not any(s.index == stage_index for s in doc.stages):
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r}: no stage {stage_index} in {target}",
            )
    parsed, parse_error = _parse_review_concerns(state, list(getattr(args, "concerns", None) or []), args.verdict)
    if parsed is None:
        return Directive(False, state.node, "noop", parse_error)
    # An override is the USER's escape from a reviewer's `revise` deadlock — the
    # reviewer who issued the blocking verdict cannot override themselves. Checked
    # here, before the record is overwritten and the prior reviewer's identity lost.
    # Scope-aware: an override of a STAGE-scoped revise compares against the prior
    # review of that SAME scope, never against the whole-plan record.
    if args.verdict == gates._PLAN_REVIEW_OVERRIDE:
        prev = state.plan_stage_reviews.get(scope) if scope else state.plan_review
        if prev is not None and prev.plan_path != target:
            prev = None
        # Mirrors cmd_accept's author/customer_id check; degrades to a pass-through
        # when the plan has no [meta.order] or an empty customer_id, same as cmd_accept.
        refusal = _plan_review_override_refusal(
            state, prev, (getattr(args, "reviewer", "") or "").strip(),
            doc.meta.order if doc is not None else None,
        )
        if refusal:
            return Directive(False, state.node, "noop", refusal)
    # --plan-digest is the sha256 the REVIEWER computed from its OWN read of the
    # target plan file. Cross-check it against the engine's live digest and REFUSE
    # to record on mismatch (a reviewer that read a different/stale file must not
    # bind a pass). The matching digest becomes PlanReview.plan_sha256 — the field
    # is now REVIEWER-attested, NOT engine-auto-computed. CONTRACT INVERSION: an
    # ABSENT --plan-digest yields plan_sha256="" (unattested); the pass path of
    # gates.plan_review_blockers then BLOCKS on the empty hash (see the inversion
    # note there), so a reviewer that could not read the plan cannot bind a pass.
    #
    # An UNREADABLE target refuses too, because an attestation the engine cannot
    # cross-check is not an attestation: the stored plan_sha256 is what
    # gates._binds_across_path_change accepts as proof a review of some OTHER path
    # examined this plan's bytes, so recording an unverified one lets any caller
    # bind a plan by naming a path nobody read (#195). The absent-digest case keeps
    # its fail-open degradation — nothing was claimed, so nothing needs checking.
    attested = (getattr(args, "plan_digest", None) or "").strip().lower()
    if attested:
        live = _plan_file_sha256(target)
        if not live:
            return Directive(
                False, state.node, "noop",
                f"--plan-digest {attested!r} cannot be cross-checked: {target!r} is "
                "unreadable, so the engine cannot confirm the reviewer read it; "
                "re-run plan-review once the plan file is readable (or omit "
                "--plan-digest to record an unattested review that does not bind)",
            )
        if attested != live:
            return Directive(
                False, state.node, "noop",
                f"--plan-digest {attested!r} does not match the live plan bytes "
                f"({live!r}) at {target!r}: the reviewer read a different or stale "
                "plan; re-read the current plan and re-run plan-review",
            )
    # Terminal-pass check: a `revise` arriving after a PASS already recorded
    # for this scope this approval cycle only overturns it with run-demonstrated
    # regression evidence — see gates.plan_review_prior_pass /
    # _plan_review_regression_evidence. `prior_pass` is history within the
    # cycle, not "the current authoritative record": it stays found even after
    # a later evidenced revise supersedes it, because the requirement is "a
    # pass has been recorded this cycle", not "the most recent verdict is one".
    prior_pass = gates.plan_review_prior_pass(state, scope, target)
    is_post_pass_revise = args.verdict == gates._PLAN_REVIEW_REVISE and prior_pass is not None
    regression_command = (getattr(args, "regression_command", "") or "").strip()
    regression_exit: int | None = None
    regression_command_error: str | None = None
    if is_post_pass_revise and regression_command:
        try:
            _, result = _run_check(regression_command, 0, runner, cwd=state.repo_root)
            regression_exit = result.returncode if result is not None else None
        except Exception as exc:  # noqa: BLE001 - never let a broken regression command crash recording
            regression_exit = None
            regression_command_error = str(exc)
    concerns = [pc.body for pc in parsed]
    review = PlanReview(
        plan_path=target,
        verdict=args.verdict,
        reviewer=getattr(args, "reviewer", "") or "",
        concerns=concerns,
        note=getattr(args, "note", "") or "",
        plan_sha256=attested,
        scope=scope,
        reviewed_meta_digest=plan_meta_digest(doc) if doc is not None else "",
        reviewed_stage_keys=(
            {str(k): v for k, v in plan_stage_digests(doc).items()} if doc is not None else {}
        ),
        concern_ids=list(getattr(args, "concern_ids", None) or []),
        regression_command=regression_command if is_post_pass_revise else "",
        regression_exit=regression_exit if is_post_pass_revise else None,
        remedy_tags=[gates._remedy_tag_for_concern(c) for c in concerns],
    )
    review.in_scope_concern_ids, review.out_of_scope_concern_ids = gates.classify_concerns(
        scope, plan_review_concern_ids(review), review.concerns
    )
    if not scope:
        _stamp_whole_plan_baseline(state, review, doc)
    else:
        review.record_seq = _next_record_seq(state)
    evidenced = is_post_pass_revise and gates._plan_review_regression_evidence(prior_pass, review, doc)
    ledger_scope = _ledger_scope(scope)
    concern_plan = _plan_concern_severity(
        state, doc, ledger_scope, (ledger_scope,) if scope else (),
        state.plan_stage_reviews.get(scope) if scope else state.plan_review, parsed, args.verdict,
        attested=bool(attested), regression_command=regression_command,
        evidence=bool(evidenced), may_promote=not (is_post_pass_revise and not evidenced),
    )
    _stamp_concern_plan(review, concern_plan, args.verdict)
    unevidenced = is_post_pass_revise and not evidenced
    review.stable_ids = _commit_concern_ledger(
        state, review, concern_plan, ledger_scope, plan_review_concern_ids(review),
        review.record_seq, record_only=unevidenced)
    if unevidenced:
        # The prior PASS stays authoritative — plan_review_passes is never
        # touched here, so gates.plan_review_prior_pass keeps reporting it as
        # found — meaning THIS revise does not reopen the gate by itself
        # (requirement (a)). Recorded as a non-blocking note instead
        # (requirement (b)) — logged under its own event name, with the
        # concern text surfaced verbatim in the Directive so the user (not
        # another thinker round) decides override vs. editing the plan.
        #
        # That is not the same as the gate being clear: the plan may have been
        # EDITED since the pass, which stales its coverage independently of
        # this revise (gates._plan_review_blockers_coverage). Reporting
        # ok/continue while gates.plan_review_blockers is still non-empty would
        # claim the pass is "in force" when it no longer covers the current
        # plan — so the real blockers are computed below and, if non-empty,
        # returned instead of the swallowed-note continue.
        #
        # Whether plan_review/plan_stage_reviews[scope] gets overwritten with
        # THIS revise is conditional, not "never": it matters only when this
        # scope's OWN part actually moved since the prior pass, because that
        # is the one case _plan_review_blockers_coverage will go looking for a
        # record at this scope next — leaving nothing there would make
        # risk-accept/override at this scope find nothing to discharge
        # (gates.plan_review_scope_moved_since_pass's docstring). An unmoved
        # scope has no such lookup coming, so the prior pass stays the sole
        # record there (test_stage_scoped_revise_after_whole_plan_pass_is_terminal).
        remedy_cut = review.remedy_tags.count("cut")
        remedy_add = review.remedy_tags.count("add")
        if _is_counted_review(args.verdict, review.reviewer):
            _count_review_round(state, review.plan_sha256 or _plan_file_sha256(target), thinker_record=True)
        if gates.plan_review_scope_moved_since_pass(prior_pass, doc, scope):
            if scope:
                state.plan_stage_reviews[scope] = review
            else:
                state.plan_review = review
        state.log("plan_review_post_pass_unevidenced", target=target, scope=scope,
                   reviewer=review.reviewer, concerns=review.concerns, note=review.note,
                   regression_command=review.regression_command,
                   regression_exit=review.regression_exit,
                   regression_command_error=regression_command_error,
                   remedy_cut=remedy_cut, remedy_add=remedy_add)
        store.save(state)
        concern_text = "; ".join(review.concerns) if review.concerns else "(no concern text given)"
        data = {
            "plan_review_post_pass_unevidenced": True,
            "prior_pass_reviewer": prior_pass.reviewer,
            "concerns": review.concerns,
            "concern_ids": review.stable_ids,
            "remedy_tags": review.remedy_tags,
            "regression_command_error": regression_command_error,
        }
        real_blockers = gates.plan_review_blockers(state, target)
        if real_blockers:
            round_release = _note_round_release(state, real_blockers, store)
            return Directive(
                False, state.node, "plan_review",
                f"thinker revise recorded for {target} at scope {scope!r} does not "
                "overturn the prior PASS (no run-demonstrated regression evidence), "
                "and the plan-review gate is still blocked on its own terms — the "
                "pass is stale, not in force "
                f"(concern raised: {concern_text!r}). This is not another thinker "
                "round: the decision is the user's — override (plan-review "
                "--verdict override --reviewer <you> --note <why it is acceptable>), "
                "or edit the plan as the concern proposes (cut or add).",
                data={**data, "blockers": real_blockers, "plan_review_round_release": round_release},
            )
        return Directive(
            True, state.node, "continue",
            f"thinker revise recorded for {target} at scope {scope!r} but does not "
            "overturn the whole-plan/stage PASS already recorded this approval cycle "
            "(a recorded pass is terminal without run-demonstrated regression "
            "evidence — a --regression-command that actually exits non-zero — naming "
            "a part changed since that pass). "
            f"Concern raised: {concern_text!r}. This is a non-blocking note, not another "
            "thinker round: the decision is the user's — override "
            "(plan-review --verdict override --reviewer <you> --note <why it is "
            "acceptable>), or edit the plan as the concern proposes (cut or add).",
            data=data,
        )
    _log_concerns_downgraded(state, concern_plan, scope, args.verdict)
    if scope:
        state.plan_stage_reviews[scope] = review
    else:
        state.plan_review = review
    if review.verdict == gates._PLAN_REVIEW_PASS and review.plan_sha256:
        # Attested pass only — an unattested one never clears the gate (see the
        # CONTRACT INVERSION note on the pass path of _plan_review_verdict_blockers),
        # so it must not become a terminal pass either.
        state.plan_review_passes[scope] = review
    _record_first_thinker_verdict(state, doc, review, scope)
    # POST-APPROVAL round counting. cmd_submit_plan's increment covers only the
    # pre-approval resubmission loop; review cycles overwhelmingly recur AFTER
    # approval, on the `replan` path, where the same thinker review is demanded and
    # nothing advanced the counter — leaving the round-release valve unreachable
    # exactly where it is needed. The two increments are disjoint in time, not by
    # convention: cmd_submit_plan sets approval.passed = False BEFORE its own
    # increment, so no single call can satisfy both conditions.
    #
    # The unit is a plan VERSION, not a verdict — see plan_review_counted_digest's
    # field comment in state.py for why counting verdicts would fire the release
    # inside a legitimate stage-coverage pass and retire reviews nobody performed.
    #
    # An UNREADABLE plan does not count and leaves the marker untouched. That is the
    # conservative direction here even though over-counting is the usual fail-safe:
    # because the release SUBSTITUTES the outstanding blockers rather than adding to
    # them, an over-count can cancel a review requirement nobody satisfied, whereas an
    # under-count only leaves the user's existing one-sentence override as the exit.
    #
    # Placed BEFORE the blockers call so the verdict that exhausts the budget surfaces
    # the release in its own Directive, rather than one round later.
    _count_plan_review_round(state, target)
    if _is_counted_review(review.verdict, review.reviewer):
        _count_review_round(state, review.plan_sha256 or _plan_file_sha256(target), thinker_record=True)
    blockers =gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("plan_review", target=target, verdict=review.verdict, raw_verdict=args.verdict,
              scope=scope,
              reviewer=review.reviewer,
              reviewer_raw=review.reviewer,
              reviewer_token=_normalize_reviewer_token(review.reviewer),
              plan_sha256=review.plan_sha256,
              plan_bytes=_plan_file_bytes(target),
              concerns=review.concerns,
              note=review.note,
              findings_blocking=getattr(args, "findings_blocking", None),
              findings_nonblocking=getattr(args, "findings_nonblocking", None),
              remedy_cut=review.remedy_tags.count("cut"),
              remedy_add=review.remedy_tags.count("add"),
              regression_command=review.regression_command,
              regression_exit=review.regression_exit,
              regression_command_error=regression_command_error)
    store.save(state)
    if blockers:
        round_release = _note_round_release(state, blockers, store)
        return Directive(
            False, state.node, "plan_review",
            "thinker review recorded but does not clear the gate",
            data={
                "blockers": blockers,
                "concern_ids": review.stable_ids,
                "plan_review_round_release": round_release,
                "regression_command_error": regression_command_error,
            },
        )
    out_of_scope_note = ""
    if review.out_of_scope_concern_ids:
        out_of_scope_note = (
            f" ({len(review.out_of_scope_concern_ids)} out-of-scope findings "
            "recorded, not blocking)"
        )
    return Directive(
        True, state.node, "continue",
        f"thinker review recorded for {target} (verdict={review.verdict}); "
        "the plan-review gate is now satisfied for this plan version" + out_of_scope_note,
        data={"concern_ids": review.stable_ids},
    )


def cmd_risk_accept(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a customer-facing acceptance of ONE named PlanReview concern's risk —
    the alternative to editing the plan to make a `revise` concern go away. Purely a
    recorder, mirroring cmd_plan_review: gates.plan_review_blockers re-derives
    discharge itself at approve/replan by reading state.risk_acceptances, so a
    mis-bound acceptance recorded here simply fails to clear the gate rather than
    erroring.

    `--basis`/`--risk` mirror premise.py's `assumed` question disposition exactly:
    both are required free text, and neither may be a bare placeholder (see
    gates._PLACEHOLDER_SET). Bound to the plan version at record time via the SAME
    meta/stage-digest snapshot a PlanReview itself carries — gates._risk_acceptance_stale
    reads them via plan.changed_parts identically."""
    state = _require(store, args.session)
    target = state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to accept a risk against: submit a plan first",
        )
    scope = (getattr(args, "scope", None) or "").strip()
    concern_id = (getattr(args, "concern_id", "") or "").strip()
    basis = (getattr(args, "basis", "") or "").strip()
    risk = (getattr(args, "risk", "") or "").strip()
    author = (getattr(args, "author", "") or "").strip()
    missing = [name for name, value in
               (("concern-id", concern_id), ("basis", basis), ("risk", risk), ("author", author))
               if not value]
    if missing:
        return Directive(
            False, state.node, "noop",
            "risk-accept requires a non-empty --" + " and --".join(missing),
        )
    for value, flag in ((basis, "--basis"), (risk, "--risk")):
        if gates._normalize_string(value) in gates._PLACEHOLDER_SET:
            return Directive(
                False, state.node, "noop",
                f"{flag} {value!r} reads as a placeholder, not a reason — say what concretely",
            )
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(
            False, state.node, "noop",
            f"cannot record a risk acceptance: {target} failed to load: {e}",
        )
    if scope:
        stage_index = plan_review_scope_stage_index(scope)
        if stage_index is None:
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r} is not a recognized scope (expected 'stage:<n>')",
            )
        if not any(s.index == stage_index for s in doc.stages):
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r}: no stage {stage_index} in {target}",
            )
    review = state.plan_stage_reviews.get(scope) if scope else state.plan_review
    if review is None:
        return Directive(
            False, state.node, "noop",
            f"no thinker review recorded at scope {scope!r} — nothing there to accept a concern from",
        )
    valid_ids = plan_review_concern_ids(review)
    if concern_id not in valid_ids:
        return Directive(
            False, state.node, "noop",
            f"concern {concern_id!r} is not among scope {scope!r}'s recorded concerns "
            f"{valid_ids!r} — check --concern-id against the review",
        )
    if valid_ids.count(concern_id) > 1:
        return Directive(
            False, state.node, "noop",
            f"concern {concern_id!r} appears {valid_ids.count(concern_id)} times in scope "
            f"{scope!r}'s recorded concerns {valid_ids!r} — ambiguous which one this "
            "acceptance binds to; the review must give each concern a distinct --concern-id",
        )
    acceptance = RiskAcceptance(
        scope=scope,
        concern_id=concern_id,
        plan_path=target,
        basis=basis,
        risk=risk,
        author=author,
        meta_digest=plan_meta_digest(doc),
        stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        concern_text=review.concerns[valid_ids.index(concern_id)],
    )
    state.risk_acceptances.append(acceptance)
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("risk_accept", target=target, scope=scope, concern_id=concern_id,
              author=author, basis=basis, risk=risk)
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "risk acceptance recorded but does not clear the gate",
            data={"blockers": blockers},
        )
    return Directive(
        True, state.node, "continue",
        f"risk acceptance recorded for {target} (scope={scope!r} concern={concern_id!r}); "
        "the plan-review gate is now satisfied for this plan version",
    )


def cmd_plan_review_delta(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: what a reviewer needs to look at in the plan RIGHT NOW, given
    what's already been reviewed — the brief `plan-review` itself does not need,
    but a human/thinker preparing to run it does. Replaces hand-computing this
    from a raw digest dump: `data['stages']` names the moved stages, and the
    Directive's markdown is their actual current rendering via render_stages_md
    (or the whole plan via render_plan_md when a meta/order change, or the
    absence of any prior review, means nothing narrower will do)."""
    state = _require(store, args.session)
    target = getattr(args, "plan", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to diff: submit a plan first, or pass --plan <plan.toml>",
        )
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} failed to load: {e}")
    whole_plan_needed, stage_indices = gates.plan_review_delta(state, doc)
    stages = sorted(stage_indices)
    pairs = gates.pairs_hint_for(state, doc, target)
    if whole_plan_needed:
        md = render_plan_md(doc)
        detail = (
            f"whole-plan review needed for {target}: its meta/order changed since "
            "the last whole-plan review, or none has been recorded yet; the topological "
            "route reviews it pair by pair (plan-review-topological.py, then "
            "plan-review-compose)"
        )
    elif stages:
        md = render_stages_md(doc, stages)
        detail = f"stage-scoped review needed for stage(s) {stages} in {target}"
    else:
        md = ""
        detail = f"no review gap: every part of {target} is covered by its current review"
    if pairs:
        detail += f"; walk-stale pairs — run: plan-review-topological.py --pairs {','.join(pairs)}"
    return Directive(
        True, state.node, "inspect", detail,
        data={"markdown": md, "whole_plan": whole_plan_needed, "stages": stages, "pairs": pairs},
    )


def _walk_target(args, state: SessionState) -> "str | None":
    return getattr(args, "target", None) or state.plan_path


def cmd_plan_review_walk(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: every review pair of the plan in dependency order (base before
    service), with its status, advisory readiness and the commands that spawn its
    reviewer and record its verdict. `--target` walks a plan file other than the
    session's own, computing every digest and depth from it. Writes nothing."""
    state = _require(store, args.session)
    target = _walk_target(args, state)
    if not target:
        return Directive(False, state.node, "noop", "no plan to walk: submit a plan first, or pass --target <plan.toml>")
    try:
        doc = load_plan(target)
        rows = gates.pair_walk(state, doc, target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} cannot be walked: {e}")
    quoted = shlex.quote(target)
    levels: list[list[dict]] = []
    for row in rows:
        while len(levels) <= row["level"]:
            levels.append([])
        open_pair = row["status"] not in gates.PAIR_SATISFIED
        levels[row["level"]].append({
            **row,
            "spawn": (f"spawn-specialist.py --kind thinker --plan-brief --review-topo {row['pair']} "
                      f"--plan {quoted}") if open_pair else None,
            "record": (f"agentctl plan-review --session {state.session_id} --target {quoted} "
                       f"--scope topo:{row['pair']} --reviewer thinker --verdict <pass|revise> "
                       "--plan-digest <sha256 of the plan bytes read>") if open_pair else None,
        })
    walk = {
        "plan_path": target,
        "discharges": gates.pair_discharged_stages(state, doc, target),
        "levels": levels,
    }
    if getattr(args, "format", "text") == "json":
        return Directive(True, state.node, "inspect", json.dumps(walk, indent=2), data=walk)
    lines = [f"review pairs of {target} (base before service; readiness is advisory):"]
    for depth, level in enumerate(levels):
        lines.append(f"level {depth}:")
        for row in level:
            waiting = f" waiting on {','.join(row['waiting'])}" if row["waiting"] else ""
            lines.append(f"  {row['pair']}: {row['status']}, {'ready' if row['ready'] else 'not ready'}{waiting}")
    if walk["discharges"]:
        lines.append("discharged by pairs alone: " + ", ".join(walk["discharges"]))
    return Directive(True, state.node, "inspect", "\n".join(lines), data=walk)


def cmd_plan_review_compose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Compose the pair records into one ordinary whole-plan pass, written only when
    EVERY pair of `plan.review_pairs` is current or override against `--target` (the
    session's plan by default). Readiness is not consulted. When every pair is a
    pass (none override-satisfied) the composed record is the first thinker verdict
    the autonomy boundary requires and goes to the order ledger; a compose that
    includes an override pair records nothing there — an override never becomes a
    thinker verdict."""
    state = _require(store, args.session)
    target = _walk_target(args, state)
    if not target:
        return Directive(False, state.node, "noop", "no plan to compose: submit a plan first, or pass --target <plan.toml>")
    try:
        doc, _, live = load_plan_with_digest(target)
        pairs = review_pairs(doc)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop", f"{target} cannot be composed: {e}")
    status = {pid: gates.pair_status(state, doc, target, pid) for pid in pairs}
    failing = {pid: s for pid, s in status.items() if s not in gates.PAIR_SATISFIED}
    if failing:
        listing = ", ".join(f"{pid} ({s})" for pid, s in failing.items())
        return Directive(
            False, state.node, "plan_review",
            f"compose refused for {target}: every review pair must be current or override; "
            f"failing: {listing}",
            data={"plan_path": target, "failing": failing},
        )
    overridden = [pid for pid, s in status.items() if s == "override"]
    review = PlanReview(
        plan_path=target, verdict=gates._PLAN_REVIEW_PASS, reviewer=TOPOLOGICAL_COMPOSITION_REVIEWER,
        note=f"composed from {len(pairs)} pair review(s): {','.join(pairs)}; overridden: {','.join(overridden)}",
        plan_sha256=live,
        reviewed_meta_digest=plan_meta_digest(doc),
        reviewed_stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
    )
    _stamp_whole_plan_baseline(state, review, doc)
    state.plan_review = review
    state.plan_review_passes[""] = review
    if not overridden:
        _record_first_thinker_verdict(state, doc, review, "")
    _count_plan_review_round(state, target)
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("plan_review_compose", target=target, pairs=list(pairs), overridden=overridden,
              plan_sha256=live)
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "composed pass recorded but does not clear the gate",
            data={"blockers": blockers, "plan_path": target},
        )
    return Directive(
        True, state.node, "continue",
        f"composed whole-plan pass recorded for {target} from {len(pairs)} pair review(s); "
        "the plan-review gate is now satisfied for this plan version",
        data={"plan_path": target, "pairs": list(pairs), "overridden": overridden},
    )


def cmd_stage_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a manual review of the active stage's observation, backing the
    acceptance-review judge gate. Mirrors cmd_plan_review — purely a recorder; the
    COGNITION (a human judging, or authoring an override) happens outside. The verdict is
    bound to the observation bytes passed via --observation (defaulting to the stage's
    current observation), so gates.acceptance_review_blockers can reject a drift. The
    automated cheap judge writes an equivalent record inline in record-result; this
    command is the human path (chiefly the override deadlock escape).

    Scope is gates.stage_review_active(state) — the SAME predicate the gate itself
    consumes — not criterion_type. This is a deliberate widening (GitHub issue #145):
    the gate was already broadened past acceptance_review-only stages (Defect 2: control
    compares result with goal at every stage of a SUBSTANTIVE session, see
    cmd_record_result's observation gate), but this escape hatch had not followed, so a
    measurable-criterion stage judge-deadlocked with NO scoped override at all — only the
    session-wide AGENTCTL_STAGE_REVIEW=0 kill switch, which records a strictly weaker,
    unattributed JudgeBypass(kind='killswitch') instead of this command's
    reviewer+note-bound JudgeBypass(kind='override'). Widening the escape to the gate's
    own scope strictly improves auditability; narrowing the gate back down would
    contradict that deliberate broadening instead of resolving the mismatch."""
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to review")
    if not gates.stage_review_active(state):
        return Directive(
            False, state.node, "noop",
            f"the acceptance-judge gate is not active for this session "
            f"(weight_class={state.weight_class}, "
            f"AGENTCTL_STAGE_REVIEW={os.environ.get('AGENTCTL_STAGE_REVIEW', '<unset>')}); "
            "stage-review records an override of that gate and has nothing to override here",
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


def cmd_code_review(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a code-reviewer/human review of the active spawn:developer stage's
    produced code, backing the code-review gate. Mirrors cmd_stage_review — purely a
    recorder; the COGNITION (the code-reviewer specialization judging the diff, or a
    human authoring an override) happens outside. There is no automated judge for
    code review (unlike the acceptance path's fail-open cheap judge) — this command
    is the only path that ever writes a CodeReview. The verdict is bound to a
    caller-supplied --code-ref (hashed via _digest, never recomputed from git — gates.py
    stays pure), so gates.code_review_blockers can reject a drift when record-result
    later supplies a different --code-ref."""
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to review")
    if not stage.needs_control():
        return Directive(
            False, state.node, "noop",
            f"stage {stage.index} is not a spawn:developer stage; code-review applies "
            "only to developer-produced code",
        )
    code_ref = getattr(args, "code_ref", None) or None
    not_checked = getattr(args, "not_checked", None) or ""
    if args.verdict == gates.CODE_REVIEW_VERDICTS[0] and not not_checked:
        # CODE_REVIEW_VERDICTS[0] is "pass" — an approving verdict with no stated
        # axis coverage is exactly the omission verify-right-axis-report-honestly
        # forbids: a reviewer approving without running tests and saying nothing
        # about it (issue #92). Refused, not just noted — nothing is recorded.
        return Directive(
            False, state.node, "rejected",
            "pass verdict requires --not-checked (axes not checked, or 'none')",
        )
    if gates._code_review_for(state, stage.index) is not None:
        # A re-review of a stage already reviewed once — the code-review axis's
        # round-release counter (item A / issue #96), mirroring plan_review_rounds'
        # per-resubmission increment; reset by cmd_approve/cmd_replan alongside it.
        state.code_review_rounds += 1
    _record_code_review(
        state,
        CodeReview(
            stage_index=stage.index,
            verdict=args.verdict,
            reviewer=getattr(args, "reviewer", "") or "",
            concerns=list(getattr(args, "concerns", None) or []),
            note=getattr(args, "note", "") or "",
            code_sha256=_digest(code_ref) if code_ref else "",
            not_checked=not_checked,
        ),
    )
    state.log("code_review", stage=stage.index, verdict=args.verdict,
              reviewer=getattr(args, "reviewer", "") or "", not_checked=not_checked)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"code review recorded for stage {stage.index} (verdict={args.verdict}); "
        "record-result --status passed will re-check the code-review gate against it",
    )


def _parse_verdicts(raw_verdicts: list[str]) -> tuple[list[RequirementVerdict], list[str]]:
    """Parse repeatable ``--verdict '<requirement_id>|<pass|fail>[|<note>]'`` specs into
    typed RequirementVerdict objects. Returns ``(verdicts, errors)`` — a non-empty
    ``errors`` list means the caller must reject with a failing Directive and record
    nothing. Mirrors ``_parse_partition_units``'s pipe-delimited shape and
    ``(parsed, errors)`` return contract.

    Deliberately does NOT cross-check requirement ids against the order here — that
    check is completeness, not parse well-formedness, and belongs to
    ``gates.resolution_blockers`` (which re-reads the order fresh at resolution time
    rather than at write time; see AcceptanceReview's docstring)."""
    verdicts: list[RequirementVerdict] = []
    errors: list[str] = []
    seen: dict[str, int] = {}  # requirement id -> owning position (1-based)
    for pos, spec in enumerate(raw_verdicts, start=1):
        parts = spec.split("|")
        if len(parts) < 2:
            errors.append(
                f"verdict {pos}: expected '<requirement_id>|<pass|fail>[|<note>]', got {spec!r}"
            )
            continue
        req_id = parts[0].strip()
        verdict = parts[1].strip()
        note = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else ""
        if not req_id:
            errors.append(f"verdict {pos}: empty requirement id")
        if verdict not in ("pass", "fail"):
            errors.append(f"verdict {pos}: verdict must be 'pass' or 'fail', got {verdict!r}")
        if req_id in seen:
            errors.append(
                f"verdict {pos}: requirement id {req_id!r} already verdicted at position "
                f"{seen[req_id]} (one verdict per requirement)"
            )
        else:
            seen[req_id] = pos
        verdicts.append(RequirementVerdict(requirement_id=req_id, verdict=verdict, note=note))
    return verdicts, errors


def cmd_accept(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the plan-level AcceptanceReview: the ORDER's customer comparing the
    delivered PRODUCT against every declared requirement, once — the acceptance half
    of Defect 2 (control checks result-against-goal per stage, repeatedly;
    acceptance checks product-against-order, once, and is recorded).

    Author-matched at WRITE time against [meta.order].customer_id (a mismatch is
    refused outright — this is not a gate to degrade past, it is a wrong-person
    writing the record). Completeness (every declared requirement id covered) and
    negative-verdict blocking are deferred to gates.resolution_blockers, which
    re-reads the order fresh rather than trusting what was true at write time.

    Corroboration mirrors the stage-level acceptance path: the cheap fail-open judge
    (advisor.acceptance_judge) is consulted unless --bypass is given; an unreachable
    judge refuses the write and directs the caller to --bypass --bypass-reason rather
    than silently waving the review through. A --bypass is recorded as an
    AcceptanceBypass alongside the AcceptanceReview (never standalone — see
    AcceptanceBypass's docstring for why resolution_blockers never reads it)."""
    state = _require(store, args.session)
    # Guarded exactly like _refresh_venue_fields: an absent or unreadable plan_path is a
    # refusal Directive, never a PlanError escaping the CLI. Acceptance IS the comparison
    # against the order that plan declares, so with no plan there is nothing to record
    # against — and gates.resolution_blockers refuses the same shape from the other side.
    doc = None
    if state.plan_path:
        try:
            doc = load_plan(state.plan_path, strict=False)
        except (OSError, PlanError):
            doc = None
    if doc is None:
        return Directive(
            False, state.node, "noop",
            "cannot read the plan to accept against "
            f"({state.plan_path or 'no plan_path on this session'}); acceptance compares the "
            "delivered product with the order that plan declares",
        )
    order = doc.meta.order
    author = getattr(args, "author", "") or ""
    if order is not None and order.customer_id and author != order.customer_id:
        return Directive(
            False, state.node, "noop",
            f"acceptance author {author!r} does not match order customer_id "
            f"{order.customer_id!r}; record it as the customer of record, or correct --author",
        )
    verdicts, verdict_errors = _parse_verdicts(getattr(args, "verdict", None) or [])
    if verdict_errors:
        return Directive(
            False, state.node, "noop",
            "invalid --verdict argument(s): " + "; ".join(verdict_errors),
            data={"errors": verdict_errors},
        )
    bypass = bool(getattr(args, "bypass", False))
    bypass_reason = getattr(args, "bypass_reason", "") or ""
    note = getattr(args, "note", "") or ""
    if bypass and not bypass_reason:
        return Directive(
            False, state.node, "noop",
            "--bypass requires --bypass-reason (a bypass is a reasoned override, not a shrug)",
        )
    if not verdicts:
        # Not a bypass-only rule: a verdictless review on the ordinary path records that
        # nothing was compared, and resolution's completeness check cannot catch it —
        # `missing` is empty whenever the review omits nothing because the order declares
        # nothing. Refuse at write time, where the emptiness is still visible.
        return Directive(
            False, state.node, "noop",
            "a bypass requires an accompanying AcceptanceReview: supply at least one --verdict"
            if bypass else
            "acceptance requires at least one --verdict: a review with no verdicts compares "
            "nothing against the order",
        )
    judge_reason = "no judge attempted (--bypass)"
    if not bypass:
        expected_text = "; ".join(
            f"{r.id}: {r.text}" if r.text else r.id for r in (order.requirements if order else [])
        )
        observed_text = note or "; ".join(
            f"{v.requirement_id}:{v.verdict}" for v in verdicts
        )
        judge_runner = runner if runner is not None else advisor.subprocess_runner
        verdict, judge_reason = advisor.acceptance_judge(
            observed_text, expected_text, judge_runner, enabled=True,
            timeout=advisor._ACCEPTANCE_JUDGE_TIMEOUT_S,
        )
        if verdict is None:
            return Directive(
                False, state.node, "noop",
                f"acceptance judge unreachable ({judge_reason}); re-run with "
                "--bypass --bypass-reason '<why this acceptance stands without judge "
                "corroboration>'",
                data={"reason": judge_reason},
            )
    state.acceptance_review = AcceptanceReview(
        author=author, verdicts=verdicts, note=note,
        plan_sha256=state.accepted_plan_digest or "",
    )
    if bypass:
        state.acceptance_bypass = AcceptanceBypass(
            reason=bypass_reason, reviewer=author, note=note,
        )
    state.log("accept", author=author, verdicts=len(verdicts), bypass=bypass)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"acceptance review recorded ({len(verdicts)} verdict(s), bypass={bypass}); "
        "resolution will re-check completeness, verdicts, and plan-digest freshness",
    )


def cmd_approve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    # plan_presentation_blockers is fail-open on the RECEIPT side (mirrors
    # plan_review_blockers) but fail-CLOSED on the DELIVERY side: approval — the
    # irreversible act — cannot be recorded without proof the plan actually
    # reached the user, even if no live-turn ask ever fired. Admissible only
    # because confirm-delivery is a reachable, audit-logged escape (gates.py's
    # plan_presentation_blockers docstring has the full justification).
    state = _require(store, args.session)
    # Submission seam (c), BEFORE _log_gate: the coordinator may have edited plan_path in
    # place at plan-mutable PLAN_READY, so the bytes approve is about to attest to have
    # never been through submission validation. Refusing here — as a fix_plan Directive,
    # never a raised PlanError — keeps the gate row out of the log entirely rather than
    # writing a failed plan_approval for a plan that was never really put to the gate; the
    # coordinator fixes the file and re-runs approve from the same node.
    #
    # The whole REFRESH, not only its refusal, now precedes the gate, and that widening is
    # the contract rather than an accident of placement: every plan_approval blocker below
    # — core, plugin, review, presentation — is evaluated against the POST-refresh session,
    # so an in-place PLAN_READY edit can flip a blocker's verdict within a single approve
    # call. That is the intent (the gate must judge the bytes it is about to attest to, not
    # the pre-edit cache); a blocker that must instead see the cache as submitted has no
    # place on this gate. `test_plan_approval_blockers_see_the_refreshed_state` asserts it.
    echo_advice: list[str] = []
    # Entry-point fallback — see cmd_submit_plan's identical comment. cmd_approve is the
    # entry point (_refresh_caches_from_plan_path is a private single-caller helper), so
    # the runner is resolved here and threaded down.
    run = runner if runner is not None else advisor.subprocess_runner
    submission = _refresh_caches_from_plan_path(state, runner=run, advice=echo_advice)
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "cannot approve: the plan at plan_path does not meet submission "
                         "requirements (edit it and re-run approve)",
                         data={"problems": submission})
    # Folded AFTER seam (c)'s refusal above, so a plan that fails submission validation
    # is never folded into and never persisted: the fold's store.save would otherwise
    # write a premise bag for bytes this command is about to reject.
    # Read plan_path's bytes ONCE and derive `_approved_doc`, the digest stamped
    # below (seam (c)) and the snapshot written near the end of this function from
    # that SAME buffer -- three separate reads of the same file (`load_plan`, then
    # `_stamp_accepted_plan_digest`'s own read, then `_snapshot_approved_plan`'s own
    # read) used to leave a window for a concurrent edit to desynchronize the
    # digest, the snapshot and the doc this approval actually validated.
    _approved_doc = None
    _approved_data: bytes | None = None
    _approved_digest: str | None = None
    if state.plan_path:
        try:
            _approved_doc, _approved_data, _approved_digest = load_plan_with_digest(state.plan_path)
        except (OSError, PlanError):
            _approved_doc = None
        if _approved_doc is not None and _fold_enumeration_sidecar(
                state, _approved_doc, state.plan_path):
            # Persist BEFORE the gate is evaluated, not after: the blockers below
            # are computed from the folded bag and name its `qenum-<part>-N`
            # candidates, and this function returns on any blocker WITHOUT reaching
            # its own store.save() — so a fold left in memory would refuse the approve
            # while `question-candidate-dispose --id qenum-meta-1` had nothing to find.
            store.save(state)
    review_blockers = gates.plan_review_blockers(state, state.plan_path)
    by_agent = bool(args.by and args.by.strip().casefold() == AGENT_ACTOR)
    blockers = (
        gates.blockers(state, "plan_approval")
        + plugins.plugin_gate_blockers(state, "plan_approval")
        + review_blockers
    )
    if not by_agent:
        blockers = (
            blockers
            + gates.plan_presentation_blockers(state, state.plan_path)
            + gates.grants_approval_blockers(state, state.plan_path)
        )
    agent_autonomy: dict | None = None
    if not args.by or not args.by.strip():
        blockers = blockers + ["empty approver: --by must name who approved"]
    elif by_agent:
        # The coordinator approves only inside the boundary the user approved for this
        # order (a ledger entry and an eligible verdict on the CURRENT bytes); it never
        # stamps the ledger, so its approval is never a later boundary.
        if _approved_doc is not None and _ledgered_order_key(state) is not None:
            agent_autonomy = _autonomy_for(state, _approved_doc)
        if agent_autonomy is None:
            blockers = blockers + [
                f"--by {args.by!r}: {gates.AUTONOMY_REASON_NO_VERSION} -- approval must be "
                "attributed to the customer"
            ]
        elif not agent_autonomy["eligible"]:
            blockers = blockers + [
                f"--by {args.by!r} refused: outside the approved boundary -- "
                + "; ".join(agent_autonomy["reasons"])
            ]
    _log_gate(state, "plan_approval", blockers, passed=not blockers)
    if blockers:
        # The escape counts ride the REFUSAL specifically: the coordinator reading it
        # is the one person who both can see the number and is about to decide what to
        # do about it — and if the blocker below is the enumeration one, the decision
        # is literally whether to add to that count.
        round_release = _note_round_release(state, review_blockers, store)
        return _with_advisories(
            Directive(False, state.node, "fix_plan", "cannot approve", data={
                "blockers": blockers,
                "enumeration_escapes": _enumeration_escape_counts(state, _approved_doc),
                "plan_review_round_release": round_release,
            }),
            echo_advice)
    # Seam (c)'s stamp, past BOTH of this command's refusals — the submission check above and
    # the plan_approval gate. The refresh helper that owns the seam cannot stamp it: it runs
    # before the gate by contract, so a blocked approve would leave the session carrying a
    # digest for bytes it did not approve.
    if _approved_digest is not None:
        # From the SAME buffer _approved_doc was parsed from, not a fresh re-read
        # of state.plan_path (the former `_stamp_accepted_plan_digest(state,
        # state.plan_path)`).
        state.accepted_plan_digest = _approved_digest
    else:
        # The single-buffer read above failed (OSError/PlanError) -- fall back to
        # the legacy best-effort re-read rather than leaving the digest stale.
        _stamp_accepted_plan_digest(state, state.plan_path)
    effort.arm(state)  # opens the effort-divergence window — see effort.py's ARMED-ONLY
    # Fold this session's review-round counts into the cross-session task accumulator
    # (item B) BEFORE the reset-to-0 below — approval is the reset point, so this is
    # the last moment these session-local counts are readable.
    task_accumulator.add(
        state.task_id, "plan_review_rounds", state.plan_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    task_accumulator.add(
        state.task_id, "code_review_rounds", state.code_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by=args.by)
    state.plan_review_rounds = 0
    # Cleared with the counter, not merely alongside it: the marker is what makes the
    # NEXT post-approval review count as round 1. Left carrying the approved plan's
    # digest, a first replan-time review of that same unedited plan would be read as
    # "already counted" and skipped.
    state.plan_review_counted_digest = ""
    # Reset alongside plan_review_rounds (item A) — approval starts a fresh execution
    # against the newly-approved plan, so friction spent reviewing code under the
    # PRIOR plan version should not count against this one.
    state.code_review_rounds = 0
    # Bind the approved grant set: stage-grants only returns DERIVED grants when a
    # live re-derivation still hashes to this value, so a materialization-layer
    # change after approval can never silently widen what a dispatched stage
    # receives without a fresh approve. None (rather than a stale prior digest) on
    # a plan that failed to load or grants nothing, matching grants_approval_
    # blockers' own fail-open reading of the same two conditions.
    state.approved_grants_sha256 = (
        grants_sha256(_approved_doc) if _approved_doc is not None else None
    )
    if _approved_doc is not None:
        _rekey_runtime_grants(state, _approved_doc)
    # Terminal-pass custody: a fresh approval cycle starts with no pass on
    # record for any scope — the same "current approval cycle" boundary
    # plan_review_rounds resets at, immediately above.
    state.plan_review_passes = {}
    state.node = transition(state.node, "approve")
    # Write from the SAME buffer already read above when available, so the
    # snapshot can never describe bytes other than the ones `_approved_doc` was
    # parsed from and `accepted_plan_digest` was just stamped from; only a session
    # whose single-buffer read failed falls back to `_snapshot_approved_plan`'s
    # own best-effort re-read.
    if _approved_data is not None and _approved_digest is not None:
        snap = _write_plan_snapshot(store, state, _approved_data, _approved_digest)
    else:
        snap = _snapshot_approved_plan(store, state)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    if agent_autonomy is not None:
        approved = order_approvals.latest_user_approved_record(_ledgered_order_key(state))
        frozen = (approved or {}).get("effort_estimate") or {}
        if frozen:
            state.order_effort_frozen = {scale: float(value) for scale, value in frozen.items()}
            effort.rederive(state)
        state.log(
            "approve", by=args.by,
            extra_resources=[e["name"] for e in agent_autonomy["extra_resources"]],
            unresolved_changed_commands=[
                c["source"] for c in agent_autonomy["unresolved_changed_commands"]],
            active_overrides=[
                f"{name}={os.environ[name]}"
                for name in ("AGENTCTL_ORDER_APPROVALS_DIR", "AGENTCTL_TOOL_CONTRACTS")
                if os.environ.get(name)
            ],
        )
    else:
        state.log("approve", by=args.by)
    # REQ4: stamp the order-approvals ledger for the CUSTOMER only -- never
    # AGENT_ACTOR (refused above, before this point is ever reached) and
    # never a non-customer --by (a reviewer, a delegate) whose approval does
    # not carry the customer's own authority to pre-grant resources. Gated on
    # a non-empty customer_id: an order-less plan (customer_id == "") can
    # never casefold-match any --by, so this is a no-op for every plan
    # authored before [meta.order] existed.
    if _approved_doc is not None:
        order = _approved_doc.meta.order
        customer_id = order.customer_id if order is not None else ""
        if customer_id and args.by.strip().casefold() == customer_id.casefold():
            order_key = order_digest(_approved_doc)
            view = plan_resources.compute_boundary_view(_approved_doc, order_key)
            all_resources = view.resources
            all_unresolved = [item["identity"] for item in view.unresolved]
            all_stage_effects: list = []
            for sr in plan_resources.compute_plan_resources(_approved_doc):
                all_stage_effects.extend(e.to_dict() for e in sr.stage_effects)
            armed_estimate = {
                scale: float((state.effort_estimate or {}).get(scale) or 0.0)
                for scale in effort.RATIO_SCALES
            }
            order_approvals.record_approval(
                order_digest(_approved_doc),
                plan_sha256=_approved_digest or "",
                resources=all_resources,
                unresolved_identities=all_unresolved,
                stage_effects=all_stage_effects,
                by=args.by,
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
                effort_estimate=armed_estimate,
            )
            state.order_effort_frozen = armed_estimate
            actual_now = effort.actual(state)
            state.order_effort_flushed = {
                scale: actual_now[scale] for scale, _ in _ORDER_EFFORT_SCALES
            }
            state.order_effort_base = {}
        else:
            _flush_order_effort(state)
    store.save(state)
    return _with_advisories(Directive(
        True, state.node, "partition",
        "approved; assess partition (M1–M4) before execution",
    ), echo_advice)


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
        # #17: a substantive replan whose stage carry-keys were unchanged (e.g. a
        # control-criterion-only change) carries every PASSED outcome forward, so a
        # re-approval lands the session at PARTITIONED with no PENDING stage left to
        # start — the intended next step (final verification) had no edge from here.
        # Fire it only under the exact guard that makes VERIFYING the faithful node
        # (every stage already PASSED); a genuine dependency problem (a non-ready,
        # non-PASSED stage) must NOT be silently finalized, so it keeps the prior
        # non-ok directive below.
        if state.node == Node.PARTITIONED.value and state.all_stages_passed():
            state.node = transition(state.node, "finalize_partitioned")
            state.log("finalize_partitioned")
            store.save(state)
            return Directive(
                True, state.node, "verify_final",
                "all stages already passed (a replan preserved them); advanced to "
                "final verification — run verify-final",
            )
        return Directive(False, state.node, "verify_final", "no ready stages; run verify-final if all passed")
    stage = ready[0]
    # pick the entry edge into EXECUTING from the current node
    if state.node == Node.PARTITIONED.value:
        event = "execute_approved"
    elif state.node == Node.ROUTED.value:
        event = "execute_small"
    elif state.node == Node.VERIFYING.value:
        event = "next_stage"
    elif state.node == Node.EXECUTING.value and state.active_stage() is None:
        # cmd_pop_subplan restores the parent at EXECUTING with no ACTIVE stage
        # (the originating stage was marked PASSED directly, bypassing the
        # record_result "verify" edge) and its own Directive tells the caller
        # to "run next-stage to continue" — so this node must be a legal entry
        # here too, not just VERIFYING. No edge to cross: already EXECUTING.
        event = None
    else:
        return Directive(False, state.node, "blocked", f"cannot start a stage from node={state.node}")
    if event is not None:
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


def _continuation_worktree(state: SessionState, stage: Stage) -> str | None:
    """The shared worktree/branch a DEPENDENT spawn stage should continue, or
    None for an independent stage (empty depends_on) or one whose dependencies
    are all in-thread. A prior SPAWN stage's committed-but-un-landed work lives
    in a branch dispatch never told the next developer about — this names it.

    Prefers the plan's declared [meta] delivery_worktree (state.delivery_worktree);
    falls back to the task_id-scoped default only when repo_root is known (a
    relative worktree path with no anchor would be meaningless)."""
    if not any(
        d in {s.index for s in state.stages if s.is_spawn()}
        for d in stage.depends_on
    ):
        return None
    if state.delivery_worktree:
        return state.delivery_worktree
    if state.repo_root:
        return f"{state.repo_root}/.claude/worktrees/{state.task_id}"
    return None


def _reattest_stash_for(state: SessionState, stage_index: int) -> ReattestStash | None:
    """The most-recently-built ReattestStash entry for `stage_index`, or None.

    Last-wins, mirroring gates._stage_review_for / _code_review_for — though in
    practice cmd_replan's substantive branch replaces state.reattest_stash
    wholesale each time, so at most one entry per stage_index ever exists at
    once; the scan is written to match the family's convention rather than
    because duplicates are expected."""
    match = [r for r in state.reattest_stash if r.stage_index == stage_index]
    return match[-1] if match else None


def _try_reattest(
    state: SessionState, stage: Stage, store: StateStore, runner: Runner | None,
) -> Directive | None:
    """Stage 6: re-arm a PASSED stage that a substantive replan re-armed, via a
    fresh control re-run, instead of paying for a full specialist re-spawn.

    Returns a terminal Directive on success. Returns None on ANY refusal, after
    logging the specific failing condition via state.log("reattest_declined", ...)
    — the caller (cmd_dispatch) falls through to the existing, unmodified dispatch
    path on None, so refusal always degrades to a normal (byte-identical) dispatch
    rather than stranding the session. The three conditions, checked in order:

      1. a ReattestStash exists for this stage (built only for a stage that had a
         prior PASSED outcome at replan time — see cmd_replan);
      2. the replan that built the stash did not touch the stage's operative
         surface (stash.operative_surface_matched), AND nothing has re-edited the
         stage since (the live stage_reattest_digest still matches the digest
         stashed at replan time — a plan edit made during the PLAN_READY window
         is caught here rather than trusted stale);
      3. the stage's own control — its verify_command/landed check, in its
         declared venue — passes when RE-RUN NOW. Reuses the exact primitives
         cmd_record_result uses for a measurable stage, so a re-attest pass is
         held to the identical bar as a normal pass; a stale prior PASS is never
         carried forward on faith.

    Gate preservation: the code-review gate reads state.code_reviews directly
    (keyed by stage_index, untouched by replan), so calling gates.code_review_
    blockers here — exactly as cmd_record_result does — is naturally fresh with
    no stash of its own; a stage cannot reach PASSED via this route that
    couldn't reach PASSED via the normal one.
    """
    stash = _reattest_stash_for(state, stage.index)
    if stash is None:
        state.log("reattest_declined", stage=stage.index,
                   reason="no prior PASSED outcome recorded for this stage")
        return None
    if not stash.operative_surface_matched:
        state.log("reattest_declined", stage=stage.index,
                   reason="replan touched the stage's operative surface "
                          "(method/control criterion/expected result image/executor/done criterion)")
        return None
    if stage_reattest_digest(stage) != stash.reattest_digest:
        state.log("reattest_declined", stage=stage.index,
                   reason="stage was edited again after the re-attest stash was built")
        return None

    crit = stage.criterion
    if crit.criterion_type == CriterionType.MEASURABLE.value and crit.verify_kind == CheckKind.LANDED.value:
        ok, refusal, _result = _landed_check_result(state, crit.landed, runner)
        if refusal:
            state.log("reattest_declined", stage=stage.index,
                       reason=f"landed check refused: {refusal}")
            return None
        if not ok:
            state.log("reattest_declined", stage=stage.index,
                       reason="control failed on re-run (landed check: delivered commit "
                              "not (yet) contained in the declared target)")
            return None
    else:
        cwd = None
        if crit.verify_command and crit.criterion_type == CriterionType.MEASURABLE.value:
            cwd, refusal = _resolve_or_refuse(state, crit.verify_venue)
            if refusal:
                state.log("reattest_declined", stage=stage.index,
                           reason=f"verify_command refused: {refusal}")
                return None
        ok, result = _verify_command_result(stage, runner, cwd=cwd)
        if not ok:
            state.log("reattest_declined", stage=stage.index,
                       reason=f"control failed on re-run (exit {result.returncode} != "
                              f"expected {crit.expected_exit}: {crit.verify_command})")
            return None

    if stage.needs_control() and not (stash.prior_control or "").strip():
        state.log("reattest_declined", stage=stage.index,
                   reason="stage needs a control attestation but the stashed prior "
                          "control is empty")
        return None
    if stage.needs_control() and gates.code_review_active(state):
        crb = gates.code_review_blockers(state, stage)
        if crb:
            state.log("reattest_declined", stage=stage.index,
                       reason=f"code review gate: {'; '.join(crb)}")
            return None

    # All three conditions hold: carry the prior Outcome AND control attestation
    # forward instead of re-spawning to reproduce them — marking the record with
    # an explicit [re_attested] tag so the saving is countable/auditable.
    stage.outcome = stash.prior_outcome
    stage.control = stash.prior_control
    marker = "[re_attested]"
    stage.outcome.actual = (
        f"{stage.outcome.actual}\n{marker}" if stage.outcome.actual else marker
    )
    stage.outcome.status = StageStatus.PASSED.value
    state.current_stage = None
    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING
    state.log("reattest", stage=stage.index)
    store.save(state)
    if state.all_stages_passed():
        return Directive(True, state.node, "verify_final",
                          f"stage {stage.index} re-attested; all stages passed")
    return Directive(True, state.node, "next_stage",
                      f"stage {stage.index} re-attested; more stages ready")


# cmd_dispatch's fallback when neither --effort nor a per-stage reasoning-effort
# field (there is no such field on Actor, unlike cost_tier) supplies one. Mirrors
# the cost_tier -> budget relationship one rung down: a stage costed "large"
# is presumed to warrant deeper reasoning than one costed "small". Unmapped or
# absent cost_tier falls through to the dict's .get default of "medium", same
# as the --budget resolution just above.
_EFFORT_BY_COST_TIER = {"small": "low", "medium": "medium", "large": "high"}


def _dispatch_project_settings_path(state) -> str | None:
    """Absolute path to the delivery venue's own `.claude/settings.local.json`,
    when one exists — forwarded to a spawned developer so it inherits the same
    project-scope Bash/tool grants an interactive session in that project
    already has (see spawn-specialist.py's project_settings_permission_rules).

    Resolved off the SAME venue cmd_dispatch already pins the child's cwd to
    (resolve_check_venue(DELIVERY)) — the tree the spawned developer actually
    writes in and runs commands in, not an unrelated repo_root. None when the
    venue is unresolved or the file doesn't exist: dispatch then behaves
    exactly as before this function existed (no --project-settings forwarded,
    the child gets only the fleet-wide KIND_BASELINES["developer"] grant)."""
    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    if not venue:
        return None
    candidate = Path(venue) / ".claude" / "settings.local.json"
    return str(candidate) if candidate.is_file() else None


def cmd_dispatch(args, *, store: StateStore, runner: Runner | None = None,
                 perm_checker=None) -> Directive:
    state = _require(store, args.session)
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if efblock:
        return _effort_fire_refusal(state, "dispatch", efblock)
    try:
        host = runtime_host.require_bound_host(state)
    except runtime_host.HostAmbiguousError as exc:
        return Directive(False, state.node, "noop", str(exc))
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to dispatch")
    if not stage.is_spawn():
        return Directive(True, state.node, "execute_in_thread", f"stage {stage.index} is in-thread; no spawn")
    # Stage 6: an explicit, OPT-IN re-attest request. Absent --re-attest this
    # branch never runs — cmd_dispatch's behavior stays byte-identical to
    # before. On refusal _try_reattest has already logged the specific failing
    # condition; falling through below runs the existing, unmodified dispatch.
    if bool(getattr(args, "re_attest", False)):
        directive = _try_reattest(state, stage, store, runner)
        if directive is not None:
            return directive
    dry_run = bool(getattr(args, "dry_run", False))
    # Tier resolution order: explicit --budget flag > the stage's declared
    # Actor.cost_tier > the "medium" default — same precedence on the argparse
    # path (getattr(args, "budget", None)) and any in-process Namespace caller
    # that also omits --budget.
    tier = getattr(args, "budget", None) or stage.actor.cost_tier or "medium"
    # Same resolution order as --budget above: explicit --effort flag > a
    # cost_tier-derived default > "medium". spawn-specialist.py now hard-requires
    # --effort (no inherit-the-parent fallback there either), so dispatch_stage
    # must always hand it a value — never omit the flag and let the child's own
    # argparse refuse with "the following arguments are required: --effort".
    # There is no plan-declared per-stage reasoning-effort field (unlike
    # cost_tier on Actor), so the derived default reuses the same three-tier
    # cost_tier already on the stage rather than inventing a new plan field.
    effort_tier = getattr(args, "effort", None) or _EFFORT_BY_COST_TIER.get(
        stage.actor.cost_tier, "medium"
    )
    child_cwd = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    # Pre-launch: read the stage's effective grant set and hash the live
    # settings documents BEFORE the child runs. Grant coverage is captured
    # here (not after dispatch_stage returns) so a denial is classified
    # against what the stage was ACTUALLY spawned with, not against whatever
    # runtime_grants happens to hold once the child has already exited --
    # the two can differ if this session's grants are touched concurrently.
    # The settings hashes are the pre-image half of the settings_drift check
    # below: a spawned developer is untrusted to touch settings*.json, so any
    # difference after the child returns is itself the signal, independent of
    # whatever marker the child reports.
    # Refuse to dispatch outright when the stage has grants to lose -- DECLARED
    # (`stage.grants`) or DERIVED (`_stage_would_derive_grants`, trust-independent
    # so a derived-hash mismatch is caught too, not just a missing/unloadable
    # snapshot) -- but its approved-plan snapshot can no longer be read cleanly.
    # Spawning anyway would silently launch the child under whatever
    # `_effective_stage_grants` degrades to (declared entries alone, or nothing at
    # all) rather than the coverage this stage was actually approved with. A
    # stage with nothing to lose either way has a read error that is not fatal
    # here (`cmd_stage_grants` still surfaces it for inspection).
    _grant_read_error = _stage_grant_entries(state, stage.index)[3]
    _stage_declares_grants = bool(getattr(stage, "grants", None)) and not stage.grants.is_empty()
    if _grant_read_error and (
        _stage_declares_grants or _stage_would_derive_grants(state, stage.index)
    ):
        return Directive(
            False, state.node, "noop",
            f"dispatch refused: stage {stage.index} has grants to lose (declared "
            "and/or derived) but they could not be read from the approved-plan "
            f"snapshot ({_grant_read_error}) -- spawning now would silently drop "
            "that coverage instead of materializing it; a fresh `approve` "
            "re-snapshots the plan and rebinds approved_grants_sha256",
        )
    coverage = _effective_stage_grants(state, stage.index)
    _defects_before = len(state.materialization_defects)
    _misses_before = len(state.planning_misses)
    _settings_paths = widening_targets.enumerate_live_settings(child_cwd, state.repo_root)
    _settings_before = {p: _plan_file_sha256(p) for p in _settings_paths}
    # Resolved (not created) BEFORE the child spawns, so an unresolvable evidence
    # root refuses the dispatch before real spawn cost is spent. Only a
    # spawn:developer stage actually writes checkpoint evidence, so only that
    # kind is refused on failure; other kinds report the path best-effort and
    # proceed without it.
    try:
        evidence_dir = str(evidence_dir_for(state.session_id, stage.index))
    except EvidenceDirError as exc:
        if stage.spawn_kind() == "developer":
            return Directive(False, state.node, "noop", f"dispatch refused: {exc}")
        evidence_dir = None
    result = dispatch_stage(
        stage, state.plan_path or "",
        runner=runner,
        budget=tier,
        complexity=getattr(args, "complexity", "medium"),
        effort=effort_tier,
        continue_worktree=_continuation_worktree(state, stage),
        constraints=getattr(args, "constraints", "") or "",
        dry_run=dry_run,
        # A hard-sandboxed spawned child can only write the tree it is
        # launched in, so pin its cwd to the plan's delivery venue rather
        # than relying on the dispatching process's ambient cwd. Always
        # "delivery" — dispatch has no verify_venue/venue of its own to read,
        # and delivery is where a spawned developer must write.
        cwd=child_cwd,
        runtime_host=host,
        project_settings=_dispatch_project_settings_path(state),
        session_id=state.session_id,
    )
    if dry_run:
        # #10: a dry-run is a pure preview — no event log, no state save, no
        # marker routing. The echoed command is the whole result.
        return Directive(
            True, state.node, "preview",
            f"stage {stage.index} dry-run preview (no state change)",
            data={
                "returncode": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "evidence_dir": evidence_dir,
            },
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
    base = {
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        "evidence_dir": evidence_dir,
    }

    # Post-launch half of the settings_drift check: re-hash the SAME paths
    # enumerated before dispatch_stage ran. A spawned child is never granted
    # write access to a settings document, so any difference here is itself
    # the signal regardless of what marker the child reports -- recorded and
    # surfaced on every Directive this call returns, not only on a denial.
    _settings_after = {p: _plan_file_sha256(p) for p in _settings_paths}
    if _settings_after != _settings_before:
        drift_row = {
            "stage_index": stage.index,
            "changed": sorted(
                p for p in _settings_paths if _settings_before.get(p) != _settings_after.get(p)
            ),
            "ts": _utcnow(),
        }
        state.settings_drift.append(drift_row)
        base["settings_drift"] = drift_row

    # After every dispatched child returns (any marker, not just PERMISSION-REQUEST):
    # classify any permission-denial stop its transcript recorded against the stage's
    # PRE-LAUNCH effective grant set (`coverage`, captured before dispatch_stage ran
    # above). The PERMISSION-REQUEST branch below covers the same ground via its own
    # self-reported `Rule:`-line fallback for the case this transcript-based path
    # finds nothing (announcer-thread timeout, or discovery failed).
    _classify_transcript_denials(state, stage, coverage, _parse_transcript_path(result.stderr))
    if marker not in (CHILD_INFRA_FAILURE, CHILD_EXHAUSTED):
        # A transient/resource marker means the child never meaningfully ran
        # against the once-scoped grant this launch was meant to spend — the
        # launch didn't count, so consuming a single-use grant here would
        # burn it for a re-dispatch that gets no benefit from having used it.
        _consume_once_grants(state, stage.index)

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
        # The permissions-cli check still runs for its own audit-row side effect, but
        # its answer no longer skips the ask: a global grant recorded there never
        # reaches the child's own --settings/--add-dir, so the old auto-continue just
        # re-spawned into the identical denial (removed per the plan's design).
        checker = perm_checker or permissions.check_permission
        checker(action)
        # The optional `Rule:` line is a SEPARATE line from the marker's own first
        # line, so it must be read off the full child stdout -- parse_marker's
        # `body` is only the text after "PERMISSION-REQUEST:" on that first line.
        rule_line = _parse_rule_line(result.stdout)
        call = _rule_line_to_call(rule_line) if rule_line else None
        # Finding N1: a transcript denial newly recorded THIS dispatch call may
        # belong to an entirely unrelated command (the stage's transcript can
        # carry several denials in one launch) -- counting "any new
        # materialization_defects row" as covering THIS request conflates the
        # two. When the request names its own `Rule:`, tie transcript_covered
        # to a new row matching that SAME (tool_name, tool_input) pair; only
        # absent a `Rule:` line (nothing to disambiguate against) fall back to
        # the old any-new-row signal.
        # Finding S9 (stage 9 live run, 2026-09-27): the self-reported `Rule:` line
        # can simply be WRONG -- the specialist misnames the command it actually ran
        # (e.g. reports the covered repo-relative spelling while the transcript shows
        # an uncovered worktree-absolute one it actually invoked). Trusting
        # `self_covered` whenever it agrees, without first checking whether the
        # transcript ALREADY classified this same call as an uncovered miss, let a
        # wrong self-report override the transcript's own record of what was denied.
        # The transcript is ground truth when it has an opinion at all: check BOTH
        # transcript outcomes (covered -> materialization_defects, uncovered ->
        # planning_misses[source="transcript"]) and let a transcript-sourced row —
        # of either kind — win over the self-report; the self-report drives
        # classification only when the transcript recorded nothing for this call.
        if call is not None:
            expected_text = call[1].get("command") or call[1].get("file_path")
            expected_digest = _digest(expected_text) if expected_text is not None else None
            transcript_covered = any(
                row.get("tool_name") == call[0] and row.get("tool_input_digest") == expected_digest
                for row in state.materialization_defects[_defects_before:]
            )
            transcript_uncovered = any(
                row.get("source") == "transcript"
                and row.get("tool_name") == call[0]
                and row.get("tool_input_digest") == expected_digest
                for row in state.planning_misses[_misses_before:]
            )
        else:
            transcript_covered = len(state.materialization_defects) > _defects_before
            transcript_uncovered = any(
                row.get("source") == "transcript"
                for row in state.planning_misses[_misses_before:]
            )
        self_covered = call is not None and _grants.grant_covers_call(coverage, call[0], call[1])
        if transcript_covered or (not transcript_uncovered and self_covered):
            if not transcript_covered:
                state.materialization_defects.append({
                    "stage_index": stage.index, "action": action,
                    "tool_name": call[0],
                    "tool_input_digest": _digest(json.dumps(call[1], sort_keys=True)),
                    "evidence": "self-reported", "ts": _utcnow(),
                })
            return _diagnose_materialization_defect(
                state, store, stage, action=action,
                evidence="transcript" if transcript_covered else "self-reported",
            )
        # REQ5 (dispatch half): a Rule:-line request may resolve to a resource
        # already approved for this order, or already covered by the stage's
        # own effective grants under different-but-equivalent text -- self_grant
        # instead of parking on the user. An optional advisory `Resource:` line
        # is NEVER trusted to DECIDE this by itself -- self_grant is decided
        # ONLY from the engine's own resolution of the `Rule:` line; a
        # disagreeing `Resource:` line routes to the user with a reason naming
        # BOTH, rather than silently preferring either.
        resource_line = _parse_resource_line(result.stdout)
        reported_resource = _parse_resource_spec(resource_line) if resource_line else None
        self_grant_resources: list = []
        self_grant_reason: str | None = None
        if rule_line is not None:
            try:
                _grants.validate_rule(rule_line)
            except _grants.GrantValidationError as exc:
                self_grant_reason = f"refused rule grant: {exc}"
            else:
                self_grant_coverage = _effective_stage_grants_for_self_grant(state, stage.index)
                self_grant_resources, self_grant_reason = _self_grant_resources_for_rule(
                    state, self_grant_coverage, rule_line
                )
        resource_disagreement = None
        if (
            reported_resource is not None
            and self_grant_resources
            and not any(reported_resource == r for r in self_grant_resources)
        ):
            resource_disagreement = (
                f"specialist-reported Resource: {resource_line!r} disagrees with the "
                f"engine's own resolution of Rule: {rule_line!r} to "
                f"{[plan_resources._resource_to_dict(r) for r in self_grant_resources]!r} "
                "-- asking the user rather than trusting either alone"
            )
        if self_grant_resources and resource_disagreement is None:
            # The self_grant directive's own message tells the caller to run
            # `resolve-permission --by agent --scope stage --rule ...` next --
            # that command requires a pending `state.permission_request`
            # (checked first thing in cmd_resolve_permission), so it must be
            # parked here exactly as the ask_user_permission path below does,
            # or the directive's own instructions would fail when followed.
            state.permission_request = PermissionRequest(
                action=action, stage_index=stage.index, raw=body
            )
            state.log(
                "permission_self_grant", stage=stage.index, action=action, rule=rule_line,
                resources=[plan_resources._resource_to_dict(r) for r in self_grant_resources],
            )
            store.save(state)
            return Directive(
                True, state.node, "self_grant",
                f"stage {stage.index} permission request for {rule_line!r} resolves to "
                "resource(s) already approved for this order or already covered by the "
                "stage's own effective grants -- self-grant (`resolve-permission --by "
                f"agent --scope stage --rule {rule_line!r}`) and re-dispatch, no user ask needed",
                marker="PERMISSION-REQUEST",
                data={
                    **base, "action": action, "rule": rule_line,
                    "resources": [plan_resources._resource_to_dict(r) for r in self_grant_resources],
                },
            )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=body
        )
        # `_classify_transcript_denials` above may already have appended a
        # planning_misses row for this SAME denial (source="transcript",
        # asked_user=False) before we knew the marker would ask the user.
        # Promote that row in place rather than appending a second one for
        # the same event -- search only the rows THIS dispatch call added
        # (`_misses_before:`), most-recent first, since an earlier stage's
        # unrelated miss must never be touched. A single dispatch call can
        # legitimately add MULTIPLE unasked transcript rows (one per denial
        # stop in the transcript), so "most recent unasked" alone is not
        # enough to identify the SAME denial the self-report names --
        # require the row's own stored `command` to be prefixed by this
        # call's rule command (via `_bash_rule_command_from_call`); a
        # `None` call command (no Rule: line, or a non-Bash call) never
        # promotes, since it cannot be confirmed to be the same event, and
        # appending a second row is a cheaper mistake than silently
        # mutating the wrong denial's row.
        _call_command = _bash_rule_command_from_call(call)
        _promoted = None
        if _call_command is not None:
            for _row in reversed(state.planning_misses[_misses_before:]):
                _row_command = _row.get("command")
                if (
                    _row.get("source") == "transcript"
                    and not _row.get("asked_user")
                    and isinstance(_row_command, str)
                    and (
                        _row_command == _call_command
                        or _row_command.startswith(_call_command + " ")
                    )
                ):
                    _promoted = _row
                    break
        if _promoted is not None:
            _promoted["asked_user"] = True
            _promoted["action"] = action
        else:
            state.planning_misses.append({
                "stage_index": stage.index, "asked_user": True, "ts": _utcnow(),
                "tool_name": call[0] if call else None,
                "tool_input_digest": _digest(json.dumps(call[1], sort_keys=True)) if call else None,
                "action": action, "source": "permission-request",
            })
        state.log("permission_request", stage=stage.index, action=action)
        store.save(state)
        _ask_data = {**base, "action": action, "options": ["once", "stage", "project", "global", "deny"]}
        if rule_line is not None:
            _ask_data["reason_class"] = self_grant_reason
        if resource_disagreement is not None:
            _ask_data["resource_disagreement"] = resource_disagreement
        return Directive(
            True, state.node, "ask_user_permission",
            f"stage {stage.index} requests permission: {action}",
            marker="PERMISSION-REQUEST",
            # Finding S8: "stage" was materializable via --scope stage but
            # missing from this options list, so the root never saw it as an
            # offered choice.
            data=_ask_data,
        )
    if marker == CHILD_INFRA_FAILURE:
        # A transient condition about the RUN, never a judgement about the
        # output — named directive only, no automatic re-spawn: the
        # coordinator still spends the money on the retry.
        store.save(state)
        return Directive(
            False, state.node, "retry_dispatch",
            f"stage {stage.index} spawn never reached (or lost) the API — "
            "transient; recommend retrying the same dispatch",
            marker="CHILD_INFRA_FAILURE", data={**base, "reason": body},
        )
    if marker == CHILD_EXHAUSTED:
        # A resource condition about the RUN — the child was refused for size
        # before it could answer. Recommend a reduced brief or the re-attest
        # path (stage 6); again a directive only, no automatic re-spawn.
        store.save(state)
        return Directive(
            False, state.node, "reduce_brief_or_reattest",
            f"stage {stage.index} spawn was refused for size before it could "
            "answer — recommend a reduced brief or the re-attest path",
            marker="CHILD_EXHAUSTED", data={**base, "reason": body},
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


def _parse_add_dir_spec(spec: str) -> tuple[str, str] | None:
    """Parse a `--add-dir 'PATH:MODE'` CLI spec into `(path, mode)`, or None on
    any shape that doesn't parse — the caller turns that into a refusal rather
    than guessing. Splits from the RIGHT so a path containing ':' (rare, but
    not impossible) still parses correctly; MODE itself never contains ':'."""
    if ":" not in spec:
        return None
    path, mode = spec.rsplit(":", 1)
    if not path or mode not in ("read", "write"):
        return None
    return path, mode


_TRANSCRIPT_PATH_RE = re.compile(r"^spawn-specialist: transcript=(.+)$")


def _parse_transcript_path(stderr: str) -> str | None:
    """The dispatched child's transcript path, read off the
    `spawn-specialist: transcript=<path>` line spawn-specialist.py's own
    `_announce_transcript` writes to its stderr on every launch -- the one
    place this process learns the path today (RunResult carries no dedicated
    field, and the cost-log ledger does not carry it either until stage 2).
    The `<not-found-within-10s>` sentinel (and any other non-path text) reads
    as "no transcript available", same as a missing line -- never mistaken
    for a literal path."""
    for line in reversed((stderr or "").splitlines()):
        m = _TRANSCRIPT_PATH_RE.match(line.strip())
        if m:
            path = m.group(1).strip()
            return path if path and not path.startswith("<") else None
    return None


_RULE_LINE_RE = re.compile(r"^[-*]?\s*\*{0,2}Rule:\*{0,2}\s*(.*)$")


def _parse_rule_line(body: str) -> str | None:
    """The optional trailing `Rule: <rule>` line a specialist's PERMISSION-REQUEST
    body may carry -- the self-reported fallback `cmd_dispatch` classifies a denial
    against when the transcript-based classification above found nothing (the
    child exited before spawn-specialist.py's announcer thread resolved a path,
    or discovery timed out). Tolerates the markdown decoration a specialist's own
    reply commonly wraps the line in -- a leading `-`/`*` list bullet and `**bold**`
    around the `Rule:` label -- so `- **Rule:** `Bash(...)`` parses the same as a
    bare `Rule: Bash(...)` line; an inline-code span around the value is also
    unwrapped. Still refuses a line where "Rule:" is not the line's own label (mid-
    sentence text is never mistaken for the marker). Returns the rule string, or
    None if no such line is present."""
    for line in reversed(body.splitlines()):
        line = line.strip()
        m = _RULE_LINE_RE.match(line)
        if not m:
            continue
        rule = m.group(1).strip()
        if len(rule) >= 2 and rule.startswith("`") and rule.endswith("`"):
            rule = rule[1:-1].strip()
        if rule:
            return rule
    return None


def _rule_line_to_call(rule: str) -> tuple[str, dict] | None:
    """Convert a `Bash(X:*)`/`Bash(X)`/`Edit(//abs/path)`-shaped permission-rule
    string into the `(tool_name, tool_input)` pair `grants.grant_covers_call`
    checks -- the inverse of how a derived/declared rule names a call. Unparseable
    -> None, which `grant_covers_call`'s caller must treat as NOT covered."""
    parsed = _grants.rule_program_and_arg(rule)
    if parsed is None:
        return None
    tool, arg = parsed
    if tool == "Bash":
        return "Bash", {"command": _grants.bash_command_from_rule_arg(arg)}
    if tool in ("Edit", "Write", "Read", "NotebookEdit"):
        return tool, {"file_path": _grants.rule_file_path(arg)}
    return None


_RESOURCE_LINE_RE = re.compile(r"^[-*]?\s*\*{0,2}Resource:\*{0,2}\s*(.*)$")
_RESOURCE_SPEC_RE = re.compile(r"^(file|vcs_ref|specialist|service|dataset)\((.*)\)$")


def _parse_resource_line(body: str) -> str | None:
    """The optional trailing `Resource: <kind>(<args>)` line a specialist's
    PERMISSION-REQUEST body may carry -- ADVISORY ONLY (REQ5 dispatch half):
    it names what the specialist BELIEVES it needs, never what decides
    self_grant on its own. Mirrors `_parse_rule_line`'s markdown-tolerance
    (bullet/bold decoration, an inline-code-wrapped value) exactly, since a
    specialist that decorates one line commonly decorates both."""
    for line in reversed(body.splitlines()):
        line = line.strip()
        m = _RESOURCE_LINE_RE.match(line)
        if not m:
            continue
        spec = m.group(1).strip()
        if len(spec) >= 2 and spec.startswith("`") and spec.endswith("`"):
            spec = spec[1:-1].strip()
        if spec:
            return spec
    return None


def _parse_resource_spec(text: str):
    """Parse a `kind(arg, arg, ...)` resource spec into the matching
    `resources.Resource` subclass, or `None` if it does not parse -- a
    parse failure is never fatal (the line is advisory), it just means
    there is nothing to cross-check the engine's own resolution against."""
    m = _RESOURCE_SPEC_RE.match(text.strip())
    if not m:
        return None
    kind, raw_args = m.group(1), m.group(2)
    args = [a.strip() for a in raw_args.split(",")] if raw_args.strip() else []
    try:
        if kind == "file" and len(args) == 2:
            return _resources.FileResource(args[0], args[1])
        if kind == "vcs_ref" and len(args) == 3:
            return _resources.VcsRefResource(args[0], args[1], args[2])
        if kind == "specialist" and len(args) == 1:
            return _resources.SpecialistResource(args[0])
        if kind == "service" and len(args) == 2:
            return _resources.ServiceResource(args[0], args[1])
        if kind == "dataset" and len(args) == 2:
            return _resources.DatasetResource(args[0], args[1])
    except ValueError:
        return None
    return None


def _self_grant_resources_for_rule(
    state: "SessionState", coverage: "_grants.StageGrants", rule_line: str
) -> tuple[list, str | None]:
    """Resolve a PERMISSION-REQUEST's self-reported `Rule:` line to the
    resource(s) it names, then check those resources against the UNION of
    (a) `order_approvals.boundary_resources` -- the last user-approved
    version's resources plus customer stage grants, keyed by
    `plan.order_digest` -- the SAME resolution
    `cmd_resolve_permission --by agent` uses, REQ5) and (b) the stage's own
    EFFECTIVE (declared + derived) grants, each independently re-resolved to
    resources rather than compared by rule TEXT -- a declared grant spelled
    differently from the requested rule (e.g. a push rule naming a
    different source ref for the same target) still counts, catching a case
    the old string/rule-based `grants.grant_covers_call` would miss.

    Returns `(resources, None)` when every resolved resource is covered
    (dispatch may self_grant), or `([], reason_class)` otherwise --
    `reason_class` names why, so a wildcard-tail rule and an out-of-scope
    resource are both distinguishable in the returned Directive's data."""
    if not state.plan_path:
        return [], "no-plan"
    if _open_effort_fire(state):
        return [], "open-effort-fire"
    try:
        doc = load_plan(state.plan_path)
    except (OSError, PlanError):
        return [], "unloadable-plan"
    venue = _venue_for(doc)
    res = plan_resources.resolve_rule_grant(rule_line, venue)
    if res.status != "resolved" or not res.resources:
        return [], (res.reason_class or "contract-unresolved")
    protected = _resources.protected_permission_surfaces(
        repo_root=state.repo_root, delivery_worktree=state.delivery_worktree,
        ledger_dir=str(order_approvals._root(None)),
    )
    approved: list = list(order_approvals.boundary_resources(order_digest(doc)))
    for rule_grant in coverage.allow:
        eff = plan_resources.resolve_rule_grant(rule_grant.rule, venue)
        if eff.status == "resolved":
            approved.extend(eff.resources)
    for add_dir in coverage.add_dirs:
        eff = plan_resources.resolve_add_dir_grant(add_dir.path, add_dir.mode)
        approved.extend(eff.resources)
    for requested in res.resources:
        if not any(_covers_resource(appr, requested, protected) for appr in approved):
            return [], "not-approved"
    return res.resources, None


def _stage_grant_entries(
    state: SessionState, stage_index: int,
) -> tuple[list[dict], list[dict], list[dict], str | None, str | None]:
    """Read one stage's declared + derived grant entries from the hash-verified plan
    snapshot -- the shared read behind both `cmd_stage_grants`'s report and
    `_effective_stage_grants`'s merged coverage object, so the two never drift on
    when a grant becomes trustworthy (snapshot presence, `approved_grants_sha256`
    re-hash gate over the live derivation code). Returns
    (declared_entries, derived_entries, dropped, error, note).

    `error` is None on a clean read; otherwise a short string naming what failed,
    meant for a caller to surface VERBATIM rather than translate into a silently
    empty grant set -- a plan that declared a grant nobody can currently read is a
    materialization gap, not the same thing as a plan that declared none. Each
    return statement below reports at most one failure and stops there, so one
    unreadable stage/snapshot can never masquerade as "no grants" for a caller
    that then proceeds to dispatch a child under an emptied-out coverage set.

    `note` is None on every path except the legacy one below: it is advisory, never
    a refusal condition, and never changes which entries are returned.

    A session that never took a snapshot at all -- a plan approved before the
    snapshot mechanism existed, or no plan approved yet -- is deliberately NOT an
    error: it carries no grants, the same fail-open reading `grants_approval_
    blockers` already gives the identical condition at approve time."""
    declared_entries: list[dict] = []
    derived_entries: list[dict] = []
    dropped: list[dict] = []
    snap_path = state.plan_snapshot_path
    snap_hash = state.plan_snapshot_hash
    if not snap_path:
        return declared_entries, derived_entries, dropped, None, None
    if not Path(snap_path).exists():
        return declared_entries, derived_entries, dropped, (
            f"approved-plan snapshot is missing on disk: {snap_path!r}"
        ), None
    if not snap_hash or hashlib.sha256(Path(snap_path).read_bytes()).hexdigest() != snap_hash:
        return declared_entries, derived_entries, dropped, (
            f"approved-plan snapshot hash mismatch at {snap_path!r}: the file on "
            "disk no longer matches the bytes this session approved"
        ), None
    try:
        snap_doc = load_plan(snap_path)
    except (OSError, PlanError) as exc:
        return declared_entries, derived_entries, dropped, (
            f"cannot load approved-plan snapshot {snap_path!r}: {exc}"
        ), None
    snap_stage = next((s for s in snap_doc.stages if s.index == stage_index), None)
    if snap_stage is None:
        return declared_entries, derived_entries, dropped, (
            f"stage {stage_index} not found in approved-plan snapshot {snap_path!r}"
        ), None
    declared = snap_stage.grants if getattr(snap_stage, "grants", None) \
        else _grants.StageGrants()
    declared_entries = [r.to_dict() for r in declared.allow] + \
        [a.to_dict() for a in declared.add_dirs]
    if not state.approved_grants_sha256:
        # Legacy session (approved before grant hashing existed): derived grants
        # stay withheld exactly as before -- there is no hash to trust them
        # against -- but this is advisory, not an error, so it rides a `note`
        # instead of `error` and never touches the returned entries.
        return declared_entries, derived_entries, dropped, None, (
            "derived grants unverified: session approved before grant hashing"
        )
    if grants_sha256(snap_doc) != state.approved_grants_sha256:
        return declared_entries, derived_entries, dropped, (
            "derived-grants hash mismatch: the live grant derivation no longer "
            "reproduces approved_grants_sha256 (the materialization layer changed "
            "since this plan was approved) -- derived grants withheld"
        ), None
    venue = _venue_for(snap_doc)
    derived, dropped = _grants.derive_stage_grants(snap_stage, venue=venue)
    derived_entries = [r.to_dict() for r in derived.allow] + \
        [a.to_dict() for a in derived.add_dirs]
    return declared_entries, derived_entries, dropped, None, None


def _stage_would_derive_grants(state: SessionState, stage_index: int) -> bool:
    """Best-effort, TRUST-INDEPENDENT probe for whether `stage_index`'s
    approved-plan snapshot would derive any non-empty grant set -- used only to
    decide whether a `_stage_grant_entries` read error is safe to dispatch
    through (nothing to lose) or must refuse (something to lose). Deliberately
    ignores `approved_grants_sha256`: the whole point is to answer "does this
    stage have derived grants" even when the trust check that would normally
    gate them is exactly what failed (a derived-grants hash mismatch). Returns
    False on any failure to load the snapshot/stage -- a caller reaching for
    this helper already has its own read error and a declared-grants check to
    fall back on, so this never needs to fail closed on its own."""
    snap_path = state.plan_snapshot_path
    if not snap_path or not Path(snap_path).exists():
        return False
    try:
        snap_doc = load_plan(snap_path)
    except (OSError, PlanError):
        return False
    snap_stage = next((s for s in snap_doc.stages if s.index == stage_index), None)
    if snap_stage is None:
        return False
    venue = _venue_for(snap_doc)
    derived, _dropped = _grants.derive_stage_grants(snap_stage, venue=venue)
    return not derived.is_empty()


def _refresh_runtime_grant_titles(state: SessionState, stage_index: int, new_title: str) -> None:
    """A refinement/no_change replan keeps every stage at its existing index (only a
    substantive replan can renumber -- that case is `_rekey_runtime_grants`, run on
    the next approve), but MAY retitle a stage the same replan touches. A runtime
    grant's stamped `stage_title` must track that retitle in place, else a later
    substantive approve's re-key-by-title finds zero matches for the grant's now-stale
    title and drops it (round-4 F2)."""
    for entry in state.runtime_grants.get(str(stage_index), []):
        entry["stage_title"] = new_title


def _rekey_runtime_grants(state: SessionState, doc) -> None:
    """A runtime grant is keyed by stage INDEX but was granted for a stage IDENTIFIED
    by its title at grant time -- a replan can renumber stages (insert/remove before
    it) without changing what the grant was actually about. Called on every approve
    (fresh snapshot, fresh indices): for each stage index currently holding runtime
    grants, re-key each entry to whichever new-snapshot stage has the SAME title it
    was stamped with. Zero or several matches (title now shared or gone) drops the
    entry with a stderr note rather than guessing."""
    title_to_indices: dict[str, list[int]] = {}
    for s in doc.stages:
        title_to_indices.setdefault(s.title, []).append(s.index)
    rekeyed: dict[str, list[dict]] = {}
    for old_key, entries in state.runtime_grants.items():
        for entry in entries:
            title = entry.get("stage_title")
            matches = title_to_indices.get(title, []) if title else []
            if len(matches) != 1:
                print(
                    f"stage-grants: dropping runtime grant stamped for stage title "
                    f"{title!r} (was stage {old_key}) -- {len(matches)} stages now "
                    "match that title",
                    file=sys.stderr,
                )
                continue
            new_key = str(matches[0])
            rekeyed.setdefault(new_key, []).append(entry)
    state.runtime_grants = rekeyed


def _kind_baseline_rule_grants(kind: str, workdir: str | None = None) -> list[_grants.RuleGrant]:
    """The fleet-wide baseline Bash rules `kind_baselines.KIND_BASELINES`
    injects for `kind` (falling back to its own `"default"` baseline),
    each wrapped as a `RuleGrant` with provenance `"baseline"` -- finding
    S3: a call covered only by this baseline (never by a
    declared/derived/runtime grant) must classify as a materialization
    defect, not a planning miss, since the child's actual settings
    genuinely carry it. `workdir`, when given, is expanded through
    `kind_baselines.baseline_for_workdir` -- the same workdir-absolute script-rule
    spelling `spawn-specialist.py` materializes into the child's `--settings`
    (finding S9), so coverage here never lags what the child actually receives."""
    baseline = kind_baselines.baseline_for_workdir(kind, workdir)
    return [_grants.RuleGrant(rule=r, provenance="baseline") for r in baseline]


def _effective_stage_grants(state: SessionState, stage_index: int) -> _grants.StageGrants:
    """The merged declared + derived + unconsumed-runtime + kind-baseline grant
    set for one stage -- what `grants.grant_covers_call` checks a denied call
    against in `cmd_dispatch`. An entry dict carrying a "rule" key becomes a
    RuleGrant; a "path"/"mode" pair becomes an AddDirGrant -- the same two
    shapes `cmd_stage_grants` already reports separately by provenance. The
    kind baseline is unioned in for a `spawn:*` stage only -- an in-thread
    stage never spawns a child, so no baseline settings ever materialize for
    it (finding S3).

    Silent on a `_stage_grant_entries` read error -- callers that need to REFUSE
    on one (`cmd_dispatch`) check `_stage_grant_entries` themselves before calling
    this; this function's own signature and existing callers (`cmd_dispatch`'s
    coverage object, `test_pra_review_catalogue.py`'s direct call) stay
    unchanged, so an error here degrades to the same partial-entries reading the
    error-carrying return already yields (declared/derived withheld precisely on
    the failure paths that produce one)."""
    declared_entries, derived_entries, _dropped, _error, _note = _stage_grant_entries(state, stage_index)
    runtime_entries = [
        e for e in state.runtime_grants.get(str(stage_index), [])
        if not e.get("consumed")
    ]
    allow: list[_grants.RuleGrant] = []
    add_dirs: list[_grants.AddDirGrant] = []
    for e in declared_entries + derived_entries + runtime_entries:
        if "rule" in e:
            allow.append(_grants.RuleGrant.from_dict(e))
        elif "path" in e and "mode" in e:
            add_dirs.append(_grants.AddDirGrant.from_dict(e))
    stage = state.stage(stage_index)
    if stage.is_spawn():
        # The SAME venue `cmd_dispatch` passes as the child's actual cwd
        # (`child_cwd = state.resolve_check_venue(CheckVenue.DELIVERY.value)`)
        # -- not a second, independently-reloaded reading of the approved-plan
        # snapshot (the deleted `_stage_grant_venue`), which could drift from
        # the child's real cwd whenever the snapshot's own `meta` lacked both
        # `delivery_worktree` and `repo_root` (falling back to `"."`, resolved
        # against the ENGINE's cwd rather than the child's) or had merely
        # drifted from the state fields `_sync_venue_from_plan` keeps current.
        venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
        allow.extend(_kind_baseline_rule_grants(stage.spawn_kind(), workdir=venue))
    return _grants.StageGrants(allow=allow, add_dirs=add_dirs)


def _effective_stage_grants_for_self_grant(state: SessionState, stage_index: int) -> _grants.StageGrants:
    """The NARROWER grant set an agent self-grant (`resolve-permission --by
    agent`, either call site) may draw on -- declared + derived +
    kind-baseline grants exactly as `_effective_stage_grants` computes them,
    but excluding two runtime-grant shapes `_effective_stage_grants` itself
    correctly counts for ORDINARY coverage/denial classification (finding
    #4, root cause: both self-grant call sites reused that broader function
    unmodified):

    - `scope: "once"` runtime grants -- a single-launch bypass the user
      granted to get ONE re-launch past a denial, not a standing approval a
      self-grant should be able to lean on for a DIFFERENT rule later.
    - runtime grants the AGENT itself materialized (`granted_by ==
      AGENT_ACTOR`, recorded at materialization time in
      `cmd_resolve_permission`) -- counting these would let a self-grant
      bootstrap an ever-widening loop of self-approvals with no user
      decision anywhere in the chain.

    A runtime entry with no `granted_by` at all (materialized before this
    field existed, or via a path this function does not know about) is
    treated as NOT agent-granted -- fail-toward-inclusion here mirrors
    `_effective_stage_grants`'s own bias for ordinary coverage; the
    scope-"once" exclusion above already blocks the one shape this project
    actually reviewed as attacker-reachable (REQ5's test scenario)."""
    declared_entries, derived_entries, _dropped, _error, _note = _stage_grant_entries(state, stage_index)
    runtime_entries = [
        e for e in state.runtime_grants.get(str(stage_index), [])
        if not e.get("consumed")
        and e.get("scope") != "once"
        and (e.get("granted_by") or "").strip().casefold() != AGENT_ACTOR
    ]
    allow: list[_grants.RuleGrant] = []
    add_dirs: list[_grants.AddDirGrant] = []
    for e in declared_entries + derived_entries + runtime_entries:
        if "rule" in e:
            allow.append(_grants.RuleGrant.from_dict(e))
        elif "path" in e and "mode" in e:
            add_dirs.append(_grants.AddDirGrant.from_dict(e))
    stage = state.stage(stage_index)
    if stage.is_spawn():
        venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
        allow.extend(_kind_baseline_rule_grants(stage.spawn_kind(), workdir=venue))
    return _grants.StageGrants(allow=allow, add_dirs=add_dirs)


def _consume_once_grants(state: SessionState, stage_index: int) -> None:
    """Mark every `scope: "once"` runtime grant on this stage as consumed after
    a dispatch that used it. A once-scoped grant exists to get a single denied
    call past a single re-launch (`cmd_resolve_permission --scope once`); left
    live it would silently widen every LATER launch of the same stage index
    (a re-dispatch, a replan) beyond what that one approval covered, exactly
    the un-scoped growth the "stage" scope already avoids by staying live only
    for the stage's remaining launches. `scope: "stage"` and declared/derived
    entries carry no "scope" key (or a different one) and are untouched."""
    for entry in state.runtime_grants.get(str(stage_index), []):
        if entry.get("scope") == "once" and not entry.get("consumed"):
            entry["consumed"] = True


def _split_words_with_spans(text: str) -> list[tuple[str, int, int]] | None:
    """Split a single, separator-free command statement into its whitespace-
    delimited words. Each entry is `(unquoted_value, start, end)`, where
    `(start, end)` is the word's span in the ORIGINAL text, quotes and
    backslash-escapes included -- so a caller can rewrite only the words it
    actually changes and leave every other word's original spelling
    (quoting, escaping) byte-identical. `None` on unbalanced quoting."""
    n = len(text)
    i = 0
    words: list[tuple[str, int, int]] = []
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        buf: list[str] = []
        while i < n and not text[i].isspace():
            c = text[i]
            if c in "'\"":
                quote = c
                i += 1
                while i < n and text[i] != quote:
                    if quote == '"' and text[i] == "\\" and i + 1 < n and text[i + 1] in ('"', "\\", "$", "`"):
                        buf.append(text[i + 1])
                        i += 2
                        continue
                    buf.append(text[i])
                    i += 1
                if i >= n:
                    return None
                i += 1
                continue
            if c == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(c)
            i += 1
        words.append(("".join(buf), start, i))
    return words


def _rebase_bash_command_to_venue(command: str, drifted_cwd: str, venue: str) -> str | None:
    """Rewrite `command` as if it had been run from `venue` instead of
    `drifted_cwd`: for each RELATIVE word that names a path actually existing
    under `drifted_cwd`, replace just that word's span with that same path's
    spelling relative to `venue`. Every other word -- verbs, flags, bare
    arguments, and any word that is already an absolute path -- is left
    byte-identical, quoting and escaping included. Returns None when:
    `command` is not a single top-level statement (a compound command's
    OTHER segment may be a genuine miss that this function has no way to
    check -- see finding 1, round 4); it doesn't tokenize (unbalanced
    quoting); or rebasing changed nothing (a no-op rewrite is never worth
    re-checking against `coverage`).

    An absolute word is skipped on purpose (finding 2, round 4): rewriting
    `<venue>/scripts/x` to `scripts/x` would call a command covered under its
    RELATIVE spelling even though the child actually typed the absolute one,
    and `_segment_covered`'s own docstring says the two spellings are not
    equivalent -- that would name the cwd as the cause when the real cause is
    the absolute spelling, which a venue-launched child would still have hit.

    Text rewrite, not path resolution, by design: `grants.grant_covers_call`
    matches Bash rules against the LITERAL command string (round 3, see
    `_segment_covered`), so the only way to ask "would this be covered from
    the venue" is to produce the literal text a venue-launched child would
    actually have typed, then run it through that same matcher. Editing
    spans in place (rather than `shlex.split` + `shlex.join`, the prior
    approach) is what keeps an unchanged word's original quoting intact --
    `shlex.join` re-quotes by its own minimal-quoting heuristic, which drops
    a literal quote a derived grant still expects (finding 3, round 4)."""
    if _grants.top_level_segment_count(command) != 1:
        return None
    words = _split_words_with_spans(command)
    if not words:
        return None
    real_drifted = os.path.realpath(drifted_cwd)
    real_venue = os.path.realpath(venue)
    edits: list[tuple[int, int, str]] = []
    for word, start, end in words:
        if os.path.isabs(word):
            continue
        candidate = os.path.realpath(os.path.join(real_drifted, word))
        if not os.path.exists(candidate):
            continue
        rel = os.path.relpath(candidate, real_venue)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        edits.append((start, end, shlex.quote(rel)))
    if not edits:
        return None
    rebased = command
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        rebased = rebased[:start] + replacement + rebased[end:]
    return rebased


def _classify_transcript_denials(
    state: SessionState, stage: Stage, coverage: _grants.StageGrants, transcript_path: str | None,
) -> None:
    """After a dispatched child returns, classify every permission-denial stop its
    transcript recorded against the stage's PRE-LAUNCH coverage set: covered ->
    `materialization_defects` (the grant existed; --settings/--add-dir
    materialization failed to carry it), uncovered -> either `cwd_drift` (a Bash
    call denied only because the child ran it from a subdirectory of the stage
    venue -- rebasing the command's path-like tokens back to the venue would make
    it coverable under the SAME matcher) or `planning_misses` (a genuine miss
    the child correctly worked around without asking). A stop already present in
    any of the three ledgers by `tool_use_id` is never re-appended, so a later
    PERMISSION-REQUEST self-report for the SAME call doesn't double-count it --
    and a rescan of the same transcript never reclassifies a `cwd_drift` row.
    No resolvable transcript (no `transcript_path`, or the file is absent -- the
    common case until stage 2 wires `transcript_path` onto the cost-log ledger) is a
    no-op; the `Rule:`-line self-reported fallback is `cmd_dispatch`'s job instead."""
    if not transcript_path or not Path(transcript_path).exists():
        return
    known_ids = {m.get("tool_use_id") for m in state.materialization_defects} | \
        {m.get("tool_use_id") for m in state.planning_misses} | \
        {m.get("tool_use_id") for m in state.cwd_drift}
    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    for use in transcript_stops.parse_tool_uses(transcript_path):
        if use.stop_kind != "permission-denial" or use.tool_use_id in known_ids:
            continue
        # Finding S4: a file-tool (Edit/Read/Write/NotebookEdit) add_dir
        # materialization denial must classify the same way a Bash one does
        # — not just Bash, which is all this loop checked before.
        if use.tool_name == "Bash":
            call_input = {"command": use.command}
            call_text = use.command
        else:
            call_input = {"file_path": use.file_path}
            call_text = use.file_path
        row_base = {
            "stage_index": stage.index, "tool_use_id": use.tool_use_id,
            "tool_name": use.tool_name, "tool_input_digest": _digest(call_text),
            # The raw command/path text itself, not just its digest -- needed
            # so `cmd_dispatch`'s promotion logic (finding S2) can match a
            # later self-reported Rule: line against this SPECIFIC row by
            # prefix (`_bash_rule_command_from_call`), which a digest alone
            # cannot support since the two texts are rarely byte-identical.
            "command": call_text, "ts": _utcnow(),
        }
        if _grants.grant_covers_call(coverage, use.tool_name, call_input):
            state.materialization_defects.append({**row_base, "evidence": "transcript"})
            continue
        if use.tool_name == "Bash" and use.cwd and venue and \
                os.path.realpath(use.cwd) != os.path.realpath(venue):
            rebased = _rebase_bash_command_to_venue(use.command, use.cwd, venue)
            if rebased and _grants.grant_covers_call(coverage, "Bash", {"command": rebased}):
                state.cwd_drift.append({**row_base, "cwd": use.cwd, "rebased_command": rebased})
                continue
        state.planning_misses.append({**row_base, "asked_user": False, "source": "transcript"})


def _diagnose_materialization_defect(
    state: SessionState, store: StateStore, stage: Stage, *, action: str, evidence: str,
) -> Directive:
    """Route a PERMISSION-REQUEST whose denied call WAS covered by the stage's
    effective grant set into FAILED/DIAGNOSING -- the grant existed, materialization
    into the child's --settings/--add-dir simply failed to carry it, so re-asking the
    user (or re-dispatching unchanged) would only reproduce the same denial. Mirrors
    `cmd_record_result`'s failed-stage branch, including its two-step EXECUTING ->
    VERIFYING -> DIAGNOSING transition (`machine.TRANSITIONS` permits `diagnose`
    only from VERIFYING), but pre-frames the Declaration from the grant/call/evidence
    triple instead of leaving it for a human to write from scratch. The caller is
    responsible for having already appended the triggering row to
    `state.materialization_defects` -- this function only transitions and reports."""
    stage.outcome.status = StageStatus.FAILED.value
    note = f"permission request already covered by materialized grants ({evidence}): {action}"
    stage.outcome.actual = (stage.outcome.actual + "\n" + note) if stage.outcome.actual else note
    state.log("materialization_defect", stage=stage.index, action=action, evidence=evidence)
    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty(declaration=Declaration(
        expected=f"stage {stage.index}'s materialized grants should have covered: {action}",
        actual=f"the child was denied and asked permission for: {action}",
        mismatch="the effective grant set declares/derives coverage for this call, but "
                 "the child's own --settings/--add-dir materialization failed to carry "
                 "it through -- a materialization defect, not a planning miss.",
    ))
    store.save(state)
    return Directive(
        False, state.node, "declare",
        f"stage {stage.index} permission request was already covered by its "
        f"materialized grants ({evidence}) -- this is a materialization defect, not "
        "a planning miss; run overcome-difficulty (investigate why materialization "
        "failed to carry the grant, then critique); no re-dispatch, no user ask",
        marker="OVERCOME-DIFFICULTY",
    )


def _covers_resource(approved, requested, protected: list[str]) -> bool:
    """`Resource.covers` dispatch that also threads `protected` through for a
    `FileResource` pair -- the only kind whose `covers` accepts that kwarg
    (resources.py's other four kinds have no path-containment notion to
    protect). Kept here rather than in resources.py itself: `protected` is a
    cli.py-computed, session-specific value (repo_root/delivery_worktree/
    ledger dir), not a property of the resource pair alone."""
    if isinstance(approved, _resources.FileResource):
        return approved.covers(requested, protected=protected)
    return approved.covers(requested)


def cmd_resolve_permission(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Resume a session parked on a PERMISSION-REQUEST once the manager has the
    user's decision. The user ask is cognitive; this only records the outcome,
    clears the parked request, and hands back the continuation to re-spawn with.

    `--scope stage` additionally MATERIALIZES `--rule`/`--add-dir` entries as
    RUNTIME grants on the active stage (state.runtime_grants), validated through
    `grants.validate_rule`/`grants.validate_add_dir` -- the same validator every
    declared/derived grant passes through, so a runtime grant is never trusted
    more than either. This is ADDITIVE to the pre-existing decision/continuation
    logic below, never a replacement for it: a `--scope stage` grant with a
    validation failure refuses the WHOLE call (nothing partially recorded) so the
    manager can correct the rule/add-dir spec and retry, rather than leaving a
    parked request half-resolved."""
    state = _require(store, args.session)
    req = state.permission_request
    if req is None:
        return Directive(False, state.node, "noop", "no pending permission request to resolve")
    scope = getattr(args, "scope", "once")
    # Finding S8: neither "project" nor "global" scope materializes anything
    # anywhere in this engine -- there is no persistence path for either, so
    # a --rule/--add-dir passed alongside them was silently dropped while
    # continuations.permission_granted told the child it had been "recorded".
    # Refuse the whole call instead of accepting and discarding.
    if scope in ("project", "global") and (getattr(args, "rules", None) or getattr(args, "add_dirs", None)):
        return Directive(
            False, state.node, "noop",
            f"--rule/--add-dir is not materialized for --scope {scope} (no persistence path exists); "
            "use --scope stage or --scope once, or omit --rule/--add-dir",
        )
    by = (getattr(args, "by", None) or "").strip()
    if by.casefold() == AGENT_ACTOR and args.decision == "granted":
        # REQ5: an agent-authored --by gets no benefit of human judgment -- it
        # must instead prove every --rule/--add-dir it wants materialized
        # resolves (via the SAME plan_resources.py functions cmd_approve's own
        # ledger-stamping uses -- one resolution story, never a second one
        # trusted less) to a typed resource already covered by a resource the
        # CUSTOMER approved for this plan's order (order_approvals.py's
        # ledger, keyed by plan.order_digest). Unresolved or uncovered refuses
        # the WHOLE call fail-closed, mirroring validate_rule's refusal below.
        if scope not in ("once", "stage"):
            return Directive(False, state.node, "noop",
                              f"agent self-grant requires --scope once or stage, got {scope!r}")
        if _open_effort_fire(state):
            return Directive(False, state.node, "noop",
                              f"agent self-grant refused: {gates.AUTONOMY_REASON_OPEN_FIRE}",
                              data={"reason_class": "open-effort-fire"})
        self_rules = getattr(args, "rules", None) or []
        self_add_dirs = getattr(args, "add_dirs", None) or []
        if not self_rules and not self_add_dirs:
            return Directive(False, state.node, "noop",
                              "agent self-grant requires --rule/--add-dir naming what to grant")
        if not state.plan_path:
            return Directive(False, state.node, "noop",
                              "agent self-grant requires an approved plan (state.plan_path is empty)")
        from .plan import PlanError, load_plan as _load
        try:
            self_doc = _load(state.plan_path)
        except (OSError, PlanError) as exc:
            return Directive(False, state.node, "noop", f"agent self-grant: cannot read plan: {exc}")
        self_venue = _venue_for(self_doc)
        # The SAME union `_self_grant_resources_for_rule` checks (REQ5/finding
        # #4): the order-approvals ledger alone is not the whole coverage
        # story -- a stage's own declared/derived [stage.grants] are already
        # user-approved (as part of the plan approval) and must self-grant
        # here too, or dispatch's self_grant directive (which DOES check this
        # union) would tell the caller to run a command that then refuses.
        self_approved = list(order_approvals.boundary_resources(order_digest(self_doc)))
        self_active_stage = state.active_stage()
        if self_active_stage is not None:
            self_coverage = _effective_stage_grants_for_self_grant(state, self_active_stage.index)
            for rule_grant in self_coverage.allow:
                eff = plan_resources.resolve_rule_grant(rule_grant.rule, self_venue)
                if eff.status == "resolved":
                    self_approved.extend(eff.resources)
            for add_dir in self_coverage.add_dirs:
                eff = plan_resources.resolve_add_dir_grant(add_dir.path, add_dir.mode)
                self_approved.extend(eff.resources)
        self_protected = _resources.protected_permission_surfaces(
            repo_root=state.repo_root, delivery_worktree=state.delivery_worktree,
            ledger_dir=str(order_approvals._root(None)),
        )
        self_to_check: list = []
        for rule in self_rules:
            self_to_check.append((rule, plan_resources.resolve_rule_grant(rule, self_venue)))
        for spec in self_add_dirs:
            parsed = _parse_add_dir_spec(spec)
            if parsed is None:
                return Directive(False, state.node, "noop",
                                  f"malformed --add-dir spec (want 'PATH:read|write'): {spec}")
            path, mode = parsed
            self_to_check.append((spec, plan_resources.resolve_add_dir_grant(path, mode)))
        for label, res in self_to_check:
            if res.status != "resolved" or not res.resources:
                # Mirrors _self_grant_resources_for_rule's identical check
                # (dispatch's own self_grant path) -- a "resolved" status
                # with an EMPTY resource set (a readonly rule that resolves
                # to nothing) must still refuse here, not vacuously pass the
                # `for resource in res.resources` loop below with zero
                # iterations and zero approvals actually checked.
                reason_class = res.reason_class or "contract-unresolved"
                detail = res.reason if res.status != "resolved" else "resolves to an empty resource set"
                return Directive(
                    False, state.node, "noop",
                    f"agent self-grant refused: {label!r} is unresolved ({reason_class}): {detail}",
                    data={"reason_class": reason_class},
                )
            for resource in res.resources:
                if not any(_covers_resource(appr, resource, self_protected) for appr in self_approved):
                    return Directive(
                        False, state.node, "noop",
                        f"agent self-grant refused: {label!r} resolves to a resource not "
                        f"covered by any resource the customer approved for this order",
                        data={"reason_class": "not-approved"},
                    )
    new_entries: list[dict] = []
    # `once` materializes a runtime grant exactly like `stage` does -- the
    # difference is lifetime, not whether a --rule/--add-dir gets recorded at
    # all: `_consume_once_grants` marks a `scope: "once"` entry consumed
    # after the next dispatch, while a `scope: "stage"` entry stays live for
    # the stage's remaining launches. Without this branch also firing for
    # "once", a --scope once resolution silently dropped its --rule/--add-dir
    # on the floor instead of granting a single re-launch past the denial.
    if args.decision == "granted" and scope in ("once", "stage"):
        stage = state.active_stage()
        if stage is None:
            return Directive(False, state.node, "noop",
                              "--scope stage/once requires an active stage to grant onto")
        for rule in (getattr(args, "rules", None) or []):
            try:
                _grants.validate_rule(rule)
            except _grants.GrantValidationError as exc:
                return Directive(False, state.node, "noop", f"refused rule grant: {exc}")
            new_entries.append({
                "rule": rule, "provenance": "runtime", "consumed": False,
                "stage_title": stage.title, "scope": scope, "granted_by": by,
            })
        for spec in (getattr(args, "add_dirs", None) or []):
            parsed = _parse_add_dir_spec(spec)
            if parsed is None:
                return Directive(False, state.node, "noop",
                                  f"malformed --add-dir spec (want 'PATH:read|write'): {spec}")
            path, mode = parsed
            try:
                _grants.validate_add_dir(path, mode)
            except _grants.GrantValidationError as exc:
                return Directive(False, state.node, "noop", f"refused add_dir grant: {exc}")
            new_entries.append({
                "path": path, "mode": mode, "provenance": "runtime", "consumed": False,
                "stage_title": stage.title, "scope": scope, "granted_by": by,
            })
    if args.decision == "granted":
        cont = continuations.permission_granted(req.action, scope)
        detail = f"permission granted for {req.action}; re-spawn the stage"
    else:
        cont = continuations.permission_denied(req.action)
        detail = f"permission denied for {req.action}; re-spawn with the fallback"
    if new_entries:
        key = str(state.active_stage().index)
        state.runtime_grants.setdefault(key, []).extend(new_entries)
        _record_customer_stage_grants(state, by, scope, new_entries)
    state.permission_request = None
    # `by` + the two env overrides that steer WHICH ledger/contract-table a
    # self-grant resolved against are recorded on every call, not only a
    # self-grant one, so the audit trail is uniform regardless of who --by
    # names -- a human-attributed resolution reads the same overrides.
    state.log(
        "resolve_permission", action=req.action, decision=args.decision, by=by,
        order_approvals_dir_override=os.environ.get("AGENTCTL_ORDER_APPROVALS_DIR"),
        tool_contracts_override=os.environ.get("AGENTCTL_TOOL_CONTRACTS"),
    )
    store.save(state)
    return Directive(
        True, state.node, "continue_spawn", detail,
        data={"action": req.action, "decision": args.decision, "continuation": cont,
              "runtime_grants_added": new_entries},
    )


EVIDENCE_ROOT_ENV = "AGENTCTL_EVIDENCE_ROOT"


class EvidenceDirError(Exception):
    """Raised when the durable per-stage evidence directory cannot be resolved —
    it would fall under an OS-temp scratch root (exempt_paths.scratch_roots()), or
    grants.validate_add_dir refuses it outright. No fallback search: the caller
    fails loudly rather than silently degrading to a directory nobody granted."""


def evidence_dir_for(session_id: str, stage_index: int) -> Path:
    """Durable per-stage evidence directory: `<evidence root>/<session>/stage-<N>/`.

    `<evidence root>` is $AGENTCTL_EVIDENCE_ROOT when set (a TEST-ONLY override:
    it skips only the scratch-root refusal below, grants.validate_add_dir still
    applies to whatever it names), else `$XDG_STATE_HOME/agentctl-evidence` when
    $XDG_STATE_HOME is set to an absolute path, else `~/.local/state/agentctl-
    evidence` — deliberately NOT under `config_root.agentctl_state_dir()`
    (`~/.claude[-agent]/agentctl/state`), which `widening_targets.
    add_dir_under_protected_root` refuses outright, so a write add_dir onto the
    evidence dir (see spawn-specialist.py's load_or_create_evidence_dir) would
    have been refused at the root every stage's evidence lives under.

    Session id is sanitized with store.safe_session_id — the same scheme session state
    files are keyed by — so an adversarial session id cannot escape the
    evidence tree. Raises EvidenceDirError instead of falling back to another
    root when the resolved path is unusable — see the class docstring."""
    override = os.environ.get(EVIDENCE_ROOT_ENV)
    if override:
        root = Path(override)
        skip_scratch_check = True
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        if xdg and Path(xdg).is_absolute():
            root = Path(xdg) / "agentctl-evidence"
        else:
            root = Path.home() / ".local" / "state" / "agentctl-evidence"
        skip_scratch_check = False
    path = root / _safe_session_id(session_id) / f"stage-{int(stage_index)}"
    if not skip_scratch_check and exempt_paths.under_scratch_root(str(path)):
        raise EvidenceDirError(
            f"evidence directory {path} resolves under an OS-temp scratch root — "
            f"set ${EVIDENCE_ROOT_ENV} or $XDG_STATE_HOME to a durable location"
        )
    try:
        _grants.validate_add_dir(str(path), "write")
    except _grants.GrantValidationError as exc:
        raise EvidenceDirError(f"evidence directory {path} refused: {exc}") from exc
    return path


def cmd_evidence_dir(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Create (idempotently) and report the durable per-stage evidence directory (see
    evidence_dir_for) where a developer spawn commits checkpoint evidence as it goes —
    see spawn-specialist.py's '## Checkpoints' prompt section and the write grant
    load_or_create_evidence_dir arranges for it. Purely path-derived from --session/
    --stage — no session-state lookup — since the directory must be creatable (and
    granted to a spawned child) before dispatch necessarily reflects the stage in
    progress."""
    try:
        path = evidence_dir_for(args.session, args.stage)
    except EvidenceDirError as exc:
        return Directive(False, "n/a", "noop", str(exc))
    path.mkdir(parents=True, exist_ok=True)
    return Directive(True, "n/a", "evidence_dir", str(path) + "\n", data={"evidence_dir": str(path)})


def cmd_stage_grants(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Report the EFFECTIVE grant set for one stage — declared + derived (only when
    the live re-derivation still re-hashes to `approved_grants_sha256`, per
    grants.py's docstring) + unconsumed runtime grants — the read `dispatch_stage`
    (stage 2) is meant to consult before materializing a child's `--settings`/
    `--add-dir`. Declared/derived are read from the hash-verified
    `plan_snapshot_path`/`plan_snapshot_hash` snapshot, NEVER from live `plan_path`
    -- the snapshot is the frozen, human-approved bytes; a live plan_path may
    already carry an unreviewed in-place edit at plan-mutable PLAN_READY of the
    NEXT plan. `--stage` defaults to the session's active stage."""
    state = _require(store, args.session)
    stage_index = getattr(args, "stage", None)
    if stage_index is None:
        stage_index = state.current_stage
    if stage_index is None:
        return Directive(False, state.node, "noop", "no active stage and no --stage given")
    try:
        stage = state.stage(stage_index)
    except KeyError:
        return Directive(False, state.node, "noop", f"no stage with index {stage_index}")

    declared_entries, derived_entries, dropped, error, note = _stage_grant_entries(state, stage_index)
    runtime_entries = [
        e for e in state.runtime_grants.get(str(stage_index), [])
        if not e.get("consumed")
    ]
    # Flat, not {"declared": [...], "derived": [...], "runtime": [...]} -- every
    # entry already carries its own "provenance" (RuleGrant/AddDirGrant.to_dict,
    # and the runtime constructors in this file, all stamp it), so a second,
    # positional grouping on top just gives callers two disagreeing ways to ask
    # "what kind of grant is this" and invites them to drift apart.
    all_entries = declared_entries + derived_entries + runtime_entries

    data = {
        "stage": stage_index,
        "executor": stage.actor.executor,
        "grants": all_entries,
        "dropped": dropped,
        "error": error,
        "note": note,
    }
    if getattr(args, "json", False):
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        lines = [f"Stage {stage_index} ({stage.actor.executor}):"]
        for label, entries in (
            ("declared", declared_entries),
            ("derived", derived_entries),
            ("runtime", runtime_entries),
        ):
            if not entries:
                continue
            rendered = ", ".join(e.get("rule") or f"{e.get('path')}:{e.get('mode')}" for e in entries)
            lines.append(f"  {label}: {rendered}")
        if not all_entries:
            lines.append("  (no grants)")
        if error:
            lines.append(f"  ERROR: {error}")
        if note:
            lines.append(f"  NOTE: {note}")
        text = "\n".join(lines) + "\n"
    # A read error is reported, never swallowed into a quiet "no grants" -- ok stays
    # True (this is a report command, not a gate) but the error rides d.data/text so
    # a caller cannot mistake an unreadable snapshot for a plan that grants nothing.
    return Directive(True, state.node, "inspect", text, data=data)


def cmd_grant_stats(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Report the session's accumulated grant-classification ledgers:
    planning_misses (denials NOT covered by a stage's effective grant set --
    correctly asked the user), materialization_defects (denials the effective
    grant set DID cover, but the child's own --settings/--add-dir materialization
    failed to carry -- routes the stage to FAILED/DIAGNOSING, never a re-ask),
    settings_drift (a stage's live settings document changed underneath the
    spawn), and cwd_drift (a Bash denial that's only uncovered because the child
    ran it from a subdirectory of the stage venue -- rebasing it back to the
    venue would be covered). A pure read of state.planning_misses/
    materialization_defects/settings_drift/cwd_drift -- see state.py's schema-39
    field block for each row's shape."""
    state = _require(store, args.session)
    asked = sum(1 for m in state.planning_misses if m.get("asked_user"))
    unasked = len(state.planning_misses) - asked
    data = {
        "planning_misses": state.planning_misses,
        "materialization_defects": state.materialization_defects,
        "settings_drift": state.settings_drift,
        "cwd_drift": state.cwd_drift,
        "planning_miss_counts": {"asked": asked, "unasked": unasked},
    }
    if getattr(args, "json", False):
        text = json.dumps(data, indent=2, sort_keys=True)
    else:
        text = (
            f"planning_misses: {len(state.planning_misses)} (asked={asked}, unasked={unasked})\n"
            f"materialization_defects: {len(state.materialization_defects)}\n"
            f"settings_drift: {len(state.settings_drift)}\n"
            f"cwd_drift: {len(state.cwd_drift)}\n"
        )
    return Directive(True, state.node, "inspect", text, data=data)


def _cost_rows(args) -> list[dict]:
    """Read the cost log rows for this command, honoring the ``--cost-log`` test
    override (default: cost.COST_LOG) — the shared read behind every fire site's
    and cmd_resolve's cost-log lookup."""
    log = getattr(args, "cost_log", None)
    return cost.read_rows(Path(log) if log else cost.COST_LOG)


def _utcnow() -> float:
    """The one clock read behind every `effort.record_fire(..., now=...)` call site —
    collapses five copy-pasted `dt.datetime.now(dt.timezone.utc).timestamp()` calls."""
    return dt.datetime.now(dt.timezone.utc).timestamp()


def cmd_record_result(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    stage = state.active_stage()
    if stage is None:
        return Directive(False, state.node, "next_stage", "no active stage to record")
    actual = args.actual or ""
    stage.outcome.actual = actual
    passed = args.status == "passed"
    # Set when the mechanical check below fails for a reason worth naming to a
    # later overcome-difficulty pass without scanning state.history — currently
    # only "control_not_discriminating" (R2's negative control matched the
    # stage's expected_exit on a known-bad input).
    failure_kind: str | None = None

    # Unconditional attempt log: every record-result call for this stage
    # counts and logs, including one a later gate blocks before any verification
    # runs — a diagnosing session scanning history must see every attempt made,
    # not just the ones that got past the first gate.
    stage.outcome.record_attempts = (stage.outcome.record_attempts or 0) + 1
    state.log(
        "record_result_attempt", stage=stage.index,
        attempt=stage.outcome.record_attempts, status=args.status,
    )

    # General control-criterion attestation: optional on any stage, but required
    # non-empty for spawn:developer + passed (review is the control criterion of a
    # developer-actor stage; reviewer ⊂ controller, developer ⊂ executor).
    control = getattr(args, "control", None) or None
    if control:
        stage.control = control
    if passed and stage.needs_control() and not stage.has_control():
        store.save(state)
        return Directive(
            False, state.node, "attest_control",
            f"stage {stage.index} is a spawn:developer stage; the control criterion of a "
            "developer-produced result is review — supply it via: "
            "record-result --control '<how the code was reviewed>'",
        )

    # Code-review gate: recording a PASSED spawn:developer stage additionally requires
    # a bound passing (or user-overridden) CodeReview when the gate is active
    # (substantive session / AGENTCTL_CODE_REVIEW=1) — the SUBSTANTIVE-only structured
    # upgrade layered after the free-text control floor above (Op-Q5: the two stay
    # independent, so a knob-off/non-substantive stage's behaviour is unaffected).
    # Unlike the acceptance path there is no automated judge here — the verdict is
    # always code-reviewer/human-authored via `agentctl code-review`; this fold-in only
    # checks the pure gates.code_review_blockers, which reads solely the recorded
    # CodeReview. Placed BEFORE verify_command execution + the node transition so a
    # blocked pass never advances past EXECUTING.
    code_ref = getattr(args, "code_ref", None) or None
    if passed and stage.needs_control() and gates.code_review_active(state):
        crb = gates.code_review_blockers(
            state, stage, expected_code_sha256=_digest(code_ref) if code_ref else None,
        )
        if crb:
            store.save(state)
            return Directive(
                False, state.node, "spawn_code_review",
                f"stage {stage.index} spawn:developer pass blocked by the code-review gate — "
                "spawn the `code-reviewer` specialization to review this stage's diff, then "
                "record with `agentctl code-review --session <sid> --verdict pass|revise|override "
                "--reviewer code-reviewer [--code-ref <rev>]`, then re-run record-result",
                data={"blockers": crb},
            )

    # Observation gate: recording a PASSED stage requires a non-empty observation that
    # differs (normalized) from the expected image — CONTROL comparing the actual RESULT
    # against the stage's own goal, not just a program's exit code. Originally scoped to
    # acceptance_review stages only; broadened (Defect 2) to every stage of a SUBSTANTIVE
    # session, because "the result was checked against the goal" is a claim every stage
    # makes, not a criterion-type-specific one — a measurable stage's exit-code check
    # (further below) proves the PROGRAM ran clean, never that anyone looked at what it
    # produced. Non-substantive sessions (chat/small-change) keep the pre-Defect-2
    # behaviour: only acceptance_review stages pay this cost. An echoed target ("I saw
    # the expected result") is no observation at all.
    observation = getattr(args, "observation", None) or ""
    is_acceptance_review = stage.criterion.criterion_type == CriterionType.ACCEPTANCE_REVIEW.value
    requires_observation = is_acceptance_review or (
        state.weight_class == WeightClass.SUBSTANTIVE.value
    )
    if passed and requires_observation:
        norm_obs = gates._normalize_string(observation)
        norm_img = gates._normalize_string(stage.subject.result)
        reason = (
            "is acceptance_review" if is_acceptance_review
            else "is a substantive-session stage (Defect 2: control compares result "
                 "with goal at every stage)"
        )
        if not norm_obs:
            store.save(state)
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} {reason}; pass requires recording an observation — "
                f"{OBSERVATION_CONTRACT} "
                "(supply: record-result --observation '<what you observed>')",
            )
        if norm_obs == norm_img:
            store.save(state)
            return Directive(
                False, state.node, "attest_observation",
                f"stage {stage.index} {reason}; pass requires recording an observation, "
                "not echoing the target — "
                f"{OBSERVATION_CONTRACT} "
                "(supply: record-result --observation '<what you observed>')",
            )
        # bind the observation to the stage now so the judge gate's sha recompute
        # (run below, AFTER the mechanical check) sees it.
        stage.criterion.observation = observation

    # Delivered-head freeze: stamp what commit this stage delivered BEFORE any
    # verification below dispatches — a landed check self-referencing this same
    # stage must find its own frozen head already present (see
    # _freeze_delivered_head). Only when some landed check in the plan actually
    # names this stage as its delivered_stage (_needs_delivered_head_freeze) —
    # a plan with no landed check makes no extra runner call, unchanged from
    # before schema 23.
    if _needs_delivered_head_freeze(state, stage.index):
        _freeze_delivered_head(state, stage, runner)

    # Machine-executed verification: for a measurable stage carrying a verify_command
    # (or a `kind = "landed"` check), the engine runs it and OVERRIDES a 'passed'
    # claim the command contradicts. A contradicted pass becomes a real failure
    # (digest + DIAGNOSING), so "report honestly" is an invariant for the
    # measurable subset, not a discipline. This runs BEFORE the cheap judge
    # below — a check the command itself contradicts must never spend a judge call.
    if passed:
        crit = stage.criterion
        if crit.criterion_type == CriterionType.MEASURABLE.value and crit.verify_kind == CheckKind.LANDED.value:
            ok, refusal, result = _landed_check_result(state, crit.landed, runner)
            if refusal:
                store.save(state)
                return Directive(
                    False, state.node, "fix_venue",
                    f"stage {stage.index} landed check refused: {refusal}",
                )
            if not ok:
                passed = False
                note = (
                    f"landed check: delivered commit not (yet) contained in "
                    f"{crit.landed.target!r} (or {crit.landed.remote}/{crit.landed.target})"
                )
                actual = (actual + "\n" + note) if actual else note
                stage.outcome.actual = actual
        else:
            cwd = None
            has_check = crit.verify_command and crit.criterion_type == CriterionType.MEASURABLE.value
            if has_check:
                cwd, refusal = _resolve_or_refuse(state, crit.verify_venue)
                if refusal:
                    store.save(state)
                    return Directive(
                        False, state.node, "fix_venue",
                        f"stage {stage.index} verify_command refused: {refusal}",
                    )
            # Green-check cache: a repeated record-result call whose venue tree
            # hasn't moved AND whose check definition hasn't changed since the last
            # fully-green run (the positive command AND, if declared, its
            # negative_control) skips re-running BOTH commands and goes straight to
            # the judge below. checked_tree_ok is only ever set True once the WHOLE
            # check has cleared -- see state.py's Outcome docstring -- so a cache hit
            # never needs to re-prove the control discriminates: the tree it
            # discriminated against hasn't moved, under the same definition it
            # discriminated with. A red or non-discriminating result is never
            # cached, so a genuinely broken check or control is always re-run
            # rather than trusted to still be broken. Only computed when there is
            # an actual check to cache (measurable + verify_command) — an
            # acceptance_review stage's verify_command, if present, is never
            # machine-run, so identity must never be probed for it either. The
            # tree half assumes the check reads only the venue tree (see
            # _venue_tree_identity) — a check that also reads files outside it
            # (the plan TOML, proposals.json, ...) can be served stale even though
            # its own venue is unchanged. The definition half
            # (_check_definition_digest) is what catches an edited verify_command
            # or negative_control on an otherwise-unmoved tree (R2 review B) — the
            # plan-refresh path (_apply_refined_stage_fields) resets a PASSED
            # stage's own status on any carry-key change, but never touched these
            # two fields, so an unchanged tree + an edited control used to replay
            # a stale cache hit straight past the (now different) control.
            tree_identity = _venue_tree_identity(cwd, runner) if has_check else None
            identity = (
                f"{tree_identity}:{_check_definition_digest(crit)}"
                if tree_identity is not None else None
            )
            cache_hit = _cached_check_hit(stage, identity)
            if cache_hit:
                ok, result = True, None
            else:
                ok, result = _verify_command_result(stage, runner, cwd=cwd)
            if not ok:
                passed = False
                note = (
                    f"verify_command exit {result.returncode} != expected "
                    f"{stage.criterion.expected_exit}: {stage.criterion.verify_command}"
                )
                actual = (actual + "\n" + note) if actual else note
                stage.outcome.actual = actual
                if identity is not None:
                    stage.outcome.checked_tree_identity = identity
                    stage.outcome.checked_tree_ok = False
            elif not cache_hit and has_check and crit.negative_control:
                # The positive check just went green -- now prove it CAN go red.
                # Fed the known-bad input, a matching exit code means the check
                # never discriminated bad state from good in the first place, so
                # the stage does not pass even though the positive check did.
                neg_matched, neg_result = _run_check(
                    crit.negative_control, crit.expected_exit, runner, cwd=cwd,
                )
                if neg_result.returncode in NEGATIVE_CONTROL_REFUSED_EXIT_CODES:
                    # The control command itself never ran (typo, missing binary) —
                    # a fact about the exit code alone, not a judgment on whether the
                    # bad input was caught. Refuse like an unresolvable venue: never
                    # cache, never spend a verdict either way, tell the author to fix
                    # the command text.
                    store.save(state)
                    return Directive(
                        False, state.node, "fix_control",
                        f"stage {stage.index} negative_control refused: exit "
                        f"{neg_result.returncode} (command not found or not "
                        f"executable): {crit.negative_control}",
                    )
                if neg_matched:
                    passed = False
                    note = (
                        f"negative_control exit {neg_result.returncode} == expected "
                        f"{crit.expected_exit} (did not fail on bad input): "
                        f"{crit.negative_control}"
                    )
                    actual = (actual + "\n" + note) if actual else note
                    stage.outcome.actual = actual
                    failure_kind = "control_not_discriminating"
                    state.log(
                        "control_not_discriminating",
                        stage=stage.index, negative_control=crit.negative_control,
                    )
                    if identity is not None:
                        stage.outcome.checked_tree_identity = identity
                        stage.outcome.checked_tree_ok = False
                else:
                    state.log("negative_control_discriminates", stage=stage.index)
                    if identity is not None:
                        stage.outcome.checked_tree_identity = identity
                        stage.outcome.checked_tree_ok = True
            elif not cache_hit:
                # Neither branch above fired: no negative_control to run (a
                # waiver, or no check at all). A cache hit falls through this
                # whole chain untouched -- the tree and the check definition
                # already cleared on a prior call, so there is nothing left to
                # do before the judge below.
                if has_check and crit.negative_control_waiver:
                    state.log(
                        "negative_control_waived",
                        stage=stage.index, reason=crit.negative_control_waiver,
                    )
                if identity is not None:
                    stage.outcome.checked_tree_identity = identity
                    stage.outcome.checked_tree_ok = True

    # Cheap-judge COGNITION + PURE gate (runs AFTER the mechanical check above, so
    # a check the command itself contradicts never spends a judge call).
    # When the acceptance-review gate is active (substantive session /
    # AGENTCTL_STAGE_REVIEW=1), run the fail-open haiku judge over the observation,
    # record its verdict as a StageReview bound to the observation bytes, then block
    # the pass on gates.acceptance_review_blockers (which reads that record AND any
    # fail_open JudgeBypass bound to the same observation). A judge call that itself
    # fails (disabled/errored/timed out) now records a `fail_open` JudgeBypass and
    # the pass proceeds — fail-open in effect, not just in name.
    if passed and requires_observation:
        if gates.stage_review_active(state):
            judge_runner = runner if runner is not None else advisor.subprocess_runner
            verdict, judge_reason = advisor.acceptance_judge(
                observation, stage.subject.result, judge_runner, enabled=True,
                timeout=advisor._ACCEPTANCE_JUDGE_TIMEOUT_S)
            obs_sha = _observation_sha256(observation)
            if verdict is not None:
                state.log(
                    "acceptance_judge_verdict", stage=stage.index, verdict=verdict,
                    reason=judge_reason, observation_sha256=obs_sha,
                )
                _record_stage_review(
                    state,
                    StageReview(
                        stage_index=stage.index, verdict=verdict,
                        reviewer=advisor.JUDGE_REVIEWER, note=judge_reason,
                        observation_sha256=obs_sha,
                    ),
                    from_judge=True,
                )
            else:
                # Fail-open: the judge CALL ITSELF failed (disabled/errored/timed out).
                # Record it as a bypass bound to THIS observation so the pure gate below
                # waves the pass through instead of stalling it indefinitely — a judge
                # outage is not evidence against the observation. Log the judge's own
                # reason so a session review can tell an outage apart from a genuine
                # revise even if the caller only looks at the recorded bypass. Skip the
                # recording entirely when a standing revise already binds to this exact
                # observation: the gate already refuses to let such a bypass override
                # that revise (gates.standing_revise_bound_to), so recording one here
                # would only add a misleading entry to any surface that lists bypasses
                # without also checking for the standing review.
                state.log("acceptance_judge_fail_open", stage=stage.index, reason=judge_reason)
                if not gates.standing_revise_bound_to(state, stage.index, obs_sha):
                    _record_bypass(state, JudgeBypass(
                        stage_index=stage.index, kind="fail_open", reviewer="",
                        note=judge_reason, observation_sha256=obs_sha))
            ab = gates.acceptance_review_blockers(state, stage)
            if ab:
                store.save(state)
                detail = f"stage {stage.index} acceptance pass blocked by the judge gate"
                data = {"blockers": ab}
                if verdict is None:
                    detail += f" (judge call failed: {judge_reason})"
                    data["judge_reason"] = judge_reason
                return Directive(
                    False, state.node, "attest_observation",
                    detail,
                    data=data,
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

    # Read the cost log unconditionally — not just for is_spawn() stages — because
    # effort.refresh_spend (below) sums by plan_path alone and needs to see the
    # engine-mandated review spawns too, which attribute_stage's stage_index filter
    # would otherwise hide from this read.
    _rows = _cost_rows(args)
    # Attribute cost for spawn stages from the cost log. In-thread stages leave
    # None — cost splitting per in-thread stage is out of scope for this attribution.
    if stage.is_spawn():
        _attr = cost.attribute_stage(_rows, state.plan_path, stage.index)
        stage.outcome.cost_usd = _attr["cost_usd"]
        stage.outcome.duration_ms = _attr["duration_ms"]
        stage.outcome.spawn_count = _attr["spawn_count"]

    # Effort-divergence spend refresh (call site 1) + fire check. Accounting runs
    # unconditionally — only ACTING on a fire is gated by gates.effort_active — see
    # effort.py's module docstring and gates.effort_active's docstring.
    effort.refresh_spend(state, _rows, state.plan_path)
    _flush_order_effort(state)
    div = effort.divergence(state, cross_session_totals=_effort_cross_totals(state))
    if effort.armed(state):
        _cross = _effort_cross_totals(state)
        _ro_delta = effort.effective_deltas(state, cross_session_totals=_cross)
        _ro_comparand = effort.comparands(state, Thresholds())
        for _ro_scale in effort.RECORD_ONLY_SCALES:
            if _ro_comparand[_ro_scale] > 0 and _ro_delta[_ro_scale] >= _ro_comparand[_ro_scale]:
                effort.record_crossing(
                    state, _ro_scale, actual=_ro_delta[_ro_scale],
                    estimate=_ro_comparand[_ro_scale], now=_utcnow(),
                )

    state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING

    if passed:
        stage.outcome.status = StageStatus.PASSED.value
        if observation:
            stage.criterion.observation = observation
        state.current_stage = None
        state.log("record_result", stage=stage.index, status="passed")
        if div is not None and gates.effort_active(state):
            now = _utcnow()
            fire = _record_effort_fire(state, div, now=now)
            return _diagnose_effort_divergence(state, store, div, fire)
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
                               runner, weight_class=state.weight_class,
                       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
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
            data={"failure_kind": failure_kind} if failure_kind else {},
        )
    # enter the overcome-difficulty sub-spine: a fresh Difficulty record must be
    # worked through (declare -> investigate -> critique) before replan is allowed.
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    state.difficulty = Difficulty()
    data = {"failure_kind": failure_kind} if failure_kind else {}
    if div is not None and gates.effort_active(state):
        # Already entering DIAGNOSING for the stage failure — attach the divergence
        # instead of re-transitioning or opening a second Difficulty, but still honor
        # divergence()'s CALLER OBLIGATION (record the fire so it doesn't re-trip).
        now = _utcnow()
        data["effort_divergence"] = _record_effort_fire(state, div, now=now)
    store.save(state)
    return Directive(
        False, state.node, "declare",
        f"stage {stage.index} failed; run overcome-difficulty — declare the divergence, "
        "then investigate, then critique; replan is blocked until the cycle is complete",
        marker="OVERCOME-DIFFICULTY",
        data=data,
    )


def cmd_verify_final(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    # Idempotency guard (must precede the node-agnostic resolution gate below):
    # verify-final's body performs node transitions — `final` -> RESOLUTION on
    # success, `diagnose` -> DIAGNOSING on a refusal/failure — that are legal ONLY
    # from VERIFYING (machine.TRANSITIONS). The resolution gate checks stage
    # outcomes, not the node, so a re-invocation after a prior refusal/failure has
    # already routed the session to DIAGNOSING would re-enter and raise an uncaught
    # TransitionError. From any node other than VERIFYING, return a legible
    # directive instead of re-running the transitioning body.
    if state.node == Node.DIAGNOSING.value:
        return Directive(
            False, state.node, "declare",
            "a prior final-verification refusal/failure already routed this session "
            "into the difficulty cycle; complete overcome-difficulty (declare -> "
            "investigate -> critique -> replan — replan re-arms verification) before "
            "re-running verify-final",
            marker="OVERCOME-DIFFICULTY",
        )
    if state.node == Node.RESOLUTION.value:
        return Directive(
            True, state.node, "resolve",
            "final verification already passed; the resolution gate is armed — "
            "run `resolve` once the user confirms",
        )
    if state.node != Node.VERIFYING.value:
        return Directive(
            False, state.node, "inspect",
            f"verify-final runs only from VERIFYING; node is {state.node}",
        )
    blockers = gates.blockers(state, "resolution")
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        failing_ids = gates.failing_acceptance_requirements(state)
        if failing_ids:
            return _diagnose_acceptance_rejection(state, store, failing_ids)
        return Directive(False, state.node, "fix_stages", "not ready for resolution", data={"blockers": blockers})
    # Effort-divergence spend refresh (call site 2) + divergence computation — fire
    # site 2. Hoisted here, ahead of the stage-verification loop below, so a venue
    # refusal or a failing final_check can attach this SAME live divergence via
    # _diagnose_venue_refusal / the failures branch, instead of only the clean-pass
    # path reaching it — mirrors record_result's failed branch, which attaches
    # divergence data at its own failure point rather than solely on success. A
    # separate cost-log read from the rollup below: refresh_spend needs every row by
    # plan_path (including engine-mandated review spawns no stage attributes), the
    # rollup needs only what record-result already stamped onto each Outcome.
    effort.refresh_spend(state, _cost_rows(args), state.plan_path)
    _flush_order_effort(state)
    div = effort.divergence(state, cross_session_totals=_effort_cross_totals(state))
    if effort.armed(state):
        _cross = _effort_cross_totals(state)
        _ro_delta = effort.effective_deltas(state, cross_session_totals=_cross)
        _ro_comparand = effort.comparands(state, Thresholds())
        for _ro_scale in effort.RECORD_ONLY_SCALES:
            if _ro_comparand[_ro_scale] > 0 and _ro_delta[_ro_scale] >= _ro_comparand[_ro_scale]:
                effort.record_crossing(
                    state, _ro_scale, actual=_ro_delta[_ro_scale],
                    estimate=_ro_comparand[_ro_scale], now=_utcnow(),
                )
    # Final-gate execution (defense in depth): re-run every measurable stage's
    # verify_command — a later stage may have regressed an earlier one. Any
    # non-match refuses RESOLUTION rather than trusting the recorded PASSED flags.
    failures: list[str] = []
    for stage in state.stages:
        crit = stage.criterion
        if crit.criterion_type == CriterionType.MEASURABLE.value and crit.verify_kind == CheckKind.LANDED.value:
            # No re-freeze here: verify-final re-checks the delivered_head each
            # stage's own record-result already froze, never a live re-derive.
            ok, refusal, result = _landed_check_result(state, crit.landed, runner)
            if refusal:
                return _diagnose_venue_refusal(
                    state, store, f"stage {stage.index} landed check refused: {refusal}", div,
                )
            if not ok:
                failures.append(
                    f"stage {stage.index}: landed check — delivered commit not "
                    f"contained in {crit.landed.target!r} (or "
                    f"{crit.landed.remote}/{crit.landed.target})"
                )
            continue
        cwd = None
        if crit.verify_command and crit.criterion_type == CriterionType.MEASURABLE.value:
            cwd, refusal = _resolve_final_or_refuse(state, crit)
            if refusal:
                return _diagnose_venue_refusal(
                    state, store, f"stage {stage.index} verify_command refused: {refusal}", div,
                )
        ok, result = _verify_command_result(stage, runner, cwd=cwd)
        if not ok:
            failures.append(
                f"stage {stage.index}: exit {result.returncode} != "
                f"{stage.criterion.expected_exit} ({stage.criterion.verify_command})"
            )
    for fc in state.final_check:
        if fc.kind == CheckKind.LANDED.value:
            ok, refusal, result = _landed_check_result(state, fc.landed, runner)
            if refusal:
                return _diagnose_venue_refusal(
                    state, store, f"final_check '{fc.label or fc.landed.target}' refused: {refusal}", div,
                )
            if not ok:
                label = fc.label or fc.landed.target
                failures.append(
                    f"final_check '{label}': landed check — delivered commit not "
                    f"contained in {fc.landed.target!r} (or "
                    f"{fc.landed.remote}/{fc.landed.target})"
                )
            continue
        cwd, refusal = _resolve_or_refuse(state, fc.venue)
        if refusal:
            return _diagnose_venue_refusal(
                state, store, f"final_check '{fc.label or fc.command}' refused: {refusal}", div,
            )
        ok, result = _run_check(fc.command, fc.expected_exit, runner, cwd=cwd)
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
        data = {"failures": failures}
        if div is not None and gates.effort_active(state):
            # Already entering DIAGNOSING for the failure — attach the divergence
            # instead of re-transitioning or opening a second Difficulty, mirroring
            # record_result's failed branch (still honoring divergence()'s CALLER
            # OBLIGATION: record the fire so it doesn't re-trip).
            now = _utcnow()
            data["effort_divergence"] = _record_effort_fire(state, div, now=now)
        store.save(state)
        return Directive(
            False, state.node, "declare",
            "final verification command(s) failed; run overcome-difficulty — declare "
            "the divergence, then investigate, then critique; replan is blocked until "
            "the cycle is complete",
            data=data,
        )
    # Fire check on a clean pass (fire site 2) — the last point at which `diagnose`
    # is still a legal transition (a contracted plan can reach here with no further
    # record-result to fire from).
    if div is not None and gates.effort_active(state):
        now = _utcnow()
        fire = _record_effort_fire(state, div, now=now)
        return _diagnose_effort_divergence(state, store, div, fire)

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
    acceptance_bypass = _acceptance_bypass_surface(state)
    if acceptance_bypass is not None:
        data["acceptance_bypass"] = acceptance_bypass
        detail += "; WARNING: acceptance recorded via bypass, not judge corroboration (see acceptance_bypass)"
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
    # Session-end cleanup: drop any sidecar a background enumeration wrote for this
    # session, whether or not it was ever folded (e.g. an outstanding child from the
    # LAST replan before resolve, whose result nobody will ever read now).
    enumerate_sidecar.discard_all_for_session(state.session_id)
    tracker_key = getattr(state, "tracker_key", None)
    if not tracker_key and solved_marker.key_shape(state.task_id):
        tracker_key = state.task_id
    # Realized budget-tier labels for this task, for budget-calibration.py to group
    # spend by (kind x tier) and by task-type against. Joined by plan_path — the
    # same key attribute_stage already uses — rather than session_id, whose
    # semantics (the SPAWNING session's CLAUDE_CODE_SESSION_ID) are not guaranteed
    # to equal state.session_id. [] when no spawn ever ran (in-thread task) or
    # plan_path is unset; a spawn row with a missing/null tier is skipped.
    _rows = _cost_rows(args)
    budget_tiers = sorted({
        r["budget_tier"] for r in _rows
        if r.get("plan_path") == state.plan_path and r.get("budget_tier")
    })
    # Effort-divergence surface (the plan's named mitigation for the self-set-estimate
    # weakness): carries both vectors into the quality ledger so the thresholds — and
    # the estimate() formula itself — can be recalibrated against what they actually
    # caught, not just whether this one session happened to fire.
    _effort_ratios = [v for v in effort.ratios(state).values() if v is not None]
    quality_row = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "task_id": state.task_id,
        "tracker_key": tracker_key,
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
        # n_normalizations counts every "normalize" event regardless of in_diagnosis;
        # the outside-diagnosis variant is the subset explicitly flagged
        # in_diagnosis=False (a DIAGNOSING-closure normalize never sets that key at all,
        # so it is correctly excluded here without needing an in_diagnosis=True mirror).
        "n_normalizations": sum(1 for h in state.history if h.get("event") == "normalize"),
        "n_normalizations_outside_diagnosis": sum(
            1 for h in state.history
            if h.get("event") == "normalize" and h.get("in_diagnosis") is False
        ),
        "n_renormalize_replans": sum(1 for h in state.history if h.get("event") == "renormalize"),
        "spawn_count": cost_surface.get("spawn_count", 0),
        "total_cost_usd": cost_surface.get("total_cost_usd"),
        "weight_class": state.weight_class,
        "deliverable_kind": state.deliverable_kind or None,
        "route": state.route,
        "budget_tiers": budget_tiers,
        "effort_estimate": state.effort_estimate,
        "effort_actual": effort.deltas(state),
        "effort_ratio_max": max(_effort_ratios) if _effort_ratios else None,
        "effort_fires": state.effort_fires,
        "effort_interactions": state.user_prompt_count,
        "planning_misses_asked": sum(
            1 for m in state.planning_misses if m.get("asked_user")
        ),
        "planning_misses_unasked": sum(
            1 for m in state.planning_misses if not m.get("asked_user")
        ),
        "materialization_defects": len(state.materialization_defects),
    }
    _write_quality_row(quality_row)
    # Whether to stamp is fully decidable from observed state (resolved + a known
    # tracker key) — a rule, not a judgement — so it runs unconditionally here rather
    # than being left for the coordinator to remember. Belt-and-suspenders around
    # solved_marker.stamp's own fail-open contract: resolution has already happened
    # and must never be undone by a marker failure.
    try:
        marker_status = solved_marker.stamp(tracker_key)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        marker_status = {"channel": None, "key": tracker_key, "stamped": False,
                          "skipped_reason": str(exc)}
    detail = "task resolved"
    data = {"cost": cost_surface, "quality": quality_row, "solved_marker": marker_status}
    # Bypass visibility: the resolution summary surfaces every acceptance judge bypass
    # verbatim, so a resolved task's record shows which acceptance passes were unjudged
    # (kill switch) or overridden — never hidden behind a clean COMPLETED.
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f" (with {len(bypasses)} acceptance judge bypass(es); see judge_bypassed)"
    acceptance_bypass = _acceptance_bypass_surface(state)
    if acceptance_bypass is not None:
        data["acceptance_bypass"] = acceptance_bypass
        detail += " (acceptance recorded via bypass; see acceptance_bypass)"
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
    state.log("declare", expected=args.expected, actual=args.actual, mismatch=args.mismatch)
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
    # getattr-with-default: in-process Namespace callers (test_replan.py builds one by
    # hand with only the two required fields) must keep working — an absent field is None.
    failure_address = getattr(args, "failure_address", None)
    # Reject a bogus value HERE (mirroring cmd_normalize's --level check) so an in-process
    # caller bypassing the argparse `choices` is caught before the Critique is built — this
    # (with argparse) is the ONLY validation of the value set. The closure gate
    # (failure_address_blockers) checks only non-None, so a bogus value must never reach a
    # persisted record; that also grandfathers a legacy OLD-value record on load. None is
    # allowed at critique time — the routing may be recorded now or left for a later replan,
    # where the closure gate demands it be present.
    if failure_address is not None and failure_address not in FAILURE_ADDRESS_VALUES:
        return Directive(False, state.node, "critique",
                         f"--failure-address must be one of {list(FAILURE_ADDRESS_VALUES)} "
                         f"or omitted, got {failure_address!r}")
    state.difficulty.critique = Critique(
        functional_ground=args.functional_ground,
        replanning_task=args.replanning_task,
        invariants_to_preserve=list(getattr(args, "invariants_to_preserve", None) or []),
        differences_to_remove=list(getattr(args, "differences_to_remove", None) or []),
        failure_address=failure_address,
    )
    state.log(
        "critique",
        functional_ground=state.difficulty.critique.functional_ground,
        replanning_task=state.difficulty.critique.replanning_task,
        invariants_to_preserve=state.difficulty.critique.invariants_to_preserve,
        differences_to_remove=state.difficulty.critique.differences_to_remove,
        failure_address=state.difficulty.critique.failure_address,
    )
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
    }, runner, weight_class=state.weight_class,
       runtime_host_=state.runtime_host or runtime_host.HOST_CLAUDE)
    return d


def _normalize_record_fields(factor, level, destination) -> tuple[str | None, str | None]:
    """Validate a normalize record's --factor/--level/--destination the one way every
    caller shares. Returns (error, cleaned_factor); error is None exactly when the
    fields are usable."""
    factor = (factor or "").strip()
    if not factor:
        return ("normalize requires a non-empty --factor (the reproducible cause "
                "being re-normed)"), None
    if level is not None and level not in NORMALIZATION_LEVELS:
        return (f"normalize --level must be one of {list(NORMALIZATION_LEVELS)} or "
                f"omitted (payoff-gated by rediscovery-threshold-min), got {level!r}"), None
    if destination is not None and destination not in NORMALIZATION_DESTINATIONS:
        return (f"normalize --destination must be one of "
                f"{list(NORMALIZATION_DESTINATIONS)} or omitted (the functional "
                f"place the renorming lands on), got {destination!r}"), None
    return None, factor


def _log_normalize_event(state: SessionState, *, factor, level, destination, in_diagnosis: bool) -> None:
    """The one `normalize` event shape every caller shares. `in_diagnosis` is present
    on the event ONLY when False — a DIAGNOSING-closure record never carries the key
    at all, which is what n_normalizations_outside_diagnosis's `is False` check (not
    a truthiness check) relies on to exclude it."""
    if in_diagnosis:
        state.log("normalize", factor=factor, level=level, destination=destination)
    else:
        state.log("normalize", factor=factor, level=level, destination=destination,
                  in_diagnosis=False)


def cmd_normalize(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Phase 4 (closure): record the renorming act. Mandatory at DIAGNOSING closure —
    a reproducible factor left un-normed re-fails, so replan is blocked (see
    gates.normalization_blockers) until this records the factor, or the user takes the
    explicit --normalization-waiver escape for a genuinely one-off factor. The LEVEL
    (note/leaf/principle) is payoff-gated and may be omitted, and so may the DESTINATION —
    the functional place the act lands on. The two are ORTHOGONAL: `--level` says how
    generally the record is written down, `--destination` says what is being repaired, and
    every destination is recordable at every level.

    Also callable OUTSIDE DIAGNOSING (any node from CLASSIFIED up to, but not
    including, RESOLVED) as a plain record-only act -- a normalization worth writing
    down does not always arrive framed as a difficulty closure. That branch never
    touches `state.difficulty` or any gate, and logs the SAME `normalize` event shape
    with `in_diagnosis=False` so every reader of the DIAGNOSING event still recognizes
    it. Dispatch happens FIRST, before any of this function's own validation, so a
    DIAGNOSING call reaches byte-identical code below."""
    state = _require(store, args.session)
    if state.node != Node.DIAGNOSING.value:
        return _cmd_normalize_outside_diagnosing(args, state, store)
    bad = _require_diagnosing(state)
    if bad:
        return bad
    d = state.difficulty
    if d.declaration is None or d.investigation is None or d.critique is None:
        return Directive(False, state.node, "declare",
                         "normalize is out of order: declaration, investigation, and "
                         "critique must come first")
    error, factor = _normalize_record_fields(
        getattr(args, "factor", None), getattr(args, "level", None), getattr(args, "destination", None))
    if error:
        return Directive(False, state.node, "normalize", error)
    level = getattr(args, "level", None)
    destination = getattr(args, "destination", None)
    d.normalization = Normalization(factor=factor, level=level, destination=destination)
    _log_normalize_event(state, factor=factor, level=level, destination=destination, in_diagnosis=True)
    store.save(state)
    return Directive(True, state.node, "replan",
                     "renorming recorded; replan is now unblocked")


def _cmd_normalize_outside_diagnosing(args, state: SessionState, store: StateStore) -> Directive:
    """The record-only sibling of cmd_normalize's DIAGNOSING branch -- runs at any
    node from CLASSIFIED up to, but not including, RESOLVED (a RESOLVED session has
    no further quality row or scorecard pass for the record to land on), touches no
    difficulty state and no gate, and logs the same `normalize` event shape with
    `in_diagnosis=False` so a reader scanning history for the event still finds it."""
    if state.node == Node.RESOLVED.value:
        return Directive(False, state.node, "continue",
                         "normalize outside a difficulty cannot record against a "
                         "RESOLVED session")
    error, factor = _normalize_record_fields(
        getattr(args, "factor", None), getattr(args, "level", None), getattr(args, "destination", None))
    if error:
        return Directive(False, state.node, "normalize", error)
    level = getattr(args, "level", None)
    destination = getattr(args, "destination", None)
    _log_normalize_event(state, factor=factor, level=level, destination=destination, in_diagnosis=False)
    store.save(state)
    return Directive(True, state.node, "continue",
                     "renorming recorded outside a difficulty cycle")


def _renormalize_replan(args, state, store: StateStore, runner: Runner | None) -> Directive:
    """The light path: the executor replaces his own SEQUENCE of operations.

    `Means.method` is the REQUIREMENT on the way of acting — the planner's and the
    customer's, moved only through the review and approval a replan re-arms. `Means.
    procedure` is the sequence proposed for meeting it, and it is the EXECUTOR's:
    reading the code routinely shows a better order, and making him buy that order at
    the price of a re-approval is what produces the two failures the field split exists
    to remove — an executor who follows a worse sequence because it is written down, or
    one who quietly rewrites what he is held to, neither visible in the diff as what it
    is.

    `--renormalize` is therefore a TYPED CLAIM, not a verdict of `diff_plans`: the
    executor says "the procedure is the only thing I changed", and the engine checks it
    (`gates.renormalization_blockers`) rather than inferring it. Keeping it off
    `diff_plans` leaves that function's three-word vocabulary and every caller intact,
    and makes the refusal message able to name the norm the claim turned out to touch.

    Three things this path deliberately does NOT do, each because it is not a replan:

    * It does not run `_apply_refined_stage_fields`. Only `means.procedure` is copied
      onto the live stages, so the claim the engine just verified stays true of the live
      state too — no recorded `criterion.observation`, no `outcome`, no status is
      disturbed, and the comparison stage 8 requires of a passed stage keeps standing.
    * It does not re-arm a FAILED stage or leave DIAGNOSING. Working a difficulty
      through is a separate obligation with its own record; re-sequencing inside a
      stage does not discharge it.
    * It logs `renormalize`, not `replan`, so `effort.replan_count` — which fires the
      divergence trigger at three — counts norm revisions and not an executor using the
      authority the plan gave him.

    It DOES re-stamp `accepted_plan_digest`: that field must name the bytes the session
    is executing, and after this call those are `args.plan`'s. The one consequence is
    that an AcceptanceReview recorded before a renormalization goes stale — the
    fail-closed direction, and nearly unreachable in practice since an acceptance is
    recorded once every stage has already passed."""
    from .plan import load_plan as _load

    # Backfill a snapshot for a legacy (pre-snapshot) session BEFORE this path rewrites
    # plan_path, exactly as the no_change branch of cmd_replan does and for a sharper
    # reason: without it `old_path` falls back to plan_path, which after one
    # renormalization holds the RENORMALIZED bytes, and the walk-in-small-steps this
    # branch claims to prevent would be open on precisely the sessions that have no
    # snapshot. Best-effort (see _snapshot_approved_plan); a None leaves the prior
    # fallback, so nothing here can refuse a renormalization.
    if not (state.plan_snapshot_path and Path(state.plan_snapshot_path).exists()):
        backfilled = _snapshot_approved_plan(store, state)
        if backfilled:
            state.plan_snapshot_path, state.plan_snapshot_hash = backfilled
    old_path = _replan_baseline_path(state)
    # Lenient OLD / strict NEW, for the reason cmd_replan's own loads document: the
    # comparison baseline may be a snapshot frozen before a newer trunk tightened the
    # schema, and only the incoming plan is held to today's submission grade. Comparing
    # against the SNAPSHOT (not plan_path) is also what makes successive renormalizations
    # honest: each is measured against the bytes that were approved, so a norm edit
    # cannot be walked to in small steps.
    old = _load(old_path, strict=False)
    # Read args.plan's bytes ONCE and derive `new`, the digest stamped below and the
    # snapshot written below from that same buffer -- three separate re-reads of a
    # file the coordinator can edit concurrently used to leave `accepted_plan_digest`
    # and the plan-version snapshot each free to bind to different bytes than the
    # ones `new` was diffed and gated against.
    new, _new_plan_bytes, _new_plan_digest = load_plan_with_digest(args.plan)
    run = runner if runner is not None else advisor.subprocess_runner
    submission = _submission_problems(new, run, state.weight_class)
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "renormalization blocked: the corrected plan does not meet "
                         "submission requirements",
                         data={"problems": submission})
    refusals = gates.renormalization_blockers(old, new)
    _log_gate(state, "renormalization", refusals, passed=not refusals)
    if refusals:
        return Directive(False, state.node, "replan",
                         "not a renormalization: this edit reaches the norm, not just "
                         "the sequence of operations — drop --renormalize and replan it "
                         "through the review and approval it is owed",
                         data={"blockers": refusals})
    changed: list[int] = []
    for ns in new.stages:
        try:
            cur = state.stage(ns.index)
        except KeyError:
            continue
        if cur.means.procedure != ns.means.procedure:
            changed.append(ns.index)
        cur.means.procedure = ns.means.procedure
    state.plan_path = args.plan
    state.accepted_plan_digest = _new_plan_digest
    # renormalize is one of the replan kinds every submitted version is
    # content-addressed for, despite logging a distinct event name (see the class
    # docstring above on why it's `renormalize`, not `replan`).
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest)
    _new_plan_sha256 = _new_plan_digest
    state.log("renormalize", stages=changed, plan=args.plan,
              prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        "renormalized: the procedure of "
        + (f"stage(s) {changed}" if changed else "no stage")
        + " was replaced; every norm the plan sets is unchanged",
        data={"stages": changed})


def _replan_cause(state: SessionState, explicit_reason: str | None) -> dict:
    """D9 history capture: why THIS replan happened. Preferred source is the active
    Difficulty record's critique — functional_ground/replanning_task are exactly the
    "why" the overcome-difficulty cycle already produces, so this reads them rather
    than asking the caller to retype them. Falls back to an optional free-text
    --reason for a bare, non-DIAGNOSING replan (a plain refinement/no_change with no
    difficulty behind it). Returns {} when neither is available — a replan with no
    stated cause is captured as such, never refused for lacking one."""
    critique = state.difficulty.critique if state.difficulty is not None else None
    if critique is not None:
        return {
            "cause_source": "difficulty",
            "functional_ground": critique.functional_ground,
            "replanning_task": critique.replanning_task,
        }
    reason = (explicit_reason or "").strip()
    if reason:
        return {"cause_source": "reason", "reason": reason}
    return {}


def cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    d = _cmd_replan(args, store=store, runner=runner)
    # Only on something to say: an unconditional setdefault would add an empty
    # `advisories` key to every replan Directive, which test_history_capture's
    # exact-equality pin reads as a changed contract.
    advisories = _replan_spelling_advisories(args.plan)
    if advisories:
        d.data.setdefault("advisories", []).extend(advisories)
    return d


def _replan_spelling_advisories(plan_path: str) -> list[str]:
    """The mixed-spelling lint over the NEW plan, on every Directive `replan`
    returns — refused ones included, since the author is about to edit the plan
    anyway. A plan that does not load has nothing to lint; the refusal the
    replan already returned names why."""
    from .plan import PlanError, load_plan as _load

    try:
        doc = _load(plan_path, strict=False)
    except (OSError, PlanError):
        return []
    return grant_shadow.mixed_spelling_advisories(doc)


def _cmd_replan(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    from .plan import diff_plans, load_plan as _load, stage_carry_key

    # --normalize-factor lets a replan record a normalize event in the SAME call,
    # instead of requiring a separate `normalize` invocation first. Validated here
    # (cheap, touches no state) so a bad flag refuses before any gate runs below.
    # Deliberately does NOT mutate state.difficulty.normalization: this command
    # saves state at several points (the enumeration-bag fold, the stale-
    # disposition invalidation) that sit ahead of later refusals (the
    # plan_approval-plugin block, the critique-coverage gate), and a mutation
    # staged here would ride one of those saves onto disk as an orphan if a
    # later gate then refused the replan — persisted with no normalize event
    # ever logged for it, and invisible to a retry once the hint at the bottom
    # of this function stops asking for the now-apparently-already-set flag.
    # gates.normalization_blockers instead takes this pending factor as an
    # explicit argument at its call site below, so the gate is satisfied
    # without state.difficulty being touched; the record is set together with
    # the event at the single logging point past every refusal (see below).
    normalize_factor = (getattr(args, "normalize_factor", None) or "").strip()
    normalize_level = getattr(args, "normalize_level", None)
    if normalize_factor and getattr(args, "renormalize", False):
        return Directive(False, state.node, "replan",
                         "--normalize-factor and --renormalize are incompatible: a "
                         "renormalization is defined as touching no norm, so it has "
                         "nothing for --normalize-factor to record")
    if normalize_factor:
        _nf_error, normalize_factor = _normalize_record_fields(normalize_factor, normalize_level, None)
        if _nf_error:
            return Directive(False, state.node, "replan", _nf_error)
        # An incomplete DIAGNOSING cycle is left to gates.difficulty_blockers below
        # (same action, "declare") rather than repeated here with a second message.

    # precondition: an unacknowledged effort-divergence fire (whether it forced this
    # DIAGNOSING entry itself via the passing-diagnose path, or merely rode along on
    # a failing stage/final-check's Directive) must be explicitly decided via
    # `agentctl fire-acknowledge` before the plan may be re-sequenced — this is the
    # gap the FAILING branches of record_result/verify_final left open (they attach
    # the fire data but never force a decision on it). Checked FIRST, ahead of
    # difficulty_blockers, since it is orthogonal to the declare/investigate/
    # critique/normalize cycle that difficulty_blockers governs.
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if efblock:
        return _effort_fire_refusal(state, "replan", efblock)

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

    # diagnosing-replan renegotiation gate (GitHub #177): once this task's
    # cross-session replan_count reaches the ceiling
    # (`effort-replan-absolute`) while inside DIAGNOSING, a further replan is
    # refused until the order's customer has made an explicit renegotiation
    # decision. [] (and this whole block a no-op) outside DIAGNOSING and below
    # the ceiling — gates.diagnosing_replan_blockers itself returns [] there.
    # Placed AFTER effort_fire_blockers/difficulty_blockers (an unacknowledged
    # fire or an incomplete difficulty record must still be resolved through the
    # ordinary path first) and BEFORE normalization_blockers/failure_address_
    # blockers/plan_review_blockers/renormalize (those are properties of the
    # CORRECTED PLAN or of the sequence it changes; this gate is about whether
    # another replan should happen at all, a strictly prior question).
    cross_replan_count = int(
        task_accumulator.get(state.task_id).get("per_axis_totals", {}).get("replan_count", 0) or 0
    )
    rrblock = gates.diagnosing_replan_blockers(state, task_replan_count=cross_replan_count)
    _log_gate(state, "diagnosing_replan", rrblock, passed=not rrblock)
    if rrblock:
        decision = getattr(args, "renegotiation_decision", None)
        if not decision:
            state.renegotiation_ceiling_difficulty_id = _difficulty_id(state.difficulty)
            store.save(state)
            return Directive(
                False, state.node, "renegotiate", "replan blocked: " + rrblock[0],
                marker=DIRECTIVE_ESCALATE_TO_USER,
                data={"blockers": rrblock, "replan_count": cross_replan_count},
            )
        renegotiated_by = (getattr(args, "renegotiated_by", None) or "").strip()
        if not renegotiated_by:
            return Directive(
                False, state.node, "renegotiate",
                "--renegotiation-decision requires a non-empty --renegotiated-by",
                data={"blockers": rrblock},
            )
        renegotiation_note = (getattr(args, "renegotiation_note", None) or "").strip()
        if not renegotiation_note:
            return Directive(
                False, state.node, "renegotiate",
                "--renegotiation-decision requires a non-empty --renegotiation-note",
                data={"blockers": rrblock},
            )
        try:
            order_doc = _load(args.plan, strict=False)
        except (OSError, PlanError):
            order_doc = None
        order = order_doc.meta.order if order_doc is not None else None
        if renegotiated_by.casefold() == AGENT_ACTOR:
            refusal = _agent_renegotiation_refusal(state, decision, order_doc)
            if refusal is not None:
                return Directive(
                    False, state.node, "renegotiate",
                    f"renegotiation by the agent refused: {refusal}",
                    marker=DIRECTIVE_ESCALATE_TO_USER,
                    data={"blockers": rrblock, "reason": refusal},
                )
            state.agent_ack_difficulty_ids.append(_difficulty_id(state.difficulty))
        elif order is not None and order.customer_id and renegotiated_by != order.customer_id:
            return Directive(
                False, state.node, "renegotiate",
                f"renegotiation author {renegotiated_by!r} does not match order "
                f"customer_id {order.customer_id!r}; record it as the customer of "
                "record, or correct --renegotiated-by",
                data={"blockers": rrblock},
            )
        state.renegotiations.append({
            "decision": decision,
            "note": renegotiation_note,
            "by": renegotiated_by,
            "ts": _utcnow(),
            "task_replan_count_at_decision": cross_replan_count,
        })
        state.log("renegotiation", decision=decision, by=renegotiated_by)
        if decision == "abandon":
            # mirrors cmd_block's own bypass-transition idiom (and fire-acknowledge's
            # "abandon" decision) — parks reversibly via unblock, never RESOLVED, and
            # never touches args.plan.
            state.blocked_from = state.node
            state.node = Node.BLOCKED.value
            state.log("block", reason=f"renegotiation abandoned: {renegotiation_note}")
            store.save(state)
            return Directive(
                True, state.node, "unblock",
                "session abandoned after DIAGNOSING-replan renegotiation; unblock to resume",
                marker="ESCALATE",
                data={"renegotiation": state.renegotiations[-1]},
            )
        # continue/rescope: fold this renegotiation's effect the same way task-reset
        # does (task_accumulator.reset is now a deliberate second caller — see its
        # docstring) and fall through to the rest of this command unchanged. This is
        # also the concrete fix for GitHub #201: effort.py's own REPLANS-scale
        # effective_deltas() reads this exact accumulator field against the same
        # static ceiling and never resets it itself, which is why an unbounded
        # renegotiation-free loop could re-fire on every subsequent replan; zeroing
        # it here means the closing replan starts the scale's next
        # budget from zero instead.
        task_accumulator.reset(state.task_id)

    # The renormalization branch sits HERE deliberately: after difficulty_blockers (a
    # renormalization offers a whole plan, and offering one while the difficulty record
    # is still incomplete is how a difficulty gets re-plannned away rather than worked
    # through) and after the no-plan check it needs a baseline from, but BEFORE the two
    # CLOSURE preconditions below and the plan-review and plan_approval gates after them.
    # The closure preconditions are conditions of LEAVING the DIAGNOSING cycle, and this
    # path does not leave it: blocking a re-sequencing on them demanded a re-norming as
    # the price of an act that closes nothing, and — worse — a `--normalization-waiver`
    # passed with the flag would have been spent, and logged, on a call that never
    # discharged the difficulty. The gates below govern the NORM, which is the very thing
    # this path is refused for touching: making an executor re-arm a review to reorder his
    # own operations is the cost the field split exists to remove.
    if getattr(args, "renormalize", False):
        return _renormalize_replan(args, state, store, runner)

    # closure precondition: a difficulty exposed a norm-failure; closing it (leaving the
    # DIAGNOSING cycle) REQUIRES re-norming the reproducible factor. Mandatory-if-
    # reproducible; the one-off escape is an explicit --normalization-waiver <reason>.
    # [] outside the DIAGNOSING-closure path, so a non-difficulty replan is unaffected.
    # `pending_factor` is this call's OWN still-unmutated --normalize-factor: the gate
    # judges it directly rather than reading state.difficulty.normalization, which this
    # command has deliberately not written yet (see the comment above).
    agent_acked_difficulty = (
        state.difficulty is not None
        and _difficulty_id(state.difficulty) in state.agent_ack_difficulty_ids
    )
    if agent_acked_difficulty and getattr(args, "normalization_waiver", None) is not None:
        _log_gate(state, "normalization_waiver", ["agent-acknowledged fire"], passed=False)
        return Directive(
            False, state.node, "normalize",
            "replan refused: the agent-acknowledged fire's difficulty closes on its real "
            "normalization record; a --normalization-waiver is not accepted",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"blockers": ["normalization waiver on an agent-acknowledged fire"]},
        )
    nblock = gates.normalization_blockers(state, pending_factor=normalize_factor or None)
    if nblock:
        waiver = getattr(args, "normalization_waiver", None)
        if waiver is None:
            _log_gate(state, "normalization_blockers", nblock, passed=False)
            return Directive(False, state.node, "normalize",
                             "replan blocked: difficulty closure requires re-norming",
                             data={"blockers": nblock})
        if not waiver.strip():
            _log_gate(state, "normalization_blockers", nblock, passed=False)
            return Directive(False, state.node, "normalize",
                             "normalization waiver reason must not be empty",
                             data={"blockers": nblock})
        # a conscious, recorded bypass for a genuinely one-off factor — never a bypass
        # of the difficulty-record completeness precondition checked above.
        state.log("normalization_waived", reason=waiver, blockers=list(nblock))
        _log_gate(state, "normalization_waiver", nblock, passed=True)
    else:
        _log_gate(state, "normalization_blockers", nblock, passed=True)

    # closure precondition (R2): the fault must be ROUTED — the critique's failure_address
    # must be recorded (ресурсное/нормативное/not_applicable обеспечение), never a bare
    # omission — before the difficulty may be closed. Mirrors normalization_blockers: an
    # INTERNAL precondition (absent from GUARDIANS), [] outside the DIAGNOSING-closure
    # path, so a non-difficulty replan is unaffected. Unlike the normalization gate there
    # is no waiver — the explicit escape is a legal not_applicable on the critique itself.
    fablock = gates.failure_address_blockers(state)
    _log_gate(state, "failure_address_blockers", fablock, passed=not fablock)
    if fablock:
        return Directive(False, state.node, "critique",
                         "replan blocked: the fault must be routed to the inadequate "
                         "обеспечение (re-run critique with --failure-address)",
                         data={"blockers": fablock})

    # plan-review gate: the corrected plan (args.plan) must carry a thinker review
    # with a passing/overridden verdict BOUND to it before it may be applied. Gates
    # EVERY replan kind (refinement and substantive alike, per the user decision),
    # inactive for non-substantive sessions. A genuine no-op replan (args.plan ==
    # the already-reviewed plan_path) passes on the existing review.
    prblock = gates.plan_review_blockers(state, args.plan)
    _log_gate(state, "plan_review", prblock, passed=not prblock)
    if prblock:
        # With the round-release valve active the blockers ARE the release message,
        # which says no further review is required — so the refusal must stop
        # prescribing one, or the engine would name as the cure the very act the
        # valve just retired, and the loop would have no exit. This is the path the
        # valve exists for: post-approval replan is where review cycles recur.
        round_release = _note_round_release(state, prblock, store)
        if round_release:
            message = (
                "replan blocked: the review-round budget is spent, so the decision is "
                "the coordinator's — see blockers"
            )
            replan_data = {"blockers": prblock, "plan_review_round_release": round_release}
        else:
            try:
                review_doc = _load(args.plan)
            except (OSError, PlanError):
                review_doc = None
            delta = gates.review_delta(state, review_doc, args.plan)
            if delta["record_scope_args"]:
                review_commands = [
                    "plan-review --target " + args.plan + " " + scope_arg
                    for scope_arg in delta["record_scope_args"]
                ]
            else:
                review_commands = ["plan-review --target " + args.plan]
            message = (
                "replan blocked: the corrected plan needs a thinker review "
                "(run: " + "; run: ".join(review_commands)
                + f"; render with `{delta['render_command']}`)"
            )
            replan_data = {"blockers": prblock, "plan_review_round_release": round_release,
                           "review_delta": delta}
        return Directive(False, state.node, "plan_review", message, data=replan_data)

    # Submission seam (b): the single NEW-side load and the check it feeds. Its placement
    # answers two separate orderings at once.
    # Before the enumeration/plan_approval block below, because `_launch_enumeration` there
    # is destructive and PERSISTED: it clears the premise bag's enumeration record back to
    # not-run, bumps the launch counter, pins `enumerate_launch_digest` to the PROPOSED
    # bytes, stamps a deadline and spawns a detached worker over them — and the
    # `enumeration_bag_dirty` save below writes all of that to disk. Refusing after that
    # would destroy the live session's bag in the name of a plan this command rejected,
    # leaving the still-current plan blocked on an enumeration axis it was never at fault
    # for. A command that refuses must not mutate persisted state. Both siblings already
    # read this way: cmd_submit_plan validates before its own `_launch_enumeration`, and
    # cmd_approve folds only after seam (c)'s refusal, for the same stated reason.
    # Before diff_plans further down, so all three diff outcomes are covered by one check —
    # a no_change replan re-materializes live stages from these bytes just as a refinement
    # does, so "unchanged" is no reason to let an unvalidated plan in.
    # Entry-point fallback — see cmd_submit_plan's identical comment. Bound here rather
    # than below the refusals because this seam's own judged refusal needs it; binding a
    # callable spends nothing, and the judge is reached only on a prefilter hit.
    run = runner if runner is not None else advisor.subprocess_runner
    # Read args.plan's bytes ONCE and derive `new`, the digest stamped below (seam c)
    # and the snapshot _refresh_approved_grant_snapshot writes from this SAME buffer —
    # `_load(args.plan)` used to be a separate read from the digest stamp and the
    # snapshot write, leaving a window for a concurrent edit to desynchronize them.
    new, _new_plan_bytes, _new_plan_digest = load_plan_with_digest(args.plan)
    submission = _submission_problems(new, run, state.weight_class)
    if submission:
        return Directive(False, state.node, "fix_plan",
                         "replan blocked: the corrected plan does not meet submission "
                         "requirements",
                         data={"problems": submission})

    # replan-authorization gate: outside DIAGNOSING, a non-substantive edit
    # (refinement or no_change) to an ALREADY APPROVED plan must have been
    # presented to the user as a diff and proven delivered before it may be
    # applied — the write-side twin of plan_presentation_blockers (see that
    # gate's docstring on state.py's PlanPresentation). The kind fed to the
    # gate is computed from the SAME baseline this command's own diff (below)
    # uses, via _replan_baseline_path, so the kind the gate reasons about is
    # the kind that will actually be applied. Placed strictly after the
    # submission refusal above (a plan that does not meet submission grade is
    # not worth authorizing) and strictly before the plan_approval PLUGIN
    # block below, whose enumeration folding is destructive and PERSISTED —
    # nothing that may refuse can follow it; this command has still written
    # nothing to disk at this point.
    auth_kind = _kind_within_boundary(
        state, diff_plans(_load(_replan_baseline_path(state), strict=False), new), new)
    arblock = gates.replan_authorization_blockers(state, args.plan, diff_kind=auth_kind)
    _log_gate(state, "replan_authorization", arblock, passed=not arblock)
    if arblock:
        return Directive(False, state.node, "present_plan",
                         "replan blocked: this plan edit has not been authorized by "
                         "the user",
                         data={"blockers": arblock})

    # plan_approval PLUGIN gate: mirror cmd_approve's plugins.plugin_gate_blockers
    # composition so a refinement/no_change replan cannot rotate the plan bytes back
    # to VERIFYING while a premise-plugin blocker (undispositioned question, stale
    # per-stage rebind) goes unchecked — the 2026-07-09 attest-vs-execute hole.
    # Evaluate against the PROPOSED plan (args.plan): premise_blockers reads
    # state.plan_path, so swap it for the call and restore in finally. [] when no
    # plugin extends plan_approval, so the row still logs (passed, empty) and the
    # replan is byte-identical to before.
    _saved_plan_path = state.plan_path
    try:
        state.plan_path = args.plan
        # Re-enumerate the CORRECTED plan before pblock folds premise_blockers over
        # it. Gated on args.plan's content digest, not on diff_plans' kind below: a
        # relaunch is owed exactly when the enumerated bytes moved, which is not what
        # refinement-vs-substantive classifies.
        bag = state.plugins.get("premise")
        enumeration_bag_dirty = False
        proposed = None
        if bag is not None:
            try:
                proposed = _load(args.plan)
            except (OSError, PlanError):
                proposed = None
            if proposed is not None:
                enumeration_bag_dirty = _fold_enumeration_sidecar(state, proposed, args.plan)
                proposed_digest = plugins_premise._plan_content_digest(proposed)
                # Suppressed while a window for these exact bytes is still open: a
                # relaunch would invalidate the escape just recorded against the launch
                # counter. The trade this makes, including its deliberate lack of an
                # expiry, is in docs/operations/detached-enumeration-design.md.
                outstanding = (not bag.get("enumerated")
                               and bag.get("enumerate_launch_digest") == proposed_digest)
                # Owed when a PART moved, not when the whole-plan digest did: a plan
                # whose composite rotated because a stage was deleted introduces no
                # bytes anyone has yet to read.
                owed = (not bag.get("enumerated")
                        or plugins_premise.enumeration_is_stale(bag, proposed))
                if owed and not outstanding:
                    _launch_enumeration(state, bag, proposed, args.plan)
                    enumeration_bag_dirty = True
        pblock = plugins.plugin_gate_blockers(state, "plan_approval")
    finally:
        state.plan_path = _saved_plan_path
    if enumeration_bag_dirty:
        # AFTER the finally restored plan_path — a save inside the swapped block
        # would persist the PROPOSED plan as the session's current one. Before the
        # pblock return below, because that path refuses without reaching any of
        # cmd_replan's own save sites: unsaved, the deadline stamp Stage 5's escape
        # reads would never exist on disk, and the not-run clear would leave the
        # bag pinned to the superseded digest — i.e. the inescapable
        # _ENUMERATE_STALE, the exact routing the clear exists to prevent.
        # What this save may legitimately persist is bounded from ABOVE, not here:
        # seam (b) has already accepted these bytes, so every refusal still ahead
        # (pblock, critique coverage) is one the session reached on a plan that met
        # submission requirements — never on bytes it was about to reject outright.
        store.save(state)
    # Invalidate dispositions whose cited stage fields moved in the proposed plan so
    # the mismatch is visible in question-list output even when this replan is blocked
    # by the gate (#123). Runs here — after the try-finally restored plan_path and
    # before the pblock return — so a blocked replan still surfaces stale notes on disk.
    _inv_bag = state.plugins.get("premise")
    if _inv_bag is not None:
        _inv_stage_keys = {s.index: stage_element_keys(s) for s in new.stages}
        _inv_meta_keys = plan_meta_element_keys(new)
        _inv_changed = premise.invalidate_stale_dispositions(
            _inv_bag, _inv_stage_keys, meta_keys=_inv_meta_keys
        )
        _inv_changed = (
            premise.invalidate_stale_order_dispositions(_inv_bag, _inv_stage_keys)
            or _inv_changed
        )
        if _inv_changed:
            store.save(state)
    _log_gate(state, "plan_approval_plugin", pblock, passed=not pblock)
    if pblock:
        # The escape counts ride THIS refusal for cmd_approve's reason — the coordinator
        # reading it is about to decide whether to add to them — and with more force
        # here: `question-enumerate-escape --plan` exists FOR the replan path, so the
        # person most likely to record an escape is the one reading this payload.
        # Against the PROPOSED plan's digest, not state.plan_path's, since that is the
        # plan version the blocker above speaks for. `proposed` is None here only when
        # `bag is None` — and then _enumeration_escape_counts returns None outright on
        # its own bag-None check, before ever looking at `doc`, so the fallback to
        # `state.plan_path` inside that function is never reached from THIS call site.
        # The other way `proposed` could be None — `_load(args.plan)` raising above —
        # cannot reach this line at all: with `bag is not None`, `plugin_gate_blockers`
        # (via `premise_blockers`) calls `plan.load_plan(state.plan_path)` — state.plan_path
        # having been set to args.plan a few lines up — with no try/except around it, so
        # the SAME load failure raises out of `pblock = plugins.plugin_gate_blockers(...)`
        # above and this `if pblock:` block is never entered. Verified against the code,
        # not assumed — see the stage-5-dispatch report for the trace.
        return Directive(False, state.node, "close_questions",
                         "replan blocked: the corrected plan carries unresolved "
                         "plan_approval premises (dispose open questions / rebind "
                         "stale per-stage bindings against the new plan)",
                         data={"blockers": pblock,
                               "enumeration_escapes": _enumeration_escape_counts(
                                   state, proposed)})
    # #8: diff against the plan AS APPROVED (the immutable snapshot), not plan_path —
    # which the coordinator may have edited in place. Absent a snapshot (legacy
    # session, or an approve that predates the field) fall back to plan_path.
    old_path = _replan_baseline_path(state)
    # OLD side is a read-only comparison baseline: a snapshot frozen before a
    # newer trunk tightened the schema (free-text executors #7, or a later-required
    # substantive field like [stage.principle].derivation) must stay diffable — the
    # lenient load keeps the structural parse but skips every submission-grade
    # check. Only the NEW side — loaded strictly at seam (b) above — and submit-plan
    # are strict.
    old = _load(old_path, strict=False)
    # Content-address both sides of this replan -- the OLD baseline (read fresh;
    # `old_path` may be a legacy plan_path fallback never snapshotted before) and the
    # NEW plan, reusing the (data, digest) seam already read via `load_plan_with_digest`
    # rather than re-reading args.plan a second time. Computed once here, past every
    # refusal above, and used by whichever of the three kind branches below applies.
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest)
    _new_plan_sha256 = _new_plan_digest
    # coverage gate: inside the difficulty flow, the corrected plan must CARRY the
    # critique's similarities into conditions/invariants and CHANGE a means/method
    # for the declared differences. Empty split -> [] -> behaves exactly as before.
    if state.difficulty and state.difficulty.critique:
        cov = gates.replan_coverage_blockers(old, new, state.difficulty.critique)
        _log_gate(state, "replan_coverage", cov, passed=not cov)
        if cov:
            waiver = getattr(args, "coverage_waiver", None)
            if agent_acked_difficulty and waiver is not None:
                _log_gate(state, "replan_coverage_waiver", ["agent-acknowledged fire"], passed=False)
                return Directive(
                    False, state.node, "declare",
                    "replan refused: the agent-acknowledged fire's corrected plan must pass the "
                    "critique coverage check; a --coverage-waiver is not accepted",
                    marker=DIRECTIVE_ESCALATE_TO_USER,
                    data={"coverage_blockers": cov, "blockers": ["coverage waiver on an agent-acknowledged fire"]},
                )
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

    # Past every refusal above, so this replan is going through -- the single point
    # where a --normalize-factor passed to this call both sets
    # state.difficulty.normalization (in DIAGNOSING, mirroring cmd_normalize's own
    # DIAGNOSING branch) and gets its event logged. Setting the record and logging
    # the event together here, rather than staging the record earlier, is what keeps
    # a refused-then-retried call from either double-counting the event or
    # persisting an orphaned record with no event to match it.
    if agent_acked_difficulty and (
        diff_plans(old, new) == "no_change"
        or not any((d or "").strip() for d in state.difficulty.critique.differences_to_remove)
    ):
        return Directive(
            False, state.node, "replan",
            "replan refused: the agent-acknowledged fire's corrected plan must change the "
            "operative surface against a critique that names a difference to remove",
            marker=DIRECTIVE_ESCALATE_TO_USER,
            data={"blockers": ["corrected plan changes no operative surface, or no difference to remove"]},
        )
    if normalize_factor:
        if state.node == Node.DIAGNOSING.value:
            state.difficulty.normalization = Normalization(
                factor=normalize_factor, level=normalize_level, destination=None)
        _log_normalize_event(state, factor=normalize_factor, level=normalize_level,
                             destination=None, in_diagnosis=(state.node == Node.DIAGNOSING.value))

    kind = _kind_within_boundary(state, diff_plans(old, new), new)
    # The replan-loop counterpart of cmd_approve's reset: a replan that gets this far has
    # applied a corrected plan, so the rounds spent arguing about the previous one are
    # settled and the next loop starts from zero. Placed here — past every refusal of this
    # command and common to all three of its success branches — so a REFUSED replan never
    # silently refills the budget it was blocked by.
    #
    # The `--renormalize` early return above is deliberately NOT reset: that path changes
    # the sequence without touching the norm under review (it demands no review at all and
    # logs `renormalize` precisely so the effort trigger does not read it as a norm
    # revision), so the rounds already spent still belong to the same norm and stay
    # against it.
    #
    # Fold into the cross-session task accumulator (item B) before the reset, same
    # reasoning as cmd_approve's fold above.
    task_accumulator.add(
        state.task_id, "plan_review_rounds", state.plan_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    task_accumulator.add(
        state.task_id, "code_review_rounds", state.code_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    state.plan_review_rounds = 0
    state.plan_review_counted_digest = ""
    # Reset alongside the pair above (item A) — same reasoning: rounds spent
    # code-reviewing the previous plan version are settled once a corrected plan lands.
    state.code_review_rounds = 0
    # Terminal-pass custody — same "current approval cycle" boundary as the
    # pair above: a replanned plan starts with no pass on record for any scope.
    state.plan_review_passes = {}
    # Stamped HERE and not up at the seam: every refusal path of this command is now behind
    # us — the last of them being the critique-coverage gate just above — so like seam (a)
    # the digest only ever names bytes the session ACCEPTED. (Not an enumeration: this
    # command refuses in eight or so places, and a new one added below this line would
    # break the property no matter how the list above it reads.)
    # (The load of args.plan can also raise out of the command, but it raises AT seam (b)
    # itself — the seam IS that load and the check it feeds — so no placement inside this
    # range answers for it.)
    # Stamping at seam (b) itself is now positively WRONG, not merely fragile: the
    # enumeration block's `store.save` sits between that seam and the refusals above, so a
    # digest stamped up there can be persisted for a plan the pblock or coverage gate
    # then rejects — whenever that save runs at all. Placement, not the absence of an intervening save, is what carries the
    # invariant — and the leak would be a silently wrong digest, not a crash.
    # Stamped from the SAME buffer `new` was parsed from (seam (b) above), not a
    # fresh re-read of args.plan (the former `_stamp_accepted_plan_digest(state,
    # args.plan)`) — so this digest can never name bytes different from the ones
    # `new` was diffed and gated against.
    state.accepted_plan_digest = _new_plan_digest

    # Seam (b)'s advice channel, attached to whichever of this command's several success
    # Directives is returned — an echo never changes which one that is. Computed HERE, on
    # the same "every refusal is behind us" property the digest stamp just above relies on,
    # and not up at the seam it belongs to: the judge costs live `claude -p` calls per
    # flagged stage, so a replan blocked by submission or by critique coverage must not pay
    # for advice that is then discarded. Placement is about cost only — the advice is
    # warn-only and cannot influence any decision above it either way. (`run` is bound at
    # the seam above; only this CALL is deferred, which is where the cost is.)
    echo_advice = _submission_advice(new, run, state.weight_class)
    # Non-blocking hint naming --normalize-factor -- advisory only, never a gate (a
    # replan with nothing reproducible to re-norm is a completely ordinary replan).
    # Suppressed when this call itself recorded one (the `not normalize_factor`
    # check) and when the most recent `normalize`/`replan`/`submit_plan` event in
    # history is a `normalize` -- covering both a prior separate DIAGNOSING
    # `normalize` call (the gate's own required path) and a prior `normalize`
    # OUTSIDE DIAGNOSING (record-only, no gate at all): either way the hint would
    # otherwise falsely claim "no normalize event recorded" right after one was.
    _last_normalize_plan_event = next(
        (h for h in reversed(state.history)
         if h.get("event") in ("normalize", "replan", "submit_plan")), None)
    _normalize_recorded_since_last_plan_event = (
        _last_normalize_plan_event is not None
        and _last_normalize_plan_event.get("event") == "normalize")
    if not normalize_factor and not _normalize_recorded_since_last_plan_event:
        echo_advice = echo_advice + [
            "no normalize event recorded for this replan; pass --normalize-factor "
            "<reproducible cause> to record one in the same call"
        ]

    # if we are exiting the DIAGNOSING cycle (difficulty complete), the failed
    # stage is re-armed and we leave the cycle back to VERIFYING so next_stage can
    # retry it; the difficulty record is cleared so a later failure starts fresh.
    diagnosing = state.node == Node.DIAGNOSING.value

    # D9 capture: WHY this replan happened, read off the active Difficulty record
    # (already populated by declare/investigate/critique) BEFORE any branch below
    # clears it. Every branch below either sets state.difficulty = None (substantive,
    # unconditionally; no_change/refinement, when diagnosing) or leaves it — so this is
    # the last point at which the record is guaranteed still present. Falls back to an
    # optional --reason for a bare (non-DIAGNOSING) replan; never refuses when both are
    # absent, since this is a capture and not a gate.
    replan_cause = _replan_cause(state, getattr(args, "reason", None))

    # Effort-divergence spend refresh (call site 3): book against the OLD plan_path
    # BEFORE any branch below may rewrite state.plan_path — the opposite ordering from
    # each branch's own rederive() call, which must run AFTER that branch's stage-list
    # update. A cost row a just-spawned plan review wrote against the NEW path (args.plan)
    # is not lost by refreshing against the old path here — it is simply picked up by
    # the NEXT refresh against the new path, once a branch below rewrites plan_path.
    effort.refresh_spend(state, _cost_rows(args), state.plan_path)
    _flush_order_effort(state)

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
            _refresh_runtime_grant_titles(state, ns.index, ns.title)
        # final_check is meta-level (not per-stage), so it needs its own refresh
        # next to the stage loop above — a self-diffed no_change still means the
        # FILE changed relative to what was cached at submit-plan/last replan.
        state.final_check = new.meta.final_check
        _sync_venue_from_plan(state, new)
        # …and plan_path follows the bytes too, as it does on the refinement and
        # substantive branches. "no_change" names the DIFF, not the file: the stages,
        # final_check and venue above were all just re-materialized from `args.plan`, so
        # leaving plan_path on the previous file would leave the session executing one
        # path's content while every later fresh load (the premise gate, verify-final,
        # the next replan's baseline) reads a different path — and accepted_plan_digest,
        # stamped on `args.plan`, would name bytes plan_path does not point at.
        state.plan_path = args.plan
        # Unconditional refresh (not just a legacy backfill when no snapshot exists at
        # all): a no_change replan can still carry corrected bytes for stages this
        # diff kind doesn't cover (prose/verify only, per the diff), so the approved
        # snapshot and approved_grants_sha256 must track `new` every time, not only
        # the first time a session ever takes a snapshot.
        _refresh_approved_grant_snapshot(state, store, new, _new_plan_bytes, _new_plan_digest)
        if diagnosing:
            for s in state.stages:
                if s.outcome.status == StageStatus.FAILED.value:
                    s.outcome.status = StageStatus.PENDING.value
            state.difficulty = None
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
            state.log("replan", kind="no_change", exited_diagnosing=True,
                      prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
                      **replan_cause)
            task_accumulator.add(
                state.task_id, "replan_count", 1, session_id=state.session_id, now=_utcnow(),
            )
            effort.rederive(state)  # re-derive AFTER logging so this in-flight replan is counted
            store.save(state)
            if state.ready_stages():
                return _with_advisories(Directive(
                    True, state.node, "next_stage",
                    "difficulty worked through; plan unchanged — retry the re-armed stage"), echo_advice)
            return _with_advisories(Directive(
                True, state.node, "continue", "difficulty worked through; resume execution"), echo_advice)
        effort.rederive(state)  # re-derive the estimate from the (possibly refined) stages
        store.save(state)
        return _with_advisories(Directive(
            True, state.node, "continue", "replan is a no-op; plan unchanged"), echo_advice)

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
            _refresh_runtime_grant_titles(state, ns.index, ns.title)
            if cur.outcome.status == StageStatus.FAILED.value:
                cur.outcome.status = StageStatus.PENDING.value
        state.plan_path = args.plan
        # Same unconditional refresh as the no_change branch above: a refinement
        # touches stage prose/means/verify, which can shift a stage's DERIVED grants
        # even though grants_sha256 growth is already checked by diff_plans/
        # _grants_grew before this branch is ever reached -- the snapshot bytes and
        # approved_grants_sha256 must track `new`, not the plan this session most
        # recently APPROVED via a full `approve` call.
        _refresh_approved_grant_snapshot(state, store, new, _new_plan_bytes, _new_plan_digest)
        _sync_venue_from_plan(state, new)
        state.final_check = new.meta.final_check
        if diagnosing:
            state.difficulty = None
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
        state.log("replan", kind="refinement", exited_diagnosing=diagnosing,
                  prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
                  **replan_cause)
        task_accumulator.add(
            state.task_id, "replan_count", 1, session_id=state.session_id, now=_utcnow(),
        )
        effort.rederive(state)  # re-derive AFTER logging so this replan is counted
        store.save(state)
        if state.node == Node.VERIFYING.value and state.ready_stages():
            return _with_advisories(Directive(
                True, state.node, "next_stage", "refinement applied; retry the ready stage"), echo_advice)
        return _with_advisories(Directive(
            True, state.node, "continue", "refinement applied; resume execution"), echo_advice)

    # substantive: re-arm the plan-approval gate, reload stages, return to PLAN_READY.
    # #12: carry PASSED status forward for any stage whose FULL definition is
    # unchanged by the diff, so a substantive replan doesn't reset already-delivered
    # work to PENDING and force needless re-verification. Compare each new stage
    # against the LIVE stage (what actually ran) by the full-fidelity carry key; an
    # unchanged, previously-PASSED stage keeps its recorded Outcome intact.
    # Stage 6: build the re-attest stash FRESH alongside the carry-forward pass
    # above — same PASSED gating, a NARROWER key (method/control criterion/
    # expected result image/executor/done criterion, not the stage's whole
    # definition). This never accumulates across replans: each substantive
    # replan replaces state.reattest_stash wholesale, so a stage re-armed two
    # replans ago and never re-dispatched since simply has no entry — the only
    # cost of that gap is one unnecessary full dispatch, never an incorrect
    # PASS, and `dispatch --re-attest` treats a missing entry as condition-1
    # failure (no prior PASSED to re-attest against) rather than an error.
    live_by_index = {s.index: s for s in state.stages}
    reattest_stash: list[ReattestStash] = []
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if prev is None or prev.outcome.status != StageStatus.PASSED.value:
            continue
        matched = stage_reattest_digest(prev) == stage_reattest_digest(ns)
        reattest_stash.append(ReattestStash(
            stage_index=ns.index,
            operative_surface_matched=matched,
            prior_outcome=Outcome(
                status=prev.outcome.status,
                actual=prev.outcome.actual,
                fail_digests=list(prev.outcome.fail_digests),
                cost_usd=prev.outcome.cost_usd,
                duration_ms=prev.outcome.duration_ms,
                spawn_count=prev.outcome.spawn_count,
                delivered_head=prev.outcome.delivered_head,
            ),
            prior_control=prev.control,
            reattest_digest=stage_reattest_digest(ns),
        ))
    state.reattest_stash = reattest_stash
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if (prev is not None
                and prev.outcome.status == StageStatus.PASSED.value
                and stage_carry_key(prev) == stage_carry_key(ns)):
            ns.outcome = prev.outcome
    state.stages = new.stages
    _sync_venue_from_plan(state, new)
    state.final_check = new.meta.final_check
    state.plan_path = args.plan
    state.plan_verified = True
    state.overall_done_criterion = new.meta.done_criterion or state.overall_done_criterion
    state.current_stage = None
    state.difficulty = None
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.node = Node.PLAN_READY.value
    state.log("replan", kind="substantive",
              prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
              **replan_cause)
    task_accumulator.add(
        state.task_id, "replan_count", 1, session_id=state.session_id, now=_utcnow(),
    )
    effort.rederive(state)  # re-derive AFTER logging so this replan is counted
    store.save(state)
    autonomy = _autonomy_directive_data(state, new)
    override = _agent_review_override(state)
    directive_data = {"autonomy": autonomy, **({"agent_review_override": override} if override else {})}
    if autonomy["action"] == "self_approve":
        return _with_advisories(Directive(
            True, state.node, "self_approve",
            "substantive replan inside the boundary the user approved for this order; run "
            "`approve --by agent`",
            data=directive_data,
        ), echo_advice)
    return _with_advisories(Directive(
        True, state.node, "await_user_approval",
        "substantive replan; HARD GATE — re-approval required",
        marker="PLAN-READY", data=directive_data,
    ), echo_advice)


def cmd_fire_acknowledge(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The synchronous decision `gates.effort_fire_blockers` refuses dispatch/replan/
    submit_plan without: appends an "ack" onto the LAST entry of state.effort_fires
    (never deletes — the append-only audit trail is preserved) recording who decided
    and what. Three decisions:

    - "continue": accept the overrun and keep executing the current plan as-is. No
      node change — whatever node the fire's own diagnose transition already put the
      session in (always DIAGNOSING, since every record_fire call site forces that
      transition in the same call) is left untouched; the coordinator proceeds
      through the ordinary declare/investigate/critique/normalize/replan cycle next.
    - "abandon": the order no longer warrants continuing. Parks the session at
      BLOCKED — the same bypass-transition() idiom cmd_block itself uses — rather
      than RESOLVED: a mid-execution fire routinely fires with stages still
      PENDING, and check_invariants refuses RESOLVED unless every stage is
      PASSED, so RESOLVED would be a lie for exactly the sessions this decision
      exists to stop. cmd_unblock remains the (audited) way back in, same as an
      ordinary block.
    - "revise": the plan itself needs to change in response to the overrun. No node
      change needed for the same reason as "continue" (already DIAGNOSING); the
      difference is purely in the human decision recorded, informing what the
      coordinator does next.
    """
    state = _require(store, args.session)
    if not state.effort_fires:
        return Directive(False, state.node, "noop", "no effort-divergence fire recorded")
    last = state.effort_fires[-1]
    if last.get("ack") is not None:
        return Directive(True, state.node, "noop", "fire already acknowledged", data={"fire": last})
    decision = args.decision
    if decision not in ("continue", "abandon", "revise"):
        return Directive(False, state.node, "noop", f"invalid --decision {decision!r}: "
                         "must be one of continue, abandon, revise")
    if not args.by or not args.by.strip():
        return Directive(False, state.node, "noop", "empty --by: must name who decided")
    by_agent = args.by.strip().casefold() == AGENT_ACTOR
    if by_agent:
        refusal = _agent_fire_ack_refusal(state, last, decision)
        if refusal is not None:
            return Directive(
                False, state.node, "fire_acknowledge",
                f"fire-acknowledge by the agent refused: {refusal}",
                marker=DIRECTIVE_ESCALATE_TO_USER,
                data={"effort_fire": _effort_fire_escalation_data(state), "reason": refusal},
            )
        state.agent_ack_difficulty_ids.append(_difficulty_id(state.difficulty))
        # The replans-scale fire rebased every scale's baseline; the spend / wall-clock
        # progress it swallowed is still owed to the user's own fire.
        replaced = last.get("replaced_baseline") or {}
        for scale in effort.SCALE_ORDER:
            if scale != effort.SCALE_REPLANS and scale in replaced:
                state.effort_baseline[scale] = replaced[scale]
    last["ack"] = {
        "by": args.by,
        "decision": decision,
        "ts": _utcnow(),
        "note": getattr(args, "note", None),
    }
    state.log("fire_acknowledge", by=args.by, decision=decision)
    _clear_open_fires_if_customer(state, args.by)
    if decision == "abandon":
        state.blocked_from = state.node
        state.node = Node.BLOCKED.value
        state.log("block", reason="user-abandoned-after-fire")
        store.save(state)
        return Directive(True, state.node, "unblock",
                         "session abandoned after unacknowledged effort-divergence fire; "
                         "unblock to resume",
                         marker="ESCALATE",
                         data={"fire": last})
    store.save(state)
    next_action = "declare" if state.difficulty is not None else "next_stage"
    return Directive(True, state.node, next_action,
                     f"fire acknowledged (decision={decision}); resuming",
                     data={"fire": last})


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


def cmd_check_coverage(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only pre-flight: would `replan --plan <new>` clear the critique coverage
    gate right now? Calls gates.replan_coverage_blockers directly — the same check
    cmd_replan runs — so a coverage miss surfaces BEFORE the coordinator spends a
    thinker plan-review on a corrected plan that would only bounce off replan's own
    gate afterward. Mirrors cmd_replan's OLD/NEW loading contract exactly (snapshot-
    else-plan_path, lenient OLD / strict NEW) but never saves, never logs a gate,
    never transitions state — a pre-flight that mutated what it inspects would
    corrupt the very state a later real replan diffs against."""
    state = _require(store, args.session)
    if not (state.difficulty and state.difficulty.critique):
        return Directive(True, state.node, "inspect", "no active critique; nothing to cover")
    old_path = _replan_baseline_path(state)
    old = load_plan(old_path, strict=False)
    new = load_plan(args.new)
    blockers = gates.replan_coverage_blockers(old, new, state.difficulty.critique)
    if blockers:
        return Directive(False, state.node, "inspect",
                         "coverage blockers: " + "; ".join(blockers),
                         data={"coverage_blockers": blockers})
    return Directive(True, state.node, "inspect", "OK — coverage clear")


def cmd_effort_check(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only effort-divergence report: where does this session stand against the
    norm its approved plan declared, on each of the four scales?

    Exists because the trigger only ever fires INSIDE a command the coordinator chose
    to run (`record-result`, `verify-final`, or the `gates.effort_fire_blockers` refusal
    on `dispatch`/`submit-plan`/`replan`). A session that runs long WITHOUT reaching one
    of those — a stage the coordinator keeps working, a specialist that never returns —
    diverges unobserved, however far past the multiple it goes. This command is the
    observation that does not depend on such a call arriving, and it is what the
    UserPromptSubmit watch hook drives on every prompt.

    STRICTLY READ-ONLY, and `ok=True` on every path including a refusal-shaped answer:
    it never transitions, never seeds a difficulty, never appends to `state.effort_fires`
    and never calls `store.save`. A read command with a side effect would be a SECOND,
    hidden fire site — it would consume the one-fire-per-replan budget belt 2 keeps
    (`effort._replans_since_last_fire`) without anyone having diagnosed anything, and
    the real fire site would then fall silent. `effort.refresh_spend` does mutate the
    loaded `SessionState` in memory (that is how the spend accumulator is read at all);
    with no save, the on-disk state is untouched, which the test asserts on the bytes.

    Deliberately does NOT call `effort.rederive`: the fire sites compare against the
    STORED estimate, so re-deriving here would report a divergence against a comparand
    no gate uses — a watch that disagrees with the gate it watches is worse than none.
    The same rule is why every per-scale row is computed from `effort.effective_deltas`
    / `effective_ratios` with the same cross-session totals `divergence()` is handed:
    `at_or_past_threshold` (what the hook speaks on) and `would_fire` (what a fire site
    would act on) must be answers about one vector, not two."""
    state = store.load(args.session) if getattr(args, "session", None) else None
    if state is None:
        return Directive(True, "(none)", "start", "no session state; nothing to check",
                         data={"armed": False, "scales": []})
    if not effort.armed(state):
        return Directive(
            True, state.node, "inspect",
            "effort trigger not armed (no approved-plan baseline); no scale can diverge",
            data={"armed": False, "active": gates.effort_active(state), "scales": []},
        )
    effort.refresh_spend(state, _cost_rows(args), state.plan_path)
    thr = Thresholds()
    multiple = thr.effort_divergence_multiple()
    # The SAME cross-session totals the fire sites pass, read once and used for both
    # halves of this report. Reporting the session-local vector while `would_fire` was
    # decided on the cross-session one is how a watch goes silent on exactly the case it
    # exists for: a resolved re-entry hands the fresh SessionState a replan count of 0
    # while the accumulator still holds the prior laps. See effort.effective_deltas.
    cross_totals = _effort_cross_totals(state)
    local = effort.deltas(state)
    delta = effort.effective_deltas(state, cross_session_totals=cross_totals)
    comparand = effort.comparands(state, thr)
    ratio = effort.effective_ratios(state, thr, cross_session_totals=cross_totals)
    scales = []
    for scale in effort.SCALE_ORDER:
        label, unit = effort.describe(scale)
        kind = "ratio" if scale in effort.RATIO_SCALES else "absolute"
        trigger = multiple if kind == "ratio" else 1.0
        observed = ratio[scale]
        record_only = scale in effort.RECORD_ONLY_SCALES
        scales.append({
            "scale": scale, "label": label, "unit": unit, "kind": kind,
            "actual": delta[scale], "comparand": comparand[scale], "ratio": observed,
            # Normalized "how far past its OWN line", so the four scales rank against
            # each other — the same footing effort.divergence puts them on.
            "past_own_trigger": (observed / trigger) if observed is not None else None,
            # RECORD_ONLY_SCALES never fire, so forced False here mirrors
            # effort.divergence's own skip of these scales — a watch that showed
            # at_or_past_threshold=True for a scale that can never diverge would read
            # as an alarm nothing acts on.
            "at_or_past_threshold": (not record_only) and observed is not None and observed >= trigger,
            # Whether the accumulator, not this session's own history, supplied the
            # number — so a reader of the line knows the count is the TASK's, not the
            # session's, without having to open the accumulator to find out.
            "cross_session": delta[scale] > local[scale],
            "record_only": record_only,
        })
    div = effort.divergence(state, thr, cross_session_totals=cross_totals)
    over = [s["scale"] for s in scales if s["at_or_past_threshold"]]
    # Separate from `over_threshold`: the true crossing state of a record-only scale,
    # reported for visibility but never folded into `over`/`would_fire` — those two
    # stay reserved for scales the divergence trigger can actually act on.
    record_only_over = [
        s["scale"] for s in scales
        if s["record_only"] and s["ratio"] is not None and s["ratio"] >= (
            multiple if s["kind"] == "ratio" else 1.0
        )
    ]
    detail = (
        f"effort divergence on {', '.join(over)}" if over
        else "no scale at or past its threshold"
    )
    return Directive(
        True, state.node, "inspect", detail,
        data={
            "armed": True,
            # Whether a fire site WOULD act on this: gates.effort_active is the same
            # predicate they consult, so a report that omitted it would read as an
            # alarm on a session where the trigger is switched off.
            "active": gates.effort_active(state),
            "over_threshold": over,
            "record_only_over_threshold": record_only_over,
            "would_fire": div.scale if div is not None else None,
            "framing": div.framing if div is not None else None,
            "scales": scales,
        },
    )


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
            # None when the premise plugin is not armed; `this_plan` None when no
            # plan is submitted — see _enumeration_escape_counts on why neither is
            # reported as a zero.
            "enumeration_escapes": _enumeration_escape_counts(state),
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

    # Snapshot the venue pair the PARENT plan file declares right now, at push time —
    # the comparand cmd_pop_subplan's venue-substitution guard checks the file against
    # later. A read failure here (unreadable/malformed parent) leaves the pair
    # uncaptured, which is exactly the signal that tells pop "nothing to compare;
    # re-derive as always".
    parent_pair = _plan_venue_pair(state.plan_path)

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
        delivery_worktree=state.delivery_worktree,
        final_check=list(state.final_check),
        partition=state.partition,
        approval=state.approval,
        resolution=state.resolution,
        stages=list(state.stages),
        current_stage=state.current_stage,
        originating_stage=originating,
        effort_estimate=state.effort_estimate,
        effort_baseline=state.effort_baseline,
        effort_actuals=dict(state.effort_actuals),
        effort_fires=list(state.effort_fires),
        effort_spend_seen=dict(state.effort_spend_seen),
        plan_review_rounds=state.plan_review_rounds,
        plan_review_counted_digest=state.plan_review_counted_digest,
        code_review_rounds=state.code_review_rounds,
        parent_repo_root=parent_pair[0] if parent_pair is not None else "",
        parent_delivery_worktree=parent_pair[1] if parent_pair is not None else "",
        parent_venue_captured=parent_pair is not None,
        plugins=dict(state.plugins),
        plugins_archive=dict(state.plugins_archive),
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
    # Deliberately NOT _sync_venue_from_plan: the child's venue is unknown until
    # its own submit_plan reads it, and the null window cannot be observed —
    # this same reset empties state.stages and moves EXECUTING -> CLASSIFIED, so
    # no stage exists to dispatch until submit_plan refills both fields. The
    # window is closed by the state machine, not by a convention that would
    # silently stop holding if dispatch ever became reachable from CLASSIFIED.
    state.repo_root = None
    state.delivery_worktree = None
    state.final_check = []
    state.partition = None
    state.approval = GateRecord("plan_approval")
    state.resolution = GateRecord("resolution")
    state.stages = []
    state.current_stage = None
    state.difficulty = None
    state.permission_request = None
    state.blocked_from = None
    # Effort-divergence custody (schema 25, effort.py's SUB-PLAN CUSTODY): the frame above
    # already snapshotted the parent's five fields, so the child starts an unarmed window
    # of its own — user_prompt_count is deliberately left alone, it lives outside effort_actuals.
    state.effort_estimate = None
    state.effort_baseline = None
    state.effort_actuals = {}
    state.effort_fires = []
    state.effort_spend_seen = {}
    # Review-round custody (schema 30), on the same reasoning as the effort block above:
    # the frame holds the parent's pair, so the child argues about its own plan on its own
    # budget and cannot spend — or be charged for — the parent's rounds.
    state.plan_review_rounds = 0
    state.plan_review_counted_digest = ""
    # Code-review round custody (item A, schema 33) — same reasoning, same frame.
    state.code_review_rounds = 0
    # Plugin custody (schema 35), same reasoning as the review-round block above: the
    # frame holds the parent's plugins/plugins_archive, so the child's own classify
    # auto-activates every plugin fresh, uncontaminated by parent bag content or a
    # parent-left archive-suppression entry.
    state.plugins = {}
    state.plugins_archive = {}
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
    state.delivery_worktree = frame.delivery_worktree
    state.final_check = frame.final_check
    state.partition = frame.partition
    state.approval = frame.approval
    state.resolution = frame.resolution
    state.stages = frame.stages
    # Effort-divergence custody (schema 25, effort.py's SUB-PLAN CUSTODY): estimate,
    # baseline, fires and spend_seen are restored straight from the frame, but
    # effort_actuals is ADDED — push zeroed it, so state.effort_actuals here is pure
    # child consumption and belongs on top of the parent's, not in place of it.
    state.effort_actuals = effort.merge_actuals(frame.effort_actuals, state.effort_actuals)
    state.effort_estimate = frame.effort_estimate
    state.effort_baseline = frame.effort_baseline
    state.effort_fires = frame.effort_fires
    state.effort_spend_seen = frame.effort_spend_seen
    # Review-round custody (schema 30). Restored, NOT merged like effort_actuals: rounds
    # are argument about a particular plan, and the child's rounds were spent arguing
    # about the child's plan, which no longer exists once the parent resumes.
    state.plan_review_rounds = frame.plan_review_rounds
    state.plan_review_counted_digest = frame.plan_review_counted_digest
    # Code-review round custody (item A, schema 33) — same restore-not-merge reasoning.
    state.code_review_rounds = frame.code_review_rounds
    # Plugin custody (schema 35) — restored, NOT merged, same reasoning: a plugin bag
    # argues about a particular plan, and the child's bags belonged to the child's plan,
    # which no longer exists once the parent resumes.
    state.plugins = frame.plugins
    state.plugins_archive = frame.plugins_archive
    state.node = new_node
    # The parent PLAN FILE is authoritative for the venue, so re-derive it here
    # rather than trust the frame: a frame captured after the value was already
    # lost would keep it lost. The frame fields stay as the fallback the helper
    # leaves in place when that file cannot be read.
    #
    # BUT: re-deriving unconditionally trusts the file even when it moved out from
    # under the pushed child — the one other post-approval route from an edited plan
    # FILE to live state. So re-derive only when there is nothing to compare against
    # (a legacy frame, or the parent was unreadable at push); when the parent's venue
    # pair was captured, compare it against a fresh read now and keep the frame's
    # (already-restored, two lines up) venue if either field moved. A missing venue
    # kept is a lesser-of-two-evils choice, not a clean stop: it is never refused, it
    # just runs in whatever cwd is ambient, which is safer than silently adopting a
    # venue nobody approved at plan_approval time.
    venue_source = "plan-file"
    if frame.parent_venue_captured:
        current_pair = _plan_venue_pair(state.plan_path)
        venue_substituted = (
            current_pair is not None
            and current_pair != (frame.parent_repo_root, frame.parent_delivery_worktree)
        )
        if venue_substituted:
            venue_source = "frame (parent plan venue changed while pushed)"
        else:
            _sync_venue_from_plan(state)
    else:
        _sync_venue_from_plan(state)
    # Mark the originating stage as satisfied, THEN derive the active-stage
    # pointer from stage status — order is load-bearing: deriving first would
    # re-point at a stage this same call is about to mark PASSED.
    try:
        orig = state.stage(frame.originating_stage)
        orig.outcome.status = StageStatus.PASSED.value
        orig.control = f"satisfied by sub-plan {child_task_id}"
    except KeyError:
        pass
    _restore_current_stage(state)
    state.log("pop_subplan", child_task_id=child_task_id, originating_stage=frame.originating_stage,
              depth=len(state.plan_stack), venue_source=venue_source)
    store.save(state)
    return Directive(
        True, state.node, "next_stage",
        f"sub-plan {child_task_id!r} resolved; parent restored at EXECUTING; "
        f"stage {frame.originating_stage} satisfied — run next-stage to continue "
        f"(venue: {venue_source})",
        data={"originating_stage": frame.originating_stage, "child_task_id": child_task_id,
              "stack_depth": len(state.plan_stack), "venue_source": venue_source},
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
        autonomy = None
        if not (approver and approver.strip()) and state.plan_path:
            try:
                drive_doc = load_plan(state.plan_path)
            except (OSError, PlanError):
                drive_doc = None
            if drive_doc is not None and _ledgered_order_key(state) is not None:
                autonomy = _autonomy_directive_data(state, drive_doc)
                if autonomy["action"] == "self_approve":
                    approver = AGENT_ACTOR
        if not (approver and approver.strip()):
            return Directive(True, node, "await_user_approval",
                             "drive: plan ready — HARD GATE; get explicit user approval, then "
                             "re-run drive with --approved-by <who>",
                             marker="PLAN-READY",
                             data={"trace": trace, **({"autonomy": autonomy} if autonomy else {})})
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
    "ledger-add": cmd_ledger_add,
    "ledger-check": cmd_ledger_check,
    "ledger-candidate": cmd_ledger_candidate,
    "ledger-dispose": cmd_ledger_dispose,
    "ledger-enumerate": cmd_ledger_enumerate,
    "question-raise": cmd_question_raise,
    "question-research": cmd_question_research,
    "question-dispose": cmd_question_dispose,
    "question-rebind": cmd_question_rebind,
    "question-retire": cmd_question_retire,
    "question-list": cmd_question_list,
    "question-check": cmd_question_check,
    "question-enumerate": cmd_question_enumerate,
    "question-enumerate-worker": cmd_question_enumerate_worker,
    "question-enumerate-escape": cmd_question_enumerate_escape,
    "question-candidate-dispose": cmd_question_candidate_dispose,
    "order-raise": cmd_order_raise,
    "order-dispose": cmd_order_dispose,
    "order-list": cmd_order_list,
    "classify": cmd_classify,
    "plan": cmd_plan,
    "plan-render": cmd_plan_render,
    "plan-grants": cmd_plan_grants,
    "plan-resources": plan_resources.cmd_plan_resources,
    "submit-plan": cmd_submit_plan,
    "present-plan": cmd_present_plan,
    "confirm-delivery": cmd_confirm_delivery,
    "plan-review": cmd_plan_review,
    "plan-review-delta": cmd_plan_review_delta,
    "plan-review-walk": cmd_plan_review_walk,
    "plan-review-compose": cmd_plan_review_compose,
    "risk-accept": cmd_risk_accept,
    "stage-review": cmd_stage_review,
    "code-review": cmd_code_review,
    "accept": cmd_accept,
    "approve": cmd_approve,
    "partition": cmd_partition,
    "partition-units": cmd_partition_units,
    "next-stage": cmd_next_stage,
    "dispatch": cmd_dispatch,
    "resolve-permission": cmd_resolve_permission,
    "stage-grants": cmd_stage_grants,
    "evidence-dir": cmd_evidence_dir,
    "grant-stats": cmd_grant_stats,
    "record-result": cmd_record_result,
    "declare": cmd_declare,
    "investigate": cmd_investigate,
    "critique": cmd_critique,
    "normalize": cmd_normalize,
    "verify-final": cmd_verify_final,
    "resolve": cmd_resolve,
    "reject": cmd_reject,
    "replan": cmd_replan,
    "fire-acknowledge": cmd_fire_acknowledge,
    "check-coverage": cmd_check_coverage,
    "effort-check": cmd_effort_check,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "status": cmd_status,
    "drive": cmd_drive,
    "close": cmd_close,
    "push-subplan": cmd_push_subplan,
    "pop-subplan": cmd_pop_subplan,
    "task-reset": cmd_task_reset,
}


# --- Argument partition for the `@<path>` convention ---------------------------
#
# Every argument the parser below declares belongs to exactly one of the three
# structures here. scripts/tests/test_argv_text_call_sites.py walks the parser
# itself — the root parser and every subparser — and goes RED on an argument that
# is in none of them, in two of them, or named here but no longer declared.
#
# OWNERSHIP RULE: the change that INTRODUCES an argument registers it here, in
# that same change. The table is exhaustive only as long as it stays exhaustive,
# and nobody re-derives it retroactively.
#
#   _ARG_RESOLVE      narrative free text this process CONSUMES. Resolved through
#                     lib.argv_text before dispatch, so the value may be written
#                     '@<path>' once the text outgrows a single argv string
#                     (Linux MAX_ARG_STRLEN = 131072 bytes).
#   _ARG_FORWARD      narrative free text this process only HANDS ON to a child,
#                     which resolves it at its own boundary. Resolving it here
#                     would inline the text straight back into the child's argv —
#                     the very ceiling the convention exists to stay under.
#   _ARG_DO_NOT_WRAP  identifiers, slugs, digests, file paths, enum-like tokens.
#                     Never prose, so a leading '@' carries no meaning; each entry
#                     states why in one line.

_ROOT = "<root>"  # sentinel subcommand key for the top-level parser's own options

# Every subcommand that takes --session. Spelled out rather than derived: a
# derivation would silently classify the next subcommand someone adds.
_SESSION_COMMANDS = (
    "start", "reset", "plugin-activate", "plugin-deactivate", "plugin-record",
    "ledger-add", "ledger-check", "ledger-candidate", "ledger-dispose",
    "ledger-enumerate", "question-raise", "question-research", "question-dispose",
    "question-rebind", "question-retire", "question-list", "question-check",
    "question-enumerate", "question-enumerate-worker", "question-enumerate-escape",
    "question-candidate-dispose",
    "order-raise", "order-dispose", "order-list", "classify", "plan",
    "plan-render", "plan-grants", "plan-resources", "submit-plan", "present-plan", "confirm-delivery", "plan-review",
    "plan-review-delta", "plan-review-walk", "plan-review-compose", "risk-accept",
    "stage-review", "code-review", "accept", "approve", "partition", "partition-units",
    "next-stage", "dispatch", "resolve-permission", "stage-grants", "evidence-dir", "grant-stats",
    "record-result", "declare",
    "investigate", "critique", "normalize", "verify-final", "resolve", "reject",
    "replan", "fire-acknowledge", "check-coverage", "effort-check", "block", "unblock", "status",
    "drive", "close", "push-subplan", "pop-subplan", "task-reset",
)

# (dest, subcommands that declare it)
_RESOLVE_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("goal", ("start", "reset")),
    ("done_criterion", ("start", "reset")),
    ("note", ("plugin-record", "confirm-delivery", "plan-review", "stage-review", "code-review",
              "accept", "question-enumerate-escape", "fire-acknowledge")),
    ("statement", ("ledger-add", "ledger-candidate")),
    ("source", ("ledger-add", "question-dispose")),
    ("premises", ("ledger-add",)),
    ("basis", ("ledger-add", "question-dispose", "risk-accept")),
    ("reason", ("ledger-dispose", "question-retire", "question-candidate-dispose",
                "order-dispose", "reject", "block", "task-reset", "replan")),
    ("element", ("order-raise",)),
    ("question", ("question-raise", "question-candidate-dispose")),
    ("attempted", ("question-research",)),
    ("answer", ("question-dispose",)),
    ("derivation", ("question-dispose",)),
    ("risk", ("question-dispose", "risk-accept")),
    ("confirm_still_valid", ("question-rebind",)),
    ("concerns", ("plan-review", "stage-review", "code-review")),
    ("observation", ("stage-review", "record-result", "close")),
    ("actual", ("record-result", "declare", "close")),
    ("control", ("record-result", "close")),
    ("expected", ("declare",)),
    ("mismatch", ("declare",)),
    ("localized_expectation", ("investigate",)),
    ("localized_actual", ("investigate",)),
    ("hypotheses", ("investigate",)),
    ("functional_ground", ("critique",)),
    ("replanning_task", ("critique",)),
    ("invariants_to_preserve", ("critique",)),
    ("differences_to_remove", ("critique",)),
    ("factor", ("normalize",)),
    ("normalize_factor", ("replan",)),
    ("quality_note", ("resolve", "close")),
    ("coverage_waiver", ("replan",)),
    ("normalization_waiver", ("replan",)),
    ("renegotiation_note", ("replan",)),
    ("bypass_reason", ("accept",)),
    ("reopen_reason", ("reset",)),
    ("reopen_user_decision", ("reset",)),
    ("rejection_text", ("present-plan",)),
)

# (dest, subcommands that declare it, why '@' means nothing here)
_DO_NOT_WRAP_ROWS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("state_root", (_ROOT,), "directory path the state store is rooted at"),
    ("session", _SESSION_COMMANDS, "session id — the slug state is keyed by"),
    ("task", ("start", "reset", "push-subplan", "task-reset"), "task slug, not a description"),
    ("criterion_type", ("start", "reset"), "one of two fixed verification kinds"),
    ("plugin", ("plugin-activate", "plugin-deactivate", "plugin-record"), "plugin registry name"),
    ("phase", ("plugin-record",), "plugin phase name from a fixed vocabulary"),
    ("tracker_key", ("plugin-activate", "classify", "drive"), "tracker issue key (ABC-123)"),
    ("id", ("ledger-add", "ledger-candidate", "ledger-dispose", "question-raise",
            "question-research", "question-dispose", "question-rebind", "question-retire",
            "question-candidate-dispose", "order-raise", "order-dispose"),
     "claim / question / order-element id"),
    ("claim", ("ledger-dispose",), "id of the grounding claim, not its text"),
    ("artifact", ("ledger-enumerate",), "path to the deliverable being cross-checked"),
    ("target", ("question-raise", "plan-review", "plan-review-walk", "plan-review-compose"),
     "plan element address or plan file path"),
    ("control", ("question-raise",),
     "structured control address matched against controls.MATERIALITY_GRAMMARS — a "
     "grammar-bound name, never the prose --control of record-result/close"),
    ("plan", ("plan-render", "plan-grants", "plan-resources", "plan-review-delta", "submit-plan", "replan", "drive",
              "push-subplan", "question-enumerate", "question-enumerate-worker",
              "question-enumerate-escape", "question-dispose",
              "question-rebind", "question-raise", "present-plan", "order-dispose"),
     "plan file path"),
    ("corpus", ("plan-resources",), "directory/ies to scan for *.toml plans — file paths, not narrative"),
    ("commands_file", ("plan-resources",), "path to a file of literal command lines — a file path"),
    ("dump_commands", ("plan-resources",), "output file path for the collected command list"),
    ("report_json", ("plan-resources",), "output file path for the JSON resolution report"),
    ("digest", ("question-enumerate-worker",),
     "plan content digest the launcher computed — the sidecar's key, passed down "
     "verbatim rather than a narrative"),
    ("stages", ("question-enumerate-worker",),
     "comma-separated stage indices the launcher narrowed the pass to"),
    ("new", ("check-coverage",), "corrected plan file path — the object under a coverage pre-check, not narrative"),
    ("rendering_file", ("present-plan",), "path to the rendered presentation"),
    ("by", ("confirm-delivery", "approve", "resolve", "fire-acknowledge", "resolve-permission",
            "task-reset"),
     "who acted — a name, not a narrative (resolve-permission's reserved 'agent' value is an "
     "identity token like any other --by, not free text)"),
    # --decision is NOT listed here: argparse `choices=` already makes it a non-candidate
    # for the @<path> partition (test_argv_text_call_sites.py's _is_candidate excludes any
    # action with choices set), so classifying it would be a stale entry the moment it's added.
    ("escape_reason", ("confirm-delivery",),
     "one token from delivery.DELIVERY_ESCAPE_REASONS — the narrative half of "
     "the escape is --note, which is RESOLVE"),
    ("reviewer", ("plan-review", "stage-review", "code-review"), "reviewer name"),
    ("plan_digest", ("plan-review",), "sha256 the review binds to"),
    ("scope", ("plan-review", "risk-accept"), "'' or 'stage:<n>' or 'topo:<pair>' — the review's binding, not narrative"),
    ("concern_ids", ("plan-review",),
     "explicit stable ids for --concern, positionally paired — ids, not narrative"),
    ("concern_id", ("risk-accept",), "the concern id this acceptance answers — an id, not narrative"),
    ("code_ref", ("code-review", "record-result"), "commit / PR reference the verdict binds to"),
    ("unit", ("partition", "partition-units"), "'|'-delimited partition-unit record"),
    ("author", ("accept", "risk-accept"), "acceptance author id — an identity token, not narrative"),
    ("renegotiated_by", ("replan",), "who made the renegotiation decision — a name, not a narrative"),
    ("verdict", ("accept",), "'|'-delimited requirement-verdict record"),
    ("budget", ("dispatch",), "budget tier name"),
    ("complexity", ("dispatch",), "complexity tier name"),
    ("cost_log", ("record-result", "resolve", "verify-final", "replan", "effort-check", "submit-plan"),
     "cost log file path (test override)"),
    ("quality_by", ("resolve", "close"), "how the quality rating was obtained — a fixed token"),
    ("confirmed_by", ("close",), "who confirmed — a name, not a narrative"),
    ("approved_by", ("drive",), "who approved — a name, not a narrative"),
    ("not_checked", ("code-review",),
     "short axis name(s) or 'none' — a fixed-vocabulary token, not review narrative"),
    ("rules", ("resolve-permission",),
     "Bash/Edit/etc rule strings — grant syntax tokens, not narrative"),
    ("add_dirs", ("resolve-permission",),
     "'PATH:MODE' tokens — grant syntax, not narrative"),
    ("regression_command", ("plan-review",),
     "shell command run verbatim via the runner (terminal-pass evidence) — an "
     "invocation string, not narrative prose for the @<path> convention"),
)

_ARG_RESOLVE: frozenset[tuple[str, str]] = frozenset(
    (command, dest) for dest, commands in _RESOLVE_ROWS for command in commands
)

_ARG_FORWARD: frozenset[tuple[str, str]] = frozenset({
    # dispatch's own process never reads this narrative text — it only hands it
    # on to the spawned specialist's argv, which resolves it via the same
    # lib.argv_text convention on its own side (see spawn-specialist.py /
    # spawn-cursor-specialist.py's _WRAPPER_RESOLVE).
    ("dispatch", "constraints"),
})

_ARG_DO_NOT_WRAP: dict[tuple[str, str], str] = {
    (command, dest): reason
    for dest, commands, reason in _DO_NOT_WRAP_ROWS
    for command in commands
}


def resolve_arg_text(args: argparse.Namespace) -> None:
    """Apply the `@<path>` convention in place to this command's RESOLVE arguments.

    One pass over the parsed namespace before dispatch, so no cmd_* body sees an
    unresolved reference and none has to remember to call the helper itself.

    Deliberately not an argparse ``type=`` converter: a SystemExit raised inside a
    converter is attributed to argparse's own error path rather than to the
    convention, and an append action applies its converter per element — which
    cannot preserve the None-vs-empty-list distinction the helpers guarantee.
    """
    for command, dest in _ARG_RESOLVE:
        # A _ROOT entry belongs to the top-level parser, so it applies whatever
        # subcommand ran. Matching only args.command would leave a root narrative
        # option classified RESOLVE but never actually resolved — the silent
        # false-green this partition exists to make impossible.
        if command not in (args.command, _ROOT):
            continue
        value = getattr(args, dest)
        # An append dest arrives as a list once given and as None when never
        # given; both helpers pass None through unchanged.
        setattr(
            args,
            dest,
            argv_text.read_arg_text_list(value)
            if isinstance(value, list)
            else argv_text.read_arg_text(value),
        )


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
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); auto-detected when omitted (best-effort; classify is the hard gate)")

    sp = add("reset"); sp.add_argument("--session", required=True); sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default=""); sp.add_argument("--done-criterion", dest="done_criterion", default="")
    sp.add_argument("--criterion-type", dest="criterion_type", default=CriterionType.MEASURABLE.value)
    sp.add_argument("--recursion-depth", dest="recursion_depth", type=int, default=0)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--reopen-reason", dest="reopen_reason", default="",
                    help="why a task that already RESOLVED is being re-entered — required by "
                         "gates.resolved_reentry_blockers, because reset is the only way back "
                         "into a closed order and it discards the effort baseline, the replan "
                         "count and every round-release counter on the way in")
    sp.add_argument("--reopen-user-decision", dest="reopen_user_decision", default="",
                    help="the user's answer to whether this order still warrants continuing. "
                         "Required IN ADDITION to --reopen-reason once the task has been "
                         "re-opened `effort-replan-absolute` times: past that count a reason "
                         "the coordinator wrote for itself is no longer enough, but it is "
                         "still owed — the reason says what is being re-opened, the decision "
                         "says who authorized re-opening it again")
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); auto-detected when omitted (best-effort; classify is the hard gate)")

    sp = add("plugin-activate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp.add_argument("--tracker-key", dest="tracker_key", default=None)
    sp = add("plugin-deactivate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True)
    sp = add("plugin-record"); sp.add_argument("--session", required=True)
    sp.add_argument("--plugin", required=True); sp.add_argument("--phase", required=True)
    sp.add_argument("--stage", type=int, default=None,
                    help="compose the published_phases key as '<phase>:<stage>' instead "
                         "of the bare phase name — used by the tracker plugin's per-stage "
                         "progress journal gate, e.g. --phase progress --stage 4")
    sp.add_argument("--note", default=None)
    sp.add_argument("--skipped", action="store_true",
                    help="record the phase as an honest SKIP (transport unavailable): "
                         "stores a marker+reason under the phase key so the gate is "
                         "discharged without a real post. Requires --note.")

    sp = add("ledger-add"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="claim id; re-adding an existing id upserts it")
    sp.add_argument("--status", required=True, choices=sorted(ledger.VALID_STATUSES))
    sp.add_argument("--statement", default="", help="the claim text")
    sp.add_argument("--source", default="", help="axiom provenance (sensor+window / ticket / file:line)")
    sp.add_argument("--premise", dest="premises", action="append", default=None,
                    help="a premise claim id this derivation rests on (repeatable)")
    sp.add_argument("--basis", default="", help="assumption ground")
    sp.add_argument("--load-bearing", dest="load_bearing", action="store_true", default=True)
    sp.add_argument("--not-load-bearing", dest="load_bearing", action="store_false")
    sp = add("ledger-check"); sp.add_argument("--session", required=True)

    sp = add("ledger-candidate"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="candidate id; re-adding an existing id re-raises it")
    sp.add_argument("--statement", default="", help="the decision/judgment statement")

    sp = add("ledger-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="candidate id to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["recorded", "dismissed"])
    sp.add_argument("--reason", default="", help="required when --as dismissed")
    sp.add_argument("--claim", default="", help="grounding claim id, required when --as recorded")

    sp = add("ledger-enumerate"); sp.add_argument("--session", required=True)
    sp.add_argument("--artifact", required=True,
                    help="path to the outgoing deliverable to cross-check for load-bearing "
                         "decisions/judgments/claims (raised as candidates)")

    sp = add("question-raise"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id; re-raising an existing id resets it to open")
    sp.add_argument("--target", required=True,
                    help="the plan element the question arose against: plan.goal, "
                         "plan.done_criterion, or stage:<n>.<element>")
    sp.add_argument("--question", default="", help="the question text")
    sp.add_argument("--control", required=True,
                    help="the control of this plan whose verdict the answer could flip: "
                         "'stage <n> verify_command', 'stage <n> done_criterion', "
                         "'stage <n> landed assertion', 'final_check <n>', or "
                         "'order requirement <id>'. Refused when it names nothing this "
                         "plan contains")
    sp.add_argument("--plan", default=None,
                    help="resolve --control against this plan instead of the session's "
                         "current plan_path (use when raising against a CORRECTED plan)")

    sp = add("question-research"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id to attach the research attempt to")
    sp.add_argument("--attempted", required=True, help="the own-research attempt (must precede escalation)")

    sp = add("question-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id to disposition")
    sp.add_argument("--to", dest="to", required=True, choices=["researched", "escalated", "assumed"])
    sp.add_argument("--answer", default="", help="the resolved answer (researched/escalated)")
    sp.add_argument("--source", default="", help="provenance of the answer (researched)")
    sp.add_argument("--derivation", default="", help="how the answer follows from the source (researched)")
    sp.add_argument("--basis", default="", help="the ground the assumption rests on (assumed)")
    sp.add_argument("--risk", default="", help="what breaks if the assumption is wrong (assumed)")
    sp.add_argument("--plan", default=None,
                    help="stamp disposed_at_key against this plan instead of the session's "
                         "current plan_path (use when disposing against a CORRECTED plan)")

    sp = add("question-rebind"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="disposed question id to re-stamp against its stage's current key")
    sp.add_argument("--confirm-still-valid", dest="confirm_still_valid", required=True,
                    help="why the disposition still holds against the changed stage (non-empty)")
    sp.add_argument("--plan", default=None,
                    help="re-stamp disposed_at_key against this plan instead of the session's "
                         "current plan_path (use when preparing a CORRECTED plan for replan)")

    sp = add("question-retire"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="question id whose target stage no longer exists")
    sp.add_argument("--reason", required=True, help="why the question is retired")

    sp = add("question-list"); sp.add_argument("--session", required=True)
    sp.add_argument("--format", dest="format", default="", choices=["", "md"],
                    help="md renders the thinker's markdown table; default is a compact one-liner")

    sp = add("question-check"); sp.add_argument("--session", required=True)
    sp = add("question-enumerate"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default=None,
                    help="enumerate against this plan instead of the session's current "
                         "plan_path (use when preparing a CORRECTED plan for replan)")
    sp.add_argument("--reopen-dismissed", action="store_true",
                    help="re-raise a candidate whose text was previously carried forward as "
                         "dismissed (the old fresh-pass behaviour), instead of carrying it "
                         "forward again under its recorded reason")

    sp = add("question-enumerate-escape"); sp.add_argument("--session", required=True)
    sp.add_argument("--reason", required=True, choices=list(premise.ENUMERATION_ESCAPE_REASONS),
                    help="why the mandatory enumeration cross-check is being discharged "
                         "without a healthy run — a closed set, so the escapes aggregate")
    sp.add_argument("--note", required=True,
                    help="what actually happened, for the reader of one row (the reason "
                         "token is what counts across rows)")
    sp.add_argument("--plan", default=None,
                    help="escape against this plan instead of the session's current "
                         "plan_path (use when preparing a CORRECTED plan for replan)")

    sp = add("question-enumerate-worker"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", required=True,
                    help="plan file to enumerate — this detached worker never touches "
                         "session state, so the launcher names it explicitly")
    sp.add_argument("--digest", required=True,
                    help="plan content digest the launcher computed (plugins_premise."
                         "_plan_content_digest) — the sidecar write's key")
    sp.add_argument("--stages", default="",
                    help="comma-separated stage indices to read instead of the whole "
                         "plan, when only those stages moved; omit for the whole plan")

    sp = add("question-candidate-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="candidate id (qenum-<part>-N) to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["recorded", "dismissed"])
    sp.add_argument("--reason", default="", help="required when --as dismissed")
    sp.add_argument("--question", default="",
                    help="question id this candidate resolves to, required when --as recorded")

    sp = add("order-raise"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="order-element id; re-raising an existing id resets it to undispositioned")
    sp.add_argument("--element", required=True,
                    help="one element of the order, enumerated FROM THE TEXT OF THE ORDER "
                         "and BEFORE submit-plan — elements read off an already-written "
                         "plan make coverage trivially total and prove nothing")

    sp = add("order-dispose"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True, help="order-element id to disposition")
    sp.add_argument("--as", dest="as_", required=True, choices=["covered", "cut"])
    sp.add_argument("--stage", type=int, default=None,
                    help="the stage that covers this element, required when --as covered")
    sp.add_argument("--reason", default="", help="why the element is cut, required when --as cut")
    sp.add_argument("--plan", default=None,
                    help="stamp content_digest against this plan instead of the session's "
                         "current plan_path (use when re-covering against a CORRECTED plan)")

    sp = add("order-list"); sp.add_argument("--session", required=True)
    sp.add_argument("--format", dest="format", default="", choices=["", "md"],
                    help="md renders the scope-coverage block the essence must carry; "
                         "default is a compact one-liner")

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
    sp.add_argument("--deliverable-kind", dest="deliverable_kind", default="",
                    choices=["", "reasoning", "code", "ops", "mixed"],
                    help="what kind of artifact this task produces; 'reasoning'/'mixed' "
                         "arms the claim-provenance ledger plugin on a SUBSTANTIVE session")
    sp.add_argument("--host", choices=runtime_host.HOSTS, default=None,
                    help="coordination host this session dispatches through (claude|cursor); "
                         "required (or ambient-detectable) at classify — sticky thereafter")

    sp = add("plan"); sp.add_argument("--session", required=True)
    sp = add("plan-render"); sp.add_argument("--plan", required=True,
        help="TOML plan to render to a markdown prose view on demand (a projection, "
             "never written to disk — the TOML is the single source of truth)")
    sp.add_argument("--stage", type=plan_render_stage_arg_type, default=None,
        help="render only this stage index, or a comma-separated list of stage "
             "indices (e.g. '3,5'), as a brief projection instead of the whole "
             "plan (the spawn prompt's per-dispatch projection, or a reviewer's "
             "scoped delta)")
    # Accept (and ignore) --session so the harness-session auto-injection
    # (_inject_default_session) is a no-op here: rendering is a pure, session-free
    # read of the plan file, unlike every other verb which drives session state.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-grants"); sp.add_argument("--plan", required=True,
        help="TOML plan whose per-stage grant set (declared + derived) to render "
             "on demand (a projection, never written to disk)")
    sp.add_argument("--format", choices=["compact", "full", "json"], default="compact")
    # Session-free read, same reason as plan-render's suppressed --session above.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("plan-resources"); sp.add_argument("--plan", required=False, default=None,
        help="TOML plan whose per-stage typed resources (declared + derived rules, "
             "add_dirs, spawn/landed shape) to resolve on demand -- plan mode")
    sp.add_argument("--format", choices=["compact", "full", "json"], default="compact")
    sp.add_argument("--corpus", nargs="+", default=None,
        help="corpus mode: directory/ies to scan for *.toml plans, resolving every "
             "declared+derived Bash rule found through tool_contracts.resolve_command")
    sp.add_argument("--commands-file", default=None,
        help="corpus mode: a file of literal command lines (one per line, '#'-comments "
             "skipped) to resolve directly, in addition to any --corpus scan")
    sp.add_argument("--dump-commands", default=None,
        help="corpus mode: write every collected command line to this file")
    sp.add_argument("--report-json", default=None,
        help="corpus mode: write the full resolution report (JSON) to this file")
    sp.add_argument("--require-no-unknown-program", action="store_true", default=False,
        help="corpus mode: fail (non-ok Directive) if any command resolves to "
             "reason_class=unknown-program")
    # Session-free read, same reason as plan-render's suppressed --session above.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("submit-plan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp = add("present-plan"); sp.add_argument("--session", required=True)
    sp.add_argument("--kind", choices=list(PLAN_PRESENTATION_KINDS), default=PLAN_PRESENTATION_KIND_ESSENCE,
                    help="essence = free-form summary, no completeness check; "
                         "full = every [stage N] anchor required, stage-enumerated; "
                         "replan_diff = proposed-diff rendering for a non-substantive "
                         "replan against a candidate plan file (see --plan)")
    sp.add_argument("--plan", default=None,
                    help="candidate plan file the presentation is stamped against; "
                         "only legal with --kind replan_diff (defaults to the session's "
                         "current plan_path there too), refused for essence/full which "
                         "always target the session's own plan")
    sp.add_argument("--rendering-file", dest="rendering_file", default=None,
                    help="file containing the exact bytes shown to the user")
    sp.add_argument("--emit-skeleton", dest="emit_skeleton", action="store_true",
                    help="print the [stage N] anchor scaffold for a `full` rendering; "
                         "stamps nothing")
    sp.add_argument("--rejection-text", dest="rejection_text", default=None,
                    help="what the user said when sending a PRIOR presentation of this "
                         "plan back (a correction or rejection), captured for history "
                         "only; omit on a first presentation or a clean approval — never "
                         "required")
    sp = add("confirm-delivery"); sp.add_argument("--session", required=True)
    sp.add_argument("--kind", choices=list(PLAN_PRESENTATION_KINDS), default=PLAN_PRESENTATION_KIND_ESSENCE,
                    help="which presentation's delivery this override confirms — must "
                         "match the --kind used at present-plan time")
    sp.add_argument("--by", required=True,
                    help="the human who confirms delivery (must not be 'hook')")
    sp.add_argument("--note", required=True,
                    help="why the automated delivery hook could not verify this itself")
    sp.add_argument("--escape-reason", dest="escape_reason", required=True,
                    help="which of " + "/".join(delivery.DELIVERY_ESCAPE_REASONS) +
                         " this escape is — the countable half; --note stays the "
                         "explicable half")
    sp = add("plan-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.PLAN_REVIEW_VERDICTS), required=True,
                    help="pass = clears the gate; revise = blocks; override = user's "
                         "explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (the user, for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a concern the thinker raised, written `blocking: [re:<concern-id>] "
                         "<concern>` or `note: [re:<concern-id>] <concern>` (repeatable; an "
                         "untagged concern is refused, `blocking:` on a pass is refused, "
                         "`re:<concern-id>` must name a recorded concern)")
    sp.add_argument("--concern-id", dest="concern_ids", action="append", default=None,
                    help="stable id for the --concern at the same position (repeatable, "
                         "positionally paired); omitted concerns get a derived id "
                         "(c0, c1, ...) via state.plan_review_concern_ids — risk-accept "
                         "binds to this id, never to the concern's prose")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--target", default=None,
                    help="plan file reviewed (defaults to the session's current plan_path; "
                         "pass the NEW plan for a replan-time review)")
    sp.add_argument("--plan-digest", dest="plan_digest", default=None,
                    help="sha256 the REVIEWER computed from its OWN read of the target "
                         "plan; cross-checked against the live bytes and stored as the "
                         "attested plan_sha256. A passing verdict does NOT bind without "
                         "it — a reviewer that could not read the plan cannot attest.")
    sp.add_argument("--scope", default=None,
                    help="'stage:<n>' to bind this review to one stage instead of the "
                         "whole plan, or 'topo:<pair>' (e.g. topo:3-1, topo:plan-7, "
                         "topo:base-plan) to record one reliance-pair review; omitted "
                         "(or '') means whole-plan, the only kind that existed before stage 5")
    sp.add_argument("--findings-blocking", dest="findings_blocking", type=int, default=None,
                    help="count of blocking findings this round produced (audit trail)")
    sp.add_argument("--findings-nonblocking", dest="findings_nonblocking", type=int,
                    default=None,
                    help="count of non-blocking findings this round produced (audit trail)")
    sp.add_argument("--regression-command", dest="regression_command", default=None,
                    help="a command the engine runs in repo_root to demonstrate a "
                         "regression against a plan version that already carries a "
                         "recorded whole-plan/stage PASS this approval cycle. Required "
                         "(and must exit non-zero) for a 'revise' after such a pass to "
                         "block approve/replan — a command that was never run, or that "
                         "exits 0, is refused as evidence. Ignored on every other "
                         "verdict/situation.")
    sp = add("plan-review-delta"); sp.add_argument("--session", required=True)
    sp.add_argument("--plan", default=None,
                    help="plan file to diff against recorded reviews (defaults to the "
                         "session's current plan_path)")
    sp = add("plan-review-walk"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file to walk (defaults to the session's current plan_path); "
                         "every digest, pair and depth is computed from it")
    sp.add_argument("--format", choices=("text", "json"), default="text",
                    help="'json' emits the walk as {plan_path, discharges, levels}")
    sp = add("plan-review-compose"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file to compose a whole-plan pass for (defaults to the "
                         "session's current plan_path; pass the NEW plan at replan time)")
    sp = add("risk-accept"); sp.add_argument("--session", required=True)
    sp.add_argument("--scope", default=None,
                    help="'' or 'stage:<n>' — the review scope the accepted concern was "
                         "raised in (must match a recorded plan-review's --scope)")
    sp.add_argument("--concern-id", dest="concern_id", default="",
                    help="the concern id this acceptance answers (see plan-review's "
                         "--concern-id, or its derived c0/c1/... form)")
    sp.add_argument("--basis", default="",
                    help="why the risk is being accepted rather than fixed — mirrors "
                         "question-dispose --disposition assumed's --basis")
    sp.add_argument("--risk", default="",
                    help="what could go wrong by accepting rather than fixing — mirrors "
                         "question-dispose --disposition assumed's --risk")
    sp.add_argument("--author", default="",
                    help="who is accepting the risk — an identity token, not narrative")
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
    sp = add("code-review"); sp.add_argument("--session", required=True)
    sp.add_argument("--verdict", choices=list(gates.CODE_REVIEW_VERDICTS), required=True,
                    help="pass = clears the code-review gate; revise = blocks; override = "
                         "user's explicit deadlock escape (requires --reviewer and --note)")
    sp.add_argument("--reviewer", default="",
                    help="who performed the review (code-reviewer, or the user for an override)")
    sp.add_argument("--concern", dest="concerns", action="append", default=None,
                    help="a blocking concern the reviewer raised (repeatable; audit trail)")
    sp.add_argument("--note", default="",
                    help="override justification, or a free-text note")
    sp.add_argument("--code-ref", dest="code_ref", default=None,
                    help="the reviewed-code revision/digest the reviewer names; binds the "
                         "verdict so a later record-result with a different --code-ref is stale")
    sp.add_argument("--not-checked", dest="not_checked", default=None,
                    help="axes this review did NOT check (e.g. 'tests'), or 'none' — required "
                         "for a pass verdict, so an unchecked axis is an explicit field rather "
                         "than a silent omission (verify-right-axis-report-honestly)")
    sp = add("accept"); sp.add_argument("--session", required=True)
    sp.add_argument("--author", default="",
                    help="acceptance author id; must match [meta.order].customer_id when set")
    sp.add_argument("--verdict", dest="verdict", action="append", default=None,
                    help="requirement verdict as '<requirement_id>|<pass|fail>[|<note>]'; "
                         "repeatable, one per declared order requirement")
    sp.add_argument("--note", default="",
                    help="free-text note on the acceptance review as a whole")
    sp.add_argument("--bypass", action="store_true",
                    help="record an AcceptanceBypass alongside the review, for when the "
                         "acceptance judge is unreachable; requires --bypass-reason and "
                         "at least one --verdict")
    sp.add_argument("--bypass-reason", dest="bypass_reason", default="",
                    help="why this acceptance stands without judge corroboration "
                         "(required with --bypass)")
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
    # None (not "medium") so cmd_dispatch can tell "flag omitted" apart from "flag
    # explicitly set to medium" and fall through to the stage's declared cost_tier
    # before the "medium" default.
    sp.add_argument("--budget", default=None); sp.add_argument("--complexity", default="medium")
    # None (not e.g. "medium"), same reason as --budget above: lets cmd_dispatch
    # tell "omitted" apart from "explicitly medium" and fall through to the
    # cost_tier-derived default (_EFFORT_BY_COST_TIER) before "medium". Choices
    # mirror spawn-specialist.py's own --effort (the child this argv reaches).
    sp.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default=None,
                    help="claude -p reasoning-effort level for the dispatched child. Optional: "
                    "defaults from the stage's cost_tier (small->low, medium->medium, "
                    "large->high) rather than requiring the caller to classify twice.")
    sp.add_argument("--constraints", default="",
                    help="clarification for the spawned specialist that bounds HOW it does "
                         "the already-approved stage — never a scope or done-criterion change; "
                         "long text as '@<path>' (forwarded, resolved by the specialist itself)")
    sp.add_argument("--dry-run", action="store_true")
    # Stage 6, OPT-IN: re-enter a stage recorded PASSED and re-armed by a
    # substantive replan without paying for a full (re-)spawn, when the replan
    # left the stage's operative surface untouched AND its own control passes
    # NOW. Absent, behavior is byte-identical to today — refusal is the default,
    # never an automatic route.
    sp.add_argument("--re-attest", action="store_true")
    sp = add("resolve-permission"); sp.add_argument("--session", required=True)
    sp.add_argument("--decision", choices=["granted", "denied"], required=True)
    sp.add_argument("--by", default=None,
                    help="who is resolving this request; the reserved identity 'agent' "
                         "(AGENT_ACTOR) attempts a self-grant against the order-approvals "
                         "ledger (REQ5) instead of recording a human decision -- refused "
                         "unless every --rule/--add-dir resolves to a typed resource ALREADY "
                         "covered by a resource the customer approved for this plan's order")
    sp.add_argument("--scope", choices=["once", "project", "global", "stage"], default="once",
                    help="'stage' additionally materializes --rule/--add-dir as a RUNTIME "
                         "grant on the active stage (state.runtime_grants), validated through "
                         "grants.py exactly like a declared grant -- so the next dispatch of "
                         "this stage carries it into the child's --settings/--add-dir")
    sp.add_argument("--rule", dest="rules", action="append", default=None,
                    help="a Bash/Edit/etc rule string to grant (repeatable); meaningful with "
                         "--scope stage or once (refused with project/global, which have no "
                         "materialization path), validated via grants.validate_rule before storage")
    sp.add_argument("--add-dir", dest="add_dirs", action="append", default=None,
                    help="'PATH:MODE' (mode: read|write) to grant (repeatable); meaningful with "
                         "--scope stage or once (refused with project/global, which have no "
                         "materialization path), validated via grants.validate_add_dir before storage")
    sp = add("stage-grants"); sp.add_argument("--session", required=True)
    sp.add_argument("--stage", type=int, default=None,
                    help="stage index to report (defaults to the session's active stage)")
    sp.add_argument("--json", action="store_true")
    sp = add("evidence-dir"); sp.add_argument("--session", required=True)
    sp.add_argument("--stage", type=int, required=True,
                    help="stage index the evidence directory is scoped to")
    sp = add("grant-stats"); sp.add_argument("--session", required=True)
    sp.add_argument("--json", action="store_true")
    sp = add("record-result"); sp.add_argument("--session", required=True)
    sp.add_argument("--status", choices=["passed", "failed"], required=True)
    sp.add_argument("--actual", default="")
    sp.add_argument("--control", default=None,
                    help="control-criterion attestation (required for spawn:developer stages "
                         "when recording passed; accepted on any stage)")
    sp.add_argument("--observation", default="",
                    help=f"{OBSERVATION_CONTRACT} (required when recording passed on an "
                         "acceptance_review stage, or on any stage of a substantive session)")
    sp.add_argument("--code-ref", dest="code_ref", default=None,
                    help="for spawn:developer stages: the reviewed-code revision/digest, to "
                         "cross-check against the bound CodeReview's --code-ref (drift -> stale)")
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
    sp.add_argument("--failure-address", dest="failure_address", default=None,
                    choices=list(FAILURE_ADDRESS_VALUES),
                    help="route the fault to the inadequate обеспечение (R2): ресурсное "
                         "(ресурсное обеспечение — материал/средство) | нормативное "
                         "(нормативное обеспечение — норма/способ) | not_applicable (routing "
                         "does not apply); the closure gate demands it on a difficulty-closing replan")
    sp = add("normalize"); sp.add_argument("--session", required=True)
    sp.add_argument("--factor", required=True,
                    help="the reproducible cause the difficulty exposed, being re-normed "
                         "(the ACT is mandatory at closure)")
    sp.add_argument("--level", dest="level", default=None, choices=list(NORMALIZATION_LEVELS),
                    help="recording level (payoff-gated by rediscovery-threshold-min); omit "
                         "for an in-head note below the leaf threshold")
    sp.add_argument("--destination", dest="destination", default=None,
                    choices=list(NORMALIZATION_DESTINATIONS),
                    help="the functional place the renorming lands on — материал | средство "
                         "| норма | способ | знание; ORTHOGONAL to --level (any destination "
                         "at any level), omit when the place is not being recorded")
    sp = add("verify-final"); sp.add_argument("--session", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp = add("resolve"); sp.add_argument("--session", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--quality", type=int, choices=list(_VALID_QUALITY_RATINGS), default=None,
                    help="1-5 rating, agent-proposed and user-confirmed/adjusted in the "
                         "resolution AskUserQuestion; refused if absent")
    sp.add_argument("--quality-by", dest="quality_by", default="user-confirmed",
                    help="'user-confirmed' (default), 'user-adjusted', or 'user-other' "
                         "(free-text answer)")
    sp.add_argument("--quality-note", dest="quality_note", default=None)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG); "
                         "read to derive the realized budget_tiers for the quality row")
    sp = add("reject"); sp.add_argument("--session", required=True)
    sp.add_argument("--reason", required=True,
                    help="the intent mismatch the user named when rejecting the delivery "
                         "(seeds the difficulty record)")
    sp.add_argument("--stage", dest="stage", action="append", default=None, type=int,
                    help="plan stage index to re-open as FAILED (repeatable; "
                         "defaults to the final stage so a reject is never a no-op)")
    sp = add("replan"); sp.add_argument("--session", required=True); sp.add_argument("--plan", required=True)
    sp.add_argument("--reason", default=None,
                    help="free-text cause for this replan, captured for history only; used "
                         "only when no active difficulty record supplies one (a bare "
                         "refinement/no_change replan outside DIAGNOSING) — never required, "
                         "and its absence never refuses the command")
    sp.add_argument("--coverage-waiver", dest="coverage_waiver", default=None,
                    help="bypass a failing coverage gate with a recorded reason (refused if empty); "
                         "never bypasses the difficulty-record completeness precondition")
    sp.add_argument("--normalization-waiver", dest="normalization_waiver", default=None,
                    help="close a difficulty WITHOUT a re-norming record when the exposed factor "
                         "is genuinely one-off; a recorded reason (refused if empty)")
    sp.add_argument("--renormalize", action="store_true",
                    help="claim the corrected plan changes only stage `procedure` — the "
                         "SEQUENCE of operations, which is the executor's to replace; skips "
                         "the plan-review and plan_approval gates and is REFUSED the moment "
                         "the edit reaches a method, a criterion, a result image or the goal")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG)")
    sp.add_argument("--renegotiation-decision", dest="renegotiation_decision", default=None,
                    choices=["continue", "rescope", "abandon"],
                    help="clear the diagnosing_replan round-release ceiling (the "
                         "replan-count threshold out of DIAGNOSING): continue/rescope zero the cross-session "
                         "task accumulator and let this replan proceed; abandon parks the "
                         "session at BLOCKED without applying --plan. Requires "
                         "--renegotiated-by and --renegotiation-note")
    sp.add_argument("--renegotiated-by", dest="renegotiated_by", default=None,
                    help="who made the renegotiation decision; must match "
                         "[meta.order].customer_id when the plan declares one")
    sp.add_argument("--renegotiation-note", dest="renegotiation_note", default=None,
                    help="what the customer decided and why (refused if empty)")
    sp.add_argument("--normalize-factor", dest="normalize_factor", default=None,
                    help="record a normalize event in the SAME replan call, without a "
                         "separate `normalize` invocation -- the reproducible cause being "
                         "re-normed. Optional; its absence never refuses the replan (a "
                         "non-blocking hint names the flag when it's missing)")
    sp.add_argument("--normalize-level", dest="normalize_level", default=None,
                    choices=list(NORMALIZATION_LEVELS),
                    help="paired with --normalize-factor; recording level (payoff-gated), "
                         "omit for an in-head note below the leaf threshold")
    sp = add("fire-acknowledge"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", required=True, help="who decided — a name, not a narrative")
    sp.add_argument("--decision", required=True, choices=["continue", "abandon", "revise"],
                    help="continue: accept the overrun, keep executing; abandon: park the "
                         "session at BLOCKED (via blocked_from, same as cmd_block) with "
                         "reason 'user-abandoned-after-fire' — never RESOLVED, since a "
                         "mid-execution fire routinely leaves stages PENDING and "
                         "check_invariants refuses RESOLVED unless every stage PASSED; "
                         "revise: route to the ordinary DIAGNOSING replan cycle")
    sp.add_argument("--note", default=None)
    sp = add("check-coverage"); sp.add_argument("--session", required=True)
    sp.add_argument("--new", required=True,
                    help="corrected plan file to check against the active critique, "
                         "BEFORE spending a thinker plan-review on it")
    sp = add("effort-check"); sp.add_argument("--session", required=True)
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="cost log file path (test override)")
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
    sp.add_argument("--deliverable-kind", dest="deliverable_kind", default="",
                    choices=["", "reasoning", "code", "ops", "mixed"],
                    help="what kind of artifact this task produces; 'reasoning'/'mixed' "
                         "arms the claim-provenance ledger plugin on a SUBSTANTIVE session")
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
                    help=f"{OBSERVATION_CONTRACT} (threaded to record-result; see "
                         "record-result --observation)")
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

    sp = add("task-reset", help="explicit renegotiation: zero the cross-session "
             "task accumulator (item B) for --task — never called from `reset`")
    # Session-independent by design (cmd_task_reset's own docstring), but
    # _inject_default_session unconditionally appends --session <harness> when
    # $CLAUDE_CODE_SESSION_ID is set (i.e. every real invocation inside a
    # Claude Code session) — mirror plan-render's suppressed-absorb pattern so
    # that injection doesn't crash argparse with "unrecognized arguments".
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp.add_argument("--task", required=True, help="task_id whose accumulator to zero")
    sp.add_argument("--reason", required=True,
                    help="why this task's accumulated cross-session friction is being forgiven")
    sp.add_argument("--by", required=True,
                    help="the customer authorizing the reset; the coordinator is refused")
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
    resolve_arg_text(args)
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
