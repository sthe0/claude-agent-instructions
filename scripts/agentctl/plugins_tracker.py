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

Lifecycle: task-scoped. Activated either by the skill on invocation
(`agentctl plugin-activate --plugin tracker --tracker-key <KEY>`) or by the engine
itself at classify (#11 P1: `_auto_activate` fires whenever a tracker-key-shaped
task id is detected on a SUBSTANTIVE session) — a tracker-driven task must never
leave the publish layer dark just because the skill was not explicitly invoked.
It rides the whole task and auto-retires once resolution actually passes (its bag
is archived into state.plugins_archive for audit). A publication is only marked
done by the coordinator calling `agentctl plugin-record --plugin tracker --phase
<p>` AFTER the comment lands — so the gate reflects a real post, never a mere
intention.
"""
from __future__ import annotations

from .plugins import Plugin, PluginDirective, register
from .state import Node, StageStatus, WeightClass

# Phases the gate insists on before a tracker task may resolve: the plan, the
# final result, and the ticket status transition are the ticket's minimum honest
# record. Progress posts are gated separately, PER STAGE INSTANCE rather than as
# a fixed phase name here — see _journal_gate below: every PASSED stage that
# declared output_artifacts owes its own "progress:<index>" entry, not merely "a
# progress phase somewhere in the task".
MANDATORY_PHASES = ("plan", "result", "status")


def _auto_activate(state) -> bool:
    """True when classify detected a ticket key (#11 P1): a tracker-driven task
    must never leave the publish layer dark just because the tracker-management
    skill was not explicitly invoked to `plugin-activate`."""
    return bool(getattr(state, "tracker_key", None)) and (
        getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value
    )


def _published(bag) -> dict:
    return bag.setdefault("published_phases", {})


def _key(bag) -> str:
    return bag.get("tracker_key", "") or ""


# --- observers: one per core transition that maps to a publication ------------

def _observe_submit_plan(state, bag) -> list[PluginDirective]:
    return [PluginDirective(
        "tracker", "publish_plan",
        "post a reader-facing rendering of the plan, in the dialogue language, to the "
        "ticket BEFORE asking the user for approval — never the raw plan bytes",
        blocking=True, data={"tracker_key": _key(bag), "phase": "plan"},
    )]


def _observe_approve(state, bag) -> list[PluginDirective]:
    # plan just approved (PLAN_READY -> APPROVED): work begins. Two nudges fire.
    # (1) start_progress: the open->in-progress transition, non-blocking — skip if
    #     the ticket is already in progress.
    # (2) publish_plan: approve is where the IMMUTABLE approved snapshot exists
    #     (state.plan_snapshot_path, written by _snapshot_approved_plan at approve),
    #     so THIS is the transition that carries the plan the ticket must record.
    #     Publish the snapshot bytes, never the mutable plan file. Non-blocking:
    #     enforcement lives in _publish_gate + the mandatory `plan` phase, not in a
    #     blocking approve nudge. If the resolved backend defines no
    #     tracker_publish_plan, the coordinator discharges the mandatory `plan`
    #     phase with the skip form rather than wedging resolution — the detail names
    #     both routes so the degrade is honest, never a silent unrecorded phase.
    return [
        PluginDirective(
            "tracker", "start_progress",
            "the plan is approved and work begins: if the ticket is still open, transition "
            "it to the in-progress status (e.g. \"В работе\") now; non-blocking — skip if it "
            "is already in progress",
            data={"tracker_key": _key(bag)},
        ),
        PluginDirective(
            "tracker", "publish_plan",
            "the plan is approved: the comment body must carry a reader-facing rendering "
            "of the plan in the dialogue language, with the approved plan SNAPSHOT "
            "attached or linked via tracker_publish_plan — never pasted into the comment "
            "body — then `plugin-record --plugin tracker --phase plan`. If the backend "
            "defines no tracker_publish_plan, record the skip instead: "
            "`plugin-record --plugin tracker --phase plan --skipped --note \"<why>\"` — "
            "never leave the mandatory plan phase silently unrecorded",
            data={
                "tracker_key": _key(bag),
                "phase": "plan",
                "plan_snapshot_path": getattr(state, "plan_snapshot_path", None) or "",
            },
        ),
    ]


def _stage_by_index(state, index):
    if index is None:
        return None
    for s in getattr(state, "stages", None) or []:
        if s.index == index:
            return s
    return None


def _last_passed_stage_index(state) -> int | None:
    """By the time this observer runs, `state.current_stage` is already None —
    `cmd_record_result`'s passed branch resets it BEFORE `state.log(...)` and the
    save that `_fire_plugins` then reloads fresh — so `active_stage()` cannot
    recover which stage just passed. `state.history` is not reset the same way:
    the `record_result`/status=passed entry this very call wrote is always the
    newest matching one there, so scan backward for it instead."""
    for entry in reversed(getattr(state, "history", None) or []):
        if entry.get("event") == "record_result" and entry.get("status") == "passed":
            return entry.get("stage")
    return None


def _observe_record_result(state, bag) -> list[PluginDirective]:
    # record_result fires for passed AND failed stages. A failed stage routes the
    # session into DIAGNOSING (publish_replan covers the recovery); only a passed
    # stage is a progress boundary worth a ticket note.
    if getattr(state, "node", None) == Node.DIAGNOSING.value:
        return []
    stage_index = _last_passed_stage_index(state)
    stage = _stage_by_index(state, stage_index)
    # A stage that declared output_artifacts (and carries no ephemeral waiver)
    # changed something a ticket reader cannot otherwise discover — the journal
    # gate below requires an individual, substantive entry for it, not a
    # one-liner, and one keyed to THIS stage's index specifically.
    journaled = bool(
        stage is not None
        and (stage.output_artifacts or [])
        and not (stage.ephemeral_artifacts_waiver or "").strip()
    )
    if journaled:
        detail = (
            "this stage declared output_artifacts: post a SUBSTANTIVE progress "
            "comment (figures, caveats, every artifact this stage produced — not "
            "a one-liner, and never under an internal stage-number heading like "
            "\"### Стадия N\" — tech-writer rule 3: name the result in plain "
            "words, write the thread's next beat, not a status report), then "
            "`plugin-record --plugin tracker --phase progress "
            f"--stage {stage_index}` — the resolution gate requires this exact "
            "per-stage entry before the task can resolve"
        )
    else:
        stage_flag = f" --stage {stage_index}" if stage_index is not None else ""
        detail = (
            "post a one-line progress note + artifact link (or, in PR-stage work, "
            "update the PR instead — the skill routes transport), then "
            f"`plugin-record --plugin tracker --phase progress{stage_flag}`"
        )
    return [PluginDirective(
        "tracker", "publish_progress", detail,
        data={"tracker_key": _key(bag), "stage": stage_index},
    )]


def _last_replan_kind(state) -> str | None:
    """Mirrors `_last_passed_stage_index`: the replan that just fired this
    observer is always the newest `event == "replan"` entry in `state.history`,
    so a backward scan recovers its `kind` ("no_change" / "refinement" /
    "substantive", set by `cmd_replan`) without threading it through `bag`."""
    for entry in reversed(getattr(state, "history", None) or []):
        if entry.get("event") == "replan":
            return entry.get("kind")
    return None


def _observe_replan(state, bag) -> list[PluginDirective]:
    # The engine already classifies every replan by `kind`; a `refinement` or
    # `no_change` replan is an internal correction with no reader-visible scope
    # change, so an unconditional "post what changed" directive here produces
    # exactly the bureaucratic planning-journal entries tech-writer rule 12
    # forbids — fold these instead of nudging a standalone post.
    kind = _last_replan_kind(state)
    if kind in ("refinement", "no_change"):
        detail = (
            f"replan kind={kind}: an internal correction, no reader-visible "
            "scope change. Do NOT post a standalone comment — fold one line "
            "into the next substantive ticket entry (tech-writer rule 12), "
            "then `plugin-record --plugin tracker --phase replan --skipped "
            f"--note \"{kind} replan, folded into next entry\"`"
        )
    else:
        detail = (
            "replan kind=substantive: post what changed in the plan and why, "
            "with a link to the revised plan, then `plugin-record --plugin "
            "tracker --phase replan`"
        )
    return [PluginDirective(
        "tracker", "publish_replan", detail,
        data={"tracker_key": _key(bag), "kind": kind},
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


def _passed_artifact_stages(state) -> list:
    """Every PASSED stage that declared output_artifacts without an
    ephemeral_artifacts_waiver — each one changed something a ticket reader
    could not otherwise discover, so each owes its own per-stage progress
    entry (see _observe_record_result's `journaled` branch)."""
    return [
        s for s in getattr(state, "stages", None) or []
        if getattr(s.outcome, "status", None) == StageStatus.PASSED.value
        and (s.output_artifacts or [])
        and not (s.ephemeral_artifacts_waiver or "").strip()
    ]


def _journal_gate(state, bag) -> list[str]:
    pub = _published(bag)
    missing = [s.index for s in _passed_artifact_stages(state)
               if f"progress:{s.index}" not in pub]
    if not missing:
        return []
    stage_list = ", ".join(str(i) for i in missing)
    return [f"stage(s) {stage_list} passed with declared output_artifacts but carry no "
            "per-stage ticket entry — post a substantive progress comment for each "
            "(figures/caveats/artifacts, not a one-liner), then `plugin-record --plugin "
            "tracker --phase progress --stage <n>`"]


def _resolution_gate(state, bag) -> list[str]:
    # Plugin.gates is a dict keyed by core-gate name, so only ONE callable can be
    # registered under "resolution" — combine both checks here rather than trying
    # to register two.
    return _publish_gate(state, bag) + _journal_gate(state, bag)


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
        gates={"resolution": _resolution_gate},
        state_factory=lambda: {"tracker_key": "", "published_phases": {}},
        terminal=_terminal,
        auto_activate=_auto_activate,
        # mirror what an explicit `plugin-activate --tracker-key` would seed —
        # without it the auto-activated bag's nudges carry an empty key
        auto_seed=lambda state: {"tracker_key": getattr(state, "tracker_key", "") or ""},
    )
)
