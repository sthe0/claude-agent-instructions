"""Stage dispatch and permission/grant subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from lib import widening_targets
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
)
from .directive import Directive
from .dispatch import (
    CHILD_EXHAUSTED,
    CHILD_INFRA_FAILURE,
    Runner,
    dispatch_stage,
    permission_request_action,
    parse_marker,
)
from .plan import order_digest, _venue_for
from .state import AGENT_ACTOR, CheckVenue, PermissionRequest
from .store import StateStore
from .cli_common import _refusal_vcs_resources, stage_branch_resources
from .cli_common import (
    _digest,
    _bash_rule_command_from_call,
    _plan_file_sha256,
    _open_effort_fire,
    _record_customer_stage_grants,
    _log_gate,
    _effort_fire_refusal,
    _is_recursion_refusal,
    _is_child_outside_project_refusal,
    _GRANT_WIDENING_EXIT,
    _HANDOFF_SHA_LINE_RE,
    _loop_detected_line,
    _is_grant_widening_refusal,
    _parse_rule_lines,
    _require,
    _park_node,
    _park_blocked,
    _continuation_worktree,
    _try_reattest,
    _EFFORT_BY_COST_TIER,
    _dispatch_project_settings_path,
    _parse_add_dir_spec,
    _parse_transcript_path,
    _parse_rule_line,
    _rule_line_to_call,
    _parse_resource_line,
    _parse_resource_spec,
    _self_grant_resources_for_rule,
    _stage_grant_entries,
    _stage_would_derive_grants,
    _effective_stage_grants,
    _effective_stage_grants_for_self_grant,
    _consume_once_grants,
    _classify_transcript_denials,
    _diagnose_materialization_defect,
    _child_norm_mismatch,
    _open_norm_mismatch,
    _repeat_norm_mismatch,
    _covers_resource,
    EvidenceDirError,
    evidence_dir_for,
    _utcnow,
    _begin_child_run,
    _carry_hook_stamps,
    _end_child_run,
)


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
