"""P1: every execution path the model can reach is refused by at least one mechanism.

Three runs of one scripted session that calls each path once: a control with the probe
plugin loaded and no rule, a run where only the plugin's ``tool.execute.before`` throws, and a
run where only the ``permission.evaluate`` hook denies. A path counts as executed by its effect
(a marker file, a changed file, a request the stub saw, a dispatched call), and a mechanism is
credited with a refusal only on a positive event for that call id (the plugin's own refusal log
entry or the tool part's error text); an unexecuted call with no such event is unconfirmed.
The Code Mode wrapper ``execute`` is two paths: its own fetch and a call it makes to an inner tool.
A listed tool whose control call is rejected by the runtime as unavailable is not a path. Every
tool the stub received in the model's tool list is either a path or an exclusion that carries the
reason, the tool's schema and description and a run showing whether a deny removes it from the
list. pass when the control executed every path, each path is refused by at least one mechanism
and every listed tool is accounted for; fail when some path ran under both mechanisms;
unobserved otherwise.
"""
import json

from opencode_probe import paths, record, runner

CASE_ID = "P1"
K7 = True
REST_ON_PLUGIN_DENY = True

PRE = "PreToolUse"
EVAL = "permission.evaluate"
SWITCH_OFF_CHANNEL = "OPENCODE_CONFIG_CONTENT"
SWITCH_OFF_AGENT = "slim"

EXCLUSION_REASONS = {
    "websearch": "sends the query to an external search service; the suite runs against the stub "
                 "provider only, so no call is made and the tool is judged by schema, description "
                 "and the switch-off run",
}


def _tool_entry(spec, base, by_throw, by_eval, has_eval_rule):
    throw_ok = not by_throw["executed"] and by_throw.get("refusal_event")
    eval_ok = has_eval_rule and not by_eval["executed"] and by_eval.get("refusal_event")
    refused_by = [name for name, ok in ((PRE, throw_ok), (EVAL, eval_ok)) if ok]
    if refused_by:
        verdict = "refused"
    elif by_throw["executed"] and (not has_eval_rule or by_eval["executed"]):
        verdict = "unrefused"
    else:
        verdict = "unconfirmed"
    return {"id": spec.path_id, "tool": spec.tool, "refused_by": refused_by, "verdict": verdict,
            "evidence": {"control": base, PRE: by_throw, EVAL: by_eval}}


def _exclusion(tool, control, switched_off, unavailable):
    reason = EXCLUSION_REASONS.get(tool, "")
    if tool in unavailable:
        reason = f"listed to the model but the runtime rejects a call to it: {unavailable[tool]}"
    return {"reason": reason,
            "schema": control.tool_schema(tool),
            "description": control.tool_description(tool)[:600],
            "absent_from_tool_list_when_denied": tool not in switched_off.tool_list()}


def _code_mode(control, plan):
    """What the control run's hook log shows for the ``execute`` wrapper: whether its own
    ``tool.execute.before`` fired, which tools that hook names for ``call_m1_*`` ids the scenario
    did not issue (an inner call), and the actions ``permission.evaluate`` saw for its call ids."""
    wrapper_ids = {plan.call_ids[p] for p in ("root:execute", "root:execute-inner")}
    issued = set(plan.call_ids.values())
    before = [e["input"] for e in control.probe_entries("tool.execute.before")]
    evaluated = [e["input"] for e in control.probe_entries("permission.evaluate")
                 if (e["input"].get("source") or {}).get("id") in wrapper_ids]
    return {"wrapper_before_fired": all(any(b.get("id") == i for b in before) for i in wrapper_ids),
            "before_tools_for_unissued_calls": sorted({str(b.get("tool")) for b in before
                                                       if str(b.get("id")).startswith("call_m1_")
                                                       and b.get("id") not in issued}),
            "evaluate_actions_for_wrapper_calls": sorted({str(e.get("action")) for e in evaluated})}


def run(ctx):
    h = ctx.harness()
    plan = paths.build_plan()
    control = paths.exercise(h, plan, "control")
    throw = paths.exercise(h, plan, "throw-only", rules={"throw": [paths.throw_rule(s) for s in plan.specs]})
    eval_rules, no_eval = [], []
    for spec in plan.specs:
        rule = paths.eval_rule(control, plan, spec)
        (eval_rules if rule else no_eval).append(rule or spec.path_id)
    deny = paths.exercise(h, plan, "evaluate-only", rules={"deny": eval_rules})

    entries, control_not_executed, unavailable, path_specs = [], [], {}, []
    for spec in plan.specs:
        base = paths.outcome(control, plan, spec)
        if paths.UNAVAILABLE_TEXT in str(base["error"]):
            unavailable[spec.tool] = base["error"]
            continue
        path_specs.append(spec)
        by_throw = paths.outcome(throw, plan, spec, PRE)
        by_eval = paths.outcome(deny, plan, spec, EVAL)
        if not base["executed"]:
            control_not_executed.append(spec.path_id)
        entries.append(_tool_entry(spec, base, by_throw, by_eval, spec.path_id not in no_eval))
    tool_list = control.tool_list()
    covered = {s.tool for s in path_specs}
    left_out = [t for t in tool_list if t not in covered]
    denied_agent = {"agents": {SWITCH_OFF_AGENT: {"permissions": [
        {"action": t, "resource": "*", "effect": "deny"} for t in left_out]}}}
    switched_off = h.run({"models": {"m1": [{"text": "idle"}]}}, label="switch-off",
                         env={SWITCH_OFF_CHANNEL: json.dumps(denied_agent)},
                         cli=("--agent", SWITCH_OFF_AGENT))
    exclusions = {t: _exclusion(t, control, switched_off, unavailable) for t in left_out}
    unreasoned = [t for t, e in exclusions.items() if not e["reason"]]
    not_switchable = [t for t, e in exclusions.items() if not e["absent_from_tool_list_when_denied"]]
    unrefused = [e["id"] for e in entries if e["verdict"] == "unrefused"]
    unconfirmed = [e["id"] for e in entries if e["verdict"] == "unconfirmed"]
    evidence = {
        "exit_codes": {r.label: r.exit_code for r in (control, throw, deny, switched_off)},
        "evaluate_rules": eval_rules, "evaluate_not_called_for": no_eval,
        "marked_executed": bool(unrefused), "unrefused": unrefused, "unconfirmed": unconfirmed,
        "control_not_executed": control_not_executed,
        "tool_list": tool_list, "tool_list_not_accounted_for": unreasoned,
        "exclusions": exclusions, "excluded_not_switchable": not_switchable,
        "unavailable_tools": unavailable, "tool_list_has_unavailable": sorted(set(unavailable) & set(tool_list)),
        "code_mode": _code_mode(control, plan),
        "sessions_with_hooks": sorted({e["input"].get("sessionID") for e in control.probe_entries("tool.execute.before")}),
    }
    if unrefused:
        status = record.FAIL
    elif control_not_executed or unconfirmed or unreasoned or not_switchable:
        status = record.UNOBSERVED
    else:
        status = record.PASS
    return runner.result(status, evidence, paths=entries)


def check_record(entry, cases):
    """``paths`` entries with id, tool, ``refused_by`` and a verdict that follows from them;
    every tool of the recorded tool list is a path or a fully evidenced exclusion."""
    listed = entry.get("paths")
    if not listed:
        return ["no `paths` list"]
    problems = [f"path {p.get('id')!r} has no id, tool, verdict or refused_by list" for p in listed
                if not p.get("id") or not p.get("tool") or not p.get("verdict")
                or not isinstance(p.get("refused_by"), list)]
    problems += [f"path {p['id']!r} is refused_by {p['refused_by']} but its verdict is {p['verdict']!r}"
                 for p in listed if p.get("id") and isinstance(p.get("refused_by"), list)
                 and bool(p["refused_by"]) != (p.get("verdict") == "refused")]
    evidence = entry.get("evidence", {})
    tool_list = evidence.get("tool_list")
    if not tool_list:
        return problems + ["no recorded tool list (the tools the stub received)"]
    exclusions = evidence.get("exclusions") or {}
    covered = {p.get("tool") for p in listed}
    for tool in tool_list:
        if tool in covered:
            continue
        excluded = exclusions.get(tool)
        if not excluded:
            problems.append(f"tool {tool!r} is in the model's tool list and in neither `paths` nor `exclusions`")
        elif not (excluded.get("reason") and excluded.get("schema") and excluded.get("description")
                  and isinstance(excluded.get("absent_from_tool_list_when_denied"), bool)):
            problems.append(f"exclusion {tool!r} lacks a reason, schema, description or switch-off result")
    return problems
