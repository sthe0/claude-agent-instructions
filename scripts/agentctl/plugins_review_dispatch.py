"""The review-dispatch plugin: a PROACTIVE trigger for an engine-required
specialist invocation whose spawn TIMING would otherwise ride coordinator
perception, paired with a pre-existing REACTIVE precondition it never
replaces or weakens.

This module currently covers one slot:

  plan_review -> thinker. gates.plan_review_blockers already REACTIVELY blocks
  `approve`/every `replan` when no bound passing (or overridden) thinker review
  exists for the exact plan version. Nothing PROACTIVELY names the required
  thinker spawn the moment the obligation is minted — PLAN_READY, the
  `submit_plan` event. This plugin supplies that trigger: an observer on
  `submit_plan` that emits a blocking PluginDirective naming the thinker spawn
  whenever `gates.plan_review_blockers` is non-empty for the just-submitted
  plan, and stays silent once a bound passing/overridden review exists.

`_SLOT_SPECIALIST` is the extension seam: a future slot (a second
engine-required, non-stage-actor specialist) adds one table entry plus one
observer function, without touching the existing one.

Deliberately does NOT observe `replan`: an Observer's signature is
`(state, bag)` — it never sees `args.plan`, the corrected plan a replan
applies — and `cmd_replan` early-returns WITHOUT `store.save` on a rejected
replan, so a `replan` observer would reload stale on-disk state (via
`_fire_plugins`' `store.load`) and could name the wrong plan version.
`cmd_replan`'s own inline rejection already names the correct target
reactively; adding a `replan` observer here would duplicate that with a
staleness risk, not remove one.

No `gates` entry: enforcement stays entirely in `gates.plan_review_blockers`
(already wired into `approve`/`replan`) — this plugin only supplies the
missing ACTIVE trigger in front of it, exactly like `plugins_premise`."""
from __future__ import annotations

import os

from . import gates
from .plugins import Plugin, PluginDirective, register
from .state import WeightClass

_SLOT_SPECIALIST = {
    "plan_review": "thinker",
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
    """Fires on the event that mints the plan-review obligation (PLAN_READY).
    Reuses gates.plan_review_blockers verbatim — never re-derives the
    precondition — so the trigger and the gate can never disagree about
    whether a review is still owed."""
    target_plan = getattr(state, "plan_path", None)
    blockers = gates.plan_review_blockers(state, target_plan)
    if not blockers:
        return []
    specialist = _SLOT_SPECIALIST["plan_review"]
    return [PluginDirective(
        plugin="review_dispatch",
        action="spawn_thinker_review",
        detail=(
            f"spawn the `{specialist}` specialization to review the plan; feed it "
            f"`agentctl plan-render --plan {target_plan}` and `agentctl question-list "
            f"--session {state.session_id} --format md`; then record with `agentctl "
            f"plan-review --session {state.session_id} --verdict pass|revise|override "
            f"--reviewer {specialist}`"
        ),
        blocking=True,
        data={"slot": "plan_review", "specialist": specialist, "blockers": blockers},
    )]


register(
    Plugin(
        name="review_dispatch",
        scope="task",
        auto_activate=_auto_activate,
        observers={"submit_plan": _obs_submit_plan},
        gates={},
        state_factory=dict,
    )
)
