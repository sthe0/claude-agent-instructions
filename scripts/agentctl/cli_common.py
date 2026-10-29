"""Helpers shared by more than one agentctl command group, moved verbatim from cli.py."""
from __future__ import annotations

import argparse
import copy
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
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from lib import (
    argv_text,
    ask_evidence,
    config_root,
    engine_session_binding,
    hook_telemetry,
    kind_baselines,
    transcript_stops,
    transcript_turns,
)
from . import (
    advisor,
    carry_forward,
    check_cache,
    child_runs,
    controls,
    cost,
    delivery,
    delivery_channel,
    effort,
    exempt_paths,
    gates,
    grant_shadow,
    grants as _grants,
    mandate as mandate_rules,
    mandate_store,
    norm_handoff,
    order_approvals,
    plan_refs,
    plan_resources,
    plan_store,
    plugins,
    plugins_premise,
    premise,
    resources as _resources,
    runtime_host,
    task_accumulator,
)
from . import config as agentctl_config
from .config import Thresholds
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import Runner, LABEL_DECORATION, parse_marker, subprocess_runner
from .machine import transition
from .plan import (
    CONDITION_MARKERS,
    ConcernFormatError,
    PlanDoc,
    PlanError,
    SEVERITY_BLOCKING,
    SEVERITY_NOTE,
    change_tier,
    customer_question_defect,
    entries_grants_sha256,
    grants_sha256,
    load_plan,
    load_plan_with_digest,
    materialized_grant_entries,
    is_unit_id,
    order_digest,
    pair_binding,
    pair_content,
    pair_part_tokens,
    parse_concern,
    parse_pair,
    parse_unit,
    part_digest_map,
    plan_meta_element_key,
    plan_meta_element_keys,
    refined_grant_entries,
    replan_class,
    stage_norm_keys,
    stage_question_key,
    stage_reattest_digest,
    unit_currency_hash,
    unit_node,
    _derive_stage_grants,
    _venue_for,
)
from .submission import (
    plan_size_blocks,
    plan_size_warnings,
    submission_advice,
    submission_violations,
)
from .state import (
    AGENT_ACTOR,
    CheckKind,
    CheckVenue,
    CodeReview,
    CONCERN_FIXED,
    CONCERN_OPEN,
    CONCERN_RECORDED,
    CONCERN_SUPERSEDED,
    ConcernRecord,
    CONTENT_WAIVER_ASK_MARKER,
    Criterion,
    CriterionType,
    Declaration,
    Difficulty,
    canonical_normalization_destination,
    EFFORT_FIRE_FINISHED_WORK,
    is_settled,
    is_skipped,
    JudgeBypass,
    LANDED_GIT_ERROR_EXIT,
    PartitionUnit,
    PARTITION_UNIT_MODES,
    GateRecord,
    Node,
    Normalization,
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
    Outcome,
    PARENT_DIFFICULTY_ENV,
    parse_parent_difficulty,
    PLAN_PRESENTATION_KIND_FULL,
    PLAN_PRESENTATION_KIND_REPLAN_DIFF,
    PlanPairReview,
    PlanPresentation,
    PlanReview,
    plan_review_pair_scope,
    plan_review_scope_for_pair,
    plan_review_scope_for_stage,
    plan_review_scope_stage_index,
    ReattestStash,
    RequirementVerdict,
    RESOLUTION_ASK_MARKER,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
    landed_containment_script,
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
    inlines the same sha256-of-bytes recompute (it cannot import cli — circular).

    Index-only on purpose: for a hypertext plan the digest of the index bytes already
    commits to every referenced file (each reference carries its file's sha256), so the
    whole-plan review binding goes stale on any edit, referenced files included."""
    if not target:
        return ""
    try:
        return hashlib.sha256(Path(target).read_bytes()).hexdigest()
    except OSError:
        return ""


def _plan_refs_problem(target: str) -> str | None:
    """Why the references of the plan at `target` do not all resolve (the reference named),
    or None when they do. A plan without references always resolves."""
    try:
        plan_store.visited_refs(target)
    except PlanError as exc:
        return str(exc)
    return None


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


def _store_plan_objects(state: SessionState, data: bytes, plan_path: str | None) -> None:
    """Put a plan version's index bytes and every file it references into the plan object
    store (`plan_store`), so the version resolves to the same text however its files are
    edited, moved or deleted later. `plan_path` is the file `data` was read from and defaults
    to the session's registered path. Best-effort like the snapshot writers around it: an
    unresolvable version is stored nowhere and refused by name when a consumer loads it."""
    registered = plan_path or state.plan_path
    if not registered:
        return
    try:
        plan_store.store_plan(data, registered)
    except (OSError, PlanError):
        pass


def _load_prior_plan(state: SessionState, path: str, *, strict: bool = True) -> PlanDoc:
    """Load an older version of the session's plan (the approved snapshot, a version
    snapshot): references resolve against the registered plan path and come from the
    object store, not from files that may have changed since."""
    return plan_store.load_baseline(path, state.plan_path, strict=strict)


def _write_plan_snapshot(store: StateStore, state: SessionState, data: bytes, digest: str,
                         plan_path: str | None = None) -> tuple[str, str] | None:
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
    _store_plan_objects(state, data, plan_path)
    return str(snap), digest


def _write_plan_version_snapshot(store: StateStore, state: SessionState, data: bytes, digest: str,
                                 plan_path: str | None = None) -> str | None:
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
    _store_plan_objects(state, data, plan_path)
    return str(snap)


def _read_plan_version_snapshot(store: StateStore, state: SessionState, digest: str) -> bytes | None:
    """The bytes `_write_plan_version_snapshot` stored for plan version `digest` (the full
    sha256), or None when the store exposes no on-disk path, no such version was written,
    or the file is unreadable or does not hash to `digest`. None means the old side of a
    plan diff is unknown, and a caller keeps its behaviour from before the tier existed."""
    path_fn = getattr(store, "path", None)
    if path_fn is None or not digest:
        return None
    try:
        data = (path_fn(state.session_id).parent / f"plan-version-{digest[:16]}.toml").read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == digest else None


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
    _write_plan_version_snapshot(store, state, data, digest, plan_path=state.plan_path or plan_path)
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
    """Re-snapshot the just-applied plan bytes and rebind the approved grant set
    (`approved_grants_sha256` and the entries it covers) to them, mirroring
    `cmd_approve`'s own snapshot+hash binding (see its "Bind the approved grant set"
    comment) -- called from the `no_change` and `refinement` branches of `cmd_replan`,
    which apply corrected bytes to an ALREADY-APPROVED session in place, without
    requiring a fresh `approve`. Without this, those two branches update
    `state.plan_path` but leave `plan_snapshot_path`/`plan_snapshot_hash`/
    `approved_grants_sha256` pointed at the PRIOR approval's bytes, so
    `_stage_grant_entries` either hash-mismatches on a corrected snapshot that no
    longer matches the stale stamped hash, or silently keeps re-deriving grants from
    plan content the session no longer runs.

    The binding is not a re-derivation of every stage against the venue filesystem of
    this moment: that set would depend on files the user never approved. A session with
    stored entries gets `plan.refined_grant_entries` -- a stage whose grant inputs did
    not move keeps its approved entries, a moved one admits only the plan's own change,
    and venue drift is withheld and logged (`grant_drift_withheld`). A session bound
    before entries were stored gets entries only when `doc` still reproduces the hash it
    was approved under; otherwise its hash is left untouched (as `--renormalize` does),
    so dispatch keeps withholding what the live derivation no longer reproduces.

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
    replaced = _approved_snapshot_doc(state)
    snap = _write_plan_snapshot(store, state, data, digest)
    if snap:
        state.plan_snapshot_path, state.plan_snapshot_hash = snap
    stored = state.approved_grant_entries
    if stored is None:
        if (state.approved_grants_sha256
                and grants_sha256(doc) == state.approved_grants_sha256):
            state.approved_grant_entries = materialized_grant_entries(doc)
        return
    entries, withheld = refined_grant_entries(stored, replaced, doc)
    state.approved_grant_entries = entries
    state.approved_grants_sha256 = entries_grants_sha256(entries)
    if withheld:
        state.log("grant_drift_withheld", stages=withheld)


def _approved_snapshot_doc(state: SessionState):
    """The approved-plan snapshot as a plan, or None when it cannot be read."""
    snap_path = state.plan_snapshot_path
    if not snap_path or not Path(snap_path).exists():
        return None
    try:
        return _load_prior_plan(state, snap_path, strict=False)
    except (OSError, PlanError):
        return None


def _bind_approved_grants(state: SessionState, doc, entries: dict | None = None) -> None:
    """Bind the effective grant set of `doc` to the session at approval: the entries
    materialized once, and the hash of those entries. Dispatch reads the stored entries
    (`_stage_grant_entries`) after re-checking them against the hash; re-deriving them
    there made the grant set depend on the engine code and venue filesystem of the
    moment (#338). None/None when `doc` did not load, so a prior approval's binding is
    never carried over to unrelated bytes."""
    if doc is None:
        state.approved_grants_sha256 = None
        state.approved_grant_entries = None
        return
    if entries is None:
        entries = materialized_grant_entries(doc)
    state.approved_grant_entries = entries
    state.approved_grants_sha256 = entries_grants_sha256(entries)


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
        doc = _load_prior_plan(state, path, strict=False)
    except (OSError, PlanError):
        return None
    key = order_digest(doc)
    return key if order_approvals.latest_user_approved_record(key) is not None else None


def _effort_cross_totals(state: SessionState, now: float | None = None, *,
                         store: StateStore | None = None) -> dict:
    """`task_accumulator`'s per_axis_totals, plus — for a ledgered order — the order's
    spend/wall-clock total since the last user approval (ledger window + this
    session's unflushed delta, minus the total at this session's last fire), under the
    keys `effort.effective_deltas` reads."""
    totals = dict(task_accumulator.get(state.task_id)["per_axis_totals"])
    key = _ledgered_order_key(state)
    if key is None:
        return totals
    window = order_approvals.get(key)["effort_since_user_approval"]
    flushed = state.order_effort_flushed
    if flushed is None and _is_linked(state):
        flushed = {effort.SCALE_SPEND: 0.0}
    flushed = flushed or {}
    actual = _order_actual(state, now, store=store)
    base = state.order_effort_base or {}
    for scale, total_key in _ORDER_EFFORT_SCALES:
        unflushed = max(0.0, actual[scale] - float(flushed.get(scale, actual[scale])))
        total = float(window.get(scale) or 0.0) + unflushed
        floor = float(base.get(scale) or 0.0)
        # The floor is subtracted only while the window has not been reset below it (a fresh user approval).
        totals[total_key] = total - (floor if total >= floor else 0.0)
    return totals


def _is_linked(state: SessionState) -> bool:
    link = state.parent_link
    return isinstance(link, dict) and bool(link.get("parent_session"))


def _order_actual(state: SessionState, now: float | None = None, *,
                  store: StateStore | None = None) -> dict:
    """`effort.actual` as the order window counts it. A linked session leaves out of its
    wall-clock the part of its own child runs that its parent's run of it covers: that run
    spans them and the parent flushes it, so counting them here would book the grandchild's
    minutes once per level. What no parent run covers — the session has no parent run
    (spawned straight through spawn-specialist), or the parent's run ended earlier than the
    child's run — stays here, so it is booked by someone. Without a store the parent cannot be
    read and nothing is left out.

    A pushed sub-plan zeroes the live `effort_actuals` and parks the parent's in its frame, so
    the live vector alone would fall below the watermark the parent's flush set: the sub-plan's
    spend would never reach the order window, and the parent's would be booked again after the
    pop. The order's actual is therefore the live vector plus every frame's."""
    actual = effort.actual(state, now=now)
    for frame in state.plan_stack:
        actual[effort.SCALE_SPEND] += float(frame.effort_actuals.get(effort.ACTUAL_SPEND_KEY) or 0.0)
        actual[effort.SCALE_WALL_CLOCK] += float(frame.effort_actuals.get(effort.ACTUAL_MINUTES_KEY) or 0.0)
    state_dir = _state_dir(store) if store is not None else None
    if _is_linked(state) and state_dir is not None and state.effort_child_runs:
        cover = child_runs.parent_cover(
            state_dir, state.parent_link["parent_session"], state.session_id)
        if cover is None:
            # The parent's file is mid-save: what its run covers is unknown, so the
            # wall-clock stays where the last flush left it and is settled by a later one.
            watermark = (state.order_effort_flushed or {}).get(effort.SCALE_WALL_CLOCK)
            if watermark is not None:
                actual[effort.SCALE_WALL_CLOCK] = float(watermark)
            else:
                # Nothing flushed yet to hold it at: leave out every child-run minute, the most
                # the parent can cover, so the first watermark is never above what a later read
                # settles at and the later flush cannot under-count.
                actual[effort.SCALE_WALL_CLOCK] -= effort.child_run_minutes(state, now)
            return actual
        own = effort.child_run_intervals(state.effort_child_runs, now)
        actual[effort.SCALE_WALL_CLOCK] -= effort.overlap_seconds(own, cover) / 60.0
    return actual


def _seed_linked_flush_baseline(state: SessionState, *, store: StateStore | None = None) -> None:
    """A linked session's order window counts from the user's approval, which reached it
    through its parent: whatever it spent before its first ledgered flush was spent after
    that approval (except across a later user re-approval while the session has never
    flushed), so that flush moves it instead of only seeding the watermark. Only the
    spend baseline starts at 0; wall-clock keeps the seeding behaviour."""
    if state.order_effort_flushed is None and _is_linked(state):
        state.order_effort_flushed = {
            effort.SCALE_SPEND: 0.0,
            effort.SCALE_WALL_CLOCK: _order_actual(state, store=store)[effort.SCALE_WALL_CLOCK],
        }


def _flush_order_effort(state: SessionState, key: str | None = None, *,
                        store: StateStore | None = None) -> None:
    """Move this session's unflushed spend/wall-clock into its order's ledger window."""
    key = key or _ledgered_order_key(state)
    if key is None:
        return
    _seed_linked_flush_baseline(state, store=store)
    actual = _order_actual(state, store=store)
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
        order = _load_prior_plan(state, path, strict=False).meta.order
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


def _record_effort_fire(state: SessionState, div: "effort.Divergence", *, now: float,
                        store: StateStore | None = None, finished_work: bool = False) -> dict:
    """`effort.record_fire` plus, on a spend/wall-clock fire of a ledgered order, the
    open user-owed marker and the order-level baseline rebase. A fire on finished work
    (`finished_work`) is recorded and rebases the baselines but owes the user nothing:
    there is no remaining plan for the user to renegotiate."""
    fire = effort.record_fire(state, div, now=now, finished_work=finished_work)
    fire["difficulty_id_at_fire"] = _difficulty_id(state.difficulty)
    if div.scale in effort.RATIO_SCALES:
        key = _ledgered_order_key(state)
        if key is not None:
            _flush_order_effort(state, key, store=store)
            if not finished_work:
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


NESTED_DIFFICULTY_ORIGINS = ("replan", "normalize", "stage")


def _difficulty_level_cap() -> int:
    """A difficulty at this level or deeper is never recorded (`max-recursion-depth`)."""
    return Thresholds().max_recursion_depth


def _root_difficulty_slot(state: SessionState) -> tuple[int, str | None, str]:
    """(level, parent_uid, origin) of a session's own outermost record: level 0 and no
    parent, or one level below the spawning session's open difficulty."""
    parent = state.parent_difficulty
    if parent:
        return int(parent["level"]) + 1, parent["uid"], "child"
    return 0, None, "stage"


def _open_difficulty(state: SessionState, origin: str | None = None, *,
                     declaration: Declaration | None = None,
                     nested: bool = False) -> Difficulty | None:
    """The one factory of difficulty records. Installs a fresh record as the open one and
    returns it, or returns None -- with `state` untouched -- when its level would reach
    the depth cap.

    A declared record already open is never replaced: it is pushed onto the stack and the
    new one nests under it (`nested=True` does that for an undeclared one too). An
    undeclared record is the placeholder the new one takes the slot of, and an absent one
    leaves the session's root slot. `origin` is None for a root record and then follows
    the slot (`child` inside a spawned session, else `stage`)."""
    cur = state.difficulty
    root_level, root_parent_uid, root_origin = _root_difficulty_slot(state)
    push = cur is not None and (nested or cur.declaration is not None)
    if push:
        level, parent_uid = cur.level + 1, cur.uid
    elif cur is not None:
        level, parent_uid = cur.level, cur.parent_uid
    elif state.difficulty_stack:
        top = state.difficulty_stack[-1]
        level, parent_uid = top.level + 1, top.uid
    else:
        level, parent_uid = root_level, root_parent_uid
    if level >= _difficulty_level_cap():
        return None
    if origin is None:
        origin = root_origin if level == root_level else "stage"
    if push:
        state.difficulty_stack.append(cur)
    state.difficulty = Difficulty(
        declaration=declaration, level=level, parent_uid=parent_uid, origin=origin)
    return state.difficulty


def _difficulty_cap_directive(state: SessionState, cause: str, data: dict | None = None) -> Directive:
    """What every refusal at the depth cap returns: the record is withheld and the user
    decides, since a further nested record would be the loop the cap exists to stop."""
    cap = _difficulty_level_cap()
    detail = (f"{cause}; the difficulty stack is at its depth cap (max-recursion-depth "
              f"{cap}), so no further difficulty record is opened -- the user decides how "
              f"to proceed")
    return Directive(
        False, state.node, "escalate", detail,
        marker=DIRECTIVE_ESCALATE_TO_USER,
        data={**(data or {}), "difficulty_level_cap": cap, "blockers": [detail]},
    )


def _archive_difficulty(state: SessionState, waiver: str | None = None) -> None:
    """Leave the difficulty cycle: file the open record -- its closed children inside it,
    and any ancestor the stack still holds -- in `closed_difficulties`, where a later
    reader finds it. An undeclared placeholder is not a difficulty and is dropped.
    `waiver` is the `--normalization-waiver` reason the closing replan itself took; the
    root record has no other way to carry it, and the filed record must show how it
    closed the same way a nested one does."""
    if waiver and state.difficulty is not None and not state.difficulty.normalization_waiver:
        state.difficulty.normalization_waiver = waiver.strip()
    for rec in (*state.difficulty_stack, state.difficulty):
        if rec is not None and rec.declaration is not None:
            state.closed_difficulties.append(asdict(rec))
    state.difficulty_stack = []
    state.difficulty = None


def _adopt_parent_difficulty(state: SessionState) -> None:
    """Record the spawning session's open difficulty from `AGENTCTL_PARENT_DIFFICULTY`
    (`<session>:<uid>:<level>`). Idempotent; a malformed value is logged and ignored,
    never a crash -- the child's own work must not depend on a parent's env hygiene."""
    if state.parent_difficulty is not None:
        return
    raw = os.environ.get(PARENT_DIFFICULTY_ENV, "").strip()
    if not raw:
        return
    parsed = parse_parent_difficulty(raw)
    if parsed is None:
        state.log("parent_difficulty_ignored", value=raw[:200])
        print(f"agentctl: ignoring malformed {PARENT_DIFFICULTY_ENV}={raw[:200]!r} "
              "(expected <session>:<uid>:<level>)", file=sys.stderr)
        return
    state.parent_difficulty = parsed
    state.log("parent_difficulty", **parsed)


def _handoff_spawn_refusal(state: SessionState, adopted: dict, store: StateStore) -> str | None:
    """Why `adopted` was not written by a real spawn for this session, else None: the
    file must lie under `<state_root>/handoffs/` and the spawner's child-session record
    for the stage it names must name this session. A self-consistent file anyone can
    write is not a link."""
    root = getattr(store, "root", None)
    if root is None:
        return "the store has no state root to check the spawn against"
    directory = (Path(root) / norm_handoff.HANDOFF_DIRNAME).absolute()
    if Path(adopted["handoff"]).absolute().parent != directory:
        return f"handoff file is not under {directory}"
    record = norm_handoff.read_child_session(root, adopted["parent_session"], adopted["stage_index"])
    if record is None or record["session"] != state.session_id:
        return "no spawner-written child-session record names this session"
    return None


def _adopt_norm_handoff(state: SessionState, store: StateStore) -> None:
    """Adopt the spawning stage's norm handoff from `AGENTCTL_NORM_HANDOFF`
    (`<path>:<sha256>`) as `state.parent_link`, and with it the child's depth.
    Idempotent: a session that is already linked keeps its link, and a different value
    arriving later is logged, not adopted. A handoff that fails verification is logged and
    ignored with one stderr line, never a crash -- the child's work does not depend on a
    parent's env hygiene; the session simply stays unlinked."""
    raw = os.environ.get(norm_handoff.NORM_HANDOFF_ENV, "").strip()
    link = state.parent_link
    if link is not None:
        if raw and raw != norm_handoff.format_env_value(link["handoff"], link["sha256"]):
            state.log("norm_handoff_ignored", reason="session already linked", value=raw[:200])
        return
    if not raw:
        return
    adopted, reason = norm_handoff.verify_env_value(raw)
    if adopted is not None:
        reason = _handoff_spawn_refusal(state, adopted, store)
        if reason is not None:
            adopted = None
    if adopted is None:
        state.log("norm_handoff_ignored", reason=reason, value=raw[:200])
        print(f"agentctl: ignoring {norm_handoff.NORM_HANDOFF_ENV}: {reason}", file=sys.stderr)
        return
    state.parent_link = adopted
    state.recursion_depth = adopted["depth"]
    state.log("norm_handoff", parent_session=adopted["parent_session"],
              stage_index=adopted["stage_index"], depth=adopted["depth"],
              norm_digest=adopted["norm_digest"])


def _loop_self_check(state: SessionState, thresholds: Thresholds) -> dict | None:
    """The ancestor-norm digest a deep child compares its own task against, or None.
    The comparison is perception (is my task a re-framing of an ancestor's?) and stays
    with the agent; the engine only decides *when* to ask and *what* to show."""
    link = state.parent_link
    threshold = thresholds.loop_sensitivity_depth
    if link is None or state.recursion_depth < threshold:
        return None
    return {
        "depth": state.recursion_depth,
        "threshold": threshold,
        "ancestors": [
            {k: entry.get(k) for k in ("session", "stage_index", "title", "done_criterion")}
            for entry in link.get("norm_lineage") or []
        ],
    }


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


def _agent_self_decision_refusal(
    state: SessionState, anchor_id: str | None, *, event: str = "the event",
    consumed: str = "an earlier decision", decision: str = "decision",
) -> str | None:
    """The one rule an agent-authored decision that would end a friction loop must meet:
    the open difficulty record is complete (declared, investigated, critiqued), is not the
    record that stood at the anchoring event, was not already spent on an earlier
    decision, names `differences_to_remove`, and carries a real normalization (a waiver
    does not discharge it). The fire-acknowledge, the renegotiation `continue` and the
    plan-review override all reach it, so the three cannot drift apart. Returns why the
    decision is refused, or None when the record qualifies.

    `anchor_id` is the id of the record that stood at the event (None when the event
    recorded none, which no record can equal); `event`/`consumed`/`decision` only word
    the refusal for the caller."""
    diff = state.difficulty
    did = _difficulty_id(diff)
    if did is None or not diff.complete():
        return "no complete planning-difficulty record (declare, investigate, critique) is open"
    if did == anchor_id:
        return f"the difficulty record predates {event}; declare one after it"
    if did in state.agent_ack_difficulty_ids:
        return f"this difficulty record already discharged {consumed}"
    if not diff.critique.differences_to_remove:
        return "the critique names no differences_to_remove"
    if diff.normalization is None or not diff.normalization.factor.strip():
        return f"no real normalization record (a waiver does not discharge an agent {decision})"
    return None


def _since_user_backstop_refusal(state: SessionState, axis: str, backstop: int,
                                 what: str, cleared_by: str) -> str | None:
    """Why the since-user backstop stops an agent decision, or None below it. The counter
    is the task's cross-session `axis` total since the last decision the user took."""
    spent = _since_user_total(state, axis)
    if spent < backstop:
        return None
    return (f"{axis} is {spent}, at or past its backstop of {backstop}: {what} is no longer "
            f"the agent's to take; {cleared_by} zeroes it")


def _since_user_total(state: SessionState, axis: str) -> int:
    return task_accumulator.get(state.task_id)["per_axis_totals"].get(axis, 0)


_BACKSTOP_CROSSED_EVENT = "agent_backstop_crossed"


_PLAN_DOES_NOT_LOAD = "the plan does not load"


_NO_ARMED_ESTIMATE = "no armed spend or wall-clock estimate bounds the loop"


_NO_APPROVED_VERSION = "no user-approved version of the order"


def _crossing_bound_reasons(state: SessionState) -> list[str]:
    """Why a backstop crossing is no longer the agent's, or [] while the spend and
    wall-clock fires still bound the loop: at least one of the two ratios is armed and
    every armed one is below `effort-divergence-multiple`. Read-only -- the totals are the
    ones `divergence()` itself compares, measured since the last user approval."""
    multiple = Thresholds().effort_divergence_multiple()
    ratios = effort.effective_ratios(state, cross_session_totals=_effort_cross_totals(state))
    armed = {s: ratios[s] for s in (effort.SCALE_SPEND, effort.SCALE_WALL_CLOCK)
             if ratios[s] is not None}
    if not armed:
        return [_NO_ARMED_ESTIMATE]
    return [f"{scale} is {ratio:.1f}x the estimate, at or past effort-divergence-multiple {multiple:g}"
            for scale, ratio in armed.items() if ratio >= multiple]


def _since_user_backstop_gate(
    state: SessionState, load_doc, *, decision: str, axis: str, backstop: int, what: str,
    cleared_by: str,
) -> tuple[str | None, dict | None]:
    """The since-user backstop step of an agent self-decision, as (refusal, crossing).

    Below the backstop: (None, None), the plan is not loaded and neither the boundary nor
    the bound is consulted. At or past it the decision stays the agent's only while the
    plan `load_doc()` returns is inside the autonomy boundary (`_autonomy_for`, the
    predicate the next `approve --by agent` is held to) AND the spend and wall-clock fires
    still bound the loop; then it returns the crossing for the caller to record once the
    decision is applied. Otherwise the refusal names every reason. A `load_doc()` of None
    is the plan that does not load. A session with no user-approved order version is
    outside whatever `_autonomy_for` says of the plan: without one the effort window is
    not frozen, so the spend bound would not be measured against the user's approval."""
    backstopped = _since_user_backstop_refusal(state, axis, backstop, what, cleared_by)
    if backstopped is None:
        return None, None
    doc = load_doc()
    if doc is None:
        outside = [_PLAN_DOES_NOT_LOAD]
    else:
        boundary = _autonomy_for(state, doc)
        outside = [] if boundary["eligible"] else list(boundary.get("reasons") or [boundary["reason"]])
    if _ledgered_order_key(state) is None:
        outside.append(_NO_APPROVED_VERSION)
    bound = _crossing_bound_reasons(state)
    if outside or bound:
        clauses = []
        if outside:
            clauses.append("outside the autonomy boundary: " + "; ".join(outside))
        if bound:
            clauses.append("the effort bound does not hold: " + "; ".join(bound))
        return f"{backstopped}; " + "; ".join(clauses), None
    return None, {
        "event": _BACKSTOP_CROSSED_EVENT, "decision": decision, "axis": axis,
        "spent": _since_user_total(state, axis), "backstop": backstop,
        "difficulty_id": _difficulty_id(state.difficulty),
    }


def _record_backstop_crossing(state: SessionState, crossing: dict | None) -> None:
    """Stamp the crossing with its session and moment, put it in the session's history and
    in the task's own record (`task_accumulator`), which outlives the session."""
    if crossing is None:
        return
    crossing.update(session=state.session_id, ts=_utcnow())
    state.log(**crossing)
    task_accumulator.record_crossing(
        state.task_id, crossing, session_id=state.session_id, now=crossing["ts"])


def _crossing_data(crossing: dict | None) -> dict:
    return {_BACKSTOP_CROSSED_EVENT: crossing} if crossing is not None else {}


def _backstop_crossings(state: SessionState) -> list[dict]:
    """The agent backstop crossings on record, for verify-final and the resolution summary
    to surface verbatim ([] when none): every one of the task's, whichever session made it,
    merged with this session's own history by (session, ts)."""
    recorded = task_accumulator.get(state.task_id)["agent_backstop_crossings"]
    seen = {task_accumulator.crossing_key(c) for c in recorded} - {None}
    merged = [dict(c) for c in recorded]
    for event in state.history:
        if event.get("event") != _BACKSTOP_CROSSED_EVENT:
            continue
        key = task_accumulator.crossing_key(event)
        if key is None or key not in seen:
            merged.append(dict(event))
    return merged


def _disclose_backstop_crossings(state: SessionState, data: dict, detail: str) -> str:
    """Put the crossings on `data` and, when there are any, name them in `detail`."""
    crossings = _backstop_crossings(state)
    data["agent_backstop_crossings"] = crossings
    if not crossings:
        return detail
    named = ", ".join(
        f"{c.get('decision')} ({c.get('axis')} {c.get('spent')}/{c.get('backstop')})"
        for c in crossings)
    return (f"{detail}; WARNING: {len(crossings)} agent decision(s) crossed a since-user backstop "
            f"inside the autonomy boundary: {named} (see agent_backstop_crossings)")


def _disclose_agent_plan_conversions(state: SessionState, data: dict, detail: str) -> str:
    """Put the conversions the agent ran itself (`plan-convert-live --by agent`) on `data`
    and, when there are any, name them in `detail`, so a plan changed in place under the
    user's approval is never invisible at the close."""
    agent_run = [dict(vars(c)) for c in state.plan_conversions if c.by == "agent"]
    if not agent_run:
        return detail
    data["agent_plan_conversions"] = agent_run
    named = ", ".join(
        f"{c['direction']} {c['old_digest'][:12]} -> {c['new_digest'][:12]}" for c in agent_run)
    return (f"{detail}; NOTE: {len(agent_run)} plan conversion(s) run by the agent, the content "
            f"proved equal by the engine: {named} (see agent_plan_conversions)")


# Stands for "no difficulty record was open" at the release event: no real record id can
# equal it, so any record declared afterwards is a fresh one.
_NO_RECORD_ANCHOR = "no-record"


def _plan_review_release_active(state: SessionState) -> bool:
    """True while the plan-review round budget or the combined cross-axis ceiling is
    spent -- the two releases whose messages name the reflection exit."""
    return (gates.plan_review_round_release_active(state)
            or gates.cross_axis_friction_release_active(state))


def _sync_release_anchor(state: SessionState) -> bool:
    """Keep `review_release_difficulty_id` in step with the release: set once, at the
    first moment the release is observed active, to the id of the record then open
    (never moved forward by a later observation), and cleared once it is no longer
    active. Returns True when it changed `state`, for callers that must save."""
    anchor = state.review_release_difficulty_id
    if not _plan_review_release_active(state):
        state.review_release_difficulty_id = None
        return anchor is not None
    if anchor is None:
        state.review_release_difficulty_id = _difficulty_id(state.difficulty) or _NO_RECORD_ANCHOR
        return True
    return False


def _agent_fire_ack_refusal(
    state: SessionState, last: dict, decision: str,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an agent-authored fire-acknowledge: a refusal when it may not
    proceed, else None and the backstop crossing it would be (None below the backstop)."""
    if _ledgered_order_key(state) is None:
        return "the order has no user-approved version; the fire is the user's decision", None
    if last.get("scale") != effort.SCALE_REPLANS:
        return "an open spend/wall-clock fire is owed to the user, not the agent", None
    if decision != "revise":
        return f"decision {decision!r} on a fire is the user's; the agent may only revise", None
    def load_doc():
        try:
            return load_plan(state.plan_path, strict=False)
        except (OSError, PlanError):
            return None

    backstopped, crossing = _since_user_backstop_gate(
        state, load_doc, decision="fire_acknowledge", axis="replans_since_user_decision",
        backstop=Thresholds().effort_replan_backstop(), what="acknowledging an effort fire",
        cleared_by="a user approval, fire-acknowledgement or renegotiation")
    if backstopped is not None:
        return backstopped, None
    refused = _agent_self_decision_refusal(
        state, last.get("difficulty_id_at_fire"),
        event="the fire", consumed="an earlier fire", decision="fire")
    return (refused, None) if refused is not None else (None, crossing)


def _agent_renegotiation_refusal(
    state: SessionState, decision: str, doc,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an agent-authored renegotiation at the diagnosing-replan
    ceiling: a refusal when a `continue` may not proceed, else None and the backstop
    crossing it would be (None below the backstop)."""
    if decision != "continue":
        return f"{decision!r} changes the order's scope or ends it; that is the user's decision", None
    if doc is None or _ledgered_order_key(state) is None:
        return "the order has no user-approved version to continue within", None
    if state.renegotiation_ceiling_difficulty_id is None:
        diff = state.difficulty
        if _difficulty_id(diff) is None or not diff.complete():
            return ("no complete planning-difficulty record (declare, investigate, critique) "
                    "is open"), None
        return "no ceiling refusal is recorded; run the replan without a decision first", None
    backstopped, crossing = _since_user_backstop_gate(
        state, lambda: doc, decision="renegotiation_continue", axis="replans_since_user_decision",
        backstop=Thresholds().effort_replan_backstop(), what="continuing a renegotiation",
        cleared_by="a user approval, fire-acknowledgement or renegotiation")
    if backstopped is not None:
        return backstopped, None
    refused = _agent_self_decision_refusal(
        state, state.renegotiation_ceiling_difficulty_id, event="the ceiling event")
    if refused is not None:
        return refused, None
    # Past the backstop the gate has just held `doc` to the boundary; only below it is the
    # boundary still unjudged.
    if crossing is None:
        boundary = _autonomy_for(state, doc)
        if not boundary["eligible"]:
            return f"the plan is outside the approved boundary: {boundary['reason']}", None
    return None, crossing


def handoff_vcs_resources(parent_link: dict | None) -> list:
    """The `vcs_ref` resources (DR-P's commit/push) the adopted handoff carries in its
    `resources`: the one resource kind the parent's grant entries cannot express, so the
    parent's holdings and the child's coverage both read them from here. Malformed
    entries are skipped (under-holding, never over-holding)."""
    out: list = []
    for item in (parent_link or {}).get("resources") or []:
        if not isinstance(item, dict) or item.get("kind") != "vcs_ref":
            continue
        try:
            out.append(_resources.resource_from_dict(item))
        except ValueError:
            continue
    return out


def _handoff_held(parent_link: dict | None, venue: str) -> tuple[list, list]:
    """What the adopted handoff's grant entries hold: the typed resources they resolve to,
    and the identities of the entries the engine cannot resolve (a wildcard-tail rule),
    which cover only an identical unresolved item of the child's plan."""
    held: list = handoff_vcs_resources(parent_link)
    held_unresolved: list = []
    for entry in (parent_link or {}).get("grants") or []:
        if entry.get("rule"):
            resolution = plan_resources.resolve_rule_grant(entry["rule"], venue)
        else:
            resolution = plan_resources.resolve_add_dir_grant(entry["path"], entry["mode"])
        if resolution.status == "resolved":
            held.extend(resolution.resources)
        else:
            held_unresolved.append(list(resolution.identity or ("unresolved", entry["rule"])))
    return held, held_unresolved


def _self_approve_detail(autonomy: dict, *, replan: bool) -> str:
    """The `self_approve` Directive's detail, naming the basis the plan was judged on."""
    kind = "substantive replan" if replan else "plan"
    if autonomy.get("basis") == gates.PARENT_NORM_BASIS:
        return (f"{kind} is inside the norm and grants of the parent stage this session "
                "serves; run `approve --by agent`")
    return f"{kind} is inside the boundary the user approved for this order; run `approve --by agent`"


def _autonomy_for(state: SessionState, doc) -> dict:
    """`gates.autonomy_boundary` for `doc`, against the ledger of the order this session
    was user-approved under (the baseline plan's order key), so a changed order reads as
    `order_changed` rather than as an empty ledger of its own.

    A plan that declares `[meta.order] parent_norm` is judged by `gates.
    parent_norm_boundary` against the adopted handoff alone and never falls back to the
    ledger: the parent stage's own approval is the boundary it serves, and a ledger
    record, if one exists, belongs to a different order."""
    protected = _resources.protected_permission_surfaces(
        repo_root=state.repo_root, delivery_worktree=state.delivery_worktree,
        ledger_dir=str(order_approvals._root(None)),
    )
    view = plan_resources.compute_boundary_view(doc, order_digest(doc), venue=_venue_for(doc))
    order = doc.meta.order
    if order is not None and order.parent_norm:
        held, held_unresolved = _handoff_held(state.parent_link, _venue_for(doc))
        return gates.parent_norm_boundary(
            state.parent_link, order.parent_norm,
            [(c.command, c.expected_exit) for c in doc.meta.final_check
             if c.kind == CheckKind.SHELL.value],
            view.resources, held,
            unresolved=view.unresolved, held_unresolved=held_unresolved,
            max_depth=Thresholds().max_recursion_depth, protected=protected,
            resource_origins=view.resource_origins,
        )
    key = _ledgered_order_key(state) or order_digest(doc)
    ledger = order_approvals.get(key)
    # A decline this session holds is enforced where the stage is carried and at the agent's
    # approval; the ledger's copy is what a session that never saw the decline (a new session,
    # a reset) is held to, so only the stages the session does not already know are offered.
    known_issues = gates.declined_issue_set(state)
    known_titles = gates.declined_title_set(state)
    return gates.autonomy_boundary(
        ledger, view, ledger.get("first_thinker_verdicts"), protected=protected,
        live_optional=[
            s for s in doc.stages
            if not gates.is_declined_stage(s, known_issues, known_titles)
        ],
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
        if "resource" in entry:
            order_approvals.record_customer_grant(
                key, resource=_resources.resource_from_dict(entry["resource"]), by=by,
                at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            continue
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


def _is_agent_override(record) -> bool:
    return (record.verdict == gates._PLAN_REVIEW_OVERRIDE
            and (record.reviewer or "").strip().casefold() == AGENT_ACTOR)


def _agent_review_override(state: SessionState) -> dict | None:
    """The coordinator's own plan-review override on record, which the user must see
    named when the plan reaches them: the review was waived, not passed."""
    reviews = [(review, review.scope)
               for review in (state.plan_review, *state.plan_stage_reviews.values())
               if review is not None]
    reviews += [(review, plan_review_scope_for_pair(pair))
                for pair, review in state.plan_pair_reviews.items()]
    for review, scope in reviews:
        if _is_agent_override(review):
            spent = [e.get("difficulty_id") for e in state.history
                     if e.get("event") == "agent_review_override_record" and e.get("scope") == scope]
            return {"reviewer": review.reviewer, "scope": scope, "note": review.note,
                    "plan_path": review.plan_path,
                    "difficulty_id": spent[-1] if spent else None}
    return None


def _consume_override_record(state: SessionState, scope: str) -> None:
    """Spend the open difficulty record on the agent-authored override just recorded,
    so it cannot discharge a second decision, and log which record it was for the
    disclosure the user sees."""
    did = _difficulty_id(state.difficulty)
    if did is not None and did not in state.agent_ack_difficulty_ids:
        state.agent_ack_difficulty_ids.append(did)
    state.log("agent_review_override_record", scope=scope, difficulty_id=did)


def _settle_override_decision(state: SessionState, review, scope: str,
                              crossing: dict | None = None) -> None:
    """What a recorded review does to the self-decision machinery. An agent-authored
    override spends the record that earned it, and records the backstop crossing it
    made, if any. A user's override restarts the since-user backstops, and so does the
    release it ends: a non-agent override or an attested whole-plan pass leaves no
    release event for a later record to be measured against."""
    reviewer = (review.reviewer or "").strip()
    if review.verdict == gates._PLAN_REVIEW_OVERRIDE:
        if reviewer.casefold() == AGENT_ACTOR:
            _consume_override_record(state, scope)
            _record_backstop_crossing(state, crossing)
            return
        _clear_since_user_decision(state, reviewer)
        state.review_release_difficulty_id = None
    elif review.verdict == gates._PLAN_REVIEW_PASS and review.plan_sha256 and not scope:
        state.review_release_difficulty_id = None


def _review_trigger_record_open(state: SessionState) -> bool:
    """True while the open difficulty record is the one a review-cost trigger opened and
    that record is complete -- the trigger-cycle path to an agent-authored plan-review
    override, in place of the round release."""
    fire = state.review_trigger_fire
    diff = state.difficulty
    return (fire is not None and diff is not None
            and diff.uid == fire.get("difficulty_uid") and diff.complete())


_TRIGGER_EXIT_OVERRIDE = "override"


_TRIGGER_EXIT_CHANGED = "changed"


_TRIGGER_EXIT_ADMITTED = "approve is admitted on the recorded override"


_TRIGGER_EXIT_CLEARED = "approve is admitted on the review record that clears the gate"


_TRIGGER_EXIT_FRESH_PASS = (
    "approve re-checks the plan-review gate on the records carried onto the changed plan; "
    "a fresh counted PASS is owed for whatever they leave uncovered")


def _review_trigger_exit(state: SessionState, plan_path: str) -> str | None:
    """Which exit of a review-cost trigger a replan to `plan_path` would take, or None when
    no trigger is open: the same bytes the trigger fired on leave by the recorded override,
    any other bytes by a fresh review."""
    fire = state.review_trigger_fire
    if fire is None or state.node != Node.DIAGNOSING.value:
        return None
    if gates.plan_digest_equivalent(state, fire.get("plan_sha256"), _plan_file_sha256(plan_path)):
        return _TRIGGER_EXIT_OVERRIDE
    return _TRIGGER_EXIT_CHANGED


def _trigger_exit_text(trigger_exit: str, override: dict | None) -> str:
    """What `approve` rests on after the exit: the recorded agent override when one
    stands, else the review record that cleared the gate; the changed plan owes a fresh pass."""
    if trigger_exit == _TRIGGER_EXIT_CHANGED:
        return _TRIGGER_EXIT_FRESH_PASS
    return _TRIGGER_EXIT_ADMITTED if override else _TRIGGER_EXIT_CLEARED


def _drop_agent_overrides(state: SessionState) -> None:
    """Remove every agent-authored plan-review override record: the changed plan a trigger
    exit offers is owed a fresh review, which an override of the old bytes must not stand in for."""
    if state.plan_review is not None and _is_agent_override(state.plan_review):
        state.plan_review = None
    state.plan_stage_reviews = {
        scope: record for scope, record in state.plan_stage_reviews.items()
        if not _is_agent_override(record)}
    state.plan_pair_reviews = {
        pair: record for pair, record in state.plan_pair_reviews.items()
        if not _is_agent_override(record)}


def _plan_review_override_refusal(
    state: SessionState, prev, reviewer: str, doc,
) -> tuple[str | None, dict | None]:
    """(refusal, crossing) of an override by `reviewer`: a refusal when it may not be
    recorded, else None and the since-user backstop crossing an agent-authored one would
    be (None below the backstop and for any other author). `prev` is the prior record
    of the SAME scope and plan path (or None); `doc` is the plan under review (None when
    it does not load). An override is the customer's escape from a reviewer's `revise`
    deadlock: never by that reviewer itself, by the coordinator only while a
    round/friction release names the decision as its own, and by the order's customer_id
    when one is declared."""
    if (prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE
            and reviewer and reviewer == (prev.reviewer or "").strip()):
        return (
            f"override must come from a distinct reviewer: {reviewer!r} is the "
            "reviewer whose 'revise' verdict it would override (the user is the "
            "expected override author)"
        ), None
    crossing = None
    order = doc.meta.order if doc is not None else None
    if reviewer.casefold() == AGENT_ACTOR:
        # The override never counts as a passing first verdict for the autonomy
        # boundary (it is not written to the ledger).
        release_live = _plan_review_release_active(state)
        trigger_cycle = _review_trigger_record_open(state)
        if not release_live and not trigger_cycle:
            return (
                "an agent-authored override is refused: it is available only once the "
                "review-round or cross-axis friction release is active, or on the complete "
                "difficulty record a review-cost trigger opened"
            ), None
        # The trigger's record opened at the fire, so it is fresh by construction; the
        # release anchor, set at the first observation, may name that very record and
        # would refuse it as predating the release.
        release_active = release_live and not trigger_cycle
        cause = "release" if release_active else "trigger"
        if release_live:
            _sync_release_anchor(state)
        backstopped, crossing = _since_user_backstop_gate(
            state, lambda: doc, decision="plan_review_override",
            axis="review_rounds_since_user_decision",
            backstop=Thresholds().review_round_backstop(),
            what="recording a plan-review override",
            cleared_by="a user approval, fire-acknowledgement, renegotiation or plan-review override")
        if backstopped is not None:
            return f"an agent-authored override is refused: {backstopped}", None
        refused = _agent_self_decision_refusal(
            state, state.review_release_difficulty_id if release_active else None,
            event=f"the {cause}", decision="override")
        if refused is not None:
            return (
                f"an agent-authored override is refused: {refused}. Run the reflection exit "
                "first (declare, investigate, critique with differences_to_remove, normalize "
                f"with a real factor) after the {cause}, then override"
            ), None
    elif order is not None and order.customer_id and reviewer != order.customer_id:
        return (
            f"override reviewer {reviewer!r} does not match order customer_id "
            f"{order.customer_id!r}; record it as the customer of record, or correct --reviewer"
        ), None
    return None, crossing


def _scope_stage(scope: str) -> "int | None":
    """The stage a `stage:<n>` or `topo:unit:<n>` scope is about, else None."""
    index = plan_review_scope_stage_index(scope)
    if index is not None:
        return index
    pair = plan_review_pair_scope(scope)
    if pair is not None and is_unit_id(pair):
        node = unit_node(pair)
        return node if isinstance(node, int) else None
    return None


def _review_vouch(state: SessionState, doc, scope: str) -> "tuple[int, bool] | None":
    """`(stage, sole)` when a pass at `scope` reviews a stage `plan-review-delta` reports as
    owed for `doc`: `sole` is True when that stage is the only thing owed, so its pass is the
    whole review of the edit. None for any other scope. Read before the pass is stored,
    while the delta still lists the stage."""
    index = _scope_stage(scope)
    if doc is None or index is None:
        return None
    whole_plan, owed = gates.plan_review_delta(state, doc)
    if whole_plan or index not in owed:
        return None
    return index, owed == {index}


def _record_first_thinker_verdict(state: SessionState, doc, review, scope: str,
                                  *, vouch: "tuple[int, bool] | None" = None) -> None:
    """Write the thinker's first verdict on the changed unresolved commands into the
    order's ledger, once per (approved version, changed identities) key. A whole-plan pass,
    or a stage-scoped one that is the whole review owed (`vouch` sole), is that verdict
    for the whole key once every other stage holding a changed command has its own passing
    first verdict (an override there is no pass); a stage-scoped pass beside other owed
    stages is the first verdict only on that stage's own commands, under that stage's key.
    An override or an unattested review never is."""
    if doc is None or (scope and vouch is None) or not review.plan_sha256:
        return
    if review.verdict not in (gates._PLAN_REVIEW_PASS, gates._PLAN_REVIEW_REVISE):
        return
    key = _ledgered_order_key(state)
    if key is None:
        return
    autonomy = _autonomy_for(state, doc)
    verdict_key = autonomy["first_verdict_key"]
    if vouch is not None:
        recorded = order_approvals.get(key).get("first_thinker_verdicts") or {}
        others_passed = gates.every_stage_vouched(
            autonomy["first_verdict_stage_keys"], autonomy["unresolved_changed_commands"],
            recorded, skip=vouch[0])
        if not (vouch[1] and others_passed):
            verdict_key = autonomy["first_verdict_stage_keys"].get(vouch[0])
    if verdict_key is None:
        return
    order_approvals.record_first_thinker_verdict(key, verdict_key, {
        "verdict": review.verdict, "plan_sha256": review.plan_sha256, "reviewer": review.reviewer,
        **({"scope": scope} if scope else {}),
    })


def _kind_within_boundary(state: SessionState, kind: str, doc, *, ledgered: bool) -> str:
    """A refinement that moves the order digest, adds a resource beyond the user's
    approved set, or changes a command the engine cannot resolve to a resource is not a
    refinement: it re-enters the approval gate as substantive. `ledgered`: the order has
    a user-approved ledger record (`_ledgered_order_key`) or the plan declares a
    `parent_norm`; without either nothing is judged. A plan judged on the parent-norm basis
    re-enters the gate whenever its verdict is no longer eligible (a dropped parent check,
    an uncovered resource, an unresolved item the handoff does not carry)."""
    if kind != "refinement" or not ledgered:
        return kind
    verdict = _autonomy_for(state, doc)
    if verdict.get("basis") == gates.PARENT_NORM_BASIS:
        return kind if verdict["eligible"] else "substantive"
    if (verdict["order_changed"] or verdict["extra_resources"]
            or (verdict["unresolved_changed_commands"] and not _vouched_by_thinker_pass(verdict))):
        return "substantive"
    return kind


def _vouched_by_thinker_pass(verdict: dict) -> bool:
    """Whether the changed unresolved commands of `verdict` are all engine-executed and a
    thinker pass has vouched them: the whole-plan first verdict, or a stage-scoped pass at
    the scope the review delta reports as sufficient (each stage's own, when several
    stages carry a changed command)."""
    if verdict.get("thinker_pass") is not True or verdict.get("first_verdict_key") is None:
        return False
    return not any(c["needs_user"] for c in verdict["unresolved_changed_commands"])


def _replan_kind(state: SessionState, old, new) -> str:
    """The kind a replan from `old` to `new` applies as. The verify-identity relaxation
    is admitted only for a ledgered order; a plan that declares a `parent_norm` is judged
    by `_kind_within_boundary` too but gets no relaxation. `parent_norm` sits in the order
    place, which diffs as a refinement, so removing or changing the one the baseline
    declared is substantive here: the plan would otherwise leave the parent-norm basis
    (and its parent check) through an edit nobody judges."""
    from .plan import diff_plans
    ledgered = _ledgered_order_key(state) is not None
    old_order, order = old.meta.order, new.meta.order
    old_parent_norm = old_order.parent_norm if old_order is not None else None
    parent_norm = order.parent_norm if order is not None else None
    if old_parent_norm and old_parent_norm != parent_norm:
        return "substantive"
    parent_norm_plan = bool(parent_norm)
    kind = diff_plans(old, new, relax_verify_identity=ledgered)
    return _kind_within_boundary(state, kind, new, ledgered=ledgered or parent_norm_plan)


def _baseline_plan_for_tier(store: StateStore, state: SessionState):
    """The plan version the session is running, parsed from its stored snapshot, or None
    when that version is unknown (no approved-plan snapshot, or no stored copy of its
    bytes). None means the tier of an edit cannot be computed, and the replan routes as it
    did before the tier existed."""
    if not state.plan_snapshot_path:
        return None
    return _plan_from_snapshot(store, state, state.plan_snapshot_hash or "")


def _frozen_edge_stages(state: SessionState, baseline, new) -> frozenset[int]:
    """PASSED stages whose supplies `new` leaves as `baseline` had them. Their edges are
    frozen (changing one needs --reopen), so the submission edge rule cannot ask for them
    to change; an edge the replan adds or edits is still held to it. Empty without a
    baseline."""
    if baseline is None:
        return frozenset()
    old = {s.index: s.supplies for s in baseline.stages}
    passed = {s.index for s in state.stages
              if s.outcome.status == StageStatus.PASSED.value}
    return frozenset(s.index for s in new.stages
                     if s.index in passed and s.index in old
                     and s.supplies == old[s.index])


def _plan_from_snapshot(store: StateStore, state: SessionState, digest: str):
    """The plan version `digest` parsed from its stored snapshot, or None when no readable
    snapshot of it exists or it does not parse."""
    from .plan import PlanError, parse_plan

    data = _read_plan_version_snapshot(store, state, digest)
    if data is None:
        return None
    try:
        raw = tomllib.loads(data.decode("utf-8"))
        if state.plan_path:
            registered = Path(state.plan_path)
            raw, _visited = plan_refs.resolve_refs(
                raw, registered.parent, plan_refs.default_root(registered),
                read=plan_store.reader(), index_path=registered)
        return parse_plan(raw, strict=False)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, PlanError):
        return None


def _review_baseline(store: StateStore, state: SessionState):
    """(plan, sha256) of the plan version the session's review records describe: the
    approved snapshot when there is one, else the file at `plan_path`; (None, None) when
    neither can be read."""
    from .plan import PlanError, load_plan as _load

    baseline = _baseline_plan_for_tier(store, state)
    if baseline is not None:
        return baseline, state.plan_snapshot_hash
    if not state.plan_path:
        return None, None
    try:
        return _load(state.plan_path, strict=False), _plan_file_sha256(state.plan_path)
    except (OSError, PlanError):
        return None, None


def _carry_records(store: StateStore, state: SessionState, plan_path: str, *, in_place: bool,
                   baseline: "tuple | None" = None):
    """Carry the review, premise and order-coverage records onto the plan at `plan_path`
    as far as the edit's change tier allows (`carry_forward`). Returns `(state, report,
    change)`: with `in_place` False the records are carried on a copy and `state` stays as
    it was, so a gate can read the carried view of a command that may still be refused.
    `baseline` is `(plan, sha256, path)` of the version the records describe, for a caller
    that has already moved `state.plan_path` off it."""
    from .plan import PlanError, change_tier, load_plan as _load

    report = {"tier": None, "carried": False, "owed": [], "records": [], "held": []}
    old, old_sha = _review_baseline(store, state) if baseline is None else baseline[:2]
    old_path = state.plan_path if baseline is None else baseline[2]
    if old is None:
        return state, report, None
    try:
        new = _load(plan_path, strict=False)
    except (OSError, PlanError):
        return state, report, None
    change = change_tier(old, new)
    target = state if in_place else copy.deepcopy(state)
    report = carry_forward.apply(
        target, old, new, new_path=plan_path, new_sha=_plan_file_sha256(plan_path),
        change=change, old_path=old_path, old_sha=old_sha)
    return (target if report["carried"] else state), report, change


_VERDICT_PRESERVING_CLASSES = frozenset({"B", "F", "C"})


def _frozen_stage_survives(prev_stages, new_stages, index: int, tier) -> bool:
    """Whether a PASSED stage keeps its verdict although its carry digest moved: the edit
    touched only bookkeeping, final-venue or check-only leaves (nothing the delivered work
    was built from or promised to a consumer) and every supplier's interface is unchanged.
    False without a tier (the old side is unknown)."""
    from .stage_norm import interface_token

    change = tier.stages.get(index) if tier is not None else None
    if change is None or any(cls not in _VERDICT_PRESERVING_CLASSES for _, cls in change.moved):
        return False
    prev = {s.index: s for s in prev_stages}
    new = {s.index: s for s in new_stages}
    if index not in prev or prev[index].outcome.status != StageStatus.PASSED.value:
        return False
    return all(
        sup in prev and sup in new and interface_token(prev[sup]) == interface_token(new[sup])
        for sup in {e.on for e in new[index].supplies}
    )


def _verdict_survives(prev_stages, new_stages, index: int, tier=None) -> bool:
    """The one carry decision for a stage's settled verdict across a plan edit: its
    definition and its suppliers' interfaces are unchanged (`stage_carried`), or only
    verdict-preserving leaves moved (`_frozen_stage_survives`). Without a `tier` (the
    approve-time refresh of an in-place plan edit, which has no previous version to read)
    only the first holds."""
    from .stage_norm import stage_carried

    return (stage_carried(prev_stages, new_stages, index)
            or _frozen_stage_survives(prev_stages, new_stages, index, tier))


def _passed_dependents_closure(live_stages, new_stages, roots) -> set[int]:
    """`roots` plus every PASSED stage that transitively depends on one of them, along the
    dependency edges of either plan version."""
    edges: dict[int, set[int]] = {}
    for stage in (*live_stages, *new_stages):
        edges.setdefault(stage.index, set()).update(stage.depends_on)
    passed = {s.index for s in live_stages if s.outcome.status == StageStatus.PASSED.value}
    closure = set(roots)
    grew = True
    while grew:
        grew = False
        for index in passed - closure:
            if edges.get(index, set()) & closure:
                closure.add(index)
                grew = True
    return closure


def _frozen_stage_refusal(state: SessionState, tier, reopening: set[int]):
    """Refusal text and data for a replan that edits an operative or interface leaf of a
    PASSED stage the replan does not reopen; None when none does."""
    frozen = {}
    for stage in state.stages:
        if stage.outcome.status != StageStatus.PASSED.value or stage.index in reopening:
            continue
        change = tier.stages.get(stage.index)
        paths = [p for p, cls in change.moved if cls in ("O", "I")] if change else []
        if paths:
            frozen[stage.index] = paths
    if not frozen:
        return None
    named = "; ".join(f"stage {i} ({', '.join(p)})" for i, p in sorted(frozen.items()))
    flags = " ".join(f"--reopen {i}" for i in sorted(frozen))
    return (f"replan refused: it edits what an already PASSED stage was built from or "
            f"promises — {named}; nothing was applied. Re-run with {flags} to re-arm the "
            f"stage and every PASSED stage depending on it, or revert the edit",
            {"blockers": [f"stage {i} (PASSED): {', '.join(p)}" for i, p in sorted(frozen.items())],
             "frozen_stages": sorted(frozen)})


def _replan_pending_indices(state: SessionState, new, kind: str, tier=None,
                            reopened: frozenset[int] | set[int] = frozenset()) -> list[int]:
    """Indices of `new`'s stages this replan leaves to run: every stage but one whose
    PASSED verdict survives it. A refinement or no-op never re-arms a PASSED stage; a
    substantive replan keeps a PASSED stage's verdict only when `_verdict_survives` holds:
    its definition and its suppliers' interfaces are unchanged (`stage_norm.stage_carried`),
    or only verdict-preserving leaves moved. A stage in `reopened` is always left to run."""
    live = {s.index: s for s in state.stages}
    pending = []
    for ns in new.stages:
        prev = live.get(ns.index)
        passed = prev is not None and prev.outcome.status == StageStatus.PASSED.value
        if passed and ns.index not in reopened and (
            kind != "substantive" or _verdict_survives(state.stages, new.stages, ns.index, tier)
        ):
            continue
        pending.append(ns.index)
    return pending


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

    The set must cover every field `StageNorm.from_stage` reads as well: a
    `stage:<n>` norm target is snapshotted from the LIVE stage and compared against
    stages loaded from plan files, so a norm field left stale here makes an unedited
    stage read as moved, or an edited one as unchanged.
    `test_apply_refined_stage_fields_covers_every_norm_field` derives that field set
    from `StageNorm` itself.

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
    cur.renorms = list(refined.renorms)
    for field in fields(Criterion):
        if field.name not in _CRITERION_ENGINE_WRITTEN_FIELDS:
            setattr(cur.criterion, field.name, getattr(refined.criterion, field.name))
    cur.principle = replace(refined.principle) if refined.principle is not None else None
    cur.output_artifacts = list(refined.output_artifacts)
    cur.actor.executor = refined.actor.executor
    cur.actor.capability_required = refined.actor.capability_required
    cur.actor.cost_tier = refined.actor.cost_tier
    cur.actor.guard_exempt_paths = list(refined.actor.guard_exempt_paths)
    cur.supplies = list(refined.supplies)
    cur.optional = refined.optional
    cur.backlog_issue = refined.backlog_issue
    cur.branch = refined.branch
    cur.amendments = list(refined.amendments)
    if is_skipped(cur) and not cur.optional:
        cur.outcome.status = StageStatus.PENDING.value


def _sync_venue_from_plan(state: SessionState, doc: "PlanDoc | None" = None) -> None:
    """Derive state.repo_root, state.delivery_worktree and state.plan_task_id from a plan's [meta].

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
    state.landing_waiver = doc.meta.landing_waiver
    state.plan_task_id = doc.meta.task_id


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
    baseline=None,
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
    with no extra logic. A stage that is not carried (`stage_norm.stage_carried`: its
    full definition changed, or a direct supplier's interface did) is a different
    case: its recorded PASSED outcome no longer attests to the stage's current
    criterion or inputs, so it is reset to PENDING for re-verification. Carry is
    decided for every stage BEFORE `_apply_refined_stage_fields` mutates any —
    comparing after would compare a stage against itself post-copy, which always
    matches and would let a genuinely stale PASSED outcome survive unnoticed.

    An absent plan_path still returns [] — "there is nothing to refresh" is not a
    submission violation, and the plan_approval gate already refuses a session with no
    plan artifact. A plan_path that is set but no longer LOADS is different: it is a
    session whose approve is about to attest to bytes nobody can read, so it is reported
    as a violation rather than swallowed. The old silent return let approve pass on the
    stale pre-edit cache — the very "attests to a plan it never actually executes" failure
    this function's own docstring names."""
    if not state.plan_path:
        return []
    from .plan import PlanError, load_plan as _load
    try:
        refreshed = _load(state.plan_path)
    except (OSError, PlanError) as exc:
        return [f"cannot load the plan at {state.plan_path!r}: {exc}"]
    # `baseline` is the last approved plan: a finished stage's unchanged edge is frozen
    # against it, exactly as at replan, so approve judges the same exemption replan did.
    violations = _submission_problems(refreshed, runner, state.weight_class,
                                      _frozen_edge_stages(state, baseline, refreshed))
    if violations:
        return violations
    if advice is not None:
        advice.extend(_submission_advice(refreshed, runner, state.weight_class, state.plan_path))
    # Decided for every stage BEFORE any is mutated: a consumer's verdict depends on its
    # suppliers' pre-edit interfaces.
    carried = {rs.index: _verdict_survives(state.stages, refreshed.stages, rs.index)
               for rs in refreshed.stages}
    declined_issues = gates.declined_issue_set(state)
    declined_titles = gates.declined_title_set(state)
    rebuilt: list[Stage] = []
    for rs in refreshed.stages:
        try:
            cur = state.stage(rs.index)
        except KeyError:
            # An in-place edit may renumber or add a stage; the file is the plan, so the
            # session's stage list is rebuilt from it and a stage the file no longer has
            # leaves with it (an orphan SKIPPED stage would carry a decline for work that
            # is no longer there).
            rebuilt.append(rs)
            continue
        unchanged = carried[rs.index]
        _apply_refined_stage_fields(cur, rs)
        # One rule shared with the substantive-replan carry: a changed stage loses its
        # settled status, a declined one included, and is offered again for a fresh choice.
        if not unchanged and cur.outcome.status in (
            StageStatus.PASSED.value, StageStatus.SKIPPED.value,
        ):
            cur.outcome.status = StageStatus.PENDING.value
        rebuilt.append(cur)
    for s in rebuilt:
        s.outcome.declined = is_skipped(s) or (
            not is_settled(s) and gates.is_declined_stage(s, declined_issues, declined_titles))
    state.stages = rebuilt
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
    cli.GATE_LOG and have this pick it up on the next call.

    Each call writes one line whether the gate passed or refused: escalation_orphans,
    frozen_stage and renorm_final are called on every evaluation too, so a gate's
    block rate has a real denominator. The call also appends one engine row to the
    decision log (docs/operations/decision-telemetry.md)."""
    try:
        _append_jsonl(config_root.decision_log(), hook_telemetry.decision_row(
            f"engine:{gate}", "engine", "silent" if passed else "deny", state.session_id, layer="engine"))
    except Exception:
        pass
    _append_jsonl(GATE_LOG, {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": state.session_id,
        "node": state.node,
        "gate": gate,
        "blockers": list(blockers),
        "passed": passed,
    })


def _append_jsonl(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
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


def _submission_advice(
    doc, runner: Runner | None, weight_class: str | None, plan_path: str | None = None,
) -> list[str]:
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
        advice = submission_advice(
            doc,
            judge_runner=runner,
            judge_enabled=advisor.resolve_enabled(weight_class),
        )
    except Exception:
        advice = []
    try:
        positions = plan_store.visited_positions(plan_path) if plan_path else frozenset()
        advice.extend(f["message"] for f in plan_size_warnings(doc, positions))
    except Exception:
        pass
    return advice


def _submission_problems(doc, runner: Runner | None, weight_class: str | None,
                         frozen_edge_stages: frozenset[int] = frozenset()) -> list[str]:
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
    violations = submission_violations(
        doc,
        session_weight_class=weight_class,
        judge_runner=runner,
        judge_enabled=enabled,
        frozen_edge_stages=frozen_edge_stages,
    )
    # A node over plan-stage-max-chars cannot be reviewed in one pair prompt, so every seam
    # that admits plan bytes to a session (submit-plan, approve after an in-place edit,
    # replan, renormalize) refuses it; a missing size limit refuses too (fails closed).
    return violations + [f["message"] for f in plan_size_blocks(doc)]


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


class _VenueSnapshot:
    """Out-parameter of `_venue_tree_identity`: the raw tree sha it computed and, when
    `keep_objects` is set, the throwaway object directory holding that tree, left
    in place so `TreeProbe` can read it until `close()`."""

    def __init__(self, keep_objects: bool = False):
        self.keep_objects = keep_objects
        self.tree: str | None = None
        self.tmp_dir: str | None = None
        self.env_argv: list[str] | None = None
        self.run: Runner | None = None

    def close(self) -> None:
        tmp_dir, self.tmp_dir = self.tmp_dir, None
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class _GitTreeProbe(check_cache.TreeProbe):
    def __init__(self, snapshot: _VenueSnapshot):
        self._run = snapshot.run
        self._env_argv = snapshot.env_argv

    def tree_exists(self, tree: str) -> bool:
        return self._run(self._env_argv + ["cat-file", "-e", f"{tree}^{{tree}}"]).returncode == 0

    def changed_paths(self, old: str, new: str) -> list[str] | None:
        found = self._run(
            self._env_argv + ["diff-tree", "-r", "-z", "--name-only", "--no-renames", old, new]
        )
        if found.returncode != 0:
            return None
        return [p for p in (found.stdout or "").split("\0") if p]


def _venue_tree_identity(
    cwd: str | None, runner: Runner | None, snapshot: _VenueSnapshot | None = None,
) -> str | None:
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

    `snapshot`, when given, receives the raw tree sha; with `keep_objects` it also
    takes over the throwaway directory (the caller `close()`s it), which is the only
    place a dirty tree's objects exist.

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
        if snapshot is not None:
            snapshot.tree = tree.stdout.strip()
            if snapshot.keep_objects:
                snapshot.tmp_dir, snapshot.env_argv, snapshot.run = tmp_dir, env_argv, run
                tmp_dir = None
    finally:
        if tmp_dir:
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
    opened = _open_difficulty(state)
    data = {}
    if div is not None and gates.effort_active(state):
        now = _utcnow()
        data["effort_divergence"] = _record_effort_fire(state, div, now=now, store=store)
    store.save(state)
    if opened is None:
        return _difficulty_cap_directive(state, message, data)
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
    opened = _open_difficulty(state, declaration=Declaration(
        expected="every [meta.order] requirement accepted (verdict pass)",
        actual=f"requirement id(s) {failing_ids} recorded as fail",
        mismatch="the order's customer rejected part of the delivery after every "
                  "stage already PASSED",
    ))
    store.save(state)
    if opened is None:
        return _difficulty_cap_directive(
            state, f"AcceptanceReview carries a non-pass verdict on requirement id(s) {failing_ids}")
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
    opened = _open_difficulty(state)
    if opened is not None:
        fire["difficulty_uid"] = opened.uid
    store.save(state)
    data = {"effort_divergence": fire}
    route = _agent_replans_fire_route(state)
    if route is not None:
        data["agent_route"] = route
    if opened is None:
        return _difficulty_cap_directive(state, div.framing, data)
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
    `delivered_base` (merge-base of HEAD and the landed spec's `<remote>/<target>`) is
    stamped next to it: the lower bound of the range whose `Task:` trailer the check proves.
    A changed head clears the landing `record-landing` recorded for the old one.
    Fails open: an unresolvable venue or a git error leaves whatever was
    already stamped (if anything) untouched, rather than clobbering it."""
    cwd = state.resolve_check_venue(stage.criterion.verify_venue)
    if not cwd:
        return
    spec = _landed_spec_for_stage(state, stage.index)
    if spec is not None and spec.provider != "git":
        _freeze_provider_token(stage, spec.provider, cwd)
        return
    run = runner or subprocess_runner
    result = run(["git", "-C", cwd, "rev-parse", "HEAD"])
    head = result.stdout.strip() if result.returncode == 0 else ""
    if not head:
        return
    previous_head = stage.outcome.delivered_head
    stage.outcome.delivered_head = head
    if head != previous_head:
        stage.outcome.landed_head = None
        stage.outcome.landed_base = None
        stage.outcome.landed_content_waiver = None
    if spec is None or (head == previous_head and stage.outcome.delivered_base):
        return
    # The base is frozen with the head: a re-record on an unchanged head keeps the base
    # computed before landing (afterwards HEAD is on trunk and the merge-base would
    # collapse the task-proof range to empty). A merge-base equal to HEAD means HEAD is
    # already on trunk (a legacy state frozen without a base, re-recorded after landing):
    # stamping it would freeze that empty range, so the base stays unset.
    base = run(["git", "-C", cwd, "merge-base", "HEAD", f"{spec.remote}/{spec.target}"])
    if base.returncode == 0 and base.stdout.strip() and base.stdout.strip() != head:
        stage.outcome.delivered_base = base.stdout.strip()


def _freeze_provider_token(stage, provider: str, venue: str) -> None:
    """Stamp the delivery token a non-git landed provider freezes at `venue` onto
    `delivered_head`. Fails open like the git freeze: a missing or broken plugin, a
    raising provider, or a provider that cannot freeze leaves the stamp untouched, so
    render_landed_command refuses legibly instead of the stage failing."""
    from .landed_providers import load_provider
    try:
        token = load_provider(provider).freeze(venue)
    except (Exception, SystemExit):
        return
    if isinstance(token, str) and token:
        stage.outcome.delivered_head = token


def _landed_spec_for_stage(state: SessionState, stage_index: int):
    """The first landed check in this plan — a stage's own criterion (a
    self-reference) or a final_check — that names `stage_index` as its
    `delivered_stage`, else None."""
    for s in state.stages:
        crit = s.criterion
        if (crit.verify_kind == CheckKind.LANDED.value and crit.landed
                and crit.landed.delivered_stage == stage_index):
            return crit.landed
    for fc in state.final_check:
        if (fc.kind == CheckKind.LANDED.value and fc.landed
                and fc.landed.delivered_stage == stage_index):
            return fc.landed
    return None


def _needs_delivered_head_freeze(state: SessionState, stage_index: int) -> bool:
    """True when SOME landed check in this plan names `stage_index` as its
    `delivered_stage`. Freezing is the only case that matters; skipping it
    otherwise keeps every plan with no landed check byte-identical to before
    (no runner call record-result did not already make) — the regression an
    unconditional freeze would otherwise introduce for every ordinary stage."""
    return _landed_spec_for_stage(state, stage_index) is not None


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
    if result.returncode == LANDED_GIT_ERROR_EXIT and spec.provider != "git":
        reason = (getattr(result, "stderr", "") or "").strip().splitlines()
        return False, (
            f"landed check of provider {spec.provider!r} could not answer for "
            f"{spec.target!r} (exit {LANDED_GIT_ERROR_EXIT})"
            + (f": {reason[-1]}" if reason else "")
        ), result
    if result.returncode == LANDED_GIT_ERROR_EXIT:
        return False, (
            f"landed check could not resolve {spec.target!r} (or "
            f"{spec.remote}/{spec.target!r}) against the delivered commit — "
            f"git could not answer (exit {LANDED_GIT_ERROR_EXIT})"
        ), result
    return result.returncode == 0, None, result


def _option_carries_waiver_marker(option: dict) -> bool:
    return transcript_turns.has_marker_option({"questions": [{"options": [option]}]},
                                              CONTENT_WAIVER_ASK_MARKER)


def _chose_waiver_option(tool_input: dict, answers) -> bool:
    """True iff some question offering both a marked and an unmarked option was answered
    with the label of a marked one. A question whose options are all marked leaves no
    way to decline, so it never consents."""
    if not isinstance(answers, dict):
        return False
    for q in tool_input.get("questions") or []:
        if not isinstance(q, dict):
            continue
        options = [o for o in q.get("options") or [] if isinstance(o, dict)]
        marked = [o.get("label") for o in options if _option_carries_waiver_marker(o)]
        if marked and len(marked) < len(options) and answers.get(q.get("question")) in marked:
            return True
    return False


def _content_waiver_blocker(state: SessionState, stage_index: int) -> str | None:
    """Why `--accept-content-loss` is refused for `stage_index`, else None. The stage
    must have an earlier `record_landing` event (the waiver follows a recorded red), and
    the user must have CHOSEN the marked option of an AskUserQuestion asked after the
    latest `record_landing` event of ANY stage — so one answer licenses one waiver even
    when one landing recorded several stages."""
    if not any(h.get("event") == "record_landing" and h.get("stage") == stage_index
               for h in state.history):
        return (f"no earlier record_landing of stage {stage_index} is stamped: run "
                "record-landing without --accept-content-loss first, then ask the user")
    stamps = [h for h in state.history if h.get("event") == "record_landing"]
    anchor = stamps[-1].get("at")
    if not isinstance(anchor, (int, float)):
        return ("the latest record_landing event is not stamped: run record-landing "
                "without --accept-content-loss again, then ask the user")
    path = _present_plan_transcript(state.session_id)
    scan = ask_evidence.ask_after_event(path, CONTENT_WAIVER_ASK_MARKER, anchor)
    if scan is None:
        return (f"cannot locate or read the transcript of session {state.session_id!r}, so "
                f"the {CONTENT_WAIVER_ASK_MARKER!r} answer cannot be verified")
    after = [any(_chose_waiver_option(ask.tool_input, answers) for answers in ask.sole_answers)
             for ask in scan.after if ask.answered]
    if not after:
        return (f"no answered AskUserQuestion carrying {CONTENT_WAIVER_ASK_MARKER!r} in an "
                "option label or description was asked after the latest record_landing "
                "of any stage")
    if not any(after):
        return (f"the {CONTENT_WAIVER_ASK_MARKER!r} ask after the latest record_landing was "
                "not answered with an option carrying the marker (or its answer could not "
                "be read), so the user did not consent to the waiver")
    return None


def _disclose_landed_content_waivers(state: SessionState, data: dict, detail: str) -> str:
    """Name every stage whose recorded landing waives the content fact (verify-final
    and resolve both surface them)."""
    waivers = [{"stage": s.index, "reason": s.outcome.landed_content_waiver}
               for s in state.stages if s.outcome.landed_content_waiver]
    if not waivers:
        return detail
    data["landed_content_waivers"] = waivers
    named = ", ".join(f"stage {w['stage']}: {w['reason']}" for w in waivers)
    return (f"{detail}; NOTE: the landed check's content fact was waived by the user "
            f"({named})")


def _is_recursion_refusal(result) -> bool:
    """spawn-specialist refuses at the recursion cap with returncode 3 and a
    'max-recursion-depth=' stderr line (see spawn-specialist.py)."""
    return getattr(result, "returncode", None) == 3 or (
        "max-recursion-depth" in (getattr(result, "stderr", "") or "")
    )


_CHILD_OUTSIDE_PROJECT_EXIT = 2


_CHILD_OUTSIDE_PROJECT_LINE_RE = re.compile(r"^Refusal:\s*child-outside-project\s*$", re.MULTILINE)


def _is_child_outside_project_refusal(result) -> bool:
    """spawn-specialist refuses a child whose start directory loads no project settings
    while the parent runs in a project that registers hooks: exit 2 (also its generic
    argument error) with a `Refusal: child-outside-project` stdout line, which is
    what tells the two apart."""
    return getattr(result, "returncode", None) == _CHILD_OUTSIDE_PROJECT_EXIT and bool(
        _CHILD_OUTSIDE_PROJECT_LINE_RE.search(getattr(result, "stdout", "") or "")
    )


_GRANT_WIDENING_EXIT = 4


_REFUSAL_LINE_RE = re.compile(r"^[-*]?\s*\*{0,2}Refusal:\*{0,2}\s*grant-widening\s*$", re.MULTILINE)


_HANDOFF_SHA_LINE_RE = re.compile(r"^[-*]?\s*\*{0,2}Handoff-sha256:\*{0,2}\s*(\S+)\s*$", re.MULTILINE)


def _loop_detected_line(result) -> str | None:
    """The `LOOP_DETECTED: ...` first stdout line of a norm-loop refusal
    (spawn-specialist exit 3 before any child launched), else None."""
    stdout = (getattr(result, "stdout", "") or "").lstrip()
    if stdout.startswith("LOOP_DETECTED:"):
        return stdout.splitlines()[0].strip()
    return None


def _is_grant_widening_refusal(result) -> bool:
    """spawn-specialist refuses a delegation that would hold more authority than the
    session was handed with exit 4 and a `Refusal: grant-widening` stdout line (exit 4
    is also "claude not on PATH", so the line is the discriminator). A relaying
    manager may hand the same block back inside its own `PERMISSION-REQUEST:` return
    under any exit code, so the line alone decides in that form."""
    stdout = getattr(result, "stdout", "") or ""
    if not _REFUSAL_LINE_RE.search(stdout):
        return False
    if getattr(result, "returncode", None) == _GRANT_WIDENING_EXIT:
        return True
    return parse_marker(stdout)[0] == "PERMISSION-REQUEST"


def _parse_rule_lines(body: str) -> list[str]:
    """Every `Rule:` line of a PERMISSION-REQUEST block, in order; `_parse_rule_line`
    returns only the last."""
    rules = []
    for line in body.splitlines():
        m = _RULE_LINE_RE.match(line.strip())
        if m and m.group(1).strip():
            rules.append(m.group(1).strip().strip("`").strip())
    return rules


_REFUSAL_VCS_RESOURCE_RE = re.compile(
    LABEL_DECORATION + r"Resource:\*{0,2}\s*`?vcs_ref\s+(commit|push|land)\s+([^\s:`]+):(\S+?)`?\s*$")


def _refusal_vcs_resources(body: str) -> list:
    """The `Resource: vcs_ref <op> <remote>:<ref>` lines of a grant-widening refusal: the
    DR-P entries no `Rule:` line can name, which the user's answer must grant as resources."""
    if not _REFUSAL_LINE_RE.search(body or ""):
        return []
    out = []
    for line in body.splitlines():
        m = _REFUSAL_VCS_RESOURCE_RE.match(line.strip())
        if m:
            try:
                out.append(_resources.VcsRefResource(m.group(2), m.group(3), m.group(1)))
            except ValueError:
                continue
    return out


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
    # The accumulator compares digests by equality: give it one digest per plan lineage
    # (`plan-convert-live` links the forms of one plan), so a conversion is not a new version.
    plan_sha256 = gates.plan_lineage_root(state, plan_sha256)
    digest = {"force_digest" if thinker_record else "count_if_digest_differs": plan_sha256}
    totals = task_accumulator.add(
        state.task_id, "review_rounds", 1, session_id=state.session_id, now=_utcnow(),
        mirror_axis="review_rounds_since_user_decision", **digest,
    )
    state.review_rounds = totals["per_axis_totals"]["review_rounds"]
    _sync_release_anchor(state)


def _clear_since_user_decision(state: SessionState, by: str) -> None:
    """A decision the user (not the agent) took restarts the agent self-decision
    backstops: the counters they read are 'since the last user decision'."""
    if (by or "").strip().casefold() == AGENT_ACTOR:
        return
    task_accumulator.clear_since_user_decision(
        state.task_id, session_id=state.session_id, now=_utcnow())


def _count_replan(state: SessionState, change_class: str) -> None:
    """Count one replan of the task, on the cross-session total and on the since-user
    total the agent self-decision backstop reads — when it is operative. A no_change,
    bookkeeping or check-only replan changes nothing the work runs under, so it spends
    none of the budget those totals bound (the same split `effort.replan_count` makes)."""
    if change_class != effort.OPERATIVE_CLASS:
        return
    task_accumulator.add(
        state.task_id, "replan_count", 1, session_id=state.session_id, now=_utcnow(),
        mirror_axis="replans_since_user_decision",
    )


def _replan_class_data(state: SessionState) -> dict:
    """The replan counts by change class — what the effort replans scale counts
    (`operative`) next to what it does not — for a Directive or the status JSON."""
    return {"replan_classes": effort.replan_class_counts(state)}


def _effort_fire_window(state: SessionState) -> list[int] | None:
    """The window stages of the spend/wall-clock fire whose difficulty is the open one,
    or None when the open difficulty was not opened by such a fire."""
    difficulty = state.difficulty
    if difficulty is None:
        return None
    for fire in reversed(state.effort_fires):
        if (fire.get("difficulty_uid") == difficulty.uid
                and fire.get("scale") in effort.RATIO_SCALES):
            return list(fire.get("window_stages") or [])
    return None


def _finished_work_fire(state: SessionState, div: "effort.Divergence | None", *,
                        store: StateStore) -> dict | None:
    """Record a spend/wall-clock fire raised while every stage is settled and return its
    record, or None when it does not apply (the caller then routes the fire as usual).
    Work that has finished has no remaining plan to correct, so the fire is recorded
    with its retier records and the session proceeds instead of opening a difficulty."""
    if (div is None or div.scale not in effort.RATIO_SCALES or not state.all_stages_settled()
            or not gates.effort_active(state)):
        return None
    return _record_effort_fire(state, div, now=_utcnow(), store=store, finished_work=True)


def _finished_work_data(state: SessionState) -> dict:
    """The finished-work fires and their retier records, for a Directive's data."""
    fires = [f for f in state.effort_fires if f.get("disposition") == EFFORT_FIRE_FINISHED_WORK]
    return {"effort_finished_work": fires, "effort_retiers": list(state.effort_retiers)} if fires else {}


def _disclose_finished_work(state: SessionState, data: dict, detail: str) -> str:
    """Put the finished-work fires and their retier records on `data` and, when there
    are any, name them in `detail` (verify-final and resolve both surface them)."""
    extra = _finished_work_data(state)
    if not extra:
        return detail
    data.update(extra)
    fires = extra["effort_finished_work"]
    named = ", ".join(f"{f.get('scale')} {f.get('actual')}/{f.get('estimate')}" for f in fires)
    return (f"{detail}; NOTE: {len(fires)} effort fire(s) were raised on finished work and recorded "
            f"without a difficulty: {named} (see effort_finished_work, effort_retiers)")


def _park_node(state: SessionState) -> None:
    """Move the session to BLOCKED, remembering the node `unblock` returns to. A session
    already BLOCKED keeps the node it was first parked from: recording BLOCKED as its own
    way back would leave `unblock` nothing to restore."""
    if state.node != Node.BLOCKED.value:
        state.blocked_from = state.node
    state.node = Node.BLOCKED.value


def _park_blocked(state: SessionState, store: StateStore, stage, marker, base: dict) -> Directive:
    """Park the session at BLOCKED and escalate — for a spawn whose output is
    malformed/unroutable (no marker, or a marker the engine cannot resolve)."""
    _park_node(state)
    reason = "malformed spawn output" if marker in (None, "MALFORMED") else f"marker {marker}"
    state.log("dispatch_escalate", stage=stage.index, marker=marker)
    store.save(state)
    return Directive(
        False, state.node, "escalate",
        f"stage {stage.index} -> escalate ({reason})",
        marker="ESCALATE", data=base,
    )


_MANDATE_NODE = "(mandate-scoped)"


def _mandate_by_refusal(args) -> Directive | None:
    by = (args.by or "").strip()
    if by and by.casefold() != AGENT_ACTOR:
        return None
    return Directive(
        False, _MANDATE_NODE, "noop",
        f"{args.command} is a user decision: --by must name the customer, not {AGENT_ACTOR!r}",
        marker=DIRECTIVE_ESCALATE_TO_USER,
        data={"mandate": args.mandate_id, "by": by},
    )


def _mandate_view(mandate: mandate_rules.Mandate, now: dt.datetime) -> dict:
    spend = mandate_store.spend_now(mandate.id, now)
    verdict_ = mandate_rules.gate(mandate, now, spend)
    held, pid = mandate_store.probe_cycle(mandate.id)
    return {
        "mandate": mandate.to_dict(),
        "spend": {"day_usd": spend.day_usd, "week_usd": spend.week_usd},
        "may_start": verdict_.may_start,
        "refusals": list(verdict_.reasons),
        "cycle_running": held,
        "cycle_pid": pid,
    }


def _question_bag(store: StateStore, session_id: str):
    """(state, bag) for the active premise bag, or (state, None) when the plugin is
    not armed — the caller returns the not-active Directive, mirroring the ledger
    verbs' `if bag is None` guard."""
    state = _require(store, session_id)
    return state, state.plugins.get("premise")


def _enumeration_escape_counts(state, doc: "PlanDoc | None" = None) -> dict | None:
    """LEGACY (the `enumeration_escapes` directive field): plugins_premise.escape_counts
    for `state` — the history of the retired enumeration gate's escapes, kept so a bag
    written by an earlier engine still reports it. Tolerant of the two states every
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
    key-mismatch check. Reads only; the WRITE lives in the two disposing verbs and in
    `_register_plan_assumptions` (the third writer) so the package-wide single-writer
    scan stays exact.

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
    """The current binding of the stage an OrderElement is marked 'covered'
    by — the value cmd_order_dispose stamps into `content_digest` (#123), the
    order-coverage twin of `_bound_stage_key`. Bound to the stage's deliverable (its
    interface token, `premise.order_binding_key`) rather than to a named field or the
    whole definition: an order element cites a stage's OUTCOME, so an edit to HOW the
    stage proceeds does not move it and an edit to what it hands over does. Returns "" when
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
    keys = {s.index: stage_norm_keys(s) for s in doc.stages}
    stage_keys = keys.get(element.stage)
    if not stage_keys:
        return ""
    return premise.order_binding_key(stage_keys)


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


def _record_review_questions(state: SessionState, review_id: str, questions: list[str]) -> list[str]:
    """Upsert the customer questions one review act returned as premise candidates
    `qrev-<review id>-<N>` — 'raised', addressed to the review id — and return the
    ids that now stand for them, in order. The review id is `unit:<node>`, a pair
    id, `stage:<n>` or `whole`, so a question is traceable to the act that raised it.

    A statement already on file under the same review id keeps its row and the
    disposition recorded against it (the thinker repeating a question on a
    re-review is not new work for the coordinator); a new statement takes the next
    unused number, so no earlier row is renumbered. Other review ids and the legacy
    `qenum-` rows are never touched. Returns [] when the premise plugin is not armed
    — the questions are then recorded nowhere, the same as every other premise verb,
    and the recording directive says so (`customer_questions_unrecorded`)."""
    bag = state.plugins.get("premise")
    texts = [q.strip() for q in questions or [] if q and q.strip()]
    if bag is None or not texts:
        return []
    candidates = bag.setdefault("candidates", [])
    number = re.compile(re.escape(f"qrev-{review_id}-") + r"(\d+)")
    ids: list[str] = []
    for text in texts:
        standing = next(
            (c for c in candidates
             if number.fullmatch(c.get("id") or "") and c.get("statement") == text), None)
        if standing is None:
            taken = [int(m.group(1)) for c in candidates
                     if (m := number.fullmatch(c.get("id") or ""))]
            standing = {"id": f"qrev-{review_id}-{max(taken, default=0) + 1}",
                        "statement": text, "disposition": "raised", "reason": "",
                        "question": "", "target": review_id}
            candidates.append(standing)
        ids.append(standing["id"])
    state.log("review_questions_recorded", review=review_id, candidates=ids)
    return ids


RETIRED_ENUMERATION_MESSAGE = (
    "the standalone question enumeration is retired: every review act (unit, pair, stage, "
    "whole plan) now returns the customer questions it found, and plan-review records each "
    "with --customer-question as a `qrev-` candidate in the premise bag — disposition them "
    "with `agentctl question-candidate-dispose`. Nothing was run and no state changed."
)


def cmd_question_enumerate_retired(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """The retired `question-enumerate`, `question-enumerate-worker` and
    `question-enumerate-escape` commands. The names stay in the parser so a hand-typed
    or scripted invocation meets this message, not an argparse 'invalid choice'; the
    command exits 2 before reading or writing any state."""
    command = getattr(args, "command", "question-enumerate")
    print(f"agentctl {command}: {RETIRED_ENUMERATION_MESSAGE}", file=sys.stderr)
    raise SystemExit(2)


def _register_plan_assumptions(state, doc: "PlanDoc", plan_path: str) -> None:
    """Record each typed `[[meta.assumption]]` as an `assumed` premise
    (`assume-<id>`), stamped against the submitted plan. Upsert by id, so a
    re-submission rewrites the same row instead of adding one; a session without the
    premise bag has nothing to register into (the assumptions are then dropped
    silently). Prose is never parsed. The key is re-stamped on every submission on
    purpose: the plan itself re-declares the assumption each time, so a stage edit
    never stales it the way it stales a premise the user or agent disposed."""
    bag = state.plugins.get("premise")
    if bag is None or not doc.meta.assumptions:
        return
    questions = premise.questions_from_dicts(bag.get("questions", []))
    for a in doc.meta.assumptions:
        qid = f"assume-{a.id}"
        q = premise.Question(
            id=qid, question=a.text, disposition="assumed", basis=a.basis, risk=a.risk,
            target=f"stage:{a.stage}.method" if a.stage else "plan.goal",
            own_research="declared as a typed [[meta.assumption]] in the submitted plan",
        )
        q.disposed_at_key = _bound_stage_key(state, q, plan_path)
        questions = [x for x in questions if x.id != qid] + [q]
    bag["questions"] = premise.questions_to_dicts(questions)


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


def _present_plan_transcript(session_id: str) -> Path | None:
    """The newest transcript of the conversation that ran engine session
    `session_id` under any projects root, or None (see
    `engine_session_binding.transcript_for_session`)."""
    return engine_session_binding.transcript_for_session(session_id)


def _deliver_presentation_via_channel(state: SessionState, presentation, raw: bytes) -> None:
    """With a headless customer channel configured, hand the rendering to it and,
    when it answers, write the `channel` delivery stamp for essence/replan_diff
    (`full` is delivered and logged but backs no gate). No channel: nothing."""
    result = delivery_channel.deliver(presentation.kind, raw)
    if result is None:
        return
    delivery_channel.log_delivery(state, result, plan_sha256=presentation.plan_sha256)
    if not result.answered or presentation.kind == PLAN_PRESENTATION_KIND_FULL:
        return
    state_file = config_root.resolve_agentctl_state_file(state.session_id)
    if state_file is None:
        return
    delivery.write_stamp(state_file, delivery.DeliveryStamp(
        plan_path=presentation.plan_path,
        plan_sha256=presentation.plan_sha256,
        rendering_sha256=presentation.rendering_sha256,
        verified_ts=time.time(),
        source=delivery.SOURCE_CHANNEL,
    ))


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
    anchored = _sync_release_anchor(state)
    already_logged = any(
        e.get("event") == "plan_review_round_release" and e.get("rounds") == rounds
        for e in state.history
    )
    if not already_logged:
        state.log("plan_review_round_release", rounds=rounds, review_rounds=state.review_rounds,
                  threshold=Thresholds().effort_replan_absolute())
        store.save(state)
    elif anchored:
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
    delta_changed_parts: list = field(default_factory=list)

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


def _parse_review_questions(raw: list[str]):
    """The `--customer-question` texts of one review, or the refusal text: a blank,
    multi-line, field-syntax (`Q:` / `Customer questions:`) or `none` value is refused
    before any record is written, never recorded as an unreadable candidate or dropped."""
    for text in raw:
        defect = customer_question_defect(text)
        if defect:
            return None, f"--customer-question {text.strip()[:80]!r}: {defect}"
    return [text.strip() for text in raw], ""


def _question_data(state: SessionState, args, question_ids: list[str]) -> dict:
    """The directive fields for a review's customer questions: the candidate ids, and
    `customer_questions_unrecorded` when questions were passed but the premise plugin
    is not armed — they are recorded nowhere, which the caller must see. A review
    that passed no question carries neither field, so its directive keeps the shape
    callers had before reviews returned questions."""
    data: dict = {}
    passed = len(getattr(args, "customer_questions", None) or [])
    if passed or question_ids:
        data["customer_question_ids"] = question_ids
    if passed and state.plugins.get("premise") is None:
        data["customer_questions_unrecorded"] = passed
    return data


def _concern_parts(
    body: str, own_parts, *, default_to_own: bool = True, after_marker: bool = False,
) -> list[str]:
    token = gates._concern_part_token(body, after_marker=after_marker)
    if token is None:
        return list(own_parts) if default_to_own else []
    index = plan_review_scope_stage_index(token)
    return [plan_review_scope_for_stage(index) if index is not None else token]


def _changed_parts(parts, digests: dict, prev_digests: dict) -> list[str]:
    """The parts whose current digest is missing or differs from the one a record stored."""
    return [p for p in parts if digests.get(p) is None or prev_digests.get(p) != digests[p]]


def _plan_concern_severity(
    state: SessionState, doc, ledger_scope: str, own_parts, prev, parsed, verdict: str, *,
    attested: bool, regression_command: str, evidence: bool, may_promote: bool,
    judge_delta: bool = False,
) -> _ConcernPlan:
    """`judge_delta`: a topo row re-reviewed after a pass — when some of its own parts changed
    since that pass, a blocking concern naming no changed part is recorded as advisory."""
    digests = part_digest_map(doc) if doc is not None else {}
    prev_digests = prev.part_digests if prev is not None else {}
    delta_changed = (
        _changed_parts(own_parts or (), digests, prev_digests)
        if judge_delta and prev is not None and prev.verdict == gates._PLAN_REVIEW_PASS else []
    )
    open_parts = {
        p for e in state.concern_ledger.values()
        if e.scope == ledger_scope and gates.concern_unresolved(e, state, doc)
        for p in e.parts
    }
    parts_by_concern, effective = [], []
    for pc in parsed:
        parts = _concern_parts(
            pc.body, own_parts or tuple(digests), default_to_own=not delta_changed,
            after_marker=bool(delta_changed),
        )
        changed = set(_changed_parts(
            [p for p in parts if p in own_parts] if delta_changed else parts, digests, prev_digests))
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
            after_pass_on_changed=bool(delta_changed),
            restates_open=target is not None and gates.concern_unresolved(target, state, doc),
        ))
    effective_verdict = verdict
    if (verdict == gates._PLAN_REVIEW_REVISE and parsed and attested and may_promote
            and gates.EFFECTIVE_BLOCKING not in effective):
        reraised = _blocking_parts(parsed, parts_by_concern)
        still_open = [
            e for e in state.concern_ledger.values()
            if e.scope == ledger_scope and gates.concern_unresolved(e, state, doc)
            and _entry_stays_open(e, digests, reraised)
        ]
        if not still_open:
            effective_verdict = gates._PLAN_REVIEW_PASS
    return _ConcernPlan(parsed, parts_by_concern, effective, effective_verdict, digests,
                        regression_command, delta_changed)


def _blocking_parts(parsed, parts_by_concern) -> set[str]:
    return {
        p for pc, parts in zip(parsed, parts_by_concern)
        if pc.severity == SEVERITY_BLOCKING for p in parts
    }


def _entry_stays_open(entry: ConcernRecord, digests: dict, reraised: set[str]) -> bool:
    """An open concern survives a record unless a part it names changed since it was
    last raised and the record does not re-raise that part as blocking."""
    moved = _changed_parts(entry.parts, digests, entry.raise_digests)
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
                  verdict=cp.verdict, severities=cp.severities, effective=cp.effective,
                  **({"changed_parts": cp.delta_changed_parts} if cp.delta_changed_parts else {}))


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
    if getattr(args, "escalate_pairs", False) and not is_unit_id(pair):
        return refuse(f"--escalate-pairs belongs to a unit review, not to pair {pair!r}")
    try:
        doc, _, live = load_plan_with_digest(target)
        if is_unit_id(pair):
            parse_unit(doc, pair)
        else:
            parse_pair(doc, pair)
    except (OSError, PlanError, ValueError) as e:
        return refuse(f"--scope 'topo:{pair}' cannot be recorded against {target}: {e}")
    verdict = args.verdict
    parsed, parse_error = _parse_review_concerns(state, list(getattr(args, "concerns", None) or []), verdict)
    if parsed is None:
        return refuse(parse_error)
    question_texts, question_error = _parse_review_questions(
        list(getattr(args, "customer_questions", None) or []))
    if question_texts is None:
        return refuse(question_error)
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
    crossing = None
    if verdict == gates._PLAN_REVIEW_OVERRIDE:
        if not reviewer or not note:
            return refuse("a pair override needs a non-empty --reviewer and --note")
        refusal, crossing = _plan_review_override_refusal(state, prev, reviewer, doc)
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
    ledger_scope = plan_review_scope_for_pair(pair)
    # With no readable snapshot of the judged version the reviewer was told to judge the row
    # whole, so no delta downgrade may apply.
    delta_visible = prev is not None and _prior_row_diff(store, state, doc, prev, pair)["available"]
    cp = _plan_concern_severity(
        state, doc, ledger_scope, pair_part_tokens(pair), prev,
        parsed, verdict, attested=bool(attested), regression_command=regression_command,
        evidence=bool(regression_exit), may_promote=True, judge_delta=delta_visible,
    )
    concerns = cp.bodies
    if is_unit_id(pair):
        node_fields = {
            "base": parse_unit(doc, pair), "service": "",
            "unit_norm": unit_currency_hash(doc, pair),
        }
    else:
        base, service = parse_pair(doc, pair)
        node_fields = {
            "base": base, "service": service, **pair_binding(doc, pair), **pair_content(doc, pair),
        }
    review = PlanPairReview(
        pair=pair, verdict=verdict, reviewer=reviewer,
        concerns=concerns, note=note, plan_path=target, plan_sha256=attested,
        record_seq=_next_record_seq(state), **node_fields,
    )
    _stamp_concern_plan(review, cp, verdict)
    review.concern_ids = _commit_concern_ledger(
        state, review, cp, ledger_scope, [f"c{i}" for i in range(len(concerns))], review.record_seq)
    ledger = []
    if review.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, review, concerns, "confirmed-gap")
    elif verdict == gates._PLAN_REVIEW_OVERRIDE and prev is not None and prev.verdict == gates._PLAN_REVIEW_REVISE:
        ledger = _condition4_ledger_rows(state, doc, target, prev, prev.concerns, "false-alarm")
    vouch = _review_vouch(state, doc, ledger_scope)
    state.plan_pair_reviews[pair] = review
    released: list[str] = []
    if getattr(args, "escalate_pairs", False):
        released = carry_forward.release_holds(state, unit_node(pair))
    _settle_override_decision(state, review, plan_review_scope_for_pair(pair), crossing)
    _record_first_thinker_verdict(state, doc, review, ledger_scope, vouch=vouch)
    question_ids = _record_review_questions(state, pair, question_texts)
    if _is_counted_review(review.verdict, reviewer):
        _count_review_round(state, live)
    _count_review_act(state, review.verdict, reviewer)
    _log_concerns_downgraded(state, cp, ledger_scope, verdict)
    state.log("plan_pair_review", target=target, pair=pair, verdict=review.verdict,
              raw_verdict=verdict, reviewer=reviewer, record_seq=review.record_seq,
              concern_ids=review.concern_ids,
              plan_sha256=attested, concerns=concerns, note=note,
              regression_command=regression_command, regression_exit=regression_exit,
              ledger_lines=len(ledger), escalated_pairs=released)
    store.save(state)
    _append_escalation_ledger(ledger)
    status = gates.pair_status(state, doc, target, pair)
    return Directive(
        True, state.node, "continue",
        f"pair review recorded for {pair!r} against {target} (verdict={verdict}); "
        f"pair status: {status}"
        + (f"; escalated to the pairs: {', '.join(released)}" if released else ""),
        data={"pair": pair, "pair_status": status, "ledger_lines": len(ledger),
              "escalated_pairs": released,
              "concern_ids": review.concern_ids, **_question_data(state, args, question_ids),
              **_crossing_data(crossing)},
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
    plan loads, the per-pair binding hashes and per-unit and per-pair currency hashes
    the walk-stale sets are later measured against. A plan whose pairs cannot be
    enumerated leaves them unset, which the gate reads as every unit and pair being
    stale."""
    review.record_seq = _next_record_seq(state)
    if doc is None:
        return
    try:
        review.reviewed_pair_bindings = gates.pair_baseline_bindings(doc)
        review.reviewed_pair_currency = gates.pair_baseline_currency(doc)
        review.reviewed_unit_currency = gates.unit_baseline_currency(doc)
    except PlanError:
        review.reviewed_pair_bindings = None
        review.reviewed_pair_currency = None
        review.reviewed_unit_currency = None


def _count_plan_review_round(state: SessionState, target: str) -> None:
    """The post-approval round count of a whole-plan review (see the comment in
    cmd_plan_review): once per plan VERSION, never for an unreadable plan."""
    if state.approval is not None and state.approval.passed:
        counted = _plan_file_sha256(target)
        if counted and not gates.plan_digest_equivalent(
                state, counted, state.plan_review_counted_digest):
            state.plan_review_rounds += 1
            state.plan_review_counted_digest = counted
            _sync_release_anchor(state)


def _review_cycle_arrive(state: SessionState, plan_sha256: str) -> bool:
    """A plan version arrives (a submission or a replan). A sha other than the open
    cycle's opens a fresh cycle -- acts and the minutes anchor restart, and what the last
    fire already spent on them is forgotten; the same sha leaves the cycle alone. The
    stage streaks span versions and are not touched. Returns True when a cycle opened."""
    if state.review_cycle_open and gates.plan_digest_equivalent(
            state, state.review_cycle_sha, plan_sha256):
        return False
    state.review_cycle_sha = plan_sha256
    state.review_cycle_open = True
    state.review_cycle_acts = 0
    state.review_cycle_minutes_anchor = gates.review_booked_minutes(state)
    for key in ("cycle_acts", "cycle_minutes"):
        state.review_trigger_spent.pop(key, None)
    return True


def _review_cycle_close(state: SessionState) -> None:
    """Approval ends the pre-approval review cycle, its streaks and the fire's bookkeeping."""
    state.review_cycle_open = False
    state.review_cycle_sha = ""
    state.review_cycle_acts = 0
    state.review_cycle_minutes_anchor = None
    state.review_stage_streaks = {}
    state.review_trigger_spent = {}
    state.review_trigger_fire = None


def _count_review_act(state: SessionState, verdict: str, reviewer: str, *,
                      stage_scope: str = "") -> None:
    """One counted review record (a pass or revise by a real reviewer: not an override,
    not a carried record, not the composition record) is one act of the open cycle. A
    stage's own record also moves that stage's streak -- a pass ends it, a revise extends
    it; whole-plan and pair records never do."""
    if not _is_counted_review(verdict, reviewer):
        return
    if state.review_cycle_open:
        state.review_cycle_acts += 1
    if stage_scope:
        if verdict == gates._PLAN_REVIEW_PASS:
            state.review_stage_streaks.pop(stage_scope, None)
            state.review_trigger_spent.pop(f"stage_streak:{stage_scope}", None)
        else:
            state.review_stage_streaks[stage_scope] = state.review_stage_streaks.get(stage_scope, 0) + 1


def _spend_review_trigger(state: SessionState, fire: dict) -> None:
    """Record the evidence a crossed trigger fired on, so `gates.review_trigger` stays
    quiet until fresh evidence arrives: the act count, the minutes anchor, the streak value."""
    if fire["trigger"] == "cycle_acts":
        state.review_trigger_spent["cycle_acts"] = state.review_cycle_acts
    elif fire["trigger"] == "cycle_minutes":
        state.review_trigger_spent["cycle_minutes"] = state.review_cycle_minutes_anchor
    else:
        state.review_trigger_spent[f"stage_streak:{fire['stage']}"] = fire["value"]


def _evaluate_review_trigger(
    state: SessionState, store: StateStore, review_blockers: list[str],
) -> Directive | None:
    """The review-cost trigger at the review-gate points (`plan-review-compose`, a
    whole-plan or stage `plan-review` record, `approve`, a same-sha `submit-plan`): a
    Directive routing PLAN_READY into the overcome-difficulty cycle when
    `gates.review_trigger` names a crossed trigger while the plan-review gate still has
    blockers; None otherwise. The evidence is spent whether or not a record opens, and the
    state is saved here because the callers return this Directive instead of saving. At the
    difficulty depth cap nothing is routed and the user decides."""
    if not review_blockers or state.node != Node.PLAN_READY.value or state.review_trigger_fire is not None:
        return None
    fire = gates.review_trigger(state)
    if fire is None:
        return None
    _spend_review_trigger(state, fire)
    opened = _open_difficulty(state)
    if opened is None:
        store.save(state)
        return _difficulty_cap_directive(
            state, f"review trigger {fire['trigger']} crossed ({fire['value']} >= {fire['threshold']})",
            {"review_trigger": dict(fire)})
    state.node = transition(state.node, "review_trigger")
    state.review_trigger_fire = {
        **fire, "difficulty_uid": opened.uid, "plan_sha256": state.review_cycle_sha}
    state.log("review_trigger", **fire, plan_sha256=state.review_cycle_sha)
    store.save(state)
    where = f" of {fire['stage']}" if fire["stage"] else ""
    return Directive(
        False, state.node, "declare",
        f"review trigger {fire['trigger']}{where} crossed ({fire['value']} >= {fire['threshold']}): "
        "run overcome-difficulty -- declare, investigate, critique, normalize -- then leave by a "
        "replan: an agent override of the review gate on the complete record followed by a "
        "no_change replan, or a changed plan that approve re-checks against its carried records "
        "and a fresh counted PASS",
        marker="OVERCOME-DIFFICULTY",
        data={"review_trigger": dict(fire)},
    )


def _pair_ledger_groups(state: SessionState, pair: str, target: str) -> "dict[int, list[ConcernRecord]]":
    """The pair scope's ledger entries raised against `target`, grouped by the record that
    raised them, each group in concern order — how a record taken before events carried
    `concern_ids` is found."""
    scope = plan_review_scope_for_pair(pair)
    groups: dict[int, list[ConcernRecord]] = {}
    for entry in state.concern_ledger.values():
        if entry.scope == scope and entry.plan_path == target:
            groups.setdefault(entry.record_seq, []).append(entry)
    for group in groups.values():
        group.sort(key=lambda e: (len(e.local_id), e.local_id))
    return groups


def _ledger_concern(entry: ConcernRecord, state: SessionState, doc) -> dict:
    return {
        "id": entry.id, "severity": entry.severity, "effective_severity": entry.effective,
        "parts": list(entry.parts), "text": entry.text,
        "unresolved": gates.concern_unresolved(entry, state, doc),
    }


def _prior_row_diff(store: StateStore, state: SessionState, doc, latest, pair: str) -> dict:
    """The diff of the row's parts between the plan version a reviewer judged and `doc`.
    The judged version of a carried record is the one its first carry started from
    (`carried_from_sha`), not the version it was carried onto; with no readable snapshot of
    it the row is `available: False` and the reviewer judges it whole."""
    from .render import row_diff

    judged = latest.carried_from_sha or latest.plan_sha256
    old = _plan_from_snapshot(store, state, judged)
    if old is None:
        return {"available": False, "from_sha": judged, "text": ""}
    return {"available": True, "from_sha": judged, "text": row_diff(old, doc, pair)}


def _walk_target(args, state: SessionState) -> "str | None":
    return getattr(args, "target", None) or state.plan_path


def _parse_verdicts(raw_verdicts: list[str]) -> tuple[list[RequirementVerdict], list[str]]:
    """Parse repeatable ``--verdict '<requirement_id>|<pass|fail|withdrawn>[|<note>]'`` specs
    into typed RequirementVerdict objects. Returns ``(verdicts, errors)`` — a non-empty
    ``errors`` list means the caller must reject with a failing Directive and record
    nothing. Mirrors ``_parse_partition_units``'s pipe-delimited shape and
    ``(parsed, errors)`` return contract.

    ``withdrawn`` records that a requirement was retracted by the customer (its premise
    was flawed, or the work demonstrated it was no longer wanted); it neither blocks
    resolution nor counts as a delivered requirement. A note is MANDATORY for withdrawn:
    the record must name who withdrew the requirement and why, so the ledger is not
    silently missing a delivery.

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
                f"verdict {pos}: expected "
                f"'<requirement_id>|<pass|fail|withdrawn>[|<note>]', got {spec!r}"
            )
            continue
        req_id = parts[0].strip()
        verdict = parts[1].strip()
        note = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else ""
        if not req_id:
            errors.append(f"verdict {pos}: empty requirement id")
        if verdict not in ("pass", "fail", "withdrawn"):
            errors.append(
                f"verdict {pos}: verdict must be 'pass', 'fail', or 'withdrawn', "
                f"got {verdict!r}"
            )
        elif verdict == "withdrawn" and not note:
            errors.append(
                f"verdict {pos}: 'withdrawn' requires a note naming who withdrew the "
                "requirement and why (format: '<id>|withdrawn|<note>')"
            )
        if req_id in seen:
            errors.append(
                f"verdict {pos}: requirement id {req_id!r} already verdicted at position "
                f"{seen[req_id]} (one verdict per requirement)"
            )
        else:
            seen[req_id] = pos
        verdicts.append(RequirementVerdict(requirement_id=req_id, verdict=verdict, note=note))
    return verdicts, errors


def _skip_optional_problems(
    indices: list[int], doc: "PlanDoc | None", state: SessionState, *, by_agent: bool,
) -> list[str]:
    """Why `approve --skip-optional <indices>` cannot be honoured; [] when it can."""
    if by_agent:
        return ["--skip-optional is the customer's decision and cannot be made with --by agent"]
    if doc is None:
        return ["--skip-optional needs a loadable plan at plan_path"]
    by_index = {s.index: s for s in doc.stages}
    problems: list[str] = []
    for index in indices:
        stage = by_index.get(index)
        if stage is None:
            problems.append(f"stage {index} does not exist")
        elif not stage.optional:
            problems.append(f"stage {index} is not optional (only `optional = true` stages can be declined)")
    if problems:
        return problems
    return _stranded_dependency_problems(doc, state, set(indices))


def _stranded_dependency_problems(
    doc: "PlanDoc", state: SessionState, newly_declined: set[int],
) -> list[str]:
    """Stages that stay live but depend on a declined stage and so could never become ready.

    Declined = `newly_declined` plus every stage the session already holds SKIPPED. Run on
    every approve that has any declined stage, not only on `--skip-optional`: a replan can
    carry a SKIPPED stage forward and add a live optional stage that depends on it."""
    declined = newly_declined | {s.index for s in state.stages if is_skipped(s)}
    problems: list[str] = []
    for stage in doc.stages:
        stranded = sorted(d for d in stage.depends_on if d in declined)
        if stage.index not in declined and stranded:
            problems.append(
                f"stage {stage.index} depends on declined stage(s) {stranded} — decline it too "
                f"or keep them"
            )
    return problems


def _mandate_basis_problems(plan_path, plan_data: bytes | None, approver: str) -> list[str]:
    """Why the `mandate:<id>` approver may not approve the plan at `plan_path`, empty when it may.

    The approval is the mandate's, for one item its running cycle took: `plan_data` -- the bytes
    `cmd_approve` read once and snapshots -- is the item norm re-rendered from the inputs beside
    the plan, the cycle lock is held, the cycle's start event and the item's taken event name this
    plan's issue, directory and worktree, the worktree lies under the cycle's worktree root, the
    cycle's repo is the repo this engine runs from, and the mandate's gate is open. Never raises."""
    inputs_file, item_event, cycle_event = "norm.inputs.json", "item-taken", "cycle-start"
    mandate_id = approver[len(mandate_rules.mandate_approver("")):]
    mandate = mandate_store.load_mandate(mandate_id) if mandate_id else None
    if mandate is None:
        return [f"no mandate {mandate_id!r}"]
    plan = Path(plan_path or "")
    if plan_data is None:
        return [f"the plan and {inputs_file} cannot be read beside it"]
    try:
        plan_text = plan_data.decode("utf-8")
        inputs = json.loads((plan.parent / inputs_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"the plan and {inputs_file} cannot be read beside it"]
    if not isinstance(inputs, dict):
        return [f"{inputs_file} is not an object"]
    held, _pid = mandate_store.probe_cycle(mandate_id)
    if not held:
        return ["no cycle holds the mandate's lock"]
    events = mandate_store.read_events(mandate_id)
    starts = [e for e in events if e.get("event") == cycle_event]
    if not starts:
        return ["the cycle recorded no start"]
    start = starts[-1]
    detail = start.get("detail") or {}
    problems: list[str] = []
    cycle_id = inputs.get("cycle_id")
    if detail.get("cycle_id") != cycle_id:
        problems.append("the inputs name a cycle that is not the running one")
    if not detail.get("repo") or inputs.get("repo") != detail.get("repo"):
        problems.append("the inputs' repo is not the cycle's repo")
    elif os.path.realpath(detail["repo"]) != os.path.realpath(REPO_ROOT):
        problems.append("the cycle's repo is not the repo this engine runs from")
    root = detail.get("worktree_root")
    worktree = inputs.get("worktree")
    try:
        under_root = bool(root) and isinstance(worktree, str) and os.path.commonpath(
            [os.path.normpath(root), os.path.normpath(worktree)]) == os.path.normpath(root) \
            and os.path.normpath(worktree) != os.path.normpath(root)
    except ValueError:
        under_root = False
    if not under_root:
        problems.append("the inputs' worktree is not under the cycle's worktree root")
    taken = [
        e for e in events
        if e.get("event") == item_event
        and (e.get("detail") or {}).get("cycle_id") == cycle_id
        and (e.get("detail") or {}).get("issue") == inputs.get("issue")
        and (e.get("detail") or {}).get("item_dir") == str(plan.parent)
        and (e.get("detail") or {}).get("worktree") == worktree
    ]
    if not taken:
        problems.append("the cycle recorded no item taken for this issue, directory and worktree")
    now = mandate_store.utcnow()
    started_at = mandate_rules.parse_ts(start.get("ts"))
    elapsed = max(0.0, (now - started_at).total_seconds() / 60.0) if started_at else 0.0
    gate_result = mandate_rules.gate(mandate, now, mandate_store.spend_now(mandate_id, now), elapsed)
    return problems + mandate_rules.mandate_approval_problems(plan_text, inputs, mandate, gate_result)


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


def _continuation_worktree(state: SessionState, stage: Stage) -> str | None:
    """The shared worktree/branch a DEPENDENT spawn stage should continue, or
    None for an independent stage (empty depends_on) or one whose dependencies
    are all in-thread. A prior SPAWN stage's committed-but-un-landed work lives
    in a branch dispatch never told the next developer about — this names it.

    Prefers the plan's declared [meta] delivery_worktree (state.delivery_worktree);
    falls back to the task_id-scoped default only when repo_root is known (a
    relative worktree path with no anchor would be meaningless)."""
    spawn = {s.index for s in state.stages if s.is_spawn()}
    # Decided per edge, so several edges to one supplier never shadow each other: an
    # edge continues if it says "continuation" or says nothing (the depends_on
    # inference); a dependency with no edge at all keeps that inference too.
    deliveries: dict[int, set] = {}
    for sup in stage.supplies:
        deliveries.setdefault(sup.on, set()).add(sup.delivery)
    if not any(
        deliveries.get(d, {None}) & {None, "continuation"}
        for d in stage.depends_on if d in spawn
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
    if gates.pending_renorm_stage_blockers(state, stage.index):
        state.log("reattest_declined", stage=stage.index,
                   reason="stage is bound to a norm-edit obligation whose file is unchanged")
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
    if state.all_stages_settled():
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


_RULE_LINE_RE = re.compile(LABEL_DECORATION + r"Rule:\*{0,2}\s*(.*)$")


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


_RESOURCE_LINE_RE = re.compile(LABEL_DECORATION + r"Resource:\*{0,2}\s*(.*)$")


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
    approved.extend(coverage.resources)
    for requested in res.resources:
        if not any(_covers_resource(appr, requested, protected) for appr in approved):
            return [], "not-approved"
    return res.resources, None


def stage_branch_resources(state: SessionState, stage_index: int) -> list:
    """DR-P's commit/push resources for one stage as frozen at approve beside the grant
    entries (`plan.materialized_grant_entries`), so a spawn and `stage-grants` hand out
    what the approval derived. Never derived live: a session that bound nothing, entries
    failing their hash, a stage without a frozen block (a session bound before DR-P, whose
    plan could not declare `branch`) and a malformed block all yield [] -- drift may
    under-grant, never over-grant."""
    stored = state.approved_grant_entries
    if stored is None:
        return []
    if entries_grants_sha256(stored) != state.approved_grants_sha256:
        return []
    frozen = (stored.get(str(stage_index)) or {}).get("branch")
    if not isinstance(frozen, dict):
        return []
    try:
        return [_resources.resource_from_dict(d) for d in frozen.get("resources") or []]
    except ValueError:
        return []


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
        snap_doc = _load_prior_plan(state, snap_path)
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
    if state.approved_grant_entries is not None:
        # The entries bound when the hash was (see `_bind_approved_grants`): what was
        # approved, not a re-derivation against the engine code and venue filesystem of
        # this moment. The hash stays the guard -- it must still cover them -- so state
        # corruption or a hand-edited hash withholds the derived grants as before. Only
        # a session bound before they were stored falls through to the re-derive-and-
        # compare below.
        if entries_grants_sha256(state.approved_grant_entries) != state.approved_grants_sha256:
            return declared_entries, derived_entries, dropped, (
                "derived-grants hash mismatch: the stored approved grant entries no "
                "longer reproduce approved_grants_sha256 -- derived grants withheld"
            ), None
        stored = state.approved_grant_entries.get(str(stage_index)) or {}
        return (declared_entries, list(stored.get("derived") or []),
                list(stored.get("dropped") or []), None, None)
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
    derived, dropped = _derive_stage_grants(snap_stage, venue)
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
        snap_doc = _load_prior_plan(state, snap_path)
    except (OSError, PlanError):
        return False
    snap_stage = next((s for s in snap_doc.stages if s.index == stage_index), None)
    if snap_stage is None:
        return False
    venue = _venue_for(snap_doc)
    derived, _dropped = _derive_stage_grants(snap_stage, venue)
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
    return _grants.StageGrants(
        allow=allow, add_dirs=add_dirs, resources=stage_branch_resources(state, stage_index))


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
    return _grants.StageGrants(
        allow=allow, add_dirs=add_dirs, resources=stage_branch_resources(state, stage_index))


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


def _fail_stage_into_diagnosing(
    state: SessionState, store: StateStore, stage: Stage, *, note: str, event: str,
    declaration: Declaration, escalated_from: dict | None = None, **event_fields,
):
    """FAILED / DIAGNOSING on `stage` for a fault no re-dispatch of the stage unchanged
    would remove: mirrors `cmd_record_result`'s failed-stage branch, including its two-step
    EXECUTING -> VERIFYING -> DIAGNOSING transition (`machine.TRANSITIONS` permits
    `diagnose` only from VERIFYING; a session already there takes the second step alone),
    with the Declaration pre-framed by the caller. `escalated_from` is the typed link back
    to a child's difficulty, stamped on the opened record before it is saved. Returns the
    opened difficulty, or None when the difficulty cap refused it; the state is saved
    either way and each caller builds its own Directive."""
    stage.outcome.status = StageStatus.FAILED.value
    stage.outcome.actual = (stage.outcome.actual + "\n" + note) if stage.outcome.actual else note
    state.log(event, stage=stage.index, **event_fields)
    if state.node == Node.EXECUTING.value:
        state.node = transition(state.node, "verify")  # EXECUTING -> VERIFYING
    state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
    opened = _open_difficulty(state, declaration=declaration)
    if opened is not None and escalated_from:
        opened.escalated_from = dict(escalated_from)
    store.save(state)
    return opened


def _diagnose_materialization_defect(
    state: SessionState, store: StateStore, stage: Stage, *, action: str, evidence: str,
    data: dict | None = None,
) -> Directive:
    """Route a PERMISSION-REQUEST whose denied call WAS covered by the stage's
    effective grant set into FAILED/DIAGNOSING -- the grant existed, materialization
    into the child's --settings/--add-dir simply failed to carry it, so re-asking the
    user (or re-dispatching unchanged) would only reproduce the same denial. The
    Declaration is pre-framed from the grant/call/evidence triple instead of being left
    for a human to write from scratch. The caller is responsible for having already
    appended the triggering row to `state.materialization_defects` -- this function only
    transitions and reports."""
    opened = _fail_stage_into_diagnosing(
        state, store, stage, event="materialization_defect", action=action, evidence=evidence,
        note=f"permission request already covered by materialized grants ({evidence}): {action}",
        declaration=Declaration(
            expected=f"stage {stage.index}'s materialized grants should have covered: {action}",
            actual=f"the child was denied and asked permission for: {action}",
            mismatch="the effective grant set declares/derives coverage for this call, but "
                     "the child's own --settings/--add-dir materialization failed to carry "
                     "it through -- a materialization defect, not a planning miss.",
        ))
    if opened is None:
        return _difficulty_cap_directive(
            state, f"stage {stage.index} permission request was already covered by its "
                   f"materialized grants ({evidence})")
    return Directive(
        False, state.node, "declare",
        f"stage {stage.index} permission request was already covered by its "
        f"materialized grants ({evidence}) -- this is a materialization defect, not "
        "a planning miss; run overcome-difficulty (investigate why materialization "
        "failed to carry the grant, then critique); no re-dispatch, no user ask",
        marker="OVERCOME-DIFFICULTY", data=dict(data or {}),
    )


def _escalation_link(difficulty, parent_link: dict | None) -> dict:
    """The typed `escalated_to` link of `difficulty` to its session's parent: the parent
    session, its stage and the norm digest the `parent` norm target was taken against;
    empty when the record names no such target or the session has no parent. Shared by
    `critique`, which routes the fault, and `difficulty-close`, which moves a nested
    record's routing onto the record that resumes."""
    parent_norm = next((t for t in difficulty.norm_targets
                        if t.get("kind") == gates.NORM_TARGET_KIND_PARENT), None)
    if parent_norm is None or not parent_link:
        return {}
    return {"parent_session": parent_link["parent_session"],
            "stage_index": parent_link["stage_index"],
            "norm_digest": parent_norm["baseline"]}


def _child_norm_mismatch(
    state: SessionState, store: StateStore, stage: Stage, stderr: str,
) -> tuple[dict | None, str]:
    """`(record, "")` when the child this dispatch just ran holds, in its own state, a
    live difficulty routed to this stage's norm; else `(None, reason)`.

    The decision rests on the child's state record -- never on the REPLAN text -- and holds
    only when all of these do: the transcript path the launch reported names the child's
    session id (its stem) and that state loads; the child's handoff names this session and
    this stage; one of its live difficulties -- the open one, or a suspended ancestor the
    child opened a new difficulty over after routing it -- carries an `escalated_to` link
    and a `parent` norm target whose baseline is the stage's current norm digest (a replan
    of the stage since the spawn moves it, and the child's baseline is then stale); and its
    declaration states all three of expected, actual and mismatch. The child's state is
    only read, never written.

    A rejection is `(None, reason)`, and the caller logs the reason. `(None, "")` is a
    spawn with nothing to report -- no transcript, no readable state record, or a record
    that holds no parent link -- whose REPLAN is the ordinary one and writes no row."""
    if state.node != Node.EXECUTING.value:
        return None, ""
    path = _parse_transcript_path(stderr)
    if path is None:
        return None, ""
    child_id = Path(path).stem
    if not child_id or child_id == state.session_id:
        return None, ""
    try:
        child = store.load(child_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, f"child state {child_id} is unreadable: {exc}"
    if child is None or not child.parent_link:
        return None, ""
    return _verify_child_escalation(state, stage, child_id, child)


def _verify_child_escalation(
    state: SessionState, stage: Stage, child_id: str, child: SessionState,
) -> tuple[dict | None, str]:
    """`(record, "")` when one of `child`'s live difficulty records (open, or suspended under
    a nested one) carries an `escalated_to` link and a current `parent` norm target on
    `stage`'s norm of this session; else `(None, reason)`. When several records qualify, one
    this session has not adopted is preferred, so `adopt-escalation` reaches each in turn
    instead of stopping at an already adopted one. The checks `_child_norm_mismatch`
    documents, minus the transcript lookup: `adopt-escalation` names the child directly."""
    link = child.parent_link or {}
    if link.get("parent_session") != state.session_id or link.get("stage_index") != stage.index:
        return None, (f"child {child_id} is not linked to stage {stage.index} of this session "
                      f"(parent_session={link.get('parent_session')!r}, "
                      f"stage_index={link.get('stage_index')!r})")
    found, reason = gates.child_escalations(stage, child)
    if not found:
        return None, f"child {child_id} {reason}"
    unadopted = [e for e in found
                 if gates.escalation_counterpart(state, e["child_uid"], child_id) is None]
    return (unadopted or found)[0], ""


def _open_norm_mismatch(
    state: SessionState, store: StateStore, stage: Stage, mismatch: dict, base: dict,
    via: str | None = None,
) -> Directive:
    """Route a REPLAN whose child record `_child_norm_mismatch` verified into FAILED /
    DIAGNOSING on this stage, with the child's own declaration as the opened difficulty:
    the fault is in the stage's norm, so re-dispatching it unchanged would only repeat it.
    Same transition as `_diagnose_materialization_defect`. The opened record carries
    `escalated_from`, the typed link to the child's difficulty. `via` names the verb when
    it is not dispatch's own REPLAN route."""
    decl = mismatch["declaration"]
    opened = _fail_stage_into_diagnosing(
        state, store, stage, event="norm_mismatch", child_session=mismatch["child_session"],
        norm_digest=mismatch["norm_digest"], declaration=Declaration(**decl),
        escalated_from={"child_session": mismatch["child_session"],
                        "child_uid": mismatch["child_uid"],
                        "norm_digest": mismatch["norm_digest"]},
        **({"via": via} if via else {}),
        note=(f"the child (session {mismatch['child_session']}) reported a mismatch in this "
              f"stage's norm: {decl['mismatch']}"))
    if opened is None:
        return _difficulty_cap_directive(
            state, f"stage {stage.index}'s child reported a mismatch in the stage's norm",
            data={"norm_mismatch": mismatch})
    return Directive(
        False, state.node, "investigate",
        f"stage {stage.index}'s child session recorded a difficulty addressed to this "
        "stage's norm (verified on its state record); its declaration is the opened "
        "difficulty -- run overcome-difficulty (investigate, then critique the stage's "
        "norm); no re-dispatch of the stage unchanged",
        marker="OVERCOME-DIFFICULTY",
        data={**base, "norm_mismatch": mismatch},
    )


def _repeat_norm_mismatch(
    state: SessionState, store: StateStore, stage: Stage, mismatch: dict, base: dict,
    standing: str, reason: str,
) -> Directive:
    """What a REPLAN gets when this session already holds a difficulty for the very child
    difficulty it reports (`standing` is that difficulty's `open` or `closed` state): no
    second record is opened, since the child cannot close its side and would otherwise
    mint a new parent difficulty at every dispatch. The open one's directive is the one
    `_open_norm_mismatch` gave; a closed one leaves the REPLAN's plain directive and says
    why re-dispatching without a new fault would repeat it."""
    ref = {"child_session": mismatch["child_session"], "child_uid": mismatch["child_uid"],
           "stage_index": stage.index, "status": standing}
    state.log("norm_mismatch_repeat", stage=stage.index, **ref)
    store.save(state)
    if standing == gates.ESCALATION_OPEN:
        return Directive(
            False, state.node, "investigate",
            f"stage {stage.index}'s child reported the difficulty this session already "
            "holds open for it -- continue overcome-difficulty on that record; no second "
            "difficulty is opened",
            marker="OVERCOME-DIFFICULTY",
            data={**base, "norm_mismatch": mismatch, "escalation": ref},
        )
    return Directive(
        False, state.node, "replan",
        f"stage {stage.index}'s child repeats an escalation this session already adopted "
        "and closed; the child's escalated difficulty is the one the closed record already "
        "addressed, so no new difficulty is opened -- the stage's norm is what that record "
        "changed",
        marker="REPLAN", data={**base, "reason": reason, "escalation": ref},
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
    return _evidence_session_dir(session_id, f"stage-{int(stage_index)}")


def _evidence_session_dir(session_id: str, leaf: str) -> Path:
    """`<evidence root>/<session>/<leaf>/` under the root and refusals evidence_dir_for
    documents; shared with the verify-final failure logs."""
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
    path = root / _safe_session_id(session_id) / leaf
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


def _cost_rows(args) -> list[dict]:
    """Read the cost log rows for this command, honoring the ``--cost-log`` test
    override (default: cost.COST_LOG) — the shared read behind every fire site's
    and cmd_resolve's cost-log lookup."""
    log = getattr(args, "cost_log", None)
    return cost.read_rows(Path(log) if log else cost.COST_LOG)


def _parent_session_of(store: StateStore, session: str, loaded: dict) -> str | None:
    """The `parent_link` parent of `session`, or None when it has no link or its state
    cannot be loaded. The state is kept in `loaded` (session -> state or None), so a scan
    loads each stored session once."""
    if session not in loaded:
        try:
            loaded[session] = store.load(session)
        except Exception:
            loaded[session] = None
    state = loaded[session]
    link = state.parent_link if state is not None else None
    return (link.get("parent_session") or None) if isinstance(link, dict) else None


def _link_chain(
    store: StateStore, session: str, parent: str, loaded: dict, max_hops: int,
) -> list[SessionState] | None:
    """The sessions from `session` up to, but excluding, `parent`, following `parent_link`
    one hop at a time; None when no chain of at most `max_hops` hops reaches `parent`.
    A visited set stops a cyclic link; a state that cannot be loaded ends the walk."""
    chain: list[SessionState] = []
    visited = {session}
    cursor = session
    for _ in range(max_hops):
        up = _parent_session_of(store, cursor, loaded)
        if up is None:
            return None
        chain.append(loaded[cursor])
        if up == parent:
            return chain
        if up in visited:
            return None
        visited.add(up)
        cursor = up
    return None


def _descendant_sessions(store: StateStore, parent: str, candidates) -> dict[str, list[SessionState]]:
    """D(`parent`) among `candidates`: session -> its link chain (the session first, up to but
    excluding `parent`) for every candidate whose `parent_link` chain reaches `parent`,
    directly or through other descendants, in sorted session order. Never contains `parent`.
    Read-only."""
    loaded: dict = {}
    chains = {}
    max_hops = Thresholds().max_recursion_depth
    for session in sorted(set(candidates)):
        chain = _link_chain(store, session, parent, loaded, max_hops) if session != parent else None
        if chain is not None:
            chains[session] = chain
    return chains


def _refresh_spend(state: SessionState, store: StateStore, rows: list[dict], path: str | None) -> float:
    """`effort.refresh_spend` over the ledger rows of this session AND of its linked
    descendants (effort.refresh_subtree_spend). The descendant scan runs only when a row
    names a spawner session other than this one, so a session whose ledger rows carry no
    foreign `spawner_session` returns exactly the base value.

    The part of what is booked here for a descendant that the order window already holds
    is marked flushed in this session too (`_credit_descendant_order_flush`)."""
    foreign = {
        spawner for row in rows
        if isinstance(spawner := row.get("spawner_session"), str) and spawner != state.session_id
    }
    chains = _descendant_sessions(store, state.session_id, foreign)
    delta, booked = effort.refresh_subtree_spend(state, rows, chains.keys(), path)
    if booked:
        key = _ledgered_order_key(state)
        if key is not None:
            _seed_linked_flush_baseline(state, store=store)
            if state.order_effort_flushed is not None:
                _credit_descendant_order_flush(state, key, chains, booked)
    return delta


def _flushed_spend_of_order(member: SessionState, key: str, order_keys: dict) -> float | None:
    """What `member` has flushed (or been credited) into the window of order `key`; None
    when it is not a flushing session of that order. `order_keys` caches
    `_ledgered_order_key` per session (it reads the plan file)."""
    if member.order_effort_flushed is None:
        return None
    if member.session_id not in order_keys:
        order_keys[member.session_id] = _ledgered_order_key(member)
    if order_keys[member.session_id] != key:
        return None
    return float(member.order_effort_flushed.get(effort.SCALE_SPEND) or 0.0)


def _credit_descendant_order_flush(
    state: SessionState, key: str, chains: dict[str, list[SessionState]],
    booked: dict[str, tuple[float, float]],
) -> None:
    """Mark as flushed in `state` the part of each newly booked descendant spend that the
    window of the shared order `key` already holds, so `state`'s flush does not add it again.

    A session's `order_effort_flushed` covers its own ledger rows PLUS the rows its own
    refresh booked for its descendants, so the share a descendant covers of its OWN rows
    is its flushed total less those subtree rows (attributed first; floor 0) — assuming
    every row it books by plan path was spawned by it. A linked session's first flush
    moves what it spent so far (`_seed_linked_flush_baseline`), so its flushed total is
    what the window holds, never merely a watermark. The window
    also holds a grand-descendant's rows that an intermediate session of the same order
    carried: the intermediate's flushed total, up to its subtree rows, split over its
    descendants in proportion to what it booked for each. The window holds the larger
    of the two once, whichever session put it there. What neither covers — a descendant
    that never flushed, rows on a path its own refresh never books — is left to
    `state`'s flush. `booked` carries `(earlier, gain)` per descendant; the credit is
    what the window holds of that descendant beyond what `state` already credited for
    it (`order_descendant_credited`), capped by the newly booked `gain` — the order in
    which `state` booked the descendant's rows does not matter."""
    order_keys: dict = {}
    for session, (_, gain) in booked.items():
        chain = chains[session]
        covered = 0.0
        flushed = _flushed_spend_of_order(chain[0], key, order_keys)
        if flushed is not None:
            covered = max(0.0, flushed - sum(chain[0].effort_subtree_seen.values()))
        for member in chain[1:]:
            flushed = _flushed_spend_of_order(member, key, order_keys)
            booked_by_member = sum(member.effort_subtree_seen.values())
            if flushed is None or booked_by_member <= 0:
                continue
            carried = member.effort_subtree_seen.get(session, 0.0)
            covered = max(covered, carried * min(flushed, booked_by_member) / booked_by_member)
        credited = float(state.order_descendant_credited.get(session) or 0.0)
        credit = min(gain, max(0.0, covered - credited))
        if not credit:
            continue
        state.order_descendant_credited[session] = credited + credit
        state.order_effort_flushed[effort.SCALE_SPEND] = (
            float(state.order_effort_flushed.get(effort.SCALE_SPEND) or 0.0) + credit
        )


def _utcnow() -> float:
    """The one clock read behind every `effort.record_fire(..., now=...)` call site —
    collapses five copy-pasted `dt.datetime.now(dt.timezone.utc).timestamp()` calls."""
    return dt.datetime.now(dt.timezone.utc).timestamp()


def _state_dir(store: StateStore) -> Path | None:
    root = getattr(store, "root", None)
    return Path(root) if root is not None else None


def _effort_now(store: StateStore, state: SessionState) -> float:
    """The `now` for an effort read: runs of a dead dispatcher closed first (in memory; the
    next save keeps it, and the prompt hook closes the same run the same way), then the
    clock. A store with no state directory cannot attribute a child, so its runs stay as
    recorded."""
    state_dir = _state_dir(store)
    if state_dir is not None:
        child_runs.close_dead_runs(state.effort_child_runs, state_dir, state.session_id)
    return _utcnow()


def _spawn_would_link() -> bool:
    """Whether the spawn `cmd_dispatch` is about to make can be linked at all — the kill
    switch spawn-specialist consults. A dispatch that cannot link records no run: during the
    blocking spawn the prompt hook would otherwise leave the gap to a run that is later
    dropped."""
    return agentctl_config.child_contour_linking_enabled()


def _begin_child_run(store: StateStore, state: SessionState, stage) -> dict | None:
    """Record the dispatch as an open child run and persist it BEFORE the blocking spawn,
    so a hook or a second reader sees the interval while the child is still running. Runs a
    dead dispatcher left open are closed first: the new run's child would otherwise be
    attributed to the oldest of them. None when the spawn cannot be linked: nothing is
    recorded."""
    if not _spawn_would_link():
        return None
    state_dir = _state_dir(store)
    if state_dir is not None:
        child_runs.close_dead_runs(state.effort_child_runs, state_dir, state.session_id)
    run = {
        "child_session": None, "stage_index": stage.index, "started": _utcnow(),
        "ended": None, "dispatcher_pid": os.getpid(),
    }
    state.effort_child_runs.append(run)
    store.save(state)
    return run


def _carry_closed_runs(state: SessionState, fresh: SessionState, own: dict) -> None:
    """Take over, for each run other than `own`, the close the hook or a reader saved into
    the state file while the spawn ran. This process's copy predates it, and saving that
    copy would reopen the run — letting a flush in between lower its watermark and the
    re-close book the interval twice."""
    saved = {(r.get("started"), r.get("dispatcher_pid"), r.get("stage_index")): r
             for r in fresh.effort_child_runs if isinstance(r, dict)}
    for run in state.effort_child_runs:
        if run is own or run.get("ended") is not None:
            continue
        closed = saved.get((run.get("started"), run.get("dispatcher_pid"), run.get("stage_index")))
        if closed is not None and closed.get("ended") is not None:
            run["ended"] = closed["ended"]
            run["child_session"] = closed.get("child_session")


def _carry_hook_stamps(store: StateStore, state: SessionState) -> SessionState | None:
    """Take over the keys the prompt hook owns (active minutes, last prompt, prompt count)
    from the state file, which a prompt may have stamped while the blocking spawn ran — the
    spawn's `state` predates them and saving it as is would overwrite them. Returns the
    state read, None when it cannot be (nothing is carried; never an error)."""
    try:
        fresh = store.load(state.session_id)
    except Exception:
        return None
    if effort.ACTUAL_MINUTES_KEY in fresh.effort_actuals:
        state.effort_actuals[effort.ACTUAL_MINUTES_KEY] = fresh.effort_actuals[effort.ACTUAL_MINUTES_KEY]
    state.last_user_prompt_ts = fresh.last_user_prompt_ts
    state.user_prompt_count = fresh.user_prompt_count
    return fresh


def _end_child_run(store: StateStore, state: SessionState, run: dict,
                   reported_session: str | None = None) -> None:
    """Close `run` after the spawn returned or raised, and persist. The prompt stamps the
    hook wrote into the state file during the run (active minutes, last prompt, prompt
    count) are carried over first, because this process's `state` predates them; when that
    state file cannot be read they are skipped, never allowed to fail the close — this runs
    from a `finally`, and must neither mask the spawn's own exception nor lose the marker of
    a child that finished. The run's child is the session the spawn reported if it is a
    child of this stage, else the earliest unclaimed one found in the state directory. A
    spawn that made no linked child was unlinked: its run is dropped."""
    fresh = _carry_hook_stamps(store, state)
    if fresh is not None:
        _carry_closed_runs(state, fresh, run)
    state_dir = _state_dir(store)
    if state_dir is not None and not run.get("child_session"):
        taken = {r.get("child_session") for r in state.effort_child_runs if r is not run}
        if (reported_session and reported_session not in taken and child_runs.is_child_of(
                state_dir, state.session_id, reported_session, run.get("stage_index"))):
            run["child_session"] = reported_session
        else:
            run["child_session"] = child_runs.discover_child(
                state_dir, state.session_id, run, state.effort_child_runs)
    if run.get("child_session"):
        run["ended"] = max(run["started"], _utcnow())
    else:
        state.effort_child_runs[:] = [r for r in state.effort_child_runs if r is not run]
        # The hook left the part of each prompt gap inside this run to it (and noted it on
        # the run in the file); the run is gone, so book it as the hook would have.
        lost = child_runs.gap_loss_min(fresh.effort_child_runs if fresh is not None else (), run)
        if lost:
            actuals = state.effort_actuals
            actuals[effort.ACTUAL_MINUTES_KEY] = float(
                actuals.get(effort.ACTUAL_MINUTES_KEY) or 0.0) + lost
    store.save(state)


@dataclass
class _FinalEntry:
    """One check of a verify-final run, from planning to its recorded result."""
    check_id: str
    venue: str
    label: str
    spec: check_cache.CheckSpec | None = None
    cwd: str | None = None
    fail_tail: str = ""
    landed_failure: str = ""
    decision: str = ""
    reason: str = ""
    duration_s: float = 0.0
    exit_code: int | None = None
    ok: bool = True
    error: str = ""
    log_note: str = ""

    def failure(self) -> str:
        if self.spec is None:
            return self.landed_failure
        tail = f" {self.fail_tail}" if self.fail_tail else ""
        if self.error:
            return f"{self.label}: raised {self.error}{tail}"
        return f"{self.label}: exit {self.exit_code} != {self.spec.expected_exit}{tail}{self.log_note}"


FAIL_LOG_TAIL_LINES = 20


FAIL_LOG_LINE_CHARS = 300


def _output_tail(stdout: str, stderr: str) -> str:
    """Last lines of stderr, then of stdout — stdout last, so pytest's FAILED summary
    is not pushed out by a noisy stderr."""
    lines = stderr.splitlines()[-FAIL_LOG_TAIL_LINES:] + stdout.splitlines()[-FAIL_LOG_TAIL_LINES:]
    return "".join(f"\n    {line[:FAIL_LOG_LINE_CHARS]}" for line in lines)


def _write_check_log(
    session_id: str, leaf: str, name: str, command: str, venue: str, exit_code: int,
    stdout: str, stderr: str,
) -> str:
    """Save a failed check's full output to `<evidence>/<session>/<leaf>/<name>.log` and
    return the note its failure text ends with."""
    try:
        log_dir = _evidence_session_dir(session_id, leaf)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}.log"
        path.write_text(
            f"command: {command}\nvenue: {venue}\nexit: {exit_code}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
            encoding="utf-8",
        )
    except (OSError, EvidenceDirError) as exc:
        return f"; log not written: {exc}"
    return f"; log: {path}{_output_tail(stdout, stderr)}"


def _write_fail_log(session_id: str, e: _FinalEntry, stdout: str, stderr: str) -> str:
    return _write_check_log(
        session_id, "verify-final", e.check_id.replace(":", "-"),
        e.spec.command, e.venue, e.exit_code, stdout, stderr,
    )


def _log_final_events(
    state: SessionState, entries: list[_FinalEntry], *, outcome: str, started: float, peak: int,
) -> None:
    counts = {check_cache.HIT: 0, check_cache.NARROWED: 0, check_cache.DEDUP: 0, check_cache.MISS: 0}
    for e in entries:
        if not e.decision:
            continue
        counts[e.decision] = counts.get(e.decision, 0) + 1
        state.log(
            "verify_final_check", check=e.check_id, venue=e.venue, decision=e.decision,
            reason=e.reason, duration_s=round(e.duration_s, 3), exit=e.exit_code,
        )
    state.log(
        "verify_final_run", outcome=outcome, duration_s=round(time.monotonic() - started, 3),
        counts=counts, parallelism=peak,
    )


def _evaluate_final_checks(
    state: SessionState, store: StateStore, runner: Runner | None,
    div: effort.Divergence | None,
) -> tuple[list[str], Directive | None]:
    """Evaluate every measurable stage's verify_command, then every final_check, in
    three phases. Plan: resolve venues, run landed checks, take each venue's tree
    identity once, and decide per check (check_cache.decide) whether a cache serves it,
    an earlier check of this run covers it, or it must run; a check that declared
    `verify_reads` and missed the exact cache gets a narrowed lookup
    (check_cache.narrow_misses). Run: the remaining checks, concurrently under the cap
    and worker budget when the real runner is in use. Record: results in check order,
    greens into the session cache, the verify_final_check / verify_final_run events.

    Returns (failures, None), or ([], Directive) when a venue is refused — the
    caller returns that directive as is."""
    snapshots: dict[str, _VenueSnapshot] = {}
    try:
        return _evaluate_final_checks_in(state, store, runner, div, snapshots)
    finally:
        for snapshot in snapshots.values():
            snapshot.close()


def _evaluate_final_checks_in(
    state: SessionState, store: StateStore, runner: Runner | None,
    div: effort.Divergence | None, snapshots: dict[str, _VenueSnapshot],
) -> tuple[list[str], Directive | None]:
    started = time.monotonic()
    entries: list[_FinalEntry] = []
    tree_ids: dict[str, str | None] = {}

    def tree_of(cwd: str | None) -> tuple[str | None, str | None]:
        """(tree identity, raw tree sha) of a venue, computed once per venue directory."""
        venue = cwd or str(REPO_ROOT)
        if venue not in tree_ids:
            snapshots[venue] = _VenueSnapshot(keep_objects=True)
            tree_ids[venue] = _venue_tree_identity(cwd, runner, snapshots[venue])
        return tree_ids[venue], snapshots[venue].tree

    def refuse(check_id: str, cwd: str | None, message: str) -> tuple[list[str], Directive]:
        entries.append(_FinalEntry(
            check_id, cwd or str(REPO_ROOT), "", decision=check_cache.MISS, reason="refused",
        ))
        _log_final_events(state, entries, outcome="refused", started=started, peak=0)
        return [], _diagnose_venue_refusal(state, store, message, div)

    def landed_entry(check_id: str, spec, failure: str) -> tuple[_FinalEntry | None, str | None]:
        # No re-freeze for a stage's landed check: verify-final re-checks the
        # delivered_head its own record-result already froze, never a live re-derive.
        t0 = time.monotonic()
        ok, refusal, result = _landed_check_result(state, spec, runner)
        if refusal:
            return None, refusal
        return _FinalEntry(
            check_id, str(REPO_ROOT), "", decision=check_cache.MISS, reason="landed",
            duration_s=time.monotonic() - t0, exit_code=result.returncode, ok=ok,
            landed_failure=failure,
        ), None

    stage_commands = {
        s.criterion.verify_command for s in state.stages
        if not is_skipped(s) and plan_resources.runs_verify_command(s.criterion)
    }
    for stage in state.stages:
        if is_skipped(stage):
            continue
        crit = stage.criterion
        check_id = f"stage:{stage.index}"
        if crit.criterion_type == CriterionType.MEASURABLE.value and crit.verify_kind == CheckKind.LANDED.value:
            entry, refusal = landed_entry(check_id, crit.landed, (
                f"stage {stage.index}: landed check — delivered commit not "
                f"contained in {crit.landed.target!r} (or "
                f"{crit.landed.remote}/{crit.landed.target})"
            ))
            if refusal:
                return refuse(check_id, None, f"stage {stage.index} landed check refused: {refusal}")
            entries.append(entry)
            continue
        if not plan_resources.runs_verify_command(crit):
            continue
        cwd, refusal = _resolve_final_or_refuse(state, crit)
        if refusal:
            return refuse(check_id, cwd, f"stage {stage.index} verify_command refused: {refusal}")
        tree_id, tree_sha = tree_of(cwd)
        definition = _check_definition_digest(crit)
        identity = f"{tree_id}:{definition}" if tree_id is not None else None
        outcome = stage.outcome
        same_venue = cwd == state.resolve_check_venue(crit.verify_venue)
        outcome_base = (
            outcome.checked_tree_sha
            if outcome.checked_tree_ok is True and same_venue
            and (outcome.checked_tree_identity or "").endswith(f":{definition}")
            else None
        )
        entries.append(_FinalEntry(
            check_id, cwd or str(REPO_ROOT), f"stage {stage.index}", cwd=cwd,
            fail_tail=f"({crit.verify_command})",
            spec=check_cache.CheckSpec(
                check_id, crit.verify_command, crit.expected_exit, cwd or str(REPO_ROOT), tree_id,
                cacheable=True, outcome_hit=_cached_check_hit(stage, identity),
                reads=crit.verify_reads, tree_sha=tree_sha, outcome_base_tree=outcome_base,
            ),
        ))
    for number, fc in enumerate(state.final_check, 1):
        check_id = f"final_check:{number}"
        if fc.kind == CheckKind.LANDED.value:
            label = fc.label or fc.landed.target
            entry, refusal = landed_entry(check_id, fc.landed, (
                f"final_check '{label}': landed check — delivered commit not "
                f"contained in {fc.landed.target!r} (or "
                f"{fc.landed.remote}/{fc.landed.target})"
            ))
            if refusal:
                return refuse(check_id, None, f"final_check '{label}' refused: {refusal}")
            entries.append(entry)
            continue
        cwd, refusal = _resolve_or_refuse(state, fc.venue)
        if refusal:
            return refuse(check_id, cwd, f"final_check '{fc.label or fc.command}' refused: {refusal}")
        tree_id, tree_sha = tree_of(cwd)
        entries.append(_FinalEntry(
            check_id, cwd or str(REPO_ROOT), f"final_check '{fc.label or fc.command}'", cwd=cwd,
            spec=check_cache.CheckSpec(
                check_id, fc.command, fc.expected_exit, cwd or str(REPO_ROOT), tree_id,
                cacheable=fc.cacheable or fc.command in stage_commands,
                reads=fc.verify_reads, tree_sha=tree_sha,
            ),
        ))

    def probe_for(spec: check_cache.CheckSpec) -> check_cache.TreeProbe | None:
        snapshot = snapshots.get(spec.venue)
        if snapshot is None or snapshot.env_argv is None:
            return None
        return _GitTreeProbe(snapshot)

    decided = {d.spec.id: d for d in check_cache.narrow_misses(
        check_cache.decide([e.spec for e in entries if e.spec is not None], state.check_cache),
        state.check_cache, probe_for,
    )}
    to_run = [e for e in entries if e.spec is not None and decided[e.spec.id].decision == check_cache.MISS]

    def run_one(e: _FinalEntry):
        t0 = time.monotonic()
        ok, result = _run_check(e.spec.command, e.spec.expected_exit, runner, cwd=e.cwd)
        return ok, result.returncode, time.monotonic() - t0, "", result.stdout, result.stderr

    def raised(exc: Exception):
        return False, None, 0.0, f"{type(exc).__name__}: {exc}", "", ""

    cap, budget, cpus = 1, 0, 1
    if runner is None and len(to_run) > 1:
        cap = check_cache.resolve_cap(Thresholds().verify_final_parallel_max())
        if cap > 1:
            cpus, load1 = check_cache.system_probe()
            budget = check_cache.resolve_budget(cpus, load1)
    results, peak = check_cache.run_bounded(
        [check_cache.Job(check_cache.pytest_weight(e.spec.command, cpus), (lambda e=e: run_one(e))) for e in to_run],
        cap=cap, budget=budget, on_error=raised,
    )
    ran = {e.check_id: r for e, r in zip(to_run, results)}

    by_id = {e.check_id: e for e in entries}
    now = time.time()
    for e in entries:
        if e.spec is None:
            continue
        d = decided[e.spec.id]
        e.decision, e.reason = d.decision, d.reason
        if d.decision == check_cache.MISS:
            e.ok, e.exit_code, e.duration_s, e.error, stdout, stderr = ran[e.check_id]
            if e.error:
                e.reason = f"raised: {e.error}"
            elif not e.ok and (stdout or stderr):
                e.log_note = _write_fail_log(state.session_id, e, stdout, stderr)
            if e.ok and e.spec.key is not None and e.spec.cacheable:
                check_cache.store_green(
                    state.check_cache, e.spec.key, at=now, label=e.label,
                    tree=e.spec.tree_sha, definition=e.spec.definition,
                )
        elif d.decision == check_cache.NARROWED:
            e.exit_code = None
            check_cache.store_green(
                state.check_cache, e.spec.key, at=now, label=e.label,
                tree=e.spec.tree_sha, definition=e.spec.definition, derived=True,
                reads=e.spec.reads,
            )
        elif d.decision == check_cache.HIT:
            e.exit_code = None
        else:
            earlier = by_id[d.same_as]
            if d.same_as in ran:
                e.ok, e.exit_code, _, e.error, _, _ = ran[d.same_as]
            else:
                e.ok, e.exit_code, e.error = earlier.ok, earlier.exit_code, earlier.error
            e.log_note = earlier.log_note
    failures = [e.failure() for e in entries if not e.ok]
    _log_final_events(
        state, entries, outcome="failed" if failures else "passed", started=started, peak=peak,
    )
    return failures, None


def _first_git_landed_remote_target(state: SessionState) -> tuple[str, str]:
    """(remote, target) of the first landed check whose provider is git — a stage's own
    criterion first, then the final_checks — else the repository default origin/main."""
    for spec in [s.criterion.landed for s in state.stages if s.criterion.landed] + [
        fc.landed for fc in state.final_check if fc.landed
    ]:
        if spec.provider == "git":
            return spec.remote, spec.target
    return "origin", "main"


def _unlanded_branch_blockers(state: SessionState, runner: Runner | None) -> list[str]:
    """Blockers when the plan's delivery worktree still holds commits trunk lacks.

    Network-free (compares against the local remote-tracking ref) and run only through
    the injected Runner. A delivery worktree that is gone — what land-branch.py leaves
    after landing — or is not a git work tree yields nothing: the check is git-only.
    Containment is `state.landed_containment_script`'s, the same ancestry / cherry /
    squash-patch-id test the landed check applies, so resolve cannot call landed what the
    landed check calls unlanded. A probe error blocks (fail closed); the plan-time
    landing_waiver is the escape for a plan that deliberately lands nothing."""
    worktree = state.delivery_worktree
    if not worktree or not Path(worktree).is_dir():
        return []
    run = runner or subprocess_runner
    inside = run(["git", "-C", worktree, "rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return []
    remote, target = _first_git_landed_remote_target(state)
    ref = f"{remote}/{target}"
    git = f"git -C {shlex.quote(worktree)}"
    err = LANDED_GIT_ERROR_EXIT
    script = landed_containment_script(
        git, "HEAD", shlex.quote(ref), f'$({git} merge-base "$R" HEAD) || exit {err}', err
    )
    probe = run(["bash", "-c", script])
    if probe.returncode == 0:
        return []
    branch = run(["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"])
    name = branch.stdout.strip() if branch.returncode == 0 and branch.stdout.strip() else "HEAD"
    if probe.returncode == 1:
        return [
            f"delivery branch {name!r} in {worktree} holds commits not on {ref} — land it "
            f"first (`python3 scripts/land-branch.py`), then resolve; a plan that "
            f"deliberately lands nothing declares [meta] landing_waiver via replan"
        ]
    return [
        f"cannot tell whether delivery branch {name!r} in {worktree} has landed on {ref} "
        f"(git exited {probe.returncode}: {probe.stderr.strip()[:200] or 'no message'}) — "
        f"fix the repository state, or declare [meta] landing_waiver via replan"
    ]


def _landing_gate(state: SessionState, args, runner: Runner | None) -> tuple[list[str], dict]:
    """The resolve-time landing blockers plus the record of how the gate was met.

    The record is {waiver, waiver_source, override}: the waiver text and where it came
    from ('plan' = [meta] landing_waiver, 'resolve' = --landing-waiver), and the
    AGENTCTL_LANDING_GATE value when it overrode the gate — so a bypass is never silent.
    A resolve-time waiver is accepted only when the plan has no git delivery venue: for a
    delivery_worktree plan the waiver must come from the approved plan, because an actor
    writing it after the outcome is known can excuse anything."""
    env = os.environ.get("AGENTCTL_LANDING_GATE")
    record = {"waiver": None, "waiver_source": None, "override": env if env in ("0", "1") else None}
    if not gates.landing_gate_active(state):
        return [], record
    blockers: list[str] = []
    plan_waiver = (state.landing_waiver or "").strip()
    if plan_waiver:
        record["waiver"], record["waiver_source"] = plan_waiver, "plan"
    elif not gates.plan_asserts_landing(state):
        if state.delivery_worktree:
            blockers.append(
                "plan declares neither a kind=landed check nor [meta] landing_waiver — "
                "declare one via replan (a resolve-time --landing-waiver is refused for a "
                "plan with a delivery_worktree)"
            )
        else:
            given = getattr(args, "landing_waiver", None)
            if given is not None and given.strip():
                record["waiver"], record["waiver_source"] = given.strip(), "resolve"
            else:
                blockers.append(
                    "plan declares no kind=landed check: "
                    + ("--landing-waiver must be non-empty" if given is not None else
                       "resolve needs --landing-waiver '<why nothing lands>'")
                )
    if not plan_waiver:
        blockers.extend(_unlanded_branch_blockers(state, runner))
    return blockers, record


RESOLUTION_ASK_BLOCKER_PREFIX = "resolution ask: "


RESOLUTION_ASK_RULE_HINT = ("the marker must be inside an option label or description (not the question text), "
                            "and the order must be land -> verify-final -> ask -> resolve with no "
                            "verify-final between the ask and resolve")


def _resolution_ask_gate(state: SessionState, args, *, channel_ok: bool = True) -> tuple[list[str], dict]:
    """The resolve-time resolution-ask blockers plus the record of how the gate was met.

    The 1-5 rating must be asked inside an AskUserQuestion that embeds
    RESOLUTION_ASK_MARKER, was asked AFTER the session's latest verify-final stamp, and
    was answered. Both ends are machine records — the engine's `verify_final` event and
    the harness transcript — so the order is compared, not trusted to prose. The
    transcript is derived from the session id, never supplied. The record is
    {override, unverifiable}: the AGENTCTL_RESOLUTION_ASK_GATE value when it overrode
    the gate, and the reason given when --resolution-ask-unverifiable stood in for an
    unreadable transcript — so a bypass is never silent. `channel_ok` is False while
    another resolve blocker stands, so the headless channel is asked only about a
    resolve that can otherwise succeed."""
    env = os.environ.get("AGENTCTL_RESOLUTION_ASK_GATE")
    record = {"override": env if env in ("0", "1") else None, "unverifiable": None}
    if not gates.resolution_ask_gate_active(state):
        return [], record
    prefix = RESOLUTION_ASK_BLOCKER_PREFIX
    stamps = [h for h in state.history if h.get("event") == "verify_final"]
    verified_at = stamps[-1].get("at") if stamps else None
    if not isinstance(verified_at, (int, float)):
        return [prefix + "no precise verify-final stamp is recorded for this session — "
                         "re-run verify-final, then ask the resolution question; "
                         + RESOLUTION_ASK_RULE_HINT], record
    channel_text = (f"{RESOLUTION_ASK_MARKER} Resolve task {state.task_id!r} with quality "
                    f"{getattr(args, 'quality', None)}?\n").encode("utf-8")
    channel_result = delivery_channel.deliver("resolution_ask", channel_text) if channel_ok else None
    if channel_result is not None:
        delivery_channel.log_delivery(state, channel_result, plan_sha256=None)
        if channel_result.answered:
            return [], record
        return [prefix + "the resolution question was delivered through the headless "
                         "channel but it gave no `confirm` answer"], record
    path = _present_plan_transcript(state.session_id)
    scan = ask_evidence.ask_after_event(path, RESOLUTION_ASK_MARKER, verified_at)
    given = getattr(args, "resolution_ask_unverifiable", None)
    if scan is None:
        reason = (given or "").strip()
        if reason:
            record["unverifiable"] = reason
            return [], record
        return [prefix + f"cannot locate or read the transcript of session {state.session_id!r}, "
                         "so the resolution ask cannot be verified"
                         + (" — --resolution-ask-unverifiable must be non-empty" if given is not None
                            else " — pass --resolution-ask-unverifiable '<why the transcript is unavailable>'")], record
    marked = scan.marked
    escape_note = ("" if given is None else
                   " (--resolution-ask-unverifiable is refused: the transcript is readable)")
    if not marked:
        return [prefix + f"no AskUserQuestion carrying the marker {RESOLUTION_ASK_MARKER!r} in an "
                         "option label or description was found in this session's transcript — "
                         "ask the resolution question (with the 1-5 rating) after verify-final"
                         + escape_note], record
    after = [ask.answered for ask in scan.after]
    if not after and any(ask.ts is None for ask in marked):
        return [prefix + f"a {RESOLUTION_ASK_MARKER!r} ask has a missing or unparsable timestamp "
                         "in the transcript, so its order against verify-final cannot be "
                         "established — ask the resolution question again; "
                         + RESOLUTION_ASK_RULE_HINT + escape_note], record
    if not after:
        misplaced_after = [
            inp for inp, ts, _ in transcript_turns.ask_user_question_calls(path)
            if ts is not None and ts > verified_at
            and not transcript_turns.has_marker_option(inp, RESOLUTION_ASK_MARKER)
            and transcript_turns.has_marker_in_question_text(inp, RESOLUTION_ASK_MARKER)
        ]
        if misplaced_after:
            return [prefix + f"the {RESOLUTION_ASK_MARKER!r} marker appears in the question "
                             "text of an ask after verify-final, but it must be in an option "
                             "label or description — re-ask with the marker placed inside an "
                             "option; " + RESOLUTION_ASK_RULE_HINT + escape_note], record
        return [prefix + f"the {RESOLUTION_ASK_MARKER!r} ask predates the latest verify-final — "
                         "ask the resolution question again now that final verification has passed; "
                         + RESOLUTION_ASK_RULE_HINT + escape_note], record
    if not any(after):
        return [prefix + f"the {RESOLUTION_ASK_MARKER!r} ask after verify-final has not been "
                         "answered — resolve only after the user answers it" + escape_note], record
    return [], record


def _release_reflection_open(state: SessionState) -> bool:
    """True while a plan-review or cross-axis release is active at a node other than
    DIAGNOSING and RESOLVED: the reflection exit's difficulty commands run there, without
    the node moving, because the release is where the budget runs out -- not a failure."""
    return (state.node not in (Node.DIAGNOSING.value, Node.RESOLVED.value)
            and _plan_review_release_active(state))


def _require_diagnosing(state: SessionState, *, admit_release: bool = False) -> Directive | None:
    """`admit_release` lets the reflection exit's commands (declare, investigate,
    critique, normalize) run outside DIAGNOSING while a plan-review release is active."""
    if state.node != Node.DIAGNOSING.value and not (admit_release and _release_reflection_open(state)):
        return Directive(
            False, state.node, "noop",
            f"difficulty commands run only in the DIAGNOSING cycle; node={state.node}",
        )
    if state.difficulty is None and _open_difficulty(state) is None:
        return _difficulty_cap_directive(state, "a difficulty record cannot be opened")
    return None


def _supersede_for_release(state: SessionState) -> None:
    """Under a plan-review release a re-declared difficulty is a fresh record, never the
    earlier one's investigation, critique and normalization under a new declaration (its
    id would change while its content did not). The replaced record is logged whole."""
    old = state.difficulty
    if old is None or (old.declaration is None and old.investigation is None
                       and old.critique is None and old.normalization is None):
        return
    state.log("difficulty_superseded", uid=old.uid, record=asdict(old))
    state.difficulty = Difficulty(level=old.level, parent_uid=old.parent_uid, origin=old.origin)


# Test seam for `--norm-target` classification, mirroring exempt_paths.SCRATCH_ROOTS_ENV:
# os.pathsep-separated directories that, when set and non-empty, REPLACE the whole root
# set (the scratch exclusion included).
NORM_ROOTS_ENV = "AGENTCTL_NORM_ROOTS"


def _under_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _checkout_of(path: str) -> tuple[str, str, str] | None:
    """(top level, git dir, common dir), as realpaths, of the checkout holding `path`;
    None outside one."""
    start = path if os.path.isdir(path) else os.path.dirname(path)
    try:
        run = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel", "--absolute-git-dir",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = run.stdout.splitlines()
    if run.returncode != 0 or len(lines) != 3:
        return None
    top, git_dir, common = lines
    if not os.path.isabs(common):
        common = os.path.join(start, common)
    return os.path.realpath(top), os.path.realpath(git_dir), os.path.realpath(common)


def _is_memory_path(rp: str) -> bool:
    if "/memory-global/" in rp or "/agent-memory/" in rp:
        return True
    roots = {os.path.realpath(str(r)) for r in config_root.projects_roots()}
    roots.add(os.path.realpath(str(config_root.agent_home() / "projects")))
    for root in roots:
        if rp.startswith(root + os.sep):
            parts = rp[len(root) + 1:].split(os.sep)
            if len(parts) >= 3 and parts[0] and parts[1] == "memory":
                return True
    return False


def _session_plan_paths(state: SessionState) -> set[str]:
    """Every plan file this session has installed or submitted: a norm target must not be
    one, since its bytes move with the plan rather than with a norm edit."""
    paths = {state.plan_path, state.plan_snapshot_path,
             *(f.plan_path for f in state.plan_stack),
             *(e.get("plan") for e in state.history if isinstance(e, dict))}
    return {os.path.realpath(p) for p in paths if p and isinstance(p, str)}


def _norm_file_refusal(state: SessionState, target: str) -> str | None:
    """Why `target` cannot be a file norm target, or None when it can. Classified on the
    realpath, by the norm-bearing roots: a linked worktree of the instructions repo, a
    memory root, and the agent home's skills/ and plans/."""
    rp = os.path.realpath(target)
    if not os.path.isfile(rp):
        return f"--norm-target {target!r} is not an existing file"
    if rp in _session_plan_paths(state):
        return (f"--norm-target {target!r} is this session's own plan; name the stage "
                f"norm instead as stage:<n>")
    override = os.environ.get(NORM_ROOTS_ENV)
    if override:
        roots = [os.path.realpath(r) for r in override.split(os.pathsep) if r]
        if any(_under_root(rp, r) for r in roots):
            return None
        return f"--norm-target {target!r} is under none of the {NORM_ROOTS_ENV} roots"
    if any(_under_root(rp, os.path.realpath(r)) for r in exempt_paths._gate_scratch_roots()):
        return f"--norm-target {target!r} is scratch, not a norm-bearing file"
    own_repo = _checkout_of(str(REPO_ROOT))
    here = _checkout_of(rp)
    if own_repo is not None and here is not None and here[2] == own_repo[2]:
        if here[1] != here[2]:
            return None  # a linked worktree of the instructions repo
        if "/memory-global/" not in rp:
            return (f"--norm-target {target!r} is in the canonical instructions checkout, "
                    f"which is read-only to sessions; name the same file in a linked "
                    f"worktree")
    if _is_memory_path(rp):
        return None
    for root in (config_root.skills_dir(), config_root.plans_dir()):
        if _under_root(rp, os.path.realpath(str(root))):
            return None
    return (f"--norm-target {target!r} is not under a norm-bearing root (a linked worktree "
            f"of the instructions repo, a memory root, or the agent home's skills/ or "
            f"plans/)")


def _snapshot_norm_targets(state: SessionState, targets: list[str]) -> tuple[list[dict], str | None]:
    """The `norm_targets` entries for a normative critique's --norm-target values, or
    the first refusal. A stage target is snapshotted from the live state's stage -- the
    stage that actually ran, which refinement replans rebuild and `--renormalize` patches
    -- never from a plan file, which is editable while DIAGNOSING. A stage the state does
    not hold is refused, not read from the approved snapshot, which `--renormalize`
    leaves on the approved bytes.

    `parent` names the norm of the stage that spawned this session: it is snapshotted from
    the adopted handoff (`parent_link.norm_digest`), needs a linked session, and stands
    alone -- the child cannot edit that norm, it hands the fault back with REPLAN."""
    entries: list[dict] = []
    for raw in targets:
        raw = (raw or "").strip()
        if raw == gates.NORM_TARGET_PARENT:
            if len(targets) != 1:
                return [], (f"--norm-target {gates.NORM_TARGET_PARENT} stands alone: the norm of "
                            f"the parent stage is not this session's to edit, so a fault "
                            f"routed to it is handed back with REPLAN, not mixed with "
                            f"targets this session's own replan would change")
            link = state.parent_link
            if link is None or not link.get("norm_digest"):
                return [], (f"--norm-target {gates.NORM_TARGET_PARENT} names the norm of the "
                            f"stage that spawned this session; this session holds no "
                            f"norm handoff (it is not linked to a parent stage)")
            entries.append({"target": gates.NORM_TARGET_PARENT,
                            "kind": gates.NORM_TARGET_KIND_PARENT,
                            "baseline": link["norm_digest"], "source": "critique"})
            continue
        if raw.startswith("stage:"):
            try:
                index = int(raw[len("stage:"):])
            except ValueError:
                return [], f"--norm-target {raw!r}: stage:<n> needs an integer stage index"
            try:
                stage = state.stage(index)
            except KeyError:
                return [], f"--norm-target {raw!r}: the current plan has no stage {index}"
            entries.append({"target": f"stage:{index}", "kind": "stage",
                            "baseline": gates.stage_norm_digest(stage), "source": "critique"})
            continue
        if not raw:
            return [], "--norm-target must not be empty"
        path = os.path.normpath(raw if os.path.isabs(raw) else os.path.abspath(raw))
        refusal = _norm_file_refusal(state, path)
        if refusal:
            return [], refusal
        entries.append({"target": path, "kind": "file",
                        "baseline": gates.file_norm_digest(path), "source": "critique"})
    return entries, None


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
    if (destination is not None
            and canonical_normalization_destination(destination) not in NORMALIZATION_DESTINATIONS):
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


def _record_normalization_waiver(args, state: SessionState, store: StateStore,
                                 waiver: str) -> Directive:
    """`normalize --normalization-waiver <reason>` at DIAGNOSING: the open record is
    declared to expose a genuinely one-off factor, so it carries the reason instead of a
    Normalization. It is the only way a NESTED record closes (`difficulty-close`) without
    a factor; the root record closes through `replan`'s own waiver, which `replan` itself
    files on the record. Refused for the root record (the stored field would be inert
    there: `replan` never reads it), for an empty reason, alongside a `--factor`, and for
    a record that names norm targets -- those must be delivered by the closing plan, not
    waived."""
    d = state.difficulty
    refusal = None
    if not state.difficulty_stack:
        refusal = ("the open record is the root difficulty, which closes through replan: "
                   "pass --normalization-waiver to `replan`, not to normalize")
    elif not waiver.strip():
        refusal = "normalization waiver reason must not be empty"
    elif (getattr(args, "factor", None) or "").strip():
        refusal = ("normalize takes either --factor (the re-norming record) or "
                   "--normalization-waiver (a one-off factor), not both")
    elif d.norm_targets:
        refusal = ("the difficulty record names norm target(s) the closing plan must "
                   "change; a --normalization-waiver is not accepted for it")
    if refusal:
        _log_gate(state, "normalization_waiver", [refusal], passed=False)
        return Directive(False, state.node, "normalize", refusal, data={"blockers": [refusal]})
    d.normalization_waiver = waiver.strip()
    state.log("normalization_waived", reason=d.normalization_waiver, uid=d.uid)
    _log_gate(state, "normalization_waiver", [], passed=True)
    store.save(state)
    return Directive(True, state.node, "replan",
                     "one-off factor recorded on the nested record as a normalization "
                     "waiver; it may now difficulty-close")


def _cmd_normalize_outside_diagnosing(args, state: SessionState, store: StateStore) -> Directive:
    """The record-only sibling of cmd_normalize's DIAGNOSING branch -- runs at any
    node from CLASSIFIED up to, but not including, RESOLVED (a RESOLVED session has
    no further quality row or scorecard pass for the record to land on), touches no gate,
    and logs the same `normalize` event shape with `in_diagnosis=False` so a reader
    scanning history for the event still finds it. The one difficulty state it touches:
    while a plan-review release is active and the open record is declared, investigated
    and critiqued, the re-norming lands on that record (the reflection exit's record)."""
    if state.node == Node.RESOLVED.value:
        return Directive(False, state.node, "continue",
                         "normalize outside a difficulty cannot record against a "
                         "RESOLVED session")
    if getattr(args, "normalization_waiver", None) is not None:
        return Directive(False, state.node, "normalize",
                         "--normalization-waiver applies to a difficulty being closed; "
                         "outside a difficulty cycle there is nothing to waive")
    error, factor = _normalize_record_fields(
        getattr(args, "factor", None), getattr(args, "level", None), getattr(args, "destination", None))
    if error:
        return Directive(False, state.node, "normalize", error)
    level = getattr(args, "level", None)
    destination = canonical_normalization_destination(getattr(args, "destination", None))
    record = state.difficulty
    if (_release_reflection_open(state) and record is not None and record.declaration is not None
            and record.investigation is not None and record.critique is not None):
        record.normalization = Normalization(factor=factor, level=level, destination=destination)
        _log_normalize_event(state, factor=factor, level=level, destination=destination,
                             in_diagnosis=False)
        store.save(state)
        return Directive(True, state.node, "continue",
                         "renorming recorded on the reflection record; an agent-authored "
                         "plan-review override is now admitted with it")
    _log_normalize_event(state, factor=factor, level=level, destination=destination, in_diagnosis=False)
    store.save(state)
    return Directive(True, state.node, "continue",
                     "renorming recorded outside a difficulty cycle")


def _resume_action(d: Difficulty) -> str:
    if d.declaration is None:
        return "declare"
    if d.investigation is None:
        return "investigate"
    if d.critique is None:
        return "critique"
    return "replan"


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
    is executing, and after this call those are `args.plan`'s. An AcceptanceReview
    carrying per-requirement bindings stays current (a procedure is no deliverable of
    any requirement); one written before bindings existed goes stale on the digest
    move. The directive names the requirement ids that went stale, if any."""
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
    old = _load_prior_plan(state, old_path, strict=False)
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
    live_status = {s.index: s.outcome.status for s in state.stages}
    rtblock = gates.renorm_retention_blockers(
        state, new,
        [ns.index for ns in new.stages
         if live_status.get(ns.index) != StageStatus.PASSED.value],
        installing_plan_path=args.plan)
    _log_gate(state, "renorm_retention", rtblock, passed=not rtblock)
    if rtblock:
        return Directive(False, state.node, "replan",
                         "renormalization blocked: an open norm-edit obligation is no "
                         "longer carried by a stage that still runs",
                         data={"blockers": rtblock})
    state.pending_renorms = gates.open_renorms(state)
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
    # The approved snapshot deliberately stays on the APPROVED bytes (see above), so the
    # binding is never moved here: stored entries stay as approved. A session bound
    # before entries were stored gets them materialized, but only when `new` still
    # reproduces the approved hash -- a procedure that changes what the stage derives is
    # not covered by the approval the binding records, and the hash is never moved here.
    if (state.approved_grants_sha256 and state.approved_grant_entries is None
            and grants_sha256(new) == state.approved_grants_sha256):
        state.approved_grant_entries = materialized_grant_entries(new)
    # renormalize is one of the replan kinds every submitted version is
    # content-addressed for, despite logging a distinct event name (see the class
    # docstring above on why it's `renormalize`, not `replan`).
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest, plan_path=args.plan)
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


def _report_acceptance_staleness(d: Directive, args, store: StateStore) -> None:
    """Name, on an applied replan of every kind, the requirement ids whose recorded
    acceptance the plan it carries has invalidated (`acceptance_stale`, empty when none) --
    the resolution gate's own check on the state the replan just saved
    (`gates.acceptance_stale_requirements`), so the author learns at the edit what
    verify-final would otherwise refuse later. Silent when acceptance is inactive, no
    AcceptanceReview exists or the replan was refused."""
    if not d.ok:
        return
    state = store.load(args.session)
    if state is None or state.acceptance_review is None or not gates.acceptance_active(state):
        return
    stale = gates.acceptance_stale_requirements(state) or []
    d.data["acceptance_stale"] = stale
    if stale:
        d.detail += (f"; the recorded acceptance is stale for requirement id(s) {stale} — "
                     "re-run accept before resolution")


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


def _cmd_replan(args, *, store: StateStore, runner: Runner | None = None,
                crossed: list[dict] | None = None,
                reopened: list[int] | None = None,
                reopen_ignored: list[int] | None = None) -> Directive:
    """`crossed`, when given, receives the backstop crossing a renegotiation applied;
    `reopened`, the indices of the PASSED stages an applied `--reopen` re-armed;
    `reopen_ignored`, the `--reopen` indices that named a stage that is not PASSED."""
    state = _require(store, args.session)
    from .plan import diff_plans, load_plan as _load

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

    # precondition: a nested difficulty (difficulty-open) is a sub-cycle of the enclosing
    # one -- closed with difficulty-close, never swept away by the replan of its parent.
    sblock = gates.difficulty_stack_blockers(state)
    _log_gate(state, "difficulty_stack", sblock, passed=not sblock)
    if sblock:
        return Directive(False, state.node, "difficulty_close",
                         "replan blocked by an open nested difficulty",
                         data={"blockers": sblock})

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
        crossing = None
        if renegotiated_by.casefold() == AGENT_ACTOR:
            refusal, crossing = _agent_renegotiation_refusal(state, decision, order_doc)
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
        _record_backstop_crossing(state, crossing)
        _clear_since_user_decision(state, renegotiated_by)
        if decision == "abandon":
            # mirrors cmd_block's own bypass-transition idiom (and fire-acknowledge's
            # "abandon" decision) — parks reversibly via unblock, never RESOLVED, and
            # never touches args.plan.
            _park_node(state)
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
        state.review_rounds = 0
        state.review_release_difficulty_id = None
        # The reset above is already on disk, so the record of the decision that earned it
        # lands with it: a later gate below may still refuse this replan.
        store.save(state)
        if crossing is not None and crossed is not None:
            crossed.append(crossing)

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
    wtblock = gates.normalization_waiver_blockers(state, getattr(args, "normalization_waiver", None))
    if wtblock:
        _log_gate(state, "normalization_waiver", wtblock, passed=False)
        return Directive(
            False, state.node, "normalize",
            "replan refused: the difficulty record names norm target(s) the closing plan "
            "must change; a --normalization-waiver is not accepted for it",
            data={"blockers": wtblock},
        )
    nblock = gates.normalization_blockers(state, pending_factor=normalize_factor or None)
    closing_waiver: str | None = None
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
        closing_waiver = waiver
    else:
        _log_gate(state, "normalization_blockers", nblock, passed=True)

    # closure precondition (R2): the fault must be ROUTED — the critique's failure_address
    # must be recorded (resource/normative/not_applicable provision), never a bare
    # omission — before the difficulty may be closed. Mirrors normalization_blockers: an
    # INTERNAL precondition (absent from GUARDIANS), [] outside the DIAGNOSING-closure
    # path, so a non-difficulty replan is unaffected. Unlike the normalization gate there
    # is no waiver — the explicit escape is a legal not_applicable on the critique itself.
    fablock = gates.failure_address_blockers(state)
    _log_gate(state, "failure_address_blockers", fablock, passed=not fablock)
    if fablock:
        return Directive(False, state.node, "critique",
                         "replan blocked: the fault must be routed to the inadequate "
                         "provision (re-run critique with --failure-address)",
                         data={"blockers": fablock})

    # plan-review gate: the corrected plan (args.plan) must carry a thinker review
    # with a passing/overridden verdict BOUND to it before it may be applied. Gates
    # EVERY replan kind (refinement and substantive alike, per the user decision),
    # inactive for non-substantive sessions. A genuine no-op replan (args.plan ==
    # the already-reviewed plan_path) passes on the existing review.
    # The records are judged as carried onto the corrected plan: a tier 0-1 edit owes no
    # review, a tier 2 edit owes the edited stages' own. Nothing is written until the
    # replan is applied.
    gate_state, carry_preview, carry_change = _carry_records(store, state, args.plan, in_place=False)
    trigger_exit = _review_trigger_exit(state, args.plan)
    # A changed plan leaves a review-cost trigger by owing a fresh counted PASS at approve,
    # so the replan itself does not wait on a review of bytes nobody has reviewed yet.
    prblock = (
        [] if trigger_exit == _TRIGGER_EXIT_CHANGED
        else gates.plan_review_blockers(gate_state, args.plan))
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
            delta = gates.review_delta(gate_state, review_doc, args.plan)
            if carry_change is not None:
                delta["tier"] = carry_change.tier
                delta["stage_tiers"] = {i: c.tier for i, c in sorted(carry_change.stages.items())}
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
    # answers one ordering: before the plan_approval plugin block below, so a command that
    # refuses on submission grade has mutated no persisted state.
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
    submission = _submission_problems(
        new, run, state.weight_class,
        _frozen_edge_stages(state, _baseline_plan_for_tier(store, state), new))
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
    # block below; this command has still written nothing to disk at this point.
    auth_kind = _replan_kind(state, _load_prior_plan(state, _replan_baseline_path(state), strict=False), new)

    # A finished stage is frozen against the edits that change what it was built from or
    # promises (operative and interface leaves): a replan of ANY kind touching one is
    # refused unless it names the stage with --reopen, which re-arms it and the PASSED
    # stages depending on it. Refused before anything is written. Without a known old
    # side there is no tier, and the replan routes as it did before the tier existed.
    tier = None
    baseline = _baseline_plan_for_tier(store, state)
    if baseline is not None:
        tier = change_tier(baseline, new)
    change_class = replan_class(tier)
    reopen_roots = set(getattr(args, "reopen", None) or [])
    unknown_reopen = sorted(reopen_roots - {s.index for s in state.stages})
    if unknown_reopen:
        return Directive(False, state.node, "replan",
                         f"replan refused: --reopen names no such stage: {unknown_reopen}")
    reopening = {
        i for i in _passed_dependents_closure(state.stages, new.stages, reopen_roots)
        if state.stage(i).outcome.status == StageStatus.PASSED.value
    } if reopen_roots else set()
    if reopen_ignored is not None:
        reopen_ignored.extend(sorted(i for i in reopen_roots if i not in reopening))
    if tier is not None:
        frozen = _frozen_stage_refusal(state, tier, reopening)
        _log_gate(state, "frozen_stage", [] if frozen is None else frozen[1]["blockers"],
                  passed=frozen is None)
        if frozen is not None:
            return Directive(False, state.node, "replan", frozen[0], data=frozen[1])

    tier_number = tier.tier if tier is not None else None
    unwaived_blockers = gates.replan_authorization_blockers(state, args.plan, diff_kind=auth_kind)
    auth_waived = gates.replan_authorization_waived_by_tier(tier_number) and bool(unwaived_blockers)
    arblock = [] if auth_waived else unwaived_blockers
    _log_gate(state, "replan_authorization", arblock, passed=not arblock)
    if arblock:
        return Directive(False, state.node, "present_plan",
                         "replan blocked: this plan edit has not been authorized by "
                         "the user",
                         data={"blockers": arblock})

    # Norm-edit consequence: a replan closing a difficulty whose record names norm targets
    # must be the re-norming it named, and an obligation an earlier replan admitted must
    # survive this one. Before the plan_approval plugin block below, whose dispositions
    # save can persist state, so a refusal here leaves the state file as it was.
    renorm_pending = _replan_pending_indices(state, new, auth_kind, tier, reopening)
    neblock = gates.normative_edit_blockers(state, new, renorm_pending,
                                            installing_plan_path=args.plan)
    _log_gate(state, "normative_edit", neblock, passed=not neblock)
    rtblock = gates.renorm_retention_blockers(state, new, renorm_pending,
                                              installing_plan_path=args.plan)
    _log_gate(state, "renorm_retention", rtblock, passed=not rtblock)
    if neblock or rtblock:
        return Directive(False, state.node, "replan",
                         "replan blocked: the corrected plan does not carry out the norm "
                         "edit the difficulty named (stage `renorms` must list each norm "
                         "file, on a stage that still runs)",
                         data={"blockers": neblock + rtblock})

    # plan_approval PLUGIN gate: mirror cmd_approve's plugins.plugin_gate_blockers
    # composition so a refinement/no_change replan cannot rotate the plan bytes back
    # to VERIFYING while a premise-plugin blocker (undispositioned question, stale
    # per-stage rebind) goes unchecked — the 2026-07-09 attest-vs-execute hole.
    # Evaluate against the PROPOSED plan (args.plan): premise_blockers reads
    # state.plan_path, so swap it for the call and restore in finally. [] when no
    # plugin extends plan_approval, so the row still logs (passed, empty) and the
    # replan is byte-identical to before.
    _saved_plan_path = gate_state.plan_path
    try:
        gate_state.plan_path = args.plan
        pblock = plugins.plugin_gate_blockers(gate_state, "plan_approval")
    finally:
        gate_state.plan_path = _saved_plan_path
    # Invalidate dispositions whose cited stage fields moved in the proposed plan so
    # the mismatch is visible in question-list output even when this replan is blocked
    # by the gate (#123). Runs here — after the try-finally restored plan_path and
    # before the pblock return — so a blocked replan still surfaces stale notes on disk.
    # A carried view (`gate_state` is a copy) already holds the carried premise stamps; on
    # the refused path the real state gets the same carry first, so the invalidation below
    # marks only the dispositions the edit really made stale. On the applied path the
    # carry is left to `_carry_records`, whose `carried` verdict it would otherwise change.
    _inv_bag = state.plugins.get("premise")
    if _inv_bag is not None and (gate_state is state or pblock):
        _inv_changed = False
        if gate_state is not state:
            _carry_baseline, _ = _review_baseline(store, state)
            if _carry_baseline is not None:
                _inv_changed = bool(
                    carry_forward.carry_premise(state, _carry_baseline, new, carry_change))
        _inv_stage_keys = {s.index: stage_norm_keys(s) for s in new.stages}
        _inv_meta_keys = plan_meta_element_keys(new)
        _inv_changed = premise.invalidate_stale_dispositions(
            _inv_bag, _inv_stage_keys, meta_keys=_inv_meta_keys
        ) or _inv_changed
        _inv_changed = (
            premise.invalidate_stale_order_dispositions(_inv_bag, _inv_stage_keys)
            or _inv_changed
        )
        if _inv_changed:
            store.save(state)
    _log_gate(state, "plan_approval_plugin", pblock, passed=not pblock)
    if pblock:
        # Escape counts are read against the PROPOSED plan's digest, not
        # state.plan_path's, since that is the plan version the blocker speaks for.
        return Directive(False, state.node, "close_questions",
                         "replan blocked: the corrected plan carries unresolved "
                         "plan_approval premises (dispose open questions / rebind "
                         "stale per-stage bindings against the new plan)",
                         data={"blockers": pblock,
                               # LEGACY field: the retired gate's escape history
                               "enumeration_escapes": _enumeration_escape_counts(
                                   state, new)})
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
    old = _load_prior_plan(state, old_path, strict=False)
    # Content-address both sides of this replan -- the OLD baseline (read fresh;
    # `old_path` may be a legacy plan_path fallback never snapshotted before) and the
    # NEW plan, reusing the (data, digest) seam already read via `load_plan_with_digest`
    # rather than re-reading args.plan a second time. Computed once here, past every
    # refusal above, and used by whichever of the three kind branches below applies.
    _prev_plan_sha256 = _snapshot_plan_version(store, state, old_path)
    _write_plan_version_snapshot(store, state, _new_plan_bytes, _new_plan_digest, plan_path=args.plan)
    _new_plan_sha256 = _new_plan_digest
    # coverage gate: inside the difficulty flow, the corrected plan must CARRY the
    # critique's similarities into conditions/invariants and CHANGE a means/method
    # for the declared differences. Empty split -> [] -> behaves exactly as before.
    if state.difficulty and state.difficulty.critique and trigger_exit != _TRIGGER_EXIT_OVERRIDE:
        cov = gates.replan_coverage_blockers(
            old, new, state.difficulty.critique, tier=tier_number,
            cost_tier_window=_effort_fire_window(state))
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
                return Directive(False, state.node, "declare",
                                 "replan blocked: critique coverage (carry the critique into the plan, "
                                 "or pass --coverage-waiver '<reason>' with a reason it does not apply)",
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
    if agent_acked_difficulty and trigger_exit is None and (
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

    kind = auth_kind
    if reopened is not None:
        reopened.extend(sorted(reopening))
    state.pending_renorms = gates.bound_renorms(state, new, renorm_pending)
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
    # A tier 0-2 edit does not start a new review loop: its records are carried onto the
    # new version (`carry_forward`), so the rounds, the counted digest and the passes
    # stay with them; every other edit settles the old loop and starts from zero.
    _, carry_report, _ = _carry_records(store, state, args.plan, in_place=True)
    carry_note = carry_forward.describe(carry_report)
    if carry_note:
        state.log("review_carried", plan=args.plan, tier=carry_report["tier"],
                  records=list(carry_report["records"]), owed=list(carry_report["owed"]),
                  held=list(carry_report["held"]))
    else:
        task_accumulator.add(
            state.task_id, "plan_review_rounds", state.plan_review_rounds,
            session_id=state.session_id, now=_utcnow(),
        )
        state.plan_review_rounds = 0
        state.plan_review_counted_digest = ""
        # Terminal-pass custody — same "current approval cycle" boundary as the
        # pair above: a replanned plan starts with no pass on record for any scope.
        state.plan_review_passes = {}
    task_accumulator.add(
        state.task_id, "code_review_rounds", state.code_review_rounds,
        session_id=state.session_id, now=_utcnow(),
    )
    # Reset alongside the pair above (item A) — same reasoning: rounds spent
    # code-reviewing the previous plan version are settled once a corrected plan lands.
    state.code_review_rounds = 0
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
    echo_advice = _submission_advice(new, run, state.weight_class, args.plan)
    if carry_note:
        echo_advice = echo_advice + [carry_note]
    if auth_waived:
        state.log("replan_authorization_waived", plan=args.plan, tier=tier_number)
        echo_advice = echo_advice + [
            f"replan authorization waived: a tier {tier_number} edit changes nothing the user "
            "approved or a reviewer judged, so no replan_diff presentation was required"]
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
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    _flush_order_effort(state, store=store)

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
            if ns.index in reopening:
                cur.outcome.status = StageStatus.PENDING.value
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
            _archive_difficulty(state, closing_waiver)
            if not state.approval.passed:
                return _replan_to_plan_ready(
                    state, store, new, echo_advice, trigger_exit=trigger_exit,
                    kind="no_change", exited_diagnosing=True,
                    plan=args.plan, prev_plan_sha256=_prev_plan_sha256,
                    plan_sha256=_new_plan_sha256, change_class=change_class, **replan_cause)
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
            state.log("replan", kind="no_change", exited_diagnosing=True, plan=args.plan,
                      prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
                      change_class=change_class, **replan_cause)
            _count_replan(state, change_class)
            effort.rederive(state)  # re-derive AFTER logging so this in-flight replan is counted
            store.save(state)
            if state.ready_stages():
                return _with_advisories(Directive(
                    True, state.node, "next_stage",
                    "difficulty worked through; plan unchanged — retry the re-armed stage",
                    data=_replan_class_data(state)), echo_advice)
            return _with_advisories(Directive(
                True, state.node, "continue", "difficulty worked through; resume execution",
                data=_replan_class_data(state)), echo_advice)
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
            if cur.outcome.status == StageStatus.FAILED.value or ns.index in reopening:
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
            _archive_difficulty(state, closing_waiver)
            if not state.approval.passed:
                return _replan_to_plan_ready(
                    state, store, new, echo_advice, trigger_exit=trigger_exit,
                    kind="refinement", exited_diagnosing=True,
                    plan=args.plan, prev_plan_sha256=_prev_plan_sha256,
                    plan_sha256=_new_plan_sha256, change_class=change_class, **replan_cause)
            state.node = transition(state.node, "replan_refine")  # DIAGNOSING -> VERIFYING
        state.log("replan", kind="refinement", exited_diagnosing=diagnosing, plan=args.plan,
                  prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
                  change_class=change_class, **replan_cause)
        _count_replan(state, change_class)
        effort.rederive(state)  # re-derive AFTER logging so this replan is counted
        store.save(state)
        if state.node == Node.VERIFYING.value and state.ready_stages():
            return _with_advisories(Directive(
                True, state.node, "next_stage", "refinement applied; retry the ready stage",
                data=_replan_class_data(state)), echo_advice)
        return _with_advisories(Directive(
            True, state.node, "continue", "refinement applied; resume execution",
            data=_replan_class_data(state)), echo_advice)

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
                delivered_base=prev.outcome.delivered_base,
                landed_head=prev.outcome.landed_head,
                landed_base=prev.outcome.landed_base,
                landed_content_waiver=prev.outcome.landed_content_waiver,
            ),
            prior_control=prev.control,
            reattest_digest=stage_reattest_digest(ns),
        ))
    state.reattest_stash = reattest_stash
    declined_issues = gates.declined_issue_set(state)
    declined_titles = gates.declined_title_set(state)
    for ns in new.stages:
        prev = live_by_index.get(ns.index)
        if prev is not None and ns.index not in reopening and _verdict_survives(
            state.stages, new.stages, ns.index, tier
        ):
            # A declined optional stage whose definition did not move stays declined; a
            # changed one goes back to PENDING for a fresh choice at the customer's
            # re-approval.
            if prev.outcome.status == StageStatus.PASSED.value or (
                is_skipped(prev) and ns.optional
            ):
                ns.outcome = prev.outcome
        # The customer's decline follows the stage's identity (its issue), not its index,
        # and outlives a reset to PENDING: `approve --by agent` refuses while a declined
        # stage is live, so only a customer approval can bring it back.
        if is_skipped(ns) or (
            not is_settled(ns) and gates.is_declined_stage(ns, declined_issues, declined_titles)
        ):
            ns.outcome.declined = True
    state.stages = new.stages
    _sync_venue_from_plan(state, new)
    state.final_check = new.meta.final_check
    state.plan_path = args.plan
    state.plan_verified = True
    state.overall_done_criterion = new.meta.done_criterion or state.overall_done_criterion
    state.current_stage = None
    _archive_difficulty(state, closing_waiver)
    return _replan_to_plan_ready(
        state, store, new, echo_advice, trigger_exit=trigger_exit, kind="substantive", plan=args.plan,
        prev_plan_sha256=_prev_plan_sha256, plan_sha256=_new_plan_sha256,
        change_class=change_class, **replan_cause)


def _replan_to_plan_ready(state: SessionState, store: StateStore, new, echo_advice: list[str],
                          *, trigger_exit: str | None, **log_fields) -> Directive:
    """Re-arm the plan-approval gate and park the session at PLAN_READY after a replan
    that needs the plan approved again, logging `log_fields` as the `replan` event. The
    approved plan picks the next stage afresh, so no stage stays current. `trigger_exit`
    is the review-cost trigger exit `_cmd_replan` already decided (None when no trigger is open)."""
    state.current_stage = None
    state.approval = GateRecord("plan_approval", armed=True, passed=False)
    state.node = Node.PLAN_READY.value
    state.log("replan", **log_fields)
    _count_replan(state, log_fields["change_class"])
    effort.rederive(state)  # re-derive AFTER logging so this replan is counted
    _review_cycle_arrive(state, log_fields["plan_sha256"])
    if trigger_exit == _TRIGGER_EXIT_CHANGED:
        _drop_agent_overrides(state)
    if trigger_exit is not None:
        state.review_trigger_fire = None
    store.save(state)
    autonomy = _autonomy_directive_data(state, new)
    override = _agent_review_override(state)
    directive_data = {
        "autonomy": autonomy, **_replan_class_data(state),
        **({"agent_review_override": override} if override else {}),
        **({"review_trigger_exit": _trigger_exit_text(trigger_exit, override)}
           if trigger_exit is not None else {}),
    }
    if autonomy["action"] == "self_approve":
        return _with_advisories(Directive(
            True, state.node, "self_approve",
            _self_approve_detail(autonomy, replan=True),
            data=directive_data,
        ), echo_advice)
    return _with_advisories(Directive(
        True, state.node, "await_user_approval",
        f"{log_fields['kind']} replan; HARD GATE — re-approval required",
        marker="PLAN-READY", data=directive_data,
    ), echo_advice)


def _parent_link_summary(link: dict | None) -> dict | None:
    if link is None:
        return None
    return {key: link.get(key) for key in ("parent_session", "stage_index", "depth", "norm_digest")}


def _load_or_none(store: StateStore, session: str) -> SessionState | None:
    try:
        return store.load(session)
    except Exception:
        return None


def _linked_children(store: StateStore, parent: str) -> list[SessionState]:
    """The loadable sessions whose `parent_link` names `parent`. Read-only: a state file
    that cannot be loaded is skipped, never repaired or rewritten."""
    list_ids = getattr(store, "session_ids", None)
    loaded: dict = {}
    return [loaded[session_id] for session_id in (list_ids() if list_ids is not None else [])
            if _parent_session_of(store, session_id, loaded) == parent]


def _escalation_rows(owner: SessionState, parent: str, parent_state: SessionState | None) -> list[dict]:
    """One row per difficulty of `owner` (open, suspended or closed) escalated to the norm
    of `parent`: its uid, the stage, and `gates.escalation_status` read from the parent's
    own state."""
    rows: list[dict] = []
    for rec in gates.escalation_records(owner):
        target = gates.rec_field(rec, "escalated_to") or {}
        if target.get("parent_session") != parent:
            continue
        rows.append({
            "child_uid": gates.rec_field(rec, "uid"), "stage_index": target.get("stage_index"),
            "status": gates.escalation_status(rec, parent_state, owner.session_id),
        })
    return rows


def _run_step(fn, args, *, store: StateStore, runner: Runner | None, trace: list) -> Directive:
    """Call a cmd_* function, append a compact crumb to `trace`, return its Directive."""
    d = fn(args, store=store, runner=runner)
    trace.append({"command": fn.__name__.removeprefix("cmd_"), "node": d.node,
                  "action": d.action, "ok": d.ok})
    return d


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
    "plan-render", "plan-grants", "plan-resources", "plan-digest", "plan-externalize",
    "plan-seal", "plan-convert", "plan-convert-live", "submit-plan", "present-plan", "confirm-delivery", "plan-review",
    "plan-review-delta", "plan-review-walk", "plan-review-compose", "plan-review-pair-history",
    "risk-accept", "accept-protected-grant", "stage-review", "code-review", "accept", "approve",
    "partition", "partition-units", "next-stage", "dispatch", "resolve-permission", "stage-grants", "evidence-dir", "grant-stats",
    "record-result", "record-landing", "declare",
    "investigate", "critique", "normalize", "difficulty-open", "difficulty-close",
    "verify-final", "resolve", "reject",
    "replan", "fire-acknowledge", "check-coverage", "effort-check", "block", "unblock", "status", "children",
    "adopt-escalation", "drive", "close", "push-subplan", "pop-subplan", "task-reset",
    "mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry", "mandate-stop", "mandate-status",
    "workspace-register",
)


# (dest, subcommands that declare it)
_RESOLVE_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("goal", ("start", "reset")),
    ("done_criterion", ("start", "reset")),
    ("note", ("plugin-record", "confirm-delivery", "plan-review", "stage-review", "code-review",
              "accept", "question-enumerate-escape", "fire-acknowledge", "accept-protected-grant")),
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
    ("customer_questions", ("plan-review",)),
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
    ("landing_waiver", ("resolve", "close")),
    ("resolution_ask_unverifiable", ("resolve", "close")),
    ("coverage_waiver", ("replan",)),
    ("normalization_waiver", ("replan", "normalize")),
    ("renegotiation_note", ("replan",)),
    ("bypass_reason", ("accept",)),
    ("reopen_reason", ("reset",)),
    ("reopen_user_decision", ("reset",)),
    ("rejection_text", ("present-plan",)),
    ("accept_content_loss", ("record-landing",)),
)


# (dest, subcommands that declare it, why '@' means nothing here)
_DO_NOT_WRAP_ROWS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("state_root", (_ROOT,), "directory path the state store is rooted at"),
    ("session", _SESSION_COMMANDS, "session id — the slug state is keyed by"),
    ("task", ("start", "reset", "push-subplan", "task-reset"), "task slug, not a description"),
    ("child", ("adopt-escalation",), "session id of the linked child"),
    ("criterion_type", ("start", "reset"), "one of two fixed verification kinds"),
    ("plugin", ("plugin-activate", "plugin-deactivate", "plugin-record"), "plugin registry name"),
    ("phase", ("plugin-record",), "plugin phase name from a fixed vocabulary"),
    ("attachment", ("plugin-record",), "ticket attachment id or URL, not a file to read"),
    ("comment", ("plugin-record",), "ticket comment id or URL, not a file to read"),
    ("tracker_key", ("plugin-activate", "classify", "drive"), "tracker issue key (ABC-123)"),
    ("id", ("ledger-add", "ledger-candidate", "ledger-dispose", "question-raise",
            "question-research", "question-dispose", "question-rebind", "question-retire",
            "question-candidate-dispose", "order-raise", "order-dispose", "accept-protected-grant"),
     "claim / question / order-element / protected-action id"),
    ("claim", ("ledger-dispose",), "id of the grounding claim, not its text"),
    ("artifact", ("ledger-enumerate",), "path to the deliverable being cross-checked"),
    ("target", ("question-raise", "plan-review", "plan-review-walk", "plan-review-compose",
                "plan-review-pair-history"),
     "plan element address or plan file path"),
    ("pair", ("plan-review-pair-history",), "review pair id (`<base>-<service>`) or unit id (`unit:<node>`) — an id, not narrative"),
    ("control", ("question-raise",),
     "structured control address matched against controls.MATERIALITY_GRAMMARS — a "
     "grammar-bound name, never the prose --control of record-result/close"),
    ("plan", ("plan-render", "plan-grants", "plan-resources", "plan-digest", "plan-externalize",
              "plan-seal", "plan-convert", "plan-review-delta", "submit-plan", "replan", "drive",
              "push-subplan", "question-enumerate", "question-enumerate-worker",
              "question-enumerate-escape", "question-dispose",
              "question-rebind", "question-raise", "present-plan", "order-dispose"),
     "plan file path"),
    ("out", ("plan-convert",), "output directory path for the converted tree"),
    ("field", ("plan-externalize",), "name of a plan field — an identifier, not narrative"),
    ("meta_field", ("plan-externalize",), "name of a [meta] field — an identifier, not narrative"),
    ("order_field", ("plan-externalize",),
     "name of a [meta.order] field — an identifier, not narrative"),
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
    ("mandate_id", ("mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry",
                    "mandate-stop", "mandate-status"),
     "mandate slug the state directory is keyed by"),
    ("by", ("confirm-delivery", "approve", "resolve", "fire-acknowledge", "resolve-permission",
            "task-reset", "mandate-grant", "mandate-extend", "mandate-resume", "mandate-retry"),
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
    ("scope", ("plan-review", "risk-accept"), "'' or 'stage:<n>' or 'topo:<pair-or-unit>' — the review's binding, not narrative"),
    ("concern_ids", ("plan-review",),
     "explicit stable ids for --concern, positionally paired — ids, not narrative"),
    ("concern_id", ("risk-accept",), "the concern id this acceptance answers — an id, not narrative"),
    ("norm_targets", ("critique",),
     "norm-target address (`stage:<n>` or a repo path) the critique must edit — an address, not narrative"),
    ("code_ref", ("code-review", "record-result"), "commit / PR reference the verdict binds to"),
    ("head", ("record-landing",), "commit the landing pushed — a revision, not narrative"),
    ("base", ("record-landing",), "commit the landing was rebased onto — a revision, not narrative"),
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
    ("path", ("workspace-register",), "filesystem path of the workspace — a path, not narrative"),
    ("ws_backend", ("workspace-register",), "workspace backend name from the registry — an identifier"),
    ("ws_name", ("workspace-register",), "workspace/mount name the backend teardown is keyed by — an identifier"),
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


def _inject_default_session(argv: list[str], harness: str | None) -> list[str]:
    """Return a copy of ``argv`` with ``--session <harness>`` appended when the
    harness session id is known and the caller passed no --session of its own.

    hook-state-gate.py receives only the HARNESS conversation session_id
    (payload["session_id"] == $CLAUDE_CODE_SESSION_ID) and reads the state of the
    engine session bound to it (lib/engine_session_binding.py, written by a verb
    that saves state under an explicit --session), else of the harness id itself.
    An omitted --session must default to the harness id — not stay unset and
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


class _BindingStore(FileStateStore):
    """The default store of a verb run by a harness session: saving the state of the
    session the verb was run on binds that session to the harness id, so the
    production-edit gate (which sees only the harness id) follows the session the
    coordinator drives. Query verbs save nothing and so never bind."""

    def __init__(self, harness: str, session: str):
        super().__init__(None)
        self._harness = harness
        self._session = _safe_session_id(session)

    def save(self, state) -> None:
        super().save(state)
        if _safe_session_id(state.session_id) != self._session:
            return
        try:
            engine_session_binding.record_drive(
                self._harness, state.session_id, dt.datetime.now(dt.timezone.utc))
        except Exception:
            pass  # an unwritten binding leaves the gate on the previously most recent one (the harness id if none)


def _is_followed_by_gate(harness: str, session: str) -> bool:
    # The binding store always lives under the agent home; with --state-root the verb's
    # state is not the gate's state either, so that case reports "not followed" as base did.
    try:
        return engine_session_binding.resolve(harness) == _safe_session_id(session)
    except Exception:
        return False


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
