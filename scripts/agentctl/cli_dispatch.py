"""Stage dispatch and permission/grant subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from lib import widening_targets, kind_baselines, transcript_stops
from . import (
    continuations,
    gates,
    grants as _grants,
    order_approvals,
    permissions,
    plan_resources,
    resource_estimation,
    resources as _resources,
    runtime_host,
    workspace_cleanup,
    child_runs,
    effort,
    effort_segments,
    config as agentctl_config,
)
from .cli_common import _cost_rows, _judge_act, _judge_and_refuse, _refresh_spend, judge_on_parent
from .directive import Directive
from .dispatch import (
    CHILD_EXHAUSTED,
    CHILD_INFRA_FAILURE,
    Runner,
    dispatch_stage,
    permission_request_action,
    parse_marker,
    LABEL_DECORATION,
)
from .plan import (
    order_digest,
    _venue_for,
    PlanError,
    entries_grants_sha256,
    grants_sha256,
    load_plan,
    stage_reattest_digest,
    _derive_stage_grants,
)
from .state import (
    AGENT_ACTOR,
    CheckVenue,
    PermissionRequest,
    CheckKind,
    CriterionType,
    Declaration,
    Node,
    ReattestStash,
    SessionState,
    Stage,
    StageStatus,
)
from .store import StateStore
import hashlib
import re
import shlex
from .machine import transition
from .cli_common import (
    _digest,
    _plan_file_sha256,
    _load_prior_plan,
    _ledgered_order_key,
    _order_customer_id,
    _difficulty_cap_directive,
    _log_gate,
    _verify_command_result,
    _resolve_or_refuse,
    _effort_fire_refusal,
    _landed_check_result,
    _require,
    _park_node,
    _fail_stage_into_diagnosing,
    _verify_child_escalation,
    _open_norm_mismatch,
    EvidenceDirError,
    _evidence_session_dir,
    _utcnow,
    _state_dir,
)


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


def _open_effort_fire(state: SessionState) -> list[dict]:
    """The open spend/wall-clock fire markers owed to the user for this session's order."""
    key = _ledgered_order_key(state)
    return list(order_approvals.get(key)["open_effort_fires"]) if key else []


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
    # The stamp and a descendant's act book and judge on the file during the spawn.
    state.effort_segments = fresh.effort_segments
    state.effort_fires = fresh.effort_fires
    state.effort_subtree_seen = fresh.effort_subtree_seen
    state.effort_spend_seen = fresh.effort_spend_seen
    state.effort_actuals = fresh.effort_actuals
    state.order_effort_flushed = fresh.order_effort_flushed
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
        effort.book_child_runs(state, _utcnow())
        current = effort_segments.current_arming(state.effort_segments)
        if current is not None and current["segment"] == effort_segments.PLANNING:
            _refresh_spend(state, store, _cost_rows(argparse.Namespace()), state.plan_path)
        _judge_act(state, effort_segments.ACT_SPAWNED_PLANNER_RUN_END, store=store)
    else:
        state.effort_child_runs[:] = [r for r in state.effort_child_runs if r is not run]
        # The hook left the part of each prompt gap inside this run to it (and noted it on
        # the run in the file); the run is gone, so book it as the hook would have.
        lost = child_runs.gap_loss_min(fresh.effort_child_runs if fresh is not None else (), run)
        if lost:
            actuals = state.effort_actuals
            actuals[effort.ACTUAL_MINUTES_KEY] = float(
                actuals.get(effort.ACTUAL_MINUTES_KEY) or 0.0) + lost
            effort.book_gap_minutes(state, _utcnow(), _utcnow(), lost, effort.ORIGIN_RUN_LOST)
    store.save(state)


def cmd_dispatch(args, *, store: StateStore, runner: Runner | None = None,
                 perm_checker=None) -> Directive:
    directive = _dispatch(args, store=store, runner=runner, perm_checker=perm_checker)
    judge_on_parent(args.session, effort_segments.ACT_DESCENDANT_DISPATCH, store=store, rows=_cost_rows(args))
    return directive


def _dispatch(args, *, store: StateStore, runner: Runner | None = None,
              perm_checker=None) -> Directive:
    state = _require(store, args.session)
    _fire_refusal = _judge_and_refuse(state, store, args, effort_segments.ACT_DISPATCH, "dispatch")
    if _fire_refusal is not None:
        return _fire_refusal
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
    _dispatch_started = dt.datetime.now().astimezone().replace(microsecond=0)
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
    child_run = None if dry_run else _begin_child_run(store, state, stage)
    result = None
    try:
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
    finally:
        if child_run is None:
            if not dry_run:
                _carry_hook_stamps(store, state)
        else:
            transcript = _parse_transcript_path(getattr(result, "stderr", "") or "")
            _end_child_run(store, state, child_run, Path(transcript).stem if transcript else None)
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
        # spawn-specialist refused at the recursion cap or on the norm-loop rule
        # (both exit 3, told apart by the LOOP_DETECTED stdout line) — a structural
        # blocker, not a stage result. Park at BLOCKED and escalate; never report
        # success. This must win before marker routing (a refusal carries no valid
        # marker).
        loop_line = _loop_detected_line(result)
        _park_node(state)
        state.log(
            "dispatch_refused", stage=stage.index,
            reason="norm-loop" if loop_line else "recursion-cap",
        )
        store.save(state)
        data = {"returncode": result.returncode, "stderr": result.stderr}
        if loop_line:
            data["loop_detected"] = loop_line
            message = (
                f"stage {stage.index} spawn refused: its norm is already on the delegation "
                f"lineage (norm-loop) — escalate to the user: {loop_line}"
            )
        else:
            message = f"stage {stage.index} spawn refused: recursion cap reached — escalate to the user"
        return Directive(False, state.node, "escalate", message, marker="ESCALATE", data=data)

    if _is_child_outside_project_refusal(result):
        # The delivery venue loads no project settings while this session runs in a
        # project that registers hooks: a structural blocker fixed in the venue, so
        # there is no override here. Park and escalate before marker routing.
        _park_node(state)
        state.log("dispatch_refused", stage=stage.index, reason="child-outside-project")
        store.save(state)
        return Directive(
            False, state.node, "escalate",
            f"stage {stage.index} spawn refused: its venue {child_cwd} is in no project while this "
            "session runs in one that registers hooks, so the child would run without them — "
            "escalate to the user; remedy: create .claude/settings.local.json ({} suffices) at the "
            "git root of that venue (the main checkout's root for a linked worktree)",
            marker="ESCALATE", data={"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
        )

    # The marker wins over the returncode: a specialist may exit 0 with CLARIFY, or
    # non-zero with a valid escalation marker. spawn-specialist.py has already parsed
    # and (if needed) MALFORMED-wrapped the marker onto stdout.
    marker, body = parse_marker(result.stdout)
    base = {
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        "evidence_dir": evidence_dir,
    }

    grant_widening = _is_grant_widening_refusal(result)
    # The direct form is the wrapper refusing before any child launched; the relayed
    # form is a child that DID run and handed the block back, so only it has a
    # post-launch footprint (settings drift, transcript denials, a spent once-grant).
    child_launched = not (grant_widening and result.returncode == _GRANT_WIDENING_EXIT)

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
    resource_estimation.record_stage_estimate(
        state, stage, state_root=getattr(store, "root", None), new_misses=state.planning_misses[_misses_before:],
        since=_dispatch_started)
    if child_launched and marker not in (CHILD_INFRA_FAILURE, CHILD_EXHAUSTED):
        # A transient/resource marker means the child never meaningfully ran
        # against the once-scoped grant this launch was meant to spend — the
        # launch didn't count, so consuming a single-use grant here would
        # burn it for a re-dispatch that gets no benefit from having used it.
        _consume_once_grants(state, stage.index)

    if grant_widening:
        # A delegation that would widen authority beyond what the session was
        # handed is a request for authority, not a defect of the materialized
        # grants: park it for the user's decision. No materialization_defects row
        # for the refusal itself, no self_grant (the engine never widens on its
        # own say-so), no DIAGNOSING. `project`/`global` are not offered: they
        # cannot materialize the per-rule runtime grant the re-delegation counts.
        rules = _parse_rule_lines(result.stdout)
        rules += [
            f"vcs_ref {r.op} {r.remote}:{r.ref}" for r in _refusal_vcs_resources(result.stdout)
        ]
        sha_match = _HANDOFF_SHA_LINE_RE.search(result.stdout)
        action = (
            f"grant-widening refusal on stage {stage.index}: delegation holds "
            f"{len(rules)} grant(s) beyond the session's handoff: {', '.join(rules)}"
        )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=result.stdout
        )
        state.log("dispatch_refused", stage=stage.index, reason="grant-widening", rules=rules)
        store.save(state)
        return Directive(
            True, state.node, "ask_user_permission",
            f"stage {stage.index} delegation refused: it would widen grants beyond the "
            "authority this session was handed — the user decides",
            marker="PERMISSION-REQUEST",
            data={
                **base, "action": action,
                "options": ["once", "stage", "deny"],
                "grant_widening": {
                    "rules": rules,
                    "handoff_sha256": sha_match.group(1) if sha_match else None,
                },
            },
        )

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
        mismatch, unverified = _child_norm_mismatch(state, store, stage, result.stderr)
        if mismatch is not None:
            standing = gates.escalation_counterpart(
                state, mismatch["child_uid"], mismatch["child_session"])
            if standing is not None:
                return _repeat_norm_mismatch(state, store, stage, mismatch, base, standing, body)
            return _open_norm_mismatch(state, store, stage, mismatch, base)
        if unverified:
            state.log("norm_mismatch_unverified", stage=stage.index, reason=unverified)
        store.save(state)
        return Directive(
            False, state.node, "replan",
            f"stage {stage.index} proposes a plan-level revision",
            marker="REPLAN", data={**base, "reason": body},
        )
    if marker == "PLAN-READY":
        store.save(state)
        return Directive(
            True, state.node, "await_plan_approval",
            f"stage {stage.index} returned a fresh plan — HARD GATE, get explicit user approval",
            marker="PLAN-READY", data=base,
        )

    def _route_permission_request(action: str, extra: dict | None = None) -> Directive:
        data_base = {**base, **(extra or {})}
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
                data=extra,
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
                f"{[_resources.resource_to_dict(r) for r in self_grant_resources]!r} "
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
                action=action, stage_index=stage.index, raw=action
            )
            state.log(
                "permission_self_grant", stage=stage.index, action=action, rule=rule_line,
                resources=[_resources.resource_to_dict(r) for r in self_grant_resources],
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
                    **data_base, "action": action, "rule": rule_line,
                    "resources": [_resources.resource_to_dict(r) for r in self_grant_resources],
                },
            )
        state.permission_request = PermissionRequest(
            action=action, stage_index=stage.index, raw=action
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
        _ask_data = {**data_base, "action": action, "options": ["once", "stage", "project", "global", "deny"]}
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
    if marker == "PERMISSION-REQUEST":
        return _route_permission_request(permission_request_action(result.stdout) or "")
    if marker == "INCOMPLETE":
        embedded = permission_request_action(result.stdout)
        if embedded is not None:
            return _route_permission_request(embedded, {"incomplete_summary": body})
        store.save(state)
        return Directive(
            False, state.node, "decide_incomplete",
            f"stage {stage.index} returned INCOMPLETE — re-spawn / ask / accept",
            marker="INCOMPLETE", data={**base, "reason": body},
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
    if marker == "LOOP_DETECTED":
        # The child reports its task re-frames an ancestor's: routed like ESCALATE
        # (park BLOCKED, the user decides), with the report carried on the directive.
        return _park_blocked(
            state, store, stage, marker, {**base, "loop_detected": f"LOOP_DETECTED: {body}".strip()},
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
        if _open_effort_fire(state) or gates.effort_fire_blockers(state):
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
            self_approved.extend(self_coverage.resources)
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
        if by.casefold() != AGENT_ACTOR:
            for resource in _refusal_vcs_resources(req.raw):
                new_entries.append({
                    "resource": _resources.resource_to_dict(resource), "provenance": "runtime",
                    "consumed": False, "stage_title": stage.title, "scope": scope,
                    "granted_by": by,
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


def cmd_workspace_register(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record a workspace this task created so `resolve` may clean it up (see
    workspace_cleanup.py). Session-keyed, or --pending for a workspace made before any
    engine session exists (enter-task.sh): the engine binds a pending record at resolve
    time by the resolve cwd / the plan's delivery_worktree. Needs no session state, so it
    works before `classify`."""
    kind = getattr(args, "kind", None)
    try:
        if getattr(args, "pending", False):
            record = workspace_cleanup.register_pending(
                args.path, kind=kind, backend=args.ws_backend, name=args.ws_name)
            scope = "pending"
        elif args.session:
            record = workspace_cleanup.register(
                args.session, args.path, kind=kind, backend=args.ws_backend, name=args.ws_name)
            scope = "session"
        else:
            return Directive(False, "n/a", "noop",
                             "workspace-register needs --session (or CLAUDE_CODE_SESSION_ID) "
                             "or --pending")
    except (OSError, ValueError) as exc:
        return Directive(False, "n/a", "noop", str(exc))
    return Directive(True, "n/a", "workspace_registered", f"{record}\n",
                     data={"record": str(record), "scope": scope, "path": args.path})


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

    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value)
    resource_set = plan_resources.resource_set_from_entries(
        all_entries, stage, venue, stage_branch_resources(state, stage_index)).to_dict()
    data = {
        "stage": stage_index,
        "executor": stage.actor.executor,
        "grants": all_entries,
        "resources": resource_set["resources"],
        "rule_patterns": resource_set["rule_patterns"],
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


def register(sub, add):
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
    sp = add("workspace-register"); sp.add_argument("path")
    sp.add_argument("--kind", choices=workspace_cleanup.KINDS, default=None,
                    help="workspace kind; inferred from --backend when omitted "
                         "(git or none -> git-worktree, any other backend -> mount)")
    sp.add_argument("--backend", dest="ws_backend", default=None,
                    help="workspace backend name (registry.sh), used for mount teardown")
    sp.add_argument("--name", dest="ws_name", default=None,
                    help="mount name the backend teardown is keyed by")
    sp.add_argument("--pending", action="store_true",
                    help="no engine session exists yet (enter-task.sh): record a pending "
                         "binding the engine resolves at `resolve` time by the resolve cwd")
    # Optional: the harness-session auto-injection appends it; --pending ignores it.
    sp.add_argument("--session", required=False, default=None, help=argparse.SUPPRESS)
    sp = add("grant-stats"); sp.add_argument("--session", required=True)
    sp.add_argument("--json", action="store_true")
