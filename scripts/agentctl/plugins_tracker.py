"""The tracker-management plugin: the first real consumer of the plugin layer.

The `tracker-management` skill is a deterministic publish-workflow layered on top
of coordination — it publishes the plan before approval, nudges the open->
in-progress ticket transition once the plan is approved and work begins, posts a
progress note at each stage boundary, a note when the plan is re-normed, and the
final result before the task closes. Historically the skill's "Phase hooks" table told the *coordinator*
to remember each of those moments. That is exactly the cognition the engine can
own: this plugin OBSERVES the matching core transitions and surfaces a
`publish_*` PluginDirective at each, and a gate keeps the task from closing until
the mandatory publications (plan + result + the ticket status transition) are
actually recorded.

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
# replan posts are valuable but not load-bearing; the plan, the final result, and
# the ticket status transition are the ticket's minimum honest record.
MANDATORY_PHASES = ("plan", "result", "status")


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


def _observe_approve(state, bag) -> list[PluginDirective]:
    # plan just approved (PLAN_READY -> APPROVED): work begins. Nudge the open->
    # in-progress transition, non-blocking — skip if the ticket is already in progress.
    return [PluginDirective(
        "tracker", "start_progress",
        "the plan is approved and work begins: if the ticket is still open, transition "
        "it to the in-progress status (e.g. \"В работе\") now; non-blocking — skip if it "
        "is already in progress",
        data={"tracker_key": _key(bag)},
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


_MARKER_LABELS = (
    ("m1", "M1 independent deliverables"),
    ("m2", "M2 heterogeneous work"),
    ("m3", "M3 blocking deps"),
    ("m4", "M4 rollback risk"),
)


def _fired_markers(partition) -> str:
    labels = []
    for attr, label in _MARKER_LABELS:
        if not getattr(partition, attr, False):
            continue
        severe = getattr(partition, f"{attr}_severe", False)
        labels.append(f"{label} (severe)" if severe else label)
    return ", ".join(labels) if labels else "severity override"


def _observe_partition(state, bag) -> list[PluginDirective]:
    # the generic 'subtask' mode materializes as a tracker subticket: when the
    # M1-M4 verdict recommends a split, nudge the coordinator to PROPOSE (not
    # decide) subtickets-vs-several-PRs to the user. Silent on 'possible' /
    # 'not_required' — a mere maybe doesn't warrant interrupting the user.
    partition = getattr(state, "partition", None)
    if partition is None or partition.verdict != "recommended":
        return []
    return [PluginDirective(
        "tracker", "propose_delivery_structure",
        f"partition verdict is recommended ({_fired_markers(partition)}): propose to the "
        "user via AskUserQuestion whether to split delivery into subtickets (M3 blocking "
        "deps or distinct owners favor subtickets) or ship as several PRs under this "
        "ticket — each subticket costs a full spine, so this is a nudge, not a gate",
        data={"tracker_key": _key(bag), "verdict": partition.verdict},
    )]


def _observe_partition_units(state, bag) -> list[PluginDirective]:
    # every recorded unit with mode == 'subtask' and no ref yet needs its subticket
    # created; once the coordinator re-records the unit with the new key as ref,
    # this unit converges silent (materialization done).
    partition = getattr(state, "partition", None)
    if partition is None:
        return []
    out: list[PluginDirective] = []
    for pos, unit in enumerate(partition.units, start=1):
        if unit.mode != "subtask" or unit.ref:
            continue
        out.append(PluginDirective(
            "tracker", "create_subticket",
            f"unit {pos} ({unit.title}) is mode=subtask with no ref yet: create the "
            "subticket, then re-record the unit with its key as ref via "
            "`agentctl partition-units` to silence this nudge",
            data={"tracker_key": _key(bag), "unit_index": pos, "unit_title": unit.title},
        ))
    return out


def _observe_resolve(state, bag) -> list[PluginDirective]:
    # fires on the (possibly gate-blocked) resolve attempt. Surface each
    # not-yet-published mandatory nudge independently; once a phase is recorded,
    # it stays silent so the successful second resolve does not re-nudge.
    pub = _published(bag)
    out: list[PluginDirective] = []
    if "result" not in pub:
        out.append(PluginDirective(
            "tracker", "publish_result",
            "post the final result (resolution summary + all artifacts + structured "
            "difficulty record), then `plugin-record --phase result`",
            blocking=True, data={"tracker_key": _key(bag), "phase": "result"},
        ))
    if "status" not in pub:
        out.append(PluginDirective(
            "tracker", "transition_status",
            "transition the ticket(s) to a terminal/resolved status (subtickets "
            "before parent), then `plugin-record --phase status`. If the ticket is "
            "legitimately left open (e.g. a follow-up PR still pending), record the "
            "decision explicitly: `plugin-record --phase status --note \"<why open>\"`",
            blocking=True, data={"tracker_key": _key(bag), "phase": "status"},
        ))
    return out


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
            "approve": _observe_approve,
            "partition": _observe_partition,
            "partition_units": _observe_partition_units,
            "record_result": _observe_record_result,
            "replan": _observe_replan,
            "resolve": _observe_resolve,
        },
        gates={"resolution": _publish_gate},
        state_factory=lambda: {"tracker_key": "", "published_phases": {}},
        terminal=_terminal,
    )
)
