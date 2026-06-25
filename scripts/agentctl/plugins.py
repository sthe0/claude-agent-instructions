"""The plugin layer: pluggable sub-state-machines for the coordination engine.

The core spine (machine.TRANSITIONS, gates.GUARDIANS, cli.COMMANDS) is a closed
monolith on purpose — its nodes and gates are the contract every session obeys.
A *plugin* lets a skill / tool / specialization whose own workflow is
deterministic (most acutely tracker-management) hang a sub-state-machine off that
spine WITHOUT editing the core literals:

  - it OBSERVES core events (one event per coordination command) and may emit
    `PluginDirective`s — nudges the engine surfaces to the coordinator;
  - it carries its own per-session STATE bag (`state.plugins[name]`), opaque to
    the core;
  - it may contribute GATES that fold into a core gate (resolution / plan_approval);
  - it has a LIFECYCLE: scope 'task' (retired at task boundary) or 'phase' (an
    optional `terminal(state, event)` predicate auto-retires it mid-task once its
    sub-workflow completes — archived into `state.plugins_archive`).

Two distinct concerns, mirrored in the API:
  - REGISTRATION (`register`) — the engine KNOWS the plugin exists. Import-time,
    into the module-level REGISTRY (the catalog of available plugins).
  - ACTIVATION (`state.plugins[name]`) — the plugin PARTICIPATES in THIS session.
    The owning skill activates it on invocation via `agentctl plugin-activate`.
    Presence of the name as a key in `state.plugins` == activated.

No new core `Node` values are introduced here (deliberate scope fence — see the
README): a plugin reacts to EXISTING transitions via observers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

# An observer sees the post-command state and its own bag, and may return
# PluginDirectives to surface. It mutates the bag in place for persistence.
Observer = Callable[["object", dict], "list[PluginDirective] | None"]
# A plugin guardian sees the state and its bag, and returns human-readable
# blockers ([] == may pass). Distinct from a core guardian (state-only) because a
# plugin gate decides off its own bag.
Guardian = Callable[["object", dict], "list[str]"]
# A terminal predicate: has this plugin's sub-workflow finished as of `event`?
Terminal = Callable[["object", str], bool]


@dataclass
class PluginDirective:
    """A plugin's nudge to the coordinator, surfaced under
    Directive.data['plugin_directives']. `blocking` flags a directive whose
    action must happen before the gated transition can pass (paired with a gate)."""
    plugin: str
    action: str
    detail: str = ""
    blocking: bool = False
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plugin:
    """A registered sub-state-machine. `observers`/`gates` key on event / core-gate
    names. `state_factory` seeds a fresh bag on activation. `scope` + `terminal`
    govern lifecycle; `commands` are optional extra CLI verbs (name -> handler)."""
    name: str
    scope: str = "task"  # 'task' (retire at task boundary) | 'phase' (terminal auto-retire)
    observers: dict[str, Observer] = field(default_factory=dict)
    gates: dict[str, Guardian] = field(default_factory=dict)
    state_factory: Callable[[], dict] = dict
    terminal: Terminal | None = None
    commands: dict[str, Callable] = field(default_factory=dict)


# event name a plugin observes <- coordination command that produces it. Only
# commands that mark an observable spine point appear here; meta commands (start,
# reset, status, plugin-activate/deactivate, resolve-permission) do not fire.
EVENT_FOR_COMMAND: dict[str, str] = {
    "classify": "classify",
    "plan": "plan",
    "submit-plan": "submit_plan",
    "approve": "approve",
    "partition": "partition",
    "next-stage": "next_stage",
    "dispatch": "dispatch",
    "record-result": "record_result",
    "verify-final": "verify_final",
    "resolve": "resolve",
    "replan": "replan",
    "declare": "declare",
    "investigate": "investigate",
    "critique": "critique",
    "block": "block",
    "unblock": "unblock",
}


# --- registry (the catalog of available plugins) -----------------------------
REGISTRY: dict[str, Plugin] = {}


def register(plugin: Plugin) -> Plugin:
    """Import-time registration into the catalog. Idempotent by name (last wins),
    so re-import under test reload does not raise."""
    REGISTRY[plugin.name] = plugin
    return plugin


def event_for(command: str) -> str | None:
    return EVENT_FOR_COMMAND.get(command)


def active(state) -> list[Plugin]:
    """Plugins ACTIVE for this session: name present in state.plugins AND known to
    the registry. A bag whose plugin code was removed is skipped gracefully."""
    return [REGISTRY[name] for name in state.plugins if name in REGISTRY]


def activate(state, name: str, seed: dict | None = None) -> dict:
    """Attach a registered plugin to this session: seed its bag (state_factory,
    overlaid with `seed`). Re-activation merges `seed` into the existing bag
    (idempotent — safe to re-run on session resume). Returns the bag."""
    plugin = REGISTRY.get(name)
    bag = state.plugins.get(name)
    if bag is None:
        bag = plugin.state_factory() if plugin is not None else {}
        state.plugins[name] = bag
    if seed:
        bag.update(seed)
    return bag


def deactivate(state, name: str) -> bool:
    """Manual retire (escape hatch): archive the bag and drop from the active set.
    Returns True if the plugin was active."""
    bag = state.plugins.pop(name, None)
    if bag is None:
        return False
    state.plugins_archive[name] = bag
    return True


def fire(event: str, state, directive) -> list[dict]:
    """Run every active plugin's observer for `event`, collect PluginDirectives
    onto directive.data['plugin_directives'], THEN evaluate each active plugin's
    terminal predicate and auto-retire those that fire (bag -> plugins_archive,
    dropped from the active set). Deterministic, engine-owned — observers run
    before terminal so the event that completes a sub-workflow is still observed."""
    fired: list[dict] = []
    for plugin in active(state):
        bag = state.plugins.get(plugin.name)
        if bag is None:  # defensive: ensure the bag exists before observing
            bag = plugin.state_factory()
            state.plugins[plugin.name] = bag
        observer = plugin.observers.get(event)
        if observer is None:
            continue
        for pd in observer(state, bag) or []:
            fired.append(pd.to_dict() if isinstance(pd, PluginDirective) else dict(pd))

    # auto-retire any active plugin whose terminal predicate fires on this event
    for plugin in list(active(state)):
        if plugin.terminal is not None and plugin.terminal(state, event):
            bag = state.plugins.pop(plugin.name, None)
            if bag is not None:
                state.plugins_archive[plugin.name] = bag

    if fired:
        prior = directive.data.get("plugin_directives", [])
        directive.data["plugin_directives"] = prior + fired
    return fired


def plugin_gate_blockers(state, gate_name: str) -> list[str]:
    """Aggregate the blockers every active plugin contributes to core gate
    `gate_name` (a plugin gates a core transition by keying its guardian under that
    gate's name). [] when no active plugin extends the gate — so a plugin-less
    session sees byte-identical gate behavior."""
    out: list[str] = []
    for plugin in active(state):
        guardian = plugin.gates.get(gate_name)
        if guardian is None:
            continue
        bag = state.plugins.get(plugin.name) or {}
        out.extend(f"[{plugin.name}] {b}" for b in guardian(state, bag))
    return out


# --- built-in dummy plugin ---------------------------------------------------
# A minimal, inert-until-activated plugin proving the framework end to end with
# ZERO edits to the three core literals. Phase-scoped with a terminal predicate so
# it also exercises mid-task auto-retire. Observers/gate read & mutate only the bag.

def _dummy_observe_approve(state, bag) -> list[PluginDirective]:
    bag["observed"] = bag.get("observed", 0) + 1
    return [PluginDirective("dummy", "noted_approve", "dummy observed approve", data={"count": bag["observed"]})]


def _dummy_resolution_gate(state, bag) -> list[str]:
    # blocks resolution until the bag is explicitly cleared (its 'observed' reset),
    # mirroring how a real plugin gates on an unmet sub-condition.
    return [] if bag.get("cleared") else ["dummy gate: bag not cleared"]


def _dummy_terminal(state, event: str) -> bool:
    # retire once the dummy has been told it is done (set via a future command or
    # a seed); keeps the phase-scoped lifecycle observable in tests.
    return event == "unblock"


register(
    Plugin(
        name="dummy",
        scope="phase",
        observers={"approve": _dummy_observe_approve},
        gates={"resolution": _dummy_resolution_gate},
        state_factory=lambda: {"observed": 0, "cleared": False},
        terminal=_dummy_terminal,
    )
)


# --- built-in consumers ------------------------------------------------------
# Imported for the side effect of registering into REGISTRY, so any importer of
# this module gets the full catalog of available plugins (the contract
# verify-agentctl.py and the tests rely on). Placed at the bottom so the names
# the consumer module imports from here (Plugin/PluginDirective/register) are
# already defined when its `from .plugins import ...` runs.
from . import plugins_tracker as _plugins_tracker  # noqa: E402,F401
