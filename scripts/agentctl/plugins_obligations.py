"""The conformance-obligations ledger: a generic, SELECTIVE record of blocking
PluginDirectives that behavioral-conformance-control's stage 3 (a static
verify-agentctl assertion, not runtime code) will require every `_DISCHARGE`
key to be force-registered against. This module is honest about the "belt" it
is: `mint` is not a universal blocking-directive sink — it is a filtered view
over the SUBSET of blocking directives this module explicitly knows how to
discharge. A blocking directive whose `action` is not a `_DISCHARGE` key
(`premise`'s `close_questions`, `experience`'s `record_experience`) is
deliberately IGNORED here, exactly like the reactive gates it observes never
gate on those two: minting them would deadlock `resolution` (`record_experience`
fires blocking on the `resolve` event ITSELF, so a fail-closed ledger entry for
it could never discharge before the event that would clear it).

Both `_DISCHARGE` oracles reuse the existing reactive gates verbatim
(`gates.plan_review_blockers`, `gates.code_review_blockers`) rather than
re-deriving the precondition, so the ledger and the gate it mirrors can never
disagree about whether an obligation is still owed — the same discipline
`plugins_review_dispatch`'s two observers already follow for the PROACTIVE
trigger; this plugin adds the missing REACTIVE-at-resolution backstop in case
the proactive trigger was ever missed (a spawn that never happened, a
directive lost before the specialist read it)."""
from __future__ import annotations

import os

from . import gates
from .plugins import Plugin, register
from .state import WeightClass


def _auto_activate(state) -> bool:
    """Arm for every SUBSTANTIVE session — weight_class alone, mirroring
    plugins_premise._auto_activate / plugins_review_dispatch._auto_activate.
    AGENTCTL_OBLIGATIONS is a test-seam override ("1" forces on, "0" forces
    off); env-unset — every real session — resolves to the plain weight_class
    predicate."""
    env = os.environ.get("AGENTCTL_OBLIGATIONS")
    if env == "1":
        return True
    if env == "0":
        return False
    return getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value


def _discharge_plan_review(state, ob) -> bool:
    return not gates.plan_review_blockers(state, getattr(state, "plan_path", None))


def _discharge_code_review(state, ob) -> bool:
    # A replan can renumber/drop stages between mint and check; a stage that no
    # longer exists has nothing left to review, so treat it as discharged rather
    # than as a permanently-undischargeable obligation — the safe direction,
    # since it never MASKS a real open review on a still-live stage.
    stage_index = ob.get("data", {}).get("stage")
    stage = next((s for s in state.stages if s.index == stage_index), None)
    if stage is None:
        return True
    return not gates.code_review_blockers(state, stage)


_DISCHARGE = {
    "spawn_thinker_review": _discharge_plan_review,
    "spawn_code_review": _discharge_code_review,
}


def _obligation_id(pd: dict) -> str:
    data = pd.get("data") or {}
    return "|".join(str(x) for x in (
        pd.get("plugin"), pd.get("action"), data.get("slot"), data.get("stage"),
    ))


def mint(state, fired: list[dict]) -> None:
    """Called generically from plugins.fire() after every event's observer pass.
    No-ops when the plugin is inactive (state.plugins['obligations'] absent) or
    when `fired` carries nothing this module knows how to discharge — so a
    plugin-less or non-substantive session's fire() output is byte-identical to
    today's."""
    bag = state.plugins.get("obligations")
    if bag is None:
        return
    ledger = bag.setdefault("open", {})
    for pd in fired:
        if not pd.get("blocking"):
            continue
        action = pd.get("action")
        if action not in _DISCHARGE:
            continue
        oid = _obligation_id(pd)
        ledger[oid] = {
            "plugin": pd.get("plugin"),
            "action": action,
            "detail": pd.get("detail", ""),
            "data": pd.get("data") or {},
        }


def _resolution_guardian(state, bag) -> list[str]:
    blockers = []
    for ob in bag.get("open", {}).values():
        check = _DISCHARGE.get(ob["action"])
        if check is None:
            continue  # unreachable: mint only ever records _DISCHARGE actions
        if not check(state, ob):
            blockers.append(f"undischarged obligation {ob['action']}: {ob.get('detail', '')}")
    return blockers


register(
    Plugin(
        name="obligations",
        scope="task",
        auto_activate=_auto_activate,
        observers={},
        gates={"resolution": _resolution_guardian},
        state_factory=lambda: {"open": {}},
    )
)
