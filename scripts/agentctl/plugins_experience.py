"""The record-experience plugin: a skill-less consumer of the plugin layer.

CLAUDE.md § On task resolution makes the search -> extend-vs-new -> write
experience-leaf flow mandatory for every substantive task. Unlike tracker (which
the tracker-management skill activates on invocation), this flow has no owning
skill to turn it on — so the ENGINE auto-activates it for every substantive
session (`auto_activate` predicate, fired by cmd_classify). It then OBSERVES the
resolve attempt and surfaces a `record_experience` nudge, and a gate keeps the
task from closing until the coordinator has actually searched and then either
recorded or consciously skipped the leaf.

Division of labour (engine owns WHEN; the coordinator owns WHAT/WHERE):
  - WHEN — this plugin: the resolve nudge and the did-search-and-decide gate.
  - WHAT/WHERE — the coordinator: running `record-experience.py search/new/extend`
    and deciding whether the task clears the quality bar (record) or not (skip).

A phase is only marked done by the coordinator calling `agentctl plugin-record
--plugin experience --phase <searched|recorded|skipped>` AFTER the work happens —
so the gate reflects a real search + decision, never a mere intention. A skip
must carry a `--note` reason (enforced by cmd_plugin_record).
"""
from __future__ import annotations

from .plugins import Plugin, PluginDirective, register
from .state import WeightClass


def _complete(bag) -> bool:
    return bool(bag.get("searched")) and bool(bag.get("recorded") or bag.get("skipped"))


# --- observer: nudge on the resolve attempt until the flow is complete --------

def _observe_resolve(state, bag) -> list[PluginDirective]:
    if _complete(bag):
        return []
    return [PluginDirective(
        "experience", "record_experience",
        "search existing experience leaves, then extend-vs-new the leaf for this "
        "task's recurring difficulty (or consciously skip if below the quality "
        "bar): `record-experience.py search ...` then `agentctl plugin-record "
        "--plugin experience --phase <searched|recorded|skipped>`",
        blocking=True,
    )]


# --- gate: block resolution until searched AND (recorded OR skipped) -----------

def _experience_gate(state, bag) -> list[str]:
    if _complete(bag):
        return []
    missing = []
    if not bag.get("searched"):
        missing.append("searched")
    if not (bag.get("recorded") or bag.get("skipped")):
        missing.append("recorded|skipped")
    return [f"experience leaf flow incomplete — missing: {', '.join(missing)} "
            f"(run `record-experience.py search ...`, then `agentctl plugin-record "
            f"--plugin experience --phase <searched|recorded|skipped>`; a skip needs --note)"]


# --- lifecycle: task-scoped, retire once resolution truly passes --------------

def _terminal(state, event: str) -> bool:
    return event == "resolve" and bool(getattr(state.resolution, "passed", False))


register(
    Plugin(
        name="experience",
        scope="task",
        auto_activate=lambda state: getattr(state, "weight_class", None) == WeightClass.SUBSTANTIVE.value,
        observers={"resolve": _observe_resolve},
        gates={"resolution": _experience_gate},
        state_factory=lambda: {
            "searched": False, "decision": "", "recorded": False,
            "skipped": False, "skip_reason": "",
        },
        terminal=_terminal,
    )
)
