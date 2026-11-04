"""Final verification, resolve and reject subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import datetime as dt
import time
from . import (
    cost,
    effort,
    enumerate_sidecar,
    gates,
    plugins,
    solved_marker,
    task_accumulator,
    workspace_cleanup,
    check_cache,
    delivery_channel,
    plan_resources,
)
from .config import Thresholds
from .directive import Directive
from .dispatch import Runner, subprocess_runner
from .machine import transition
from .state import (
    CriterionType,
    Declaration,
    is_skipped,
    GateRecord,
    Node,
    RESOLUTION_ASK_MARKER,
    Stage,
    StageStatus,
    CheckKind,
    Criterion,
    LANDED_GIT_ERROR_EXIT,
    SessionState,
    landed_containment_script,
)
from .store import StateStore
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from lib import ask_evidence, transcript_turns
from .cli_common import (
    REPO_ROOT,
    _VALID_QUALITY_RATINGS,
    _effort_cross_totals,
    _flush_order_effort,
    _record_effort_fire,
    _open_difficulty,
    _difficulty_cap_directive,
    _BACKSTOP_CROSSED_EVENT,
    _log_gate,
    _run_check,
    _VenueSnapshot,
    _venue_tree_identity,
    _check_definition_digest,
    _cached_check_hit,
    _resolve_or_refuse,
    _refuse_missing_cwd,
    _diagnose_effort_divergence,
    _landed_check_result,
    _require,
    _finished_work_fire,
    _finished_work_data,
    _present_plan_transcript,
    _cost_rows,
    _refresh_spend,
    _utcnow,
    _effort_now,
    _write_check_log,
    RESOLUTION_ASK_BLOCKER_PREFIX,
    _linked_children,
)


# Per-task quality ledger (quality-regression-tracking): one row per resolved
# task, stamped with the instructions-repo HEAD so a quality drop can be
# correlated back to an instruction-commit range. Same fixed-path/append-only
# idiom as ~/.local/log/claude-spawn-costs.jsonl (spawn-specialist.py).
TASK_QUALITY_LOG = Path.home() / ".local" / "log" / "claude-task-quality.jsonl"


_GIT_HEAD_TIMEOUT_S = 5


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
            "investigate -> critique -> replan — a replan, including a no_change one that "
            "counts toward no budget, re-arms verification) before re-running verify-final",
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
    renorm_block = gates.pending_renorm_final_blockers(state)
    _log_gate(state, "renorm_final", renorm_block, passed=not renorm_block)
    if renorm_block:
        return Directive(False, state.node, "renorm",
                         "not ready for resolution: a normative difficulty's norm edit is "
                         "still owed", data={"blockers": renorm_block})
    orphan_block = gates.escalation_orphan_blockers(
        gates.escalation_orphans(state, _linked_children(store, state.session_id)))
    _log_gate(state, "escalation_orphans", orphan_block, passed=not orphan_block)
    if orphan_block:
        return Directive(False, state.node, "adopt_escalation",
                         "not ready for resolution: a linked child's escalation of a stage "
                         "norm is not adopted", data={"blockers": orphan_block})
    state.pending_renorms = gates.open_renorms(state)
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
    _refresh_spend(state, store, _cost_rows(args), state.plan_path)
    _flush_order_effort(state, store=store)
    _now = _effort_now(store, state)
    div = effort.divergence(state, cross_session_totals=_effort_cross_totals(state, _now, store=store), now=_now)
    if effort.armed(state):
        _cross = _effort_cross_totals(state, _now, store=store)
        _ro_delta = effort.effective_deltas(state, cross_session_totals=_cross, now=_now)
        _ro_comparand = effort.comparands(state, Thresholds())
        for _ro_scale in effort.RECORD_ONLY_SCALES:
            if _ro_comparand[_ro_scale] > 0 and _ro_delta[_ro_scale] >= _ro_comparand[_ro_scale]:
                effort.record_crossing(
                    state, _ro_scale, actual=_ro_delta[_ro_scale],
                    estimate=_ro_comparand[_ro_scale], now=_utcnow(),
                )
    # Final-gate execution: see _evaluate_final_checks.
    failures, refusal_directive = _evaluate_final_checks(state, store, runner, div)
    if refusal_directive is not None:
        return refusal_directive
    if failures:
        # A failing final_check is a difficulty (actual result diverges from the
        # plan's declared image) exactly like a failed stage — route into the same
        # DIAGNOSING cycle (record_result's failed-stage path, above) rather than
        # stranding the session at VERIFYING with no reachable resolution: from
        # VERIFYING, declare/investigate/critique all refuse ("difficulty commands
        # run only in the DIAGNOSING cycle"), and only `reset --force` escaped.
        state.node = transition(state.node, "diagnose")  # VERIFYING -> DIAGNOSING
        opened = _open_difficulty(state)
        data = {"failures": failures}
        if div is not None and gates.effort_active(state):
            # Already entering DIAGNOSING for the failure — attach the divergence
            # instead of re-transitioning or opening a second Difficulty, mirroring
            # record_result's failed branch (still honoring divergence()'s CALLER
            # OBLIGATION: record the fire so it doesn't re-trip).
            now = _utcnow()
            data["effort_divergence"] = _record_effort_fire(state, div, now=now, store=store)
        store.save(state)
        if opened is None:
            return _difficulty_cap_directive(
                state, "final verification command(s) failed", data)
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
    if _finished_work_fire(state, div, store=store) is None and div is not None and gates.effort_active(state):
        now = _utcnow()
        fire = _record_effort_fire(state, div, now=now, store=store)
        return _diagnose_effort_divergence(state, store, div, fire)

    # Compute whole-plan cost rollup from already-attributed stage outcomes.
    # No second log read — record-result already stored the costs on each Outcome.
    rollup = cost.rollup_plan([], state.plan_path, state.stages)
    state.cost = rollup
    state.node = transition(state.node, "final")  # VERIFYING -> RESOLUTION
    state.resolution = GateRecord("resolution", armed=True, passed=False)
    state.log("verify_final", at=time.time())
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
        },
        "resolution_ask_marker": RESOLUTION_ASK_MARKER,
    }
    # Bypass visibility: verify-final never returns a clean bill while any acceptance
    # pass skipped a genuine judge verdict (kill switch / override) — the bypasses are
    # surfaced verbatim so the resolution decision is made with them in view, never
    # silently. A later passing review does not clear them.
    detail = f"all stages passed; resolution gate armed — {kind}"
    if gates.resolution_ask_gate_active(state):
        detail += (
            f"; the resolution AskUserQuestion that carries the 1-5 rating must embed the "
            f"literal marker {RESOLUTION_ASK_MARKER!r} in an option label or description "
            f"and be asked AFTER this verify-final — resolve refuses a rating without an "
            f"answered marked ask"
        )
    bypasses = _judge_bypassed_surface(state)
    if bypasses:
        data["judge_bypassed"] = bypasses
        detail += f"; WARNING: {len(bypasses)} acceptance judge bypass(es) recorded (see judge_bypassed)"
    acceptance_bypass = _acceptance_bypass_surface(state)
    if acceptance_bypass is not None:
        data["acceptance_bypass"] = acceptance_bypass
        detail += "; WARNING: acceptance recorded via bypass, not judge corroboration (see acceptance_bypass)"
    detail = _disclose_backstop_crossings(state, data, detail)
    detail = _disclose_finished_work(state, data, detail)
    detail = _disclose_agent_plan_conversions(state, data, detail)
    detail = _disclose_landed_content_waivers(state, data, detail)
    return Directive(True, state.node, "await_user_confirmation", detail, data=data)


def cmd_resolve(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    state = _require(store, args.session)
    # plugin gates fold into resolve (not verify_final) so a plugin can let the
    # session reach RESOLUTION — where its publish-directive fires — yet still
    # block the final resolve until its sub-condition is met.
    blockers = (gates.blockers(state, "resolution") + plugins.plugin_gate_blockers(state, "resolution")
                + gates.pending_renorm_final_blockers(state))
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
    landing_blockers, landing = _landing_gate(state, args, runner)
    blockers = blockers + landing_blockers
    ask_blockers, resolution_ask = _resolution_ask_gate(state, args, channel_ok=not blockers)
    blockers = blockers + ask_blockers
    _log_gate(state, "resolution", blockers, passed=not blockers)
    if blockers:
        return Directive(False, state.node, "fix_stages", "cannot resolve", data={"blockers": blockers})
    state.resolution = GateRecord("resolution", armed=True, passed=True, by=args.by)
    state.pending_renorms = gates.open_renorms(state)
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
    state.log("landing_gate", waiver=landing["waiver"], waiver_source=landing["waiver_source"],
              override=landing["override"])
    if resolution_ask["override"] is not None or resolution_ask["unverifiable"] is not None:
        state.log("resolution_ask_gate", override=resolution_ask["override"],
                  unverifiable=resolution_ask["unverifiable"])
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
    _now = _effort_now(store, state)
    _effort_ratios = [v for v in effort.ratios(state, now=_now).values() if v is not None]
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
        "effort_actual": effort.deltas(state, now=_now),
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
        "landing_waiver": landing["waiver"],
        "landing_waiver_source": landing["waiver_source"],
        "landing_gate_override": landing["override"],
        "resolution_ask_gate_override": resolution_ask["override"],
        "resolution_ask_unverifiable": resolution_ask["unverifiable"],
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
    # Same shape as the marker stamp: resolution is already saved, so a cleanup fault
    # is reported, never allowed to undo it. Runs last of the post-resolve effects that
    # act on the working directory, because it may remove the very directory the engine
    # was invoked from.
    try:
        cleanup_report = workspace_cleanup.run(
            state.session_id, cwd=workspace_cleanup.current_dir(),
            delivery_worktree=state.delivery_worktree)
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        cleanup_report = {"enabled": True, "dry_run": False, "removed": [],
                          "would_remove": [], "kept": [], "error": str(exc)}
    detail = "task resolved"
    data = {"cost": cost_surface, "quality": quality_row, "solved_marker": marker_status,
            "workspace_cleanup": cleanup_report}
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
    detail = _disclose_backstop_crossings(state, data, detail)
    detail = _disclose_finished_work(state, data, detail)
    detail = _disclose_agent_plan_conversions(state, data, detail)
    detail = _disclose_landed_content_waivers(state, data, detail)
    if landing["override"] is not None:
        data["landing_gate_override"] = landing["override"]
        detail += (f" (landing gate overridden by AGENTCTL_LANDING_GATE={landing['override']}; "
                   "see landing_gate_override)")
    if resolution_ask["override"] is not None:
        data["resolution_ask_gate_override"] = resolution_ask["override"]
        detail += (f" (resolution-ask gate overridden by AGENTCTL_RESOLUTION_ASK_GATE="
                   f"{resolution_ask['override']}; see resolution_ask_gate_override)")
    if resolution_ask["unverifiable"] is not None:
        data["resolution_ask_unverifiable"] = resolution_ask["unverifiable"]
        detail += " (resolution ask unverifiable, escape used; see resolution_ask_unverifiable)"
    # The crossings are now disclosed and accepted: drop exactly those entries from the task's
    # record, so a reopened task or a later task sharing the id does not disclose them again.
    # Fail-open like the effects above: a fault here is reported, never allowed to lose the
    # disclosure; the entries then stay on record and are disclosed once more.
    try:
        task_accumulator.discard_crossings(
            state.task_id, data["agent_backstop_crossings"], now=_utcnow())
        data["agent_backstop_crossings_cleared"] = True
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        data["agent_backstop_crossings_cleared"] = False
        data["agent_backstop_crossings_clear_error"] = str(exc)
        detail += (" (the task's backstop crossing record could not be cleared: "
                   f"{exc}; see agent_backstop_crossings_clear_error)")
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
                target = state.stage(int(idx))
            except KeyError:
                return Directive(
                    False, state.node, "noop",
                    f"reject --stage {idx} does not exist in the plan",
                )
            if is_skipped(target):
                return Directive(
                    False, state.node, "noop",
                    f"reject --stage {idx} names a SKIPPED stage; a declined optional stage "
                    "is never re-opened (R8: SKIPPED is settled and is never dispatched)",
                )
            targets.append(target)
    else:
        live = [s for s in state.stages if not is_skipped(s)]
        if not live:
            return Directive(False, state.node, "noop", "reject has no stages to re-open")
        targets = [max(live, key=lambda s: s.index)]  # default: the final non-declined stage
    state.node = transition(state.node, "reject")  # RESOLUTION -> DIAGNOSING
    for s in targets:
        s.outcome.status = StageStatus.FAILED.value
    state.current_stage = None
    # Seed the difficulty record with the rejection so the reason is durably
    # captured; the coordinator refines it through declare -> investigate -> critique
    # (which the difficulty_blockers gate still requires complete before replan).
    opened = _open_difficulty(state, declaration=Declaration(
        expected=state.overall_done_criterion or "delivery matches the user's approved intent",
        actual=reason,
        mismatch="user rejected the delivery at the resolution gate (delivered != approved intent)",
    ))
    idxs = [s.index for s in targets]
    rejecter = (getattr(args, "by", None) or "").strip()
    state.log("reject", reason=reason, stages=idxs, **({"by": rejecter} if rejecter else {}))
    store.save(state)
    if opened is None:
        return _difficulty_cap_directive(
            state, f"delivery rejected: {reason}; stage(s) {idxs} re-opened as FAILED",
            {"rejected_stages": idxs, "reason": reason})
    return Directive(
        False, state.node, "declare",
        f"delivery rejected: {reason}; stage(s) {idxs} re-opened as FAILED. Work the "
        "difficulty (declare -> investigate -> critique), then replan.",
        marker="OVERCOME-DIFFICULTY",
        data={"rejected_stages": idxs, "reason": reason},
    )


def register(sub, add):
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
    sp.add_argument("--landing-waiver", dest="landing_waiver", default=None,
                    help="reason this task has no landing to assert; accepted only when the "
                         "plan has no delivery_worktree (a git delivery venue declares its "
                         "waiver in the plan)")
    sp.add_argument("--resolution-ask-unverifiable", dest="resolution_ask_unverifiable",
                    default=None,
                    help="why the session transcript is unlocatable or unreadable, so the "
                         "answered [resolution-ask] AskUserQuestion cannot be verified; "
                         "refused whenever the transcript is readable")
    sp.add_argument("--cost-log", dest="cost_log", default=None,
                    help="override cost log path for tests (defaults to cost.COST_LOG); "
                         "read to derive the realized budget_tiers for the quality row")
    sp = add("reject"); sp.add_argument("--session", required=True)
    sp.add_argument("--by", default=None, type=str.strip,
                    help="who rejected the delivery — a name, not a narrative; recorded on the "
                         "reject row (a `mandate:<id>` rejection does not clear the since-user counters)")
    sp.add_argument("--reason", required=True,
                    help="the intent mismatch the user named when rejecting the delivery "
                         "(seeds the difficulty record)")
    sp.add_argument("--stage", dest="stage", action="append", default=None, type=int,
                    help="plan stage index to re-open as FAILED (repeatable; "
                         "defaults to the final stage so a reject is never a no-op)")
