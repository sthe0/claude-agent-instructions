"""Helpers shared by more than one agentctl command group, moved verbatim from cli.py."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path
from lib import argv_text, config_root, engine_session_binding, hook_telemetry
from . import (
    advisor,
    carry_forward,
    child_runs,
    cost,
    effort,
    effort_continuation,
    effort_segments,
    exempt_paths,
    gates,
    grants as _grants,
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
from .config import Thresholds
from .directive import Directive, DIRECTIVE_ESCALATE_TO_USER
from .dispatch import Runner, subprocess_runner
from .machine import transition
from .plan import (
    stage_carry_key,
    PlanDoc,
    PlanError,
    load_plan,
    order_digest,
    plan_meta_element_key,
    stage_question_key,
    _venue_for,
)
from .submission import (
    plan_size_blocks,
    plan_size_warnings,
    submission_advice,
    submission_violations,
)
from .state import (
    frame_path,
    new_stage_uid,
    stage_uid_key,
    AGENT_ACTOR,
    CheckKind,
    Criterion,
    Declaration,
    Difficulty,
    canonical_normalization_destination,
    EFFORT_FIRE_FINISHED_WORK,
    is_settled,
    is_skipped,
    LANDED_GIT_ERROR_EXIT,
    Node,
    NORMALIZATION_DESTINATIONS,
    NORMALIZATION_LEVELS,
    PARENT_DIFFICULTY_ENV,
    parse_parent_difficulty,
    plan_review_scope_for_pair,
    SessionState,
    Stage,
    StageReview,
    StageStatus,
)
from .store import FileStateStore, StateStore, safe_session_id as _safe_session_id


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


GATE_LOG = config_root.agentctl_gate_log()


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


def _observation_sha256(observation: str) -> str:
    """sha256 of an acceptance observation's bytes — the binding key the acceptance
    gate recomputes over the observation being recorded. Mirrors _plan_file_sha256 but
    over an in-memory string (the observation is never a file)."""
    return hashlib.sha256((observation or "").encode("utf-8")).hexdigest()


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
    tables, armings = _effort_identity_payload(state)
    order_approvals.flush_effort_identity(key, tables, armings)


def _stage_identity_record(stage, uid: str | None = None) -> dict:
    """What identity matching reads of a stage: the digest of its carry key without the edges,
    and the edges, whose supplier indices `effort_segments.match_identities` resolves to
    identities. JSON-safe, because the order ledger stores it."""
    key = stage_carry_key(stage)
    record = {
        "index": stage.index,
        "key": hashlib.sha256(repr(key[:1] + key[2:]).encode("utf-8")).hexdigest(),
        "edges": [list(edge) for edge in key[1]],
    }
    if uid is not None:
        record["uid"] = uid
    return record


def _effort_identity_payload(state: SessionState) -> tuple[dict, dict]:
    """The identity tables of this session's frames and, per segment, the latest arming's entry
    (None once it closed), for the order ledger. A migration arming carries nothing: its actual
    is the old window's, which the order window already holds."""
    tables = {}
    frames = [(frame_path(state.plan_stack[:d]), f.stages) for d, f in enumerate(state.plan_stack)]
    frames.append((frame_path(state.plan_stack), state.stages))
    for path, stages in frames:
        tables[path] = [
            _stage_identity_record(s, state.stage_uids.get(stage_uid_key(path, s.index)))
            for s in stages if stage_uid_key(path, s.index) in state.stage_uids
        ]
    latest: dict = {}
    for arming in state.effort_segments.get("armings", []):
        latest[arming["segment"]] = arming
    armings: dict = {}
    for segment, arming in latest.items():
        if arming["budget_source"] in (effort_segments.SOURCE_STRETCH, effort_segments.SOURCE_WINDOW):
            continue
        settled = arming["closed_at"] is not None and segment != effort_segments.PLANNING
        armings[segment] = None if settled else {
            "segment": segment,
            "budget": arming["budget"],
            "actual": effort_segments.arming_actual(arming),
            "session": state.session_id,
            "declared_tier": arming.get("declared_tier"),
            "title": arming.get("title"),
            "tier_ladder": arming.get("tier_ladder"),
        }
    return tables, armings


def _carried_entries(state: SessionState, key: str) -> dict:
    return {
        segment: entry
        for segment, entry in order_approvals.effort_identity(key)["armings"].items()
        if entry.get("session") != state.session_id
    }


def _match_against_order(state: SessionState, key: str) -> dict:
    """{new index: uid or None} of the live frame's stages against the order's identity table."""
    path = frame_path(state.plan_stack)
    previous = order_approvals.effort_identity(key)["stages"].get(path) or []
    return effort_segments.match_identities(previous, [_stage_identity_record(s) for s in state.stages])


def _carried_live_stage_blockers(state: SessionState, key: str | None) -> list[str]:
    """A self-approval may not leave behind the stage an earlier session of the order left live:
    its actual and budget would go with it."""
    if key is None:
        return []
    matched = set(_match_against_order(state, key).values())
    return [
        f"--by agent refused: stage {entry.get('title')!r}, live in session {entry.get('session')} of "
        "this order, has no match in this plan; only the customer's approval may drop it"
        for segment, entry in _carried_entries(state, key).items()
        if segment.startswith(effort_segments.STAGE_PREFIX)
        and segment[len(effort_segments.STAGE_PREFIX):] not in matched
    ]


def _adopt_order_identities(state: SessionState, key: str | None, now: float) -> None:
    """Take over, at a plan approval, what earlier sessions of the order left: matched stages
    keep their identity, and an unsettled arming's actual and budget continue here as carried.
    An adopted stage arming holds no span: it owns no write time of this session until the
    handoff that reaches its segment resumes it."""
    ledger = state.effort_segments
    path = frame_path(state.plan_stack)
    uids = {}
    if key is not None:
        uids = {index: uid for index, uid in _match_against_order(state, key).items() if uid}
        for index, uid in uids.items():
            state.stage_uids[stage_uid_key(path, index)] = uid
    state.assign_stage_uids()
    if key is None:
        return
    adopted = ledger.setdefault("adopted", [])
    matched = set(uids.values())
    dropped = []
    for segment, entry in _carried_entries(state, key).items():
        token = f"{entry.get('session')}:{segment}"
        if token in adopted:
            continue
        if segment == effort_segments.PLANNING:
            arming = next((a for a in reversed(ledger["armings"]) if a["segment"] == segment), None)
        elif segment[len(effort_segments.STAGE_PREFIX):] in matched:
            arming = effort_segments.open_arming(
                ledger, segment, entry.get("budget"), effort_segments.SOURCE_CARRIED, now, "adoption",
                spanned=False)
            for field_name in ("declared_tier", "title", "tier_ladder"):
                arming[field_name] = entry.get(field_name)
        else:
            dropped.append(segment)
            continue
        if arming is None:
            continue
        carried = entry.get("actual") or {}
        for scale in effort_segments.SCALES:
            arming["carried"][scale] = arming["carried"].get(scale, 0.0) + float(carried.get(scale) or 0.0)
        adopted.append(token)
    if dropped:
        _settle_carried_drops(state, key, dropped, now)
        order_approvals.flush_effort_identity(key, {}, dict.fromkeys(dropped))


def _settle_carried_drops(state: SessionState, key: str, dropped: list[str], now: float) -> None:
    """A carried stage this approval leaves unmatched settles here: only a user-approved version
    gets this far with one, so it is a drop, kept as a line and a retier record at the multiple."""
    ledger = state.effort_segments
    entries = _carried_entries(state, key)
    for segment in dropped:
        entry = entries[segment]
        arming = effort_segments.open_arming(
            ledger, segment, entry.get("budget"), effort_segments.SOURCE_CARRIED, now, "adoption",
            spanned=False)
        for field_name in ("declared_tier", "title", "tier_ladder"):
            arming[field_name] = entry.get(field_name)
        for scale in effort_segments.SCALES:
            arming["carried"][scale] = float((entry.get("actual") or {}).get(scale) or 0.0)
        _close_at_multiple(ledger, arming, effort_segments.ACT_APPROVE_BY_USER, now)


def _venues(state: SessionState) -> tuple:
    return tuple(v for v in (state.repo_root, state.delivery_worktree) if v)


def _continuation_old_records(state: SessionState) -> list[dict]:
    """The approved plan of the live frame as the continuation rule reads it: identity, settled
    status by identity, normalized outputs and the check each stage promises."""
    venues = _venues(state)
    records = []
    for s in state.stages:
        uid = state.stage_uid(s.index)
        if uid is None:
            continue
        records.append({
            "index": s.index, "uid": uid, "title": s.title, "settled": is_settled(s),
            "outputs": [effort_continuation.normalize(e, venues) for e in s.output_artifacts],
            "record": _stage_identity_record(s, uid),
            "check": [s.criterion.done_criterion, s.criterion.verify_command],
        })
    return records


def _continuation_new_records(state: SessionState, stages) -> list[dict]:
    venues = _venues(state)
    return [{
        "index": s.index, "title": s.title,
        "outputs": [effort_continuation.normalize(e, venues) for e in s.output_artifacts],
        "record": _stage_identity_record(s),
        "check": [s.criterion.done_criterion, s.criterion.verify_command],
    } for s in stages]


def _continuation_base(state: SessionState) -> list[dict]:
    """The approved plan the next approval is decided against: the first replan since an approval
    records it, and later replans and edits at PLAN_READY keep reading it."""
    stored = state.effort_segments.get("continuation_base")
    if stored and stored["path"] == frame_path(state.plan_stack):
        return stored["stages"]
    return _continuation_old_records(state)


def _live_stage_uids(state: SessionState, old: list[dict]) -> set:
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None:
        return set()
    armed = {a["segment"].removeprefix(effort_segments.STAGE_PREFIX)
             for a in ledger.get("armings") or []
             if a["closed_at"] is None and a["segment"].startswith(effort_segments.STAGE_PREFIX)}
    return armed & {o["uid"] for o in old}


def continuation_decision(state: SessionState, new_stages) -> tuple:
    """(decision, the old records it was made against) for `new_stages` in the live frame. A live
    stage with no readable approved-plan snapshot to check against breaks the check."""
    old = _continuation_base(state)
    live = _live_stage_uids(state, old)
    decision = effort_continuation.decide(old, _continuation_new_records(state, new_stages), live)
    snapshot = state.plan_snapshot_path
    if live and not (snapshot and Path(snapshot).is_file()):
        decision.breaches.append(
            "no readable approved-plan snapshot to check the live stage's continuation against")
    return decision, old


def _rekey_stage_uids(state: SessionState, stages, matches: dict) -> None:
    """The live frame's identities for `stages`: a matched stage keeps its identity, any other
    keeps one it was already given this round or gets a fresh one, so it starts from zero."""
    path = frame_path(state.plan_stack)
    known = {o["uid"] for o in _continuation_base(state)}
    given = {s.index: state.stage_uids.get(stage_uid_key(path, s.index)) for s in stages}
    for s in state.stages:
        state.stage_uids.pop(stage_uid_key(path, s.index), None)
    for s in stages:
        kept = given[s.index] if given[s.index] not in known else None
        state.stage_uids[stage_uid_key(path, s.index)] = matches.get(s.index) or kept or new_stage_uid()


def _rematch_stage_uids(state: SessionState, new_stages, decision=None) -> None:
    """Re-key the live frame's identities to `new_stages` under the continuation decision."""
    if decision is None:
        decision, _ = continuation_decision(state, new_stages)
    _rekey_stage_uids(state, new_stages, decision.matches)


def _store_continuation_base(state: SessionState, old: list[dict]) -> None:
    ledger = state.effort_segments
    stored = ledger.get("continuation_base")
    if not stored or stored["path"] != frame_path(state.plan_stack):
        ledger["continuation_base"] = {"path": frame_path(state.plan_stack), "stages": old}


def _continuation_at_approval(state: SessionState):
    """The decision an approval of the bytes now in `state.stages` rests on, or None when no
    replan since the last approval left a base to decide against."""
    stored = state.effort_segments.get("continuation_base")
    if not stored or stored["path"] != frame_path(state.plan_stack):
        return None
    return continuation_decision(state, state.stages)[0]


def _continuation_blockers(decision) -> list[str]:
    if decision is None:
        return []
    return [f"--by agent refused: {reason}; only the customer's approval may drop or move it"
            for reason in decision.reasons()]


def _apply_continuation_at_approval(state: SessionState, decision) -> None:
    """An approval decided against the base re-keys the identities to its decision, so a stage the
    edited bytes reindexed keeps its arming, and ends the base."""
    if decision is None:
        return
    _rekey_stage_uids(state, state.stages, decision.matches)
    state.effort_segments.pop("continuation_base", None)


def _note_user_approved_tiers(state: SessionState) -> None:
    ledger = state.effort_segments
    approved = ledger.setdefault("approved_tiers", {})
    lazy = ledger.setdefault("lazy_marks", {})
    for stage in state.stages:
        uid = state.stage_uid(stage.index)
        if uid is not None:
            approved[uid] = effort._stage_tier(stage)
            lazy[uid] = stage.lazy


def _renew_order_agreement(state: SessionState, store: StateStore, args) -> None:
    """An order agreement is a user approval concerning the live segment."""
    arming = effort_segments.current_arming(state.effort_segments)
    if arming is not None:
        _renew_arming(state, store, args, arming, effort_segments.ACT_ORDER_AGREE,
                      _order_customer_id(state) or "user")


def _planning_fallback(thr: Thresholds) -> dict:
    return effort.planning_fallback(thr)


def _ensure_planning_arming(state: SessionState, now: float) -> None:
    ledger = state.effort_segments
    if not state.plan_stack and effort_segments.open_arming_of(ledger, effort_segments.PLANNING) is None:
        ledger.setdefault("session_floor", float(now))
        thr = Thresholds()
        opened = effort_segments.open_arming(
            ledger, effort_segments.PLANNING, _planning_fallback(thr),
            effort_segments.SOURCE_FALLBACK, now, "classify")
        opened["tier_ladder"] = effort.tier_ladder(thr)


def _budget_migrated_planning(arming: dict) -> None:
    """The planning arming a migration opened for a still-planning session carries no budget on
    the ledger; at its first judging or closing act it takes the configuration fallback, which
    it is judged against from its own opening instant."""
    if (arming["segment"] == effort_segments.PLANNING and arming["budget"] is None
            and arming["budget_source"] == effort_segments.SOURCE_UNBUDGETED):
        arming["budget"] = _planning_fallback(Thresholds())
        arming["budget_source"] = effort_segments.SOURCE_FALLBACK


def _close_at_multiple(ledger: dict, arming: dict, act: str, now: float) -> dict | None:
    _budget_migrated_planning(arming)
    return effort_segments.close_arming(
        ledger, arming["arming_id"], act, now, float(Thresholds().effort_divergence_multiple()))


def _acknowledge_arming_fire(state: SessionState, arming: dict, by: str, act: str, now: float) -> None:
    """A user act closing a fired arming decides its fire: the record closes as acknowledged."""
    fire = arming.get("fire")
    if not fire or fire.get("ack") is not None:
        return
    ack = {"by": by, "decision": "continue", "ts": now, "note": f"closed by {act}"}
    fire["ack"] = ack
    for records in (state.effort_fires, *(frame.effort_fires for frame in state.plan_stack)):
        for record in records:
            if record.get("arming_id") == arming["arming_id"] and record.get("ack") is None:
                record["ack"] = ack
    _clear_open_fires_if_customer(state, by)


def _clear_open_fires_if_customer(state: SessionState, by: str) -> None:
    """A customer-authored acknowledgement is the only act that discharges the open
    user-owed fire marker of the order (an agent-authored one never does)."""
    key = _ledgered_order_key(state)
    customer_id = _order_customer_id(state)
    if key is not None and customer_id and by.strip().casefold() == customer_id.casefold():
        order_approvals.clear_effort_fires(key)


def _close_at_user_act(state: SessionState, arming: dict, act: str, by: str, now: float) -> dict | None:
    _acknowledge_arming_fire(state, arming, by, act, now)
    return _close_at_multiple(state.effort_segments, arming, act, now)


def _next_arming_terms(state: SessionState, arming: dict, thr: Thresholds) -> tuple:
    """(budget, source, tier) of the arming a user act opens after `arming`, in its segment."""
    segment = arming["segment"]
    if segment == effort_segments.PLANNING:
        agreed = state.effort_segments.get("agreed_estimate") if state.order_agreement is not None else None
        if agreed:
            return ({effort_segments.SCALE_USD: float(agreed["usd"]),
                     effort_segments.SCALE_MINUTES: float(agreed["active_minutes"])},
                    effort_segments.SOURCE_AGREED, None)
        return _planning_fallback(thr), effort_segments.SOURCE_FALLBACK, None
    ledger = state.effort_segments
    uid = segment.removeprefix(effort_segments.STAGE_PREFIX)
    unfold = ledger.get("unfolds", {}).get(uid)
    if unfold:
        return dict(unfold["budget"]), unfold["source"], None
    approved = ledger.get("approved_tiers", {}).get(uid)
    if approved:
        return effort.stage_budget(approved, thr), effort_segments.SOURCE_USER_APPROVED, approved
    stage = next((s for s in state.stages if state.stage_uid(s.index) == uid), None)
    tier = effort._stage_tier(stage) if stage is not None else arming.get("declared_tier")
    return effort.stage_budget(tier, thr), effort_segments.SOURCE_DECLARED, tier


def _renew_arming(state: SessionState, store: StateStore, args, arming: dict, act: str, by: str) -> dict | None:
    """A user act that approves no plan version, on the segment it concerns: book, close the
    arming, open the next from zero. A migrated window or the closing segment opens nothing."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None or arming["segment"] == effort_segments.CLOSING:
        return None
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    now = _utcnow()
    held = effort_segments.open_span_of(arming) is not None
    _close_at_user_act(state, arming, act, by, now)
    thr = Thresholds()
    budget, source, tier = _next_arming_terms(state, arming, thr)
    renewed = effort_segments.open_arming(ledger, arming["segment"], budget, source, now, act, spanned=held)
    renewed["declared_tier"] = tier
    renewed["title"] = arming.get("title")
    renewed["tier_ladder"] = effort.tier_ladder(thr)
    return renewed


def _close_armings_at_user_approval(state: SessionState, store: StateStore, args, by: str, now: float) -> list[dict]:
    """Book, then close what a plan approval by the user decides: every open arming but closing's.
    Returns them; `_open_armings_after_user_approval` opens what follows once the version's tiers
    are recorded."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None or not ledger.get("armings"):
        return []
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    decided = [a for a in ledger["armings"]
               if a["closed_at"] is None and a["segment"] != effort_segments.CLOSING]
    closed = []
    for arming in decided:
        if _live_under_its_subplan(state, arming):
            _acknowledge_arming_fire(state, arming, by, effort_segments.ACT_APPROVE_BY_USER, now)
        else:
            _close_at_user_act(state, arming, effort_segments.ACT_APPROVE_BY_USER, by, now)
            closed.append(arming)
    return closed


def _live_under_its_subplan(state: SessionState, arming: dict) -> bool:
    """A parent the user did not mark lazy is a stage at work, not a stage being planned: its
    arming outlives its sub-plan's approval and settles with its last sub-stage."""
    segment = arming["segment"]
    if not segment.startswith(effort_segments.STAGE_PREFIX):
        return False
    uid = segment.removeprefix(effort_segments.STAGE_PREFIX)
    unfold = state.effort_segments.get("unfolds", {}).get(uid)
    if unfold and unfold["source"] == effort_segments.SOURCE_FALLBACK:
        return False
    live = {state.stage_uid(s.index) for s in state.stages}
    return uid in state.stage_uids.values() and uid not in live


def _open_armings_after_user_approval(state: SessionState, closed: list[dict], now: float) -> None:
    """A stage still live in the approved version continues under a new arming (holding no span
    when the closed one was parked); a dropped, skipped or unfolded one is settled. What the
    interval goes to next is `_hand_on_after_approval`'s."""
    live = {state.stage_uid(s.index): s for s in state.stages if not is_skipped(s)}
    for arming in closed:
        stage = live.get(arming["segment"].removeprefix(effort_segments.STAGE_PREFIX))
        if stage is not None:
            spans = effort_segments.spans_of(arming)
            held = bool(spans) and spans[-1]["end_act"] == "closed"
            _ensure_stage_arming(state, stage, now, effort_segments.ACT_APPROVE_BY_USER, spanned=held)


def _hand_on_after_approval(state: SessionState, now: float, act: str) -> None:
    """A plan approval hands the interval to the segment the selection takes next when no span is
    held (planning just closed, or a stage dropped), when closing holds it (a replan added a stage
    after it), or when the sub-plan's unfolded parent holds it and the first sub-stage has no
    arming yet. A stage's own live arming, renewed or not, keeps the interval; the selection
    parks it if another stage must run first."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None or not ledger.get("armings"):
        return
    holder = effort_segments.current_arming(ledger)
    ready = state.ready_stages()
    if ready:
        target = effort_segments.open_arming_of(
            ledger, effort_segments.stage_segment(state.stage_uid(ready[0].index) or ""))
        under_parent = holder is not None and _live_under_its_subplan(state, holder) and target is None
        if holder is None or holder["segment"] == effort_segments.CLOSING or under_parent:
            _ensure_stage_arming(state, ready[0], now, act)
    elif (not state.plan_stack and state.all_stages_settled() and holder is None
            and effort_segments.open_arming_of(ledger, effort_segments.CLOSING) is None):
        effort_segments.open_arming(
            ledger, effort_segments.CLOSING, None, effort_segments.SOURCE_UNBUDGETED, now, act)


def _settle_at_agent_approval(state: SessionState, now: float) -> None:
    """A self-approval that passed its judgment settles planning and a lazily unfolded parent."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None:
        return
    segments = [effort_segments.PLANNING,
                *(effort_segments.stage_segment(uid) for uid in ledger.get("unfolds", {}))]
    for segment in segments:
        arming = effort_segments.open_arming_of(ledger, segment)
        if arming is not None and not _live_under_its_subplan(state, arming):
            _close_at_multiple(ledger, arming, effort_segments.ACT_APPROVE_BY_AGENT, now)


def _live_stage_arming(state: SessionState) -> dict | None:
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None:
        return None
    arming = effort_segments.current_arming(ledger)
    if arming is None or not arming["segment"].startswith(effort_segments.STAGE_PREFIX) or not arming["budget"]:
        return None
    return arming


def _note_mandated_review(state: SessionState) -> bool:
    """The review a replan mandates joins the live stage's budget at the first act of that replan,
    before the review's spend is booked. One stands pending until a replan is accepted, so no
    number of reviews or refused attempts adds a second. Returns whether it added one."""
    arming = _live_stage_arming(state)
    if arming is None or arming.get("review_term_pending"):
        return False
    effort_segments.add_review_term(arming, effort.stage_budget(effort._REVIEW_TIER, Thresholds()))
    arming["review_term_pending"] = True
    return True


def _settle_mandated_review(state: SessionState, mandated: bool) -> None:
    """The replan accepted: its term stands, or is withdrawn from a replan owing no review."""
    arming = _live_stage_arming(state)
    if arming is None or not arming.get("review_term_pending"):
        return
    if not mandated:
        effort_segments.remove_review_term(arming, effort.stage_budget(effort._REVIEW_TIER, Thresholds()))
    arming["review_term_pending"] = False


def _unfold_parent_budget(state: SessionState, parent_index: int) -> None:
    """A sub-plan unfolds the parent: its arming and actual stay, its budget becomes planning's.
    Only a lazy mark the user approved loosens it; otherwise it is min(tier, fallback) per scale.
    Once per identity: a second unfold replaces nothing."""
    ledger = state.effort_segments
    uid = state.stage_uid(parent_index)
    unfolds = ledger.setdefault("unfolds", {})
    arming = effort_segments.open_arming_of(ledger, effort_segments.stage_segment(uid)) if uid else None
    if arming is None or uid in unfolds or effort_segments.window_arming(ledger) is not None:
        return
    thr = Thresholds()
    fallback = _planning_fallback(thr)
    if ledger.get("lazy_marks", {}).get(uid):
        budget, source = fallback, effort_segments.SOURCE_FALLBACK
    else:
        tier = effort.stage_budget(arming.get("declared_tier") or effort._stage_tier(state.stage(parent_index)), thr)
        budget = {scale: min(tier[scale], fallback.get(scale, tier[scale])) for scale in effort_segments.SCALES}
        source = effort_segments.SOURCE_FALLBACK_CAPPED
    unfolds[uid] = {"budget": dict(budget), "source": source}
    terms = arming["review_terms"] or {}
    arming["budget"] = {scale: budget.get(scale, 0.0) + terms.get(scale, 0.0) for scale in budget}
    arming["budget_source"] = source
    arming["declared_tier"] = None
    arming["tier_ladder"] = effort.tier_ladder(thr)


def _closing_arming_figures(state: SessionState) -> dict | None:
    """What a deciding ask must show: the arming the answer would close, against its budget on
    both scales, and any open fire. Reads booked records only. A migrated window has no budget of
    its own and the closing segment none at all, so those print their actual alone."""
    ledger = state.effort_segments
    arming = effort_segments.window_arming(ledger) or effort_segments.current_arming(ledger)
    if arming is None:
        return None
    actual = effort_segments.arming_actual(arming)
    budget = arming.get("budget")
    fire = gates.open_fire_record(state)
    parts = []
    for scale, label, unit, digits in (
            (effort_segments.SCALE_USD, "spend", "USD", 2), (effort_segments.SCALE_MINUTES, "wall-clock", "min", 1)):
        of = f" of {budget[scale]:.{digits}f}" if budget and budget.get(scale) else " (no budget)"
        parts.append(f"{label} {actual[scale]:.{digits}f}{of} {unit}")
    text = f"{arming.get('title') or arming['segment']} ({arming['budget_source']}): " + ", ".join(parts)
    if fire is not None:
        text += f"; open fire on {', '.join(fire.get('scales') or [fire.get('scale')])}"
    return {
        "arming_id": arming["arming_id"],
        "segment": arming["segment"],
        "title": arming.get("title"),
        "budget": budget,
        "budget_source": arming["budget_source"],
        "actual": actual,
        "open_fire": fire,
        "text": text,
    }


def _figures_instruction(figures: dict | None) -> str:
    if figures is None:
        return ""
    return f"; state these figures in the ask: {figures['text']}"


def _fire_directive_figures(state: SessionState) -> dict:
    """The data and next_steps a directive raising or refusing on a spend or wall-clock fire adds:
    the fire is the user's to decide, and the ask states what it decides on."""
    figures = _closing_arming_figures(state)
    if figures is None:
        return {}
    return {"closing_arming": figures,
            "next_steps": [f"ask the user about this fire{_figures_instruction(figures)}"]}


def _ensure_stage_arming(state: SessionState, stage, now: float, by: str, *, uid: str | None = None,
                         spanned: bool = True) -> dict | None:
    """The stage's open arming, holding the interval from `now`: one with no open span (parked,
    or adopted from an earlier session) resumes under its own id, one already holding it is
    left, and a stage with none gets one opened, its budget the tier of the last version the user
    approved, else the declared one. `uid` names a stage of an outer frame. `spanned=False`
    opens it holding no span. While a migrated window is open it owns everything, so nothing
    opens."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None:
        return None
    if uid is None:
        uid = state.stage_uid(stage.index)
    if uid is None:
        state.assign_stage_uids()
        uid = state.stage_uid(stage.index)
    segment = effort_segments.stage_segment(uid)
    arming = effort_segments.open_arming_of(ledger, segment)
    if arming is not None and (not spanned or effort_segments.open_span_of(arming) is not None):
        return arming
    thr = Thresholds()
    closing = effort_segments.open_arming_of(ledger, effort_segments.CLOSING)
    if closing is not None:
        _close_at_multiple(ledger, closing, by, now)
    if arming is not None:
        return effort_segments.resume_arming(ledger, arming["arming_id"], now)
    unfold = ledger.get("unfolds", {}).get(uid)
    approved = ledger.get("approved_tiers", {}).get(uid)
    tier = None if unfold else approved or effort._stage_tier(stage)
    budget = dict(unfold["budget"]) if unfold else effort.stage_budget(tier, thr)
    source = (unfold["source"] if unfold
              else effort_segments.SOURCE_USER_APPROVED if approved else effort_segments.SOURCE_DECLARED)
    arming = effort_segments.open_arming(ledger, segment, budget, source, now, by, spanned=spanned)
    arming["declared_tier"] = tier
    arming["title"] = stage.title
    arming["tier_ladder"] = effort.tier_ladder(thr)
    return arming


def _select_stage_arming(state: SessionState, stage, now: float) -> None:
    """The selection makes the armed stage current and opens nothing. Only when a replan put
    another stage first does the stage holding the interval go back to PENDING with its arming
    parked, and the selected stage's arming resume or open in its place."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None:
        return
    uid = state.stage_uid(stage.index)
    arming = effort_segments.open_arming_of(ledger, effort_segments.stage_segment(uid)) if uid else None
    if arming is not None and effort_segments.open_span_of(arming) is not None:
        return
    holder = effort_segments.current_arming(ledger)
    if (holder is not None and holder["segment"].startswith(effort_segments.STAGE_PREFIX)
            and not _live_under_its_subplan(state, holder)):
        effort_segments.park_arming(ledger, holder["arming_id"], now)
    _ensure_stage_arming(state, stage, now, "next_stage")


def _settle_stage_arming(state: SessionState, stage, act: str, now: float) -> dict | None:
    """Settle `stage`'s arming at `act`. When it held the interval, hand it on at the same instant:
    to the next stage (resumed if parked or adopted, else opened), or to closing when none is
    pending at the top plan. An arming holding no span (parked) hands nothing on. Returns the
    retier record the settlement produced. While a migrated window is open nothing opens or
    settles."""
    ledger = state.effort_segments
    if effort_segments.window_arming(ledger) is not None or not ledger.get("armings"):
        return None
    record = None
    held = False
    uid = state.stage_uid(stage.index)
    arming = effort_segments.open_arming_of(ledger, effort_segments.stage_segment(uid)) if uid else None
    if arming is not None:
        held = effort_segments.open_span_of(arming) is not None
        record = effort_segments.close_arming(
            ledger, arming["arming_id"], act, now, float(Thresholds().effort_divergence_multiple()))
    if not held:
        return record
    ready = state.ready_stages()
    if ready:
        _ensure_stage_arming(state, ready[0], now, act)
    elif not state.plan_stack and state.all_stages_settled():
        if effort_segments.open_arming_of(ledger, effort_segments.CLOSING) is None:
            effort_segments.open_arming(
                ledger, effort_segments.CLOSING, None, effort_segments.SOURCE_UNBUDGETED, now, act)
    elif state.plan_stack and state.all_stages_settled():
        _settle_unfolded_parent(state, act, now)
    return record


def _settle_unfolded_parent(state: SessionState, act: str, now: float) -> None:
    """The parent settles with its last sub-stage, at the same instant, and the interval goes to
    what follows it in its frame: its next ready stage, or closing when it was the last at the top
    plan. A parent that was the last of a sub-plan itself settles in turn."""
    ledger = state.effort_segments
    for depth in range(len(state.plan_stack), 0, -1):
        frame = state.plan_stack[depth - 1]
        outer_path = frame_path(state.plan_stack[:depth - 1])
        uid = state.stage_uids.get(stage_uid_key(outer_path, frame.originating_stage))
        arming = effort_segments.open_arming_of(ledger, effort_segments.stage_segment(uid)) if uid else None
        if arming is not None:
            _close_at_multiple(ledger, arming, act, now)
        passed = {s.index for s in frame.stages
                  if s.outcome.status == StageStatus.PASSED.value or s.index == frame.originating_stage}
        ready = [s for s in frame.stages if s.outcome.status == StageStatus.PENDING.value
                 and all(d in passed for d in s.depends_on)]
        if ready:
            _ensure_stage_arming(state, ready[0], now, act, uid=state.stage_uids.get(
                stage_uid_key(outer_path, ready[0].index)))
            return
        if any(not is_settled(s) and s.index != frame.originating_stage for s in frame.stages):
            return
        if depth == 1 and effort_segments.open_arming_of(ledger, effort_segments.CLOSING) is None:
            effort_segments.open_arming(
                ledger, effort_segments.CLOSING, None, effort_segments.SOURCE_UNBUDGETED, now, act)


def _settle_agent_skipped_stages(state: SessionState) -> None:
    """Settle each skipped stage still holding an open arming: an agent's skip, which has already
    been judged, is settled only once the approval carrying it was not refused."""
    ledger = state.effort_segments
    for stage in state.stages:
        uid = state.stage_uid(stage.index)
        if uid and stage.outcome.status == StageStatus.SKIPPED.value and (
                effort_segments.open_arming_of(ledger, effort_segments.stage_segment(uid))):
            _settle_stage_arming(state, stage, effort_segments.ACT_AGENT_SKIP, _utcnow())


def _second_current_stage_refusal(state: SessionState, candidate: int | None) -> Directive | None:
    """The refusal of a writer of the current stage while another stage is ACTIVE."""
    others = gates.other_active_stages(state, candidate)
    if not others:
        return None
    return Directive(
        False, state.node, "blocked",
        f"stage {others[0]} is ACTIVE: one stage is current at a time; settle it before moving on",
        data={"active_stage": others[0], "active_stages": others},
    )


@contextmanager
def _state_lock(store: StateStore, session_id: str, *, wait_s: float = 2.0):
    """Hold `<root>/<session>.lock`; yields False if it stays busy for `wait_s` or the store has
    no directory, and the caller then does its work unjudged rather than wait on a dead holder."""
    root = _state_dir(store)
    if root is None:
        yield False
        return
    deadline = time.monotonic() + wait_s
    with open(Path(root) / f"{session_id}.lock", "a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def stamp_and_judge(session_id: str, now: float, *, store: StateStore) -> bool:
    """The prompt boundary, for the hook: stamp it, book the gap through the ledger and judge, all
    under the state lock in one save. False when the state does not load as a SessionState, for
    the hook's raw patch to handle. True otherwise, including a failure past the load: nothing was
    saved, the stamp did not move, and the next stamp books the gap once."""
    try:
        with _state_lock(store, session_id) as locked:
            if not locked:
                return True
            try:
                state = store.load(session_id)
            except Exception:
                return False
            _stamp_loaded(state, store, session_id, now)
            store.save(state)
    except Exception:
        return True
    judge_on_parent(session_id, effort_segments.ACT_DESCENDANT_STAMP, store=store, now=now)
    return True


def _fire_floor(thr: Thresholds) -> dict:
    return {
        effort_segments.SCALE_USD: thr.effort_fire_floor_usd(),
        effort_segments.SCALE_MINUTES: thr.effort_fire_floor_minutes(),
    }


def _record_arming_fire(state: SessionState, arming: dict, scales: list[str], *, now: float,
                        store: StateStore | None) -> dict:
    """The arming's fire record, gaining `scales`: one record per arming while it stands open. A
    fire leaves every other stage's budget and the whole-window baselines alone. A ledgered order
    owes the user the fire in every later session too (K3)."""
    actual = effort_segments.arming_actual(arming)
    budget = arming["budget"]
    fire = next((f for f in state.effort_fires
                 if f.get("arming_id") == arming["arming_id"] and f.get("ack") is None), None)
    if fire is None:
        first = scales[0]
        segment = arming["segment"]
        fire = {
            "arming_id": arming["arming_id"],
            "segment": segment,
            "identity": segment.removeprefix(effort_segments.STAGE_PREFIX),
            "title": arming.get("title"),
            "scale": next(name for name, unit in effort.SEGMENT_SCALE.items() if unit == first),
            "scales": [],
            "kind": "ratio",
            "budget": dict(budget),
            "actual": actual[first],
            "estimate": budget[first],
            "multiple": actual[first] / budget[first],
            "threshold_multiple": float(Thresholds().effort_divergence_multiple()),
            "history_len": len(state.history),
            "window_stages": [],
            "ts": now,
            "difficulty_id_at_fire": _difficulty_id(state.difficulty),
            "replaced_baseline": {},
        }
        state.effort_fires.append(fire)
    fire["scales"].extend(scales)
    fire["arming_actual"] = actual
    effort_segments.note_fire(arming, scales, now)
    key = _ledgered_order_key(state)
    if key is not None:
        _flush_order_effort(state, key, store=store)
        at = dt.datetime.now(dt.timezone.utc).isoformat()
        for unit in scales:
            scale = next(name for name, u in effort.SEGMENT_SCALE.items() if u == unit)
            order_approvals.record_effort_fire(
                key, scale=scale,
                detail={"actual": actual[unit], "estimate": budget[unit], "multiple": actual[unit] / budget[unit]},
                at=at,
            )
    return fire


def _judge_act(state: SessionState, act: str, *, store: StateStore | None,
               now: float | None = None) -> dict | None:
    """Judge the current segment at `act`, a member of the declared judging tuples, and return
    the fire it raised. While a migrated window is open only the whole-window rule acts."""
    ledger = state.effort_segments
    if not gates.effort_active(state) or effort_segments.window_arming(ledger) is not None:
        return None
    arming = effort_segments.current_arming(ledger)
    if arming is None:
        return None
    at = now if now is not None else _utcnow()
    thr = Thresholds()
    fire = None
    _budget_migrated_planning(arming)
    if effort_segments.replans_judged_at(arming["segment"], act):
        div = effort.replans_divergence(
            state, thr, cross_session_totals=_effort_cross_totals(state, at, store=store), now=at)
        if div is not None:
            fire = _record_effort_fire(state, div, now=at, store=store)
    scales = effort_segments.scales_to_fire(
        arming, act, float(thr.effort_divergence_multiple()), _fire_floor(thr))
    if scales:
        fire = _record_arming_fire(state, arming, scales, now=at, store=store)
    return fire


def _book_and_judge(state: SessionState, store: StateStore, args, act: str) -> dict | None:
    """Refresh the ledger at `act` and judge; a raised fire is saved even if the act goes no further."""
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    fire = _judge_act(state, act, store=store)
    if fire is not None:
        store.save(state)
    return fire


def _judge_and_refuse(state: SessionState, store: StateStore, args, act: str, what: str) -> Directive | None:
    """`_book_and_judge`, then the open-fire gate: the refusal to return, or None to proceed."""
    _book_and_judge(state, store, args, act)
    efblock = gates.effort_fire_blockers(state)
    _log_gate(state, "effort_fire", efblock, passed=not efblock)
    if not efblock:
        return None
    directive = _effort_fire_refusal(state, what, efblock, store=store)
    store.save(state)
    return directive


def judge_on_parent(session_id: str, act: str, *, store: StateStore, rows: list[dict] | None = None,
                    now: float | None = None) -> None:
    """A linked descendant's act judges its parent's live segment on what it booked. Runs after
    the child's own save and lock release; a busy parent lock leaves this act unjudged on the
    parent's side, for the next one. Fails open: the child's act has already happened."""
    try:
        child = store.load(session_id)
        link = child.parent_link if child is not None else None
        parent_id = link.get("parent_session") if isinstance(link, dict) else None
        if not parent_id:
            return
        with _state_lock(store, parent_id) as locked:
            if not locked:
                return
            parent = store.load(parent_id)
            if parent is None:
                return
            if act == effort_segments.ACT_DESCENDANT_STAMP:
                _stamp_refresh(parent, store, now if now is not None else _utcnow())
            else:
                _refresh_spend(parent, store, rows if rows is not None else cost.read_rows(cost.COST_LOG),
                               parent.plan_path)
            arming = effort_segments.current_arming(parent.effort_segments)
            judged = (effort_segments.ACT_SPAWNED_PLANNER_ACT
                      if arming is not None and arming["segment"] == effort_segments.PLANNING else act)
            _judge_act(parent, judged, store=store, now=now)
            store.save(parent)
    except Exception:
        return


def _stamp_refresh(state: SessionState, store: StateStore, now: float) -> None:
    """A stamp's row refresh, bounded to the spawners active since the session's classify time. A
    migrated session (floor 0.0 would exclude nothing) and an empty ledger book no rows here."""
    ledger = state.effort_segments
    floor = ledger.get("session_floor")
    if not floor or state.effort_migrated or effort_segments.window_arming(ledger) is not None:
        return
    _refresh_spend(state, store, cost.read_rows(cost.COST_LOG), state.plan_path, since=floor, now=now)


def _live_stage_spawned(state: SessionState) -> bool:
    """Whether a child-run interval is open for the current stage."""
    stage = state.active_stage()
    return stage is not None and any(
        run.get("stage_index") == stage.index and not isinstance(run.get("ended"), (int, float))
        for run in state.effort_child_runs if isinstance(run, dict))


def _stamp_loaded(state: SessionState, store: StateStore, session_id: str, now: float) -> None:
    previous = state.last_user_prompt_ts
    state.last_user_prompt_ts = now
    state.user_prompt_count = int(state.user_prompt_count or 0) + 1
    if not isinstance(previous, (int, float)) or now <= previous or state.node == Node.BLOCKED.value:
        return
    root = _state_dir(store)
    runs = state.effort_child_runs
    if root is not None:
        child_runs.close_dead_runs(runs, Path(root), session_id)
    covered = effort.covered_seconds(effort.child_run_intervals(runs, now), previous, now)
    cap = float(Thresholds().substantive_wall_clock_min)
    elapsed = min(max(0.0, (now - previous) - covered) / 60.0, cap)
    if covered > 0:
        def lost(others) -> float:
            others_covered = effort.covered_seconds(effort.child_run_intervals(others, now), previous, now)
            return min(max(0.0, (now - previous) - others_covered) / 60.0, cap) - elapsed

        child_runs.note_gap_loss(runs, previous, now, lost)
    actuals = state.effort_actuals
    actuals[effort.ACTUAL_MINUTES_KEY] = float(actuals.get(effort.ACTUAL_MINUTES_KEY) or 0.0) + elapsed
    effort.book_gap_minutes(state, previous, now, elapsed)
    effort.book_child_runs(state, now)
    _stamp_refresh(state, store, now)
    if not _live_stage_spawned(state):
        _judge_act(state, effort_segments.ACT_STAMP, store=store, now=now)


def _order_customer_id(state: SessionState) -> str:
    path = _replan_baseline_path(state) if state.plan_path else None
    if not path:
        return ""
    try:
        order = _load_prior_plan(state, path, strict=False).meta.order
    except (OSError, PlanError):
        return ""
    return order.customer_id if order is not None else ""


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


def _agent_replans_fire_route(state: SessionState) -> dict | None:
    """The agent's own route for an unacknowledged `replans`-scale fire of a ledgered
    order: a planning-difficulty cycle, not a question to the user. None for every other
    fire (spend/wall-clock stay the user's) and for a session with no user-approved order."""
    if _ledgered_order_key(state) is None:
        return None
    last = gates.open_fire_record(state)
    if last is None or last.get("scale") != effort.SCALE_REPLANS:
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
    cur.lazy = refined.lazy
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


def _judge_order_agreement(state: SessionState, doc) -> list[str]:
    """The order-agreement blockers against `doc`, logged; the one place the gate is judged."""
    blockers = gates.order_agreement_blockers(state, doc)
    _log_gate(state, "order_agreement", blockers, passed=not blockers)
    return blockers


def _order_agreement_refusal(
    state: SessionState, plan_path: str | None, action: str, what: str, *, doc=None,
) -> Directive | None:
    """The refusal Directive when the order-agreement gate blocks `what` against the plan at
    `plan_path` (or the already-loaded `doc`), else None."""
    if doc is None and plan_path:
        try:
            doc = load_plan(plan_path)
        except (OSError, PlanError):
            doc = None
    blockers = _judge_order_agreement(state, doc)
    if not blockers:
        return None
    return Directive(False, state.node, action, f"cannot {what}: order agreement",
                     data={"blockers": blockers})


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


def _effort_fire_escalation_data(state: SessionState) -> dict:
    """The fire-context payload attached to every gates.effort_fire_blockers refusal —
    the open fire plus the identifiers a coordinator needs to call `agentctl
    fire-acknowledge` against the right session. Shared by every refusal site so the
    payload shape never drifts between them."""
    fire = gates.open_fire_record(state) or (state.effort_fires[-1] if state.effort_fires else {})
    return {
        "task_id": state.task_id,
        "session_id": state.session_id,
        "scale": fire.get("scale"),
        "scales": fire.get("scales"),
        "kind": fire.get("kind"),
        "actual": fire.get("actual"),
        "estimate": fire.get("estimate"),
        "multiple": fire.get("multiple"),
        "arming_id": fire.get("arming_id"),
        "identity": fire.get("identity"),
        "title": fire.get("title"),
        "ts": fire.get("ts"),
    }


def _enter_replans_diagnosis(state: SessionState) -> None:
    """Move the session into DIAGNOSING so the agent's planning-difficulty cycle can run."""
    if state.node == Node.EXECUTING.value:
        state.node = transition(state.node, "verify")
    if state.node == Node.VERIFYING.value:
        state.node = transition(state.node, "diagnose")
    elif state.node == Node.PLAN_READY.value:
        state.node = transition(state.node, "review_trigger")
    else:
        return
    opened = _open_difficulty(state)
    fire = gates.open_fire_record(state)
    if opened is not None and fire is not None:
        fire["difficulty_uid"] = opened.uid


def _effort_fire_refusal(state: SessionState, what: str, efblock: list[str], *,
                         store: StateStore | None = None) -> Directive:
    """The refusal `gates.effort_fire_blockers` produces. A replans-scale fire of a ledgered order
    names the agent's planning-difficulty cycle, entered here when `store` lets it persist; a
    linked child's fire goes to its parent as a norm divergence; every other fire is the user's."""
    data = {"blockers": efblock, "effort_fire": _effort_fire_escalation_data(state)}
    route = _agent_replans_fire_route(state)
    if route is None:
        data.update(_fire_directive_figures(state))
        if state.parent_link:
            return Directive(
                False, state.node, "norm_mismatch",
                f"{what} blocked by an unacknowledged effort-divergence fire; return it to the "
                "parent stage as a norm divergence",
                marker="REPLAN", data={**data, "norm_mismatch": True},
            )
        return Directive(
            False, state.node, "fire_acknowledge",
            f"{what} blocked by an unacknowledged effort-divergence fire",
            marker=DIRECTIVE_ESCALATE_TO_USER, data=data,
        )
    data["agent_route"] = route
    if store is not None:
        _enter_replans_diagnosis(state)
    return Directive(
        False, state.node, "planning_difficulty",
        f"{what} blocked by an unacknowledged replans-scale effort-divergence fire; "
        "run the planning-difficulty cycle (" + " → ".join(route["steps"]) + ")",
        marker="OVERCOME-DIFFICULTY", data=data,
    )


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


def _require(store: StateStore, session_id: str) -> SessionState:
    state = store.load(session_id)
    if state is None:
        raise KeyError(f"no session {session_id!r}")
    state.review_rounds = task_accumulator.get(state.task_id)["per_axis_totals"]["review_rounds"]
    return state


def _clear_since_user_decision(state: SessionState, by: str) -> None:
    """A decision the user (not the agent) took restarts the agent self-decision
    backstops: the counters they read are 'since the last user decision'."""
    if (by or "").strip().casefold() == AGENT_ACTOR:
        return
    task_accumulator.clear_since_user_decision(
        state.task_id, session_id=state.session_id, now=_utcnow())


def _replan_class_data(state: SessionState) -> dict:
    """The replan counts by change class — what the effort replans scale counts
    (`operative`) next to what it does not — for a Directive or the status JSON."""
    return {"replan_classes": effort.replan_class_counts(state)}


def _finished_work_data(state: SessionState) -> dict:
    """The finished-work fires of an earlier session and every retier record, for a Directive's data."""
    fires = [f for f in state.effort_fires if f.get("disposition") == EFFORT_FIRE_FINISHED_WORK]
    retiers = [*state.effort_retiers, *(state.effort_segments.get("retier") or [])]
    return {"effort_finished_work": fires, "effort_retiers": retiers} if fires or retiers else {}


def _park_node(state: SessionState) -> None:
    """Move the session to BLOCKED, remembering the node `unblock` returns to. A session
    already BLOCKED keeps the node it was first parked from: recording BLOCKED as its own
    way back would leave `unblock` nothing to restore."""
    if state.node != Node.BLOCKED.value:
        state.blocked_from = state.node
    state.node = Node.BLOCKED.value


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


def _present_plan_transcript(session_id: str) -> Path | None:
    """The newest transcript of the conversation that ran engine session
    `session_id` under any projects root, or None (see
    `engine_session_binding.transcript_for_session`)."""
    return engine_session_binding.transcript_for_session(session_id)


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


EVIDENCE_ROOT_ENV = "AGENTCTL_EVIDENCE_ROOT"


class EvidenceDirError(Exception):
    """Raised when the durable per-stage evidence directory cannot be resolved —
    it would fall under an OS-temp scratch root (exempt_paths.scratch_roots()), or
    grants.validate_add_dir refuses it outright. No fallback search: the caller
    fails loudly rather than silently degrading to a directory nobody granted."""


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


def _refresh_spend(state: SessionState, store: StateStore, rows: list[dict], path: str | None, *,
                   since: float | None = None, now: float | None = None) -> float:
    """`effort.refresh_spend` over the ledger rows of this session AND of its linked
    descendants (effort.refresh_subtree_spend). The descendant scan runs only when a row
    names a spawner session other than this one, so a session whose ledger rows carry no
    foreign `spawner_session` returns exactly the base value.

    `since` narrows the scan to the foreign spawners with a row written at or after it, and
    drops the rows of the others from this pass as well: left in, they would read as this
    session's own rows and lift its watermark past rows it has not booked. The high-water
    marks tolerate the smaller total, and the next unbounded refresh books what is owed.

    A row written before the ledger's `session_floor` (the classify time; 0.0 on a migrated
    ledger, which excludes nothing) is left out of the totals and the watermarks: it was booked
    by the session that held it, or by none.

    The part of what is booked here for a descendant that the order window already holds
    is marked flushed in this session too (`_credit_descendant_order_flush`)."""
    floor = state.effort_segments.get("session_floor")
    if floor:
        rows = [row for row in rows if effort.row_written_at(row) >= floor]
    foreign = {
        spawner for row in rows
        if isinstance(spawner := row.get("spawner_session"), str) and spawner != state.session_id
    }
    if since is not None:
        recent = {row.get("spawner_session") for row in rows if effort.row_written_at(row) >= since}
        rows = [row for row in rows if row.get("spawner_session") not in foreign - recent]
        foreign &= recent
    chains = _descendant_sessions(store, state.session_id, foreign)
    delta, booked = effort.refresh_subtree_spend(state, rows, chains.keys(), path)
    if booked:
        key = _ledgered_order_key(state)
        if key is not None:
            _seed_linked_flush_baseline(state, store=store)
            if state.order_effort_flushed is not None:
                _credit_descendant_order_flush(state, key, chains, booked)
    effort.book_child_runs(state, now if now is not None else _utcnow())
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


RESOLUTION_ASK_BLOCKER_PREFIX = "resolution ask: "


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


def _linked_children(store: StateStore, parent: str) -> list[SessionState]:
    """The loadable sessions whose `parent_link` names `parent`. Read-only: a state file
    that cannot be loaded is skipped, never repaired or rewritten."""
    list_ids = getattr(store, "session_ids", None)
    loaded: dict = {}
    return [loaded[session_id] for session_id in (list_ids() if list_ids is not None else [])
            if _parent_session_of(store, session_id, loaded) == parent]


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
    "order-raise", "order-dispose", "order-submit", "order-agree", "order-challenge-dismiss",
    "order-list", "classify", "plan",
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
              "accept", "question-enumerate-escape", "fire-acknowledge", "accept-protected-grant",
              "order-agree", "order-challenge-dismiss")),
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
    ("title", ("classify",), "one-line task label capped at 48 characters, never a file"),
    ("id", ("ledger-add", "ledger-candidate", "ledger-dispose", "question-raise",
            "question-research", "question-dispose", "question-rebind", "question-retire",
            "question-candidate-dispose", "order-raise", "order-dispose", "accept-protected-grant",
            "order-challenge-dismiss"),
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
              "question-rebind", "question-raise", "present-plan", "order-dispose",
              "order-submit"),
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
    ("transcript", ("plan-review",), "path to the reviewer's transcript — a file path, not narrative"),
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

