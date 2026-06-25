"""The tracker-management plugin: the first real consumer of the plugin layer.

The `tracker-management` skill is a deterministic publish-workflow layered on top
of coordination — it publishes the plan before approval, a progress note at each
stage boundary, a note when the plan is re-normed, and the final result before the
task closes. Historically the skill's "Phase hooks" table told the *coordinator*
to remember each of those moments. That is exactly the cognition the engine can
own: this plugin OBSERVES the matching core transitions and surfaces a
`publish_*` PluginDirective at each, and a gate keeps the task from closing until
the mandatory publications (plan + result) are actually recorded.

Division of labour (engine owns WHEN; the skill owns WHAT/WHERE):
  - WHEN — this plugin: which transition fires which publish nudge, and the
    did-publish gate on resolution. Deterministic, no recall.
  - WHAT/WHERE — the skill: comment content, tracker API/transport, and the
    open-PR override (status to the PR, not the ticket). The plugin emits the
    same `publish_progress` nudge either way; the skill routes it.

Lifecycle: task-scoped. Activated by the skill on invocation
(`agentctl plugin-activate --plugin tracker --tracker-key <KEY>`), it rides the
whole task and auto-retires once resolution actually passes (its bag is archived
into state.plugins_archive for audit). A publication is only marked done by the
coordinator calling `agentctl plugin-record --plugin tracker --phase <p>` AFTER
the comment lands — so the gate reflects a real post, never a mere intention.
"""
from __future__ import annotations

from .plugins import Plugin, PluginDirective, register
from .state import Node

# Phases the gate insists on before a tracker task may resolve. Progress and
# replan posts are valuable but not load-bearing; the plan and the final result
# are the ticket's minimum honest record.
MANDATORY_PHASES = ("plan", "result")


def _published(bag) -> dict:
    return bag.setdefault("published_phases", {})


def _key(bag) -> str:
    return bag.get("tracker_key", "") or ""


# --- observers: one per core transition that maps to a publication ------------

def _observe_submit_plan(state, bag) -> list[PluginDirective]:
    return [PluginDirective(
        "tracker", "publish_plan",
        "post the plan to the ticket BEFORE asking the user for approval",
        blocking=True, data={"tracker_key": _key(bag), "phase": "plan"},
    )]


def _observe_record_result(state, bag) -> list[PluginDirective]:
    # record_result fires for passed AND failed stages. A failed stage routes the
    # session into DIAGNOSING (publish_replan covers the recovery); only a passed
    # stage is a progress boundary worth a ticket note.
    if getattr(state, "node", None) == Node.DIAGNOSING.value:
        return []
    return [PluginDirective(
        "tracker", "publish_progress",
        "post a one-line progress note + artifact link (or, in PR-stage work, "
        "update the PR instead — the skill routes transport)",
        data={"tracker_key": _key(bag)},
    )]


def _observe_replan(state, bag) -> list[PluginDirective]:
    return [PluginDirective(
        "tracker", "publish_replan",
        "post what changed in the plan and why, with a link to the revised plan",
        data={"tracker_key": _key(bag)},
    )]


def _observe_resolve(state, bag) -> list[PluginDirective]:
    # fires on the (possibly gate-blocked) resolve attempt. Surface the result
    # nudge until the result is actually published; once recorded, stay silent so
    # the successful second resolve does not re-nudge.
    if "result" in _published(bag):
        return []
    return [PluginDirective(
        "tracker", "publish_result",
        "post the final result (resolution summary + all artifacts + structured "
        "difficulty record), then `plugin-record --phase result`",
        blocking=True, data={"tracker_key": _key(bag), "phase": "result"},
    )]


# --- gate: block resolution until the mandatory phases are recorded -----------

def _publish_gate(state, bag) -> list[str]:
    pub = _published(bag)
    missing = [p for p in MANDATORY_PHASES if p not in pub]
    if not missing:
        return []
    return [f"mandatory tracker publication(s) not yet recorded: {', '.join(missing)} "
            f"(post the comment, then `plugin-record --plugin tracker --phase <p>`)"]


# --- lifecycle: task-scoped, retire once resolution truly passes --------------

def _terminal(state, event: str) -> bool:
    # task boundary == a resolve that actually passed (the first, gate-blocked
    # resolve leaves resolution.passed False, so the bag survives until the
    # mandatory phases are recorded and resolve goes through).
    return event == "resolve" and bool(getattr(state.resolution, "passed", False))


register(
    Plugin(
        name="tracker",
        scope="task",
        observers={
            "submit_plan": _observe_submit_plan,
            "record_result": _observe_record_result,
            "replan": _observe_replan,
            "resolve": _observe_resolve,
        },
        gates={"resolution": _publish_gate},
        state_factory=lambda: {"tracker_key": "", "published_phases": {}},
        terminal=_terminal,
    )
)
