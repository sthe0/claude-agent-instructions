"""Review and acceptance subcommands.

Moved verbatim from cli.py; see cli.py for the command table."""
from __future__ import annotations

import json
import os
import shlex
from . import advisor, gates, protected_actions
from .directive import Directive
from .dispatch import Runner
from .plan import (
    PlanError,
    acceptance_requirement_bindings,
    change_tier,
    grants_sha256,
    load_plan,
    load_plan_with_digest,
    is_unit_id,
    pair_part_tokens,
    parse_pair,
    parse_unit,
    part_digest_map,
    plan_meta_digest,
    plan_interface_digests,
    plan_stage_digests,
    review_ids,
)
from .render import render_plan_md, render_stages_md
from .state import (
    AcceptanceBypass,
    AcceptanceReview,
    CodeReview,
    CONCERN_OPEN,
    PlanReview,
    plan_review_concern_ids,
    plan_review_pair_scope,
    plan_review_scope_stage_index,
    ProtectedGrantAcceptance,
    RiskAcceptance,
    StageReview,
)
from .store import StateStore
from .cli_common import (
    _digest,
    _plan_file_sha256,
    _plan_refs_problem,
    _plan_file_bytes,
    _observation_sha256,
    _normalize_reviewer_token,
    _record_stage_review,
    _record_code_review,
    _crossing_data,
    _settle_override_decision,
    _plan_review_override_refusal,
    _review_vouch,
    _record_first_thinker_verdict,
    _review_baseline,
    _log_gate,
    _run_check,
    _require,
    _count_review_round,
    _record_review_questions,
    _note_round_release,
    _ledger_scope,
    _parse_review_concerns,
    _parse_review_questions,
    _question_data,
    _changed_parts,
    _plan_concern_severity,
    _stamp_concern_plan,
    _commit_concern_ledger,
    _log_concerns_downgraded,
    _cmd_plan_review_pair,
    TOPOLOGICAL_COMPOSITION_REVIEWER,
    _is_counted_review,
    _next_record_seq,
    _stamp_whole_plan_baseline,
    _count_plan_review_round,
    _count_review_act,
    _evaluate_review_trigger,
    _pair_ledger_groups,
    _ledger_concern,
    _prior_row_diff,
    _walk_target,
    _parse_verdicts,
)


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
    if getattr(args, "escalate_pairs", False):
        return Directive(
            False, state.node, "noop",
            "--escalate-pairs belongs to a unit review (--scope topo:unit:<n>), "
            f"not to scope {scope or 'whole-plan'!r}")
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
    question_texts, question_error = _parse_review_questions(
        list(getattr(args, "customer_questions", None) or []))
    if question_texts is None:
        return Directive(False, state.node, "noop", question_error)
    # An override is the USER's escape from a reviewer's `revise` deadlock — the
    # reviewer who issued the blocking verdict cannot override themselves. Checked
    # here, before the record is overwritten and the prior reviewer's identity lost.
    # Scope-aware: an override of a STAGE-scoped revise compares against the prior
    # review of that SAME scope, never against the whole-plan record.
    crossing = None
    if args.verdict == gates._PLAN_REVIEW_OVERRIDE:
        prev = state.plan_stage_reviews.get(scope) if scope else state.plan_review
        if prev is not None and prev.plan_path != target:
            prev = None
        # Mirrors cmd_accept's author/customer_id check; degrades to a pass-through
        # when the plan has no [meta.order] or an empty customer_id, same as cmd_accept.
        refusal, crossing = _plan_review_override_refusal(
            state, prev, (getattr(args, "reviewer", "") or "").strip(), doc,
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
        refs_problem = _plan_refs_problem(target)
        if refs_problem:
            return Directive(
                False, state.node, "noop",
                f"--plan-digest {attested!r} cannot be cross-checked: the plan at {target!r} has a "
                f"reference that does not resolve ({refs_problem}), so the engine cannot confirm "
                "the reviewer read the text the plan stands for; fix the reference and re-run plan-review",
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
        reviewed_interface_keys=(
            {str(k): v for k, v in plan_interface_digests(doc).items()} if doc is not None else {}
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
    question_ids = _record_review_questions(state, scope or "whole", question_texts)
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
            **_question_data(state, args, question_ids),
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
    vouch = _review_vouch(state, doc, scope)
    if scope:
        state.plan_stage_reviews[scope] = review
    else:
        state.plan_review = review
    if review.verdict == gates._PLAN_REVIEW_PASS and review.plan_sha256:
        # Attested pass only — an unattested one never clears the gate (see the
        # CONTRACT INVERSION note on the pass path of _plan_review_verdict_blockers),
        # so it must not become a terminal pass either.
        state.plan_review_passes[scope] = review
    _settle_override_decision(state, review, scope, crossing)
    _record_first_thinker_verdict(state, doc, review, scope, vouch=vouch)
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
    _count_review_act(state, review.verdict, review.reviewer, stage_scope=scope)
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
    fired = _evaluate_review_trigger(state, store, blockers)
    if fired is not None:
        return fired
    store.save(state)
    if blockers:
        round_release = _note_round_release(state, blockers, store)
        return Directive(
            False, state.node, "plan_review",
            "thinker review recorded but does not clear the gate",
            data={
                "blockers": blockers,
                "concern_ids": review.stable_ids,
                **_question_data(state, args, question_ids),
                "plan_review_round_release": round_release,
                "regression_command_error": regression_command_error,
                **_crossing_data(crossing),
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
        data={"concern_ids": review.stable_ids, **_question_data(state, args, question_ids),
              **_crossing_data(crossing)},
    )


def cmd_accept_protected_grant(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Record the user's acceptance of ONE protected action the current plan's grants
    cover (`protected_actions`) — a user-authority verb, so no spawned child or reviewer
    is ever granted it. Purely a recorder: `gates.protected_grant_blockers` re-derives
    the hits and the grants digest at `approve`, so an acceptance bound to a different
    grant set simply fails to clear the gate."""
    state = _require(store, args.session)
    target = state.plan_path
    if not target:
        return Directive(False, state.node, "noop",
                         "no plan to accept a protected grant against: submit a plan first")
    entry_id = (getattr(args, "id", "") or "").strip()
    note = (getattr(args, "note", "") or "").strip()
    if not entry_id or not note:
        return Directive(False, state.node, "noop",
                         "accept-protected-grant requires a non-empty --id and --note")
    if gates._normalize_string(note) in gates._PLACEHOLDER_SET:
        return Directive(False, state.node, "noop",
                         f"--note {note!r} reads as a placeholder, not a reason — say what concretely")
    try:
        doc = load_plan(target)
    except (OSError, PlanError) as e:
        return Directive(False, state.node, "noop",
                         f"cannot record a protected-grant acceptance: {target} failed to load: {e}")
    covered = sorted({hit_id for _stage, _rule, hit_id in protected_actions.protected_grant_hits(doc)})
    if entry_id not in covered:
        return Directive(
            False, state.node, "noop",
            f"the grants of {target} cover no protected action {entry_id!r} "
            f"(covered: {covered or 'none'}) — nothing there to accept",
        )
    digest = grants_sha256(doc)
    if not any(a.entry_id == entry_id and a.grants_sha256 == digest
               for a in state.protected_grant_acceptances):
        state.protected_grant_acceptances.append(ProtectedGrantAcceptance(
            entry_id=entry_id, grants_sha256=digest, plan_path=target, note=note))
    state.log("protected_grant_accept", target=target, entry_id=entry_id, grants_sha256=digest, note=note)
    store.save(state)
    return Directive(
        True, state.node, "continue",
        f"protected action {entry_id!r} accepted for the current grant set of {target}; "
        "a change to the plan's grants asks again",
        data={"entry_id": entry_id, "grants_sha256": digest},
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
    held = state.concern_ledger.get(concern_id)
    if held is not None:
        if held.status != CONCERN_OPEN:
            return Directive(
                False, state.node, "noop",
                f"concern {concern_id!r} is {held.status}, not open — nothing there to accept",
            )
        if scope and scope != held.scope:
            return Directive(
                False, state.node, "noop",
                f"--scope {scope!r} disagrees with concern {concern_id!r}, which belongs to "
                f"scope {held.scope!r} — drop --scope or pass the matching one",
            )
        accepted_scope, accepted_id, accepted_text = held.scope, held.local_id, held.text
    else:
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
        accepted_scope, accepted_id = scope, concern_id
        accepted_text = review.concerns[valid_ids.index(concern_id)]
    acceptance = RiskAcceptance(
        scope=accepted_scope,
        concern_id=accepted_id,
        plan_path=target,
        basis=basis,
        risk=risk,
        author=author,
        meta_digest=plan_meta_digest(doc),
        stage_keys={str(k): v for k, v in plan_stage_digests(doc).items()},
        concern_text=accepted_text,
    )
    state.risk_acceptances.append(acceptance)
    blockers = gates.plan_review_blockers(state, target)
    _log_gate(state, "plan_review", blockers, passed=not blockers)
    state.log("risk_accept", target=target, scope=accepted_scope, concern_id=accepted_id,
              ledger_id=concern_id, author=author, basis=basis, risk=risk)
    store.save(state)
    if blockers:
        return Directive(
            False, state.node, "plan_review",
            "risk acceptance recorded but does not clear the gate",
            data={"blockers": blockers},
        )
    return Directive(
        True, state.node, "continue",
        f"risk acceptance recorded for {target} (scope={accepted_scope!r} concern={accepted_id!r}); "
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
    whole_plan_needed, stage_indices, consumer_of = gates.plan_review_delta_detail(state, doc)
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
        if consumer_of:
            named = "; ".join(
                f"stage {c}: consumer of {', '.join(str(m) for m in consumer_of[c])}"
                for c in sorted(consumer_of)
            )
            detail += f" ({named} -- its supplier's interface moved)"
    else:
        md = ""
        detail = f"no review gap: every part of {target} is covered by its current review"
    if pairs:
        detail += f"; walk-stale pairs — run: plan-review-topological.py --pairs {','.join(pairs)}"
    data = {"markdown": md, "whole_plan": whole_plan_needed, "stages": stages, "pairs": pairs,
            "consumer_of": {str(c): consumer_of[c] for c in sorted(consumer_of)}}
    baseline, _ = _review_baseline(store, state)
    if baseline is not None:
        change = change_tier(baseline, doc)
        data["tier"] = change.tier
        data["stage_tiers"] = {str(i): c.tier for i, c in sorted(change.stages.items())}
        detail += f"; change tier {change.tier} against the version the session runs"
    return Directive(True, state.node, "inspect", detail, data=data)


def cmd_plan_review_pair_history(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: what an earlier review of one pair said, for the next reviewer of it.

    `data.records` lists every pair record taken against the target plan, oldest first,
    each with the reviewer's own verdict, the engine's effective one and its concerns
    (stable id, raw and effective severity, parts, and `unresolved` — still an open
    blocker no risk acceptance discharges). `data.changed_parts_since_last` names the
    pair's own parts (`pair_part_tokens`) whose digest differs from the one the latest
    record stored — every own part when that record stored none. No timestamp is emitted:
    the output feeds a spawn prompt, which must not vary with wall-clock time."""
    state = _require(store, args.session)
    target = getattr(args, "target", None) or state.plan_path
    if not target:
        return Directive(
            False, state.node, "noop",
            "no plan to read pair history for: submit a plan first, or pass --target <plan.toml>",
        )
    pair = args.pair
    try:
        doc = load_plan(target)
        if is_unit_id(pair):
            parse_unit(doc, pair)
        else:
            parse_pair(doc, pair)
    except (OSError, PlanError, ValueError) as e:
        return Directive(False, state.node, "noop", f"history for {pair!r} in {target}: {e}")
    events = [
        e for e in state.history
        if e.get("event") == "plan_pair_review" and e.get("pair") == pair and e.get("target") == target
    ]
    by_seq = _pair_ledger_groups(state, pair, target)
    claimed = {e["record_seq"] for e in events if e.get("record_seq")}
    claimed.update(
        state.concern_ledger[cid].record_seq for e in events for cid in e.get("concern_ids") or []
        if cid in state.concern_ledger)
    unclaimed = [seq for seq in sorted(by_seq) if seq not in claimed]
    records = []
    for event in events:
        entries = [state.concern_ledger[cid] for cid in event.get("concern_ids") or []
                   if cid in state.concern_ledger]
        if not entries and event.get("record_seq") in by_seq:
            entries = by_seq[event["record_seq"]]
        elif not entries and not event.get("record_seq") and event.get("concerns"):
            texts = list(event["concerns"])
            seq = next((s for s in unclaimed if [e.text for e in by_seq[s]] == texts), None)
            if seq is not None:
                unclaimed.remove(seq)
                entries = by_seq[seq]
        concerns = [_ledger_concern(entry, state, doc) for entry in entries] or [
            {"id": "", "severity": "", "effective_severity": "", "parts": [], "text": text,
             "unresolved": False}
            for text in event.get("concerns") or []
        ]
        records.append({
            "record_seq": event.get("record_seq") or 0,
            "plan_sha256": event.get("plan_sha256", ""),
            "reviewer_verdict": event.get("raw_verdict") or event.get("verdict", ""),
            "effective_verdict": event.get("verdict", ""),
            "concerns": concerns,
        })
    latest = state.plan_pair_reviews.get(pair)
    changed: list[str] = []
    if latest is not None:
        digests = part_digest_map(doc)
        stored = latest.part_digests if latest.plan_path == target else {}
        changed = _changed_parts(pair_part_tokens(pair), digests, stored)
    data = {"pair": pair, "records": records, "changed_parts_since_last": changed}
    if latest is not None and latest.plan_path == target:
        data["prior_diff"] = _prior_row_diff(store, state, doc, latest, pair)
    return Directive(
        True, state.node, "inspect",
        f"{len(records)} recorded review(s) of pair {pair!r} against {target}", data=data,
    )


def cmd_plan_review_walk(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Read-only: every review unit and pair of the plan in dependency order (base
    before service; each node's unit before its pairs), with its status, advisory
    readiness and the commands that spawn its reviewer and record its verdict.
    `--target` walks a plan file other than the session's own, computing every digest
    and depth from it. Writes nothing."""
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
                       "--plan-digest <sha256 of the plan bytes read> "
                       "[--customer-question <Q>]...") if open_pair else None,
        })
    walk = {
        "plan_path": target,
        "discharges": gates.pair_discharged_stages(state, doc, target),
        "levels": levels,
    }
    if getattr(args, "format", "text") == "json":
        return Directive(True, state.node, "inspect", json.dumps(walk, indent=2), data=walk)
    lines = [f"review units and pairs of {target} (base before service; readiness is advisory):"]
    for depth, level in enumerate(levels):
        lines.append(f"level {depth}:")
        for row in level:
            waiting = f" waiting on {','.join(row['waiting'])}" if row["waiting"] else ""
            lines.append(f"  {row['pair']}: {row['status']}, {'ready' if row['ready'] else 'not ready'}{waiting}")
    if walk["discharges"]:
        lines.append("discharged by pairs alone: " + ", ".join(walk["discharges"]))
    return Directive(True, state.node, "inspect", "\n".join(lines), data=walk)


def cmd_plan_review_compose(args, *, store: StateStore, runner: Runner | None = None) -> Directive:
    """Compose the unit and pair records into one ordinary whole-plan pass, written
    only when EVERY id of `plan.review_ids` is current or override against `--target` (the
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
        pairs = review_ids(doc)
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
        reviewed_interface_keys={str(k): v for k, v in plan_interface_digests(doc).items()},
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
    fired = _evaluate_review_trigger(state, store, blockers)
    if fired is not None:
        return fired
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
    plan_digest = ""
    if state.plan_path:
        try:
            doc, _data, plan_digest = load_plan_with_digest(state.plan_path, strict=False)
        except (OSError, PlanError):
            doc = None
    if doc is None:
        return Directive(
            False, state.node, "noop",
            "cannot read the plan to accept against "
            f"({state.plan_path or 'no plan_path on this session'}); acceptance compares the "
            "delivered product with the order that plan declares",
        )
    accepted_digest = state.accepted_plan_digest or ""
    if (not gates.plan_digest_equivalent(state, plan_digest, accepted_digest)
            and (accepted_digest or gates.acceptance_active(state))):
        # The per-requirement binding below is computed from `doc`; a file that is not
        # the accepted version would bind the review to bytes the engine never approved
        # while stamping it with the accepted digest. An acceptance-active session with
        # no accepted digest has no version to bind to, so it is refused the same way.
        return Directive(
            False, state.node, "noop",
            f"plan edited since it was accepted ({state.plan_path}); replan first so the "
            "engine's accepted version and the file agree, then accept against it"
            if accepted_digest else
            f"no accepted plan version is recorded for this session ({state.plan_path}); "
            "submit and approve the plan before accepting against it",
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
    verdicted_ids = {v.requirement_id for v in verdicts}
    state.acceptance_review = AcceptanceReview(
        author=author, verdicts=verdicts, note=note,
        plan_sha256=state.accepted_plan_digest or "",
        requirement_bindings={
            rid: binding for rid, binding in acceptance_requirement_bindings(doc).items()
            if rid in verdicted_ids
        },
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


def register(sub, add):
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
    sp.add_argument("--customer-question", dest="customer_questions", action="append",
                    default=None,
                    help="a question only the customer can decide that this review raised "
                         "(repeatable); recorded as a premise candidate "
                         "`qrev-<review id>-<N>` that blocks approve until dispositioned "
                         "with question-candidate-dispose — apart from --concern, which "
                         "is a plan remark the coordinator fixes itself")
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
                         "whole plan, or 'topo:<id>' (a pair, e.g. topo:3-1 or "
                         "topo:base-2; a unit, e.g. topo:unit:base or topo:unit:3) to "
                         "record one pair or unit review; omitted "
                         "(or '') means whole-plan, the only kind that existed before stage 5")
    sp.add_argument("--escalate-pairs", dest="escalate_pairs", action="store_true",
                    help="with a unit scope: re-open the pairs touching that unit that a "
                         "tier-2 edit carried without a review (held), so each is owed a "
                         "review of the edited content like a tier-3 edit")
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
    sp = add("plan-review-pair-history"); sp.add_argument("--session", required=True)
    sp.add_argument("--target", default=None,
                    help="plan file the pair's records were taken against (defaults to the "
                         "session's current plan_path)")
    sp.add_argument("--pair", required=True,
                    help="the review pair id (as listed by plan-review-walk)")
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
    sp = add("accept-protected-grant"); sp.add_argument("--session", required=True)
    sp.add_argument("--id", required=True,
                    help="the protected-action id the plan's grants cover (shown in the grants "
                         "block of present-plan, e.g. push-trunk)")
    sp.add_argument("--note", required=True,
                    help="why the user accepts that the grants cover this action")
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
                    help="requirement verdict as '<requirement_id>|<pass|fail|withdrawn>[|<note>]'; "
                         "repeatable, one per declared order requirement; "
                         "'withdrawn' requires a non-empty note naming who withdrew it and why")
    sp.add_argument("--note", default="",
                    help="free-text note on the acceptance review as a whole")
    sp.add_argument("--bypass", action="store_true",
                    help="record an AcceptanceBypass alongside the review, for when the "
                         "acceptance judge is unreachable; requires --bypass-reason and "
                         "at least one --verdict")
    sp.add_argument("--bypass-reason", dest="bypass_reason", default="",
                    help="why this acceptance stands without judge corroboration "
                         "(required with --bypass)")
