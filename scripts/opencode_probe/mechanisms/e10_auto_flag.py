"""E10: what the ``--auto`` flag of ``opencode run`` approves, and that it never overrides a deny.

One scripted shell call (a marker file) under each rule kind, with and without ``--auto``: a static
``ask`` rule, a static deny of the call's resource, a plugin ``permission.evaluate`` deny and a plugin
``tool.execute.before`` throw. Record fields: ``auto_effect`` (``approves-ask-only`` when the ask
rule runs under ``--auto`` and is rejected without it while every deny holds under ``--auto``;
``overrides-deny`` when any deny variant ran under ``--auto``; ``ask-not-approved`` or
``approves-without-flag`` otherwise) and ``marked_executed`` (a call under a plugin rule ran its
marker: with the plugin's refusals made inert this is the observation a negative run must show).
The decision of a real model under ``--auto`` is recorded unobservable. pass when ``auto_effect``
is ``approves-ask-only`` and no plugin-rule call ran; fail otherwise; unobserved when the ask
control produced no tool call or a run hung.
"""
from opencode_probe import record, runner
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E10"
K7 = True
REST_ON_PLUGIN_DENY = True
STOPPING_IS_FAIL = True

FIELDS = ("auto_effect", "marked_executed")
REAL_MODEL_REASON = "needs a paid model"
MARKER = "m-e10"
CALL_ID = "call_m1_0_0"
SCENARIO = {"models": {"m1": [{"text": "s", "tool_calls": [{"name": "shell",
                                                            "arguments": {"command": "touch {root}/" + MARKER}}]},
                              {"text": "done"}]}}
STATIC = {"ask": {"permissions": [{"action": "shell", "resource": "*", "effect": "ask"}]},
          "resource-deny": {"permissions": [{"action": "shell", "resource": "*touch*", "effect": "deny"}]}}
PLUGIN = {"plugin-deny": {"deny": [{"name": "shell", "contains": MARKER}]},
          "plugin-throw": {"throw": [{"name": "shell", "contains": MARKER}]}}
DENY_VARIANTS = ("resource-deny", *PLUGIN)


def verdict(observation):
    return sh.decide(observation["auto_effect"] != "approves-ask-only" or observation["marked_executed"],
                     STOPPING_IS_FAIL)


def classify(executed):
    """The ``auto_effect`` of the executed-marker map, keyed ``<variant>:auto`` / ``<variant>:no-auto``."""
    if any(executed[f"{name}:auto"] for name in DENY_VARIANTS):
        return "overrides-deny"
    if not executed["ask:auto"]:
        return "ask-not-approved"
    return "approves-without-flag" if executed["ask:no-auto"] else "approves-ask-only"


def _variant(h, name, auto):
    config = STATIC.get(name)
    rules = PLUGIN.get(name)
    with h.world(SCENARIO, config=config, rules=rules) as world:
        label = f"{name}:{'auto' if auto else 'no-auto'}"
        run = sh.launch_logged(h, world, sh.run_argv(world, auto=auto, prompt="go"), label, timeout=60)
        part = run.tool_part(CALL_ID)
        state = (part or {}).get("state") or {}
        return label, {"executed": run.exists(MARKER), "tool_part_seen": part is not None,
                       "status": state.get("status"), "error": state.get("error"),
                       "exit_code": run.exit_code, "timed_out": run.timed_out}


def run(ctx):
    h = ctx.harness()
    outcomes = dict(_variant(h, name, auto) for name in ("ask", "resource-deny", *PLUGIN) for auto in (True, False))
    executed = {label: o["executed"] for label, o in outcomes.items()}
    observation = {"auto_effect": classify(executed),
                   "marked_executed": any(executed[f"{name}:{mode}"] for name in PLUGIN for mode in ("auto", "no-auto"))}
    evidence = {**observation, "variants": outcomes, "no_hang": not any(o["timed_out"] for o in outcomes.values()),
                "real_model": sh.unobservable(REAL_MODEL_REASON)}
    if not outcomes["ask:auto"]["tool_part_seen"] or not evidence["no_hang"]:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(verdict(observation), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict, unobservable_parts={"real_model": REAL_MODEL_REASON})
