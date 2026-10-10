"""The review-dispatch plugin: a PROACTIVE trigger for an engine-required
specialist invocation whose spawn TIMING would otherwise ride coordinator
perception, paired with a pre-existing REACTIVE precondition it never
replaces or weakens.

This module covers two slots:

  plan_review -> thinker. gates.plan_review_blockers already REACTIVELY blocks
  `approve`/every `replan` when no bound passing (or overridden) thinker review
  exists for the exact plan version. Nothing PROACTIVELY names the required
  thinker spawn the moment the obligation is minted — PLAN_READY, the
  `submit_plan` event. The `_obs_submit_plan` observer supplies that trigger:
  it emits a blocking PluginDirective naming the thinker spawn whenever
  `gates.plan_review_blockers` is non-empty for the just-submitted plan, and
  stays silent once a bound passing/overridden review exists. On a SPAWN-route
  session (`gates.pairwise_review_route`, the predicate the blocker message
  shares) the directive names the topological pairwise review and
  `plan-review-compose` as THE review route; every other route keeps the
  whole-plan thinker spawn.

  code_review -> code-reviewer. gates.code_review_blockers already REACTIVELY
  blocks `record-result --status passed` on a needs_control() (spawn:developer)
  stage when no bound passing (or overridden) code-reviewer verdict exists.
  Nothing PROACTIVELY names the required code-reviewer spawn the moment the
  obligation is minted — the stage's `dispatch` event, once developer code
  exists to review. The `_obs_dispatch` observer supplies that trigger: it
  emits a blocking PluginDirective naming the code-reviewer spawn whenever
  `gates.code_review_blockers` is non-empty for the just-dispatched active
  stage, and stays silent once a bound passing/overridden review exists, the
  active stage is not a developer stage, or the gate is inactive.

`_SLOT_SPECIALIST` is the extension seam shared by both observers: a future
slot (a third engine-required, non-stage-actor specialist) adds one table
entry plus one observer function, without touching the existing ones.

Both directives name `--workdir <delivery venue>` for the review spawn and
NEVER `--session`: a review spawn is not the plan's stage executor, so its
`--kind` can never match a stage's `spawn_kind()` and `--session` would buy
it nothing but the temptation to run a `agentctl ... --session` user-authority
verb itself. `_obs_submit_plan` in particular has the reviewer compute the
plan's own sha256 (it has Bash(shasum -a 256:*) via PLANS_READ_KINDS, not
agentctl user-authority) and report it as a `Plan digest: <sha256>` line in
its REVIEW message; the ROOT reads that line and records the verdict itself
via `agentctl plan-review --session <id> --plan-digest <sha256>` — the
reviewer never calls `agentctl plan-review` itself.

Deliberately does NOT observe `replan`: an Observer's signature is
`(state, bag)` — it never sees `args.plan`, the corrected plan a replan
applies — and `cmd_replan` early-returns WITHOUT `store.save` on a rejected
replan, so a `replan` observer would reload stale on-disk state (via
`_fire_plugins`' `store.load`) and could name the wrong plan version.
`cmd_replan`'s own inline rejection already names the correct target
reactively; adding a `replan` observer here would duplicate that with a
staleness risk, not remove one.

`dispatch`, by contrast, IS a safe observer target: `cmd_dispatch` saves state
before returning on every non-preview path (COMPLETED / CLARIFY / recursion
refusal / any marker), so `_fire_plugins`' reload always reflects the
just-dispatched active stage — no staleness risk, unlike `replan`. `main` runs
`_fire_plugins` after EVERY command, a `dispatch --dry-run` preview included
(cmd_dispatch returns early WITHOUT a fresh save, so the observer reloads the
last-saved state); when a preview follows a `next-stage` that activated a
needs_control() stage, the code-review nudge therefore surfaces alongside the
preview. That is advisory only — a dry-run performs no spawn and the label
never blocks the preview action — so it is a harmless hint, not a staleness
bug like a `replan` observer would be.

No `gates` entry: enforcement stays entirely in `gates.plan_review_blockers`
(wired into `approve`/`replan`) and `gates.code_review_blockers` (wired into
`record-result`) — this plugin only supplies the missing ACTIVE trigger in
front of each, exactly like `plugins_premise`."""
from __future__ import annotations

import os

from . import gates
from .plan import PlanError, load_plan
from .plugins import Plugin, PluginDirective, register
from .state import CheckVenue, Node, WeightClass

_SLOT_SPECIALIST = {
    "plan_review": "thinker",
    "code_review": "code-reviewer",
}


def _auto_activate(state) -> bool:
    """Arm for every SUBSTANTIVE session — weight_class alone, mirroring
    plugins_premise._auto_activate. AGENTCTL_REVIEW_DISPATCH is a test-seam
    override ("1" forces on, "0" forces off); env-unset — every real session —
    resolves to the plain weight_class predicate."""
    env = os.environ.get("AGENTCTL_REVIEW_DISPATCH")
    if env == "1":
        return True
    if env == "0":
        return False
    return getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value


def _obs_submit_plan(state, bag) -> list[PluginDirective]:
    """Fires ONLY on the event that mints the plan-review obligation —
    node-guarded to PLAN_READY, the node `cmd_submit_plan` lands on. Reuses
    gates.plan_review_blockers verbatim — never re-derives the precondition —
    so the trigger and the gate can never disagree about whether a review is
    still owed. The render hint fed to the reviewer is scoped via
    gates.review_delta rather than hardcoded to the whole plan, so a reviewer
    asked to cover only a moved stage is not handed the entire plan to re-read."""
    if getattr(state, "node", None) != Node.PLAN_READY.value:
        return []
    target_plan = getattr(state, "plan_path", None)
    blockers = gates.plan_review_blockers(state, target_plan)
    if not blockers:
        return []
    specialist = _SLOT_SPECIALIST["plan_review"]
    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value) or "<delivery venue>"
    try:
        doc = load_plan(target_plan) if target_plan else None
    except (OSError, PlanError):
        doc = None
    delta = gates.review_delta(state, doc, target_plan)
    scopes = delta["record_scope_args"]
    if len(scopes) <= 1:
        scope_suffix = f" {scopes[0]}" if scopes else ""
        record_instruction = (
            f"The ROOT then records the verdict: `agentctl plan-review --session "
            f"{state.session_id} --verdict pass|revise|override --reviewer {specialist} "
            f"--plan-digest <sha256-hex>{scope_suffix} [--customer-question <Q>]...` (a "
            f"pass does NOT bind without a matching --plan-digest; pass each `Q:` line of "
            f"the reply's `Customer questions:` field as one --customer-question)"
        )
    else:
        record_instruction = (
            "The ROOT then records each stage's verdict separately: " + "; ".join(
                f"`agentctl plan-review --session {state.session_id} --verdict "
                f"pass|revise|override --reviewer {specialist} --plan-digest "
                f"<sha256-hex> {scope_arg} [--customer-question <Q>]...`"
                for scope_arg in scopes
            ) + (" (a pass does NOT bind without a matching --plan-digest; pass each `Q:` "
                 "line of the reply's `Customer questions:` field as one --customer-question)")
        )
    if gates.pairwise_review_route(state):
        return [PluginDirective(
            plugin="review_dispatch",
            action="spawn_thinker_review",
            detail=(
                f"run `scripts/plan-review-topological.py --session {state.session_id} "
                f"--plan {target_plan}` -- pairwise review is the required route for this "
                f"plan: it spawns one pair reviewer per base-service pair (each echoes the "
                f"spawner-rendered `Plan digest:` line), records every verdict; then run "
                f"`agentctl plan-review-compose --session {state.session_id}` to compose "
                f"the pass (exit 0 composed pass, 1 blocked, 3 nothing to review, 2 a "
                f"refused pair or a usage error). A pair bundle carries no question list: "
                f"read `agentctl question-list --session {state.session_id} --format md` "
                f"yourself as the ROOT and weigh it against the plan"
            ),
            blocking=True,
            data={"slot": "plan_review", "specialist": specialist, "mode": "pairwise",
                  "blockers": blockers, "whole_plan": delta["whole_plan"],
                  "stages": delta["stages"], "scopes": delta["scopes"],
                  "render_command": delta["render_command"]},
        )]
    return [PluginDirective(
        plugin="review_dispatch",
        action="spawn_thinker_review",
        detail=(
            f"spawn the `{specialist}` specialization with --workdir {venue} (never "
            f"--session -- a review spawn is not the plan's executor); feed it "
            f"`{delta['render_command']}` and `agentctl question-list "
            f"--session {state.session_id} --format md`; on this inline route the "
            f"reviewer must compute the sha256 of {target_plan} from its OWN read and "
            f"report it as a `Plan digest: <sha256-hex>` line in its REVIEW message -- it "
            f"never calls `agentctl plan-review` itself. {record_instruction}"
        ),
        blocking=True,
        data={"slot": "plan_review", "specialist": specialist, "mode": "whole", "blockers": blockers,
              "whole_plan": delta["whole_plan"], "stages": delta["stages"],
              "scopes": delta["scopes"], "render_command": delta["render_command"]},
    )]


def _obs_dispatch(state, bag) -> list[PluginDirective]:
    """Fires on the event that mints the code-review obligation (a spawn:developer
    stage has just been dispatched — developer code now exists, or is en route, to
    review). Reuses gates.code_review_blockers verbatim — never re-derives the
    precondition — so the trigger and the gate can never disagree about whether a
    review is still owed. Only spawn:developer (needs_control()) stages carry this
    obligation; every other active stage, or an inactive gate, is silent."""
    stage = state.active_stage()
    if stage is None or not stage.needs_control():
        return []
    if not gates.code_review_active(state):
        return []
    blockers = gates.code_review_blockers(state, stage)
    if not blockers:
        return []
    specialist = _SLOT_SPECIALIST["code_review"]
    venue = state.resolve_check_venue(CheckVenue.DELIVERY.value) or "<delivery venue>"
    return [PluginDirective(
        plugin="review_dispatch",
        action="spawn_code_review",
        detail=(
            f"spawn the `{specialist}` specialization with --workdir {venue} (never "
            f"--session -- a review spawn is not the stage's executor) to review stage "
            f"{stage.index}'s diff; `code-review` is an agentctl user-authority verb "
            f"(AGENTCTL_USER_AUTHORITY_VERBS), so the reviewer is never granted it -- it "
            f"reports its verdict (pass|revise|override) and rationale in its own REVIEW "
            f"message instead. The ROOT then records it: `agentctl code-review --session "
            f"{state.session_id} --verdict pass|revise|override --reviewer {specialist} "
            f"[--code-ref <rev>]`"
        ),
        blocking=True,
        data={"slot": "code_review", "specialist": specialist, "stage": stage.index, "blockers": blockers},
    )]


register(
    Plugin(
        name="review_dispatch",
        scope="task",
        auto_activate=_auto_activate,
        observers={"submit_plan": _obs_submit_plan, "dispatch": _obs_dispatch},
        gates={},
        state_factory=dict,
    )
)
