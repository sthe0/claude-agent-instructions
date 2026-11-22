"""P1: every execution path the model can reach is refused by at least one mechanism.

Three runs of one scripted session that calls each path once: a control with the probe
plugin loaded and no rule, a run where only the plugin's ``tool.execute.before`` throws, and a
run where only the ``permission.evaluate`` hook denies. A path counts as executed by its effect
(a marker file, a changed file, a request the stub saw, a dispatched call), and a mechanism is
credited with a refusal only on a positive event for that call id (the plugin's own refusal log
entry or the tool part's error text); an unexecuted call with no such event is unconfirmed.
The Code Mode wrapper ``execute`` is two paths: its own fetch and a call it makes to a
side-effecting inner tool (an MCP tool ``paths.INNER_NAMESPACE`` writing a marker file); the inner
path's entry carries ``code_mode``: whether ``tool.execute.before`` and ``permission.evaluate``
fire for the inner call under its own name, whether a rule of either stops the marker, whether
``execute`` can be switched off by an agent deny, and the outcome ``inner-call`` (a mechanism sees
and stops the inner call) or ``wrapper-only`` (only refusing or switching off the wrapper stops
it); an inner call nothing stops is a fail. Only a tool of ``paths.OPTIONAL_TOOLS`` whose control
call the runtime rejects as unavailable is left out of the paths; any other path whose control
call did not execute makes the case unobserved. Every tool the stub received in the model's tool
list is either a path or an exclusion that carries the reason, the tool's schema and description
and a run showing whether a deny removes it from the list; that switch-off run counts only when it
exited 0, made a chat request carrying tools and still lists the path tools it did not deny. pass
when the control executed every path, each path is refused by at least one mechanism and every
listed tool is accounted for; fail when some path ran under both mechanisms; unobserved otherwise.
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
    wrapper_ids = {plan.call_ids[p] for p in ("root:execute", paths.INNER_PATH_ID)}
    issued = set(plan.call_ids.values())
    before = [e["input"] for e in control.probe_entries("tool.execute.before")]
    evaluated = [e["input"] for e in control.probe_entries("permission.evaluate")
                 if (e["input"].get("source") or {}).get("id") in wrapper_ids]
    return {"wrapper_before_fired": all(any(b.get("id") == i for b in before) for i in wrapper_ids),
            "before_tools_for_unissued_calls": sorted({str(b.get("tool")) for b in before
                                                       if str(b.get("id")).startswith("call_m1_")
                                                       and b.get("id") not in issued}),
            "evaluate_actions_for_wrapper_calls": sorted({str(e.get("action")) for e in evaluated})}


def _logged_refusal(run, mechanism, **match):
    """The plugin's own log shows ``mechanism`` refusing a call that matches ``match``."""
    return any(e.get("mechanism") == mechanism and all(e.get(k) == v for k, v in match.items())
               for e in run.probe_entries("refusal"))


def _inner_hook_inputs(control, plan, spec):
    """The hook inputs the control run's log holds for the inner call of the ``execute`` wrapper:
    ``tool.execute.before`` inputs naming the inner namespace's tool (never the wrapper's own id)
    and ``permission.evaluate`` inputs for the wrapper's call id or the inner call's id, or naming
    the inner tool or the marker in their resources (never the wrapper's own action)."""
    wrapper = plan.call_ids[spec.path_id]
    before = [e["input"] for e in control.probe_entries("tool.execute.before")
              if paths.INNER_NAMESPACE in str(e["input"].get("tool")) and e["input"].get("id") != wrapper]
    inner_ids = {wrapper} | {b.get("id") for b in before}
    evaluated = [e["input"] for e in control.probe_entries("permission.evaluate")
                 if e["input"].get("action") != spec.tool
                 and ((e["input"].get("source") or {}).get("id") in inner_ids
                      or paths.INNER_NAMESPACE in str(e["input"].get("action"))
                      or spec.token in json.dumps(e["input"].get("resources")))]
    return before, evaluated


def _code_mode_inner_call(h, control, plan, wrapper_refused):
    """The Code Mode facts for the inner call: which hooks see it under its own name, whether a
    rule of each stops its side effect, and whether an agent deny switches ``execute`` off."""
    spec = plan.spec(paths.INNER_PATH_ID)
    inner_plan = paths.build_plan(include=(paths.INNER_PATH_ID,))
    inner_spec = inner_plan.spec(paths.INNER_PATH_ID)

    def ran(run):
        return paths.executed(run, inner_plan, inner_spec)

    before, evaluated = _inner_hook_inputs(control, plan, spec)
    before_tools = sorted({str(b.get("tool")) for b in before})
    own_eval = [e for e in evaluated if paths.INNER_NAMESPACE in str(e.get("action"))
                or spec.token in json.dumps(e.get("resources"))]
    eval_actions = sorted({str(e.get("action")) for e in own_eval})
    inner_control = paths.exercise(h, inner_plan, "inner-control")
    runs = [inner_control]
    stops_before = stops_eval = None
    if before_tools:
        thrown = paths.exercise(h, inner_plan, "inner-throw", rules={"throw": [{"name": before_tools[0]}]})
        runs.append(thrown)
        stops_before = not ran(thrown) and _logged_refusal(thrown, PRE, tool=before_tools[0])
    if eval_actions:
        rule = {"name": eval_actions[0]}
        if spec.token in json.dumps(own_eval[0].get("resources")):
            rule["contains"] = spec.token
        denied = paths.exercise(h, inner_plan, "inner-deny", rules={"deny": [rule]})
        runs.append(denied)
        stops_eval = not ran(denied) and _logged_refusal(denied, EVAL, action=eval_actions[0])
    off_agent = {"agents": {SWITCH_OFF_AGENT: {"permissions": [
        {"action": spec.tool, "resource": "*", "effect": "deny"}]}}}
    off = paths.exercise(h, inner_plan, "execute-off", env={SWITCH_OFF_CHANNEL: json.dumps(off_agent)},
                         cli=("--agent", SWITCH_OFF_AGENT))
    runs.append(off)
    switchable = bool(off.exit_code == 0 and any(r.get("tools") for r in off.requests)
                      and spec.tool not in off.tool_list() and not ran(off))
    if stops_before or stops_eval:
        outcome = "inner-call"
    elif wrapper_refused or switchable:
        outcome = "wrapper-only"
    else:
        outcome = "unstoppable"
    facts = {"before_fires_for_inner_call_under_own_name": bool(before_tools),
             "before_tools_for_inner_call": before_tools,
             "evaluate_fires_for_inner_call_under_own_name": bool(eval_actions),
             "evaluate_actions_for_inner_call": eval_actions,
             "before_rule_stops_inner_effect": stops_before,
             "evaluate_deny_stops_inner_effect": stops_eval,
             "execute_switchable_by_config": switchable,
             "wrapper_refusal_stops_inner_effect": bool(wrapper_refused),
             "outcome": outcome, "inner_control_executed": ran(inner_control),
             "exit_codes": {r.label: r.exit_code for r in runs}}
    return facts


def switch_off_problems(run, expected_listed):
    """Why the switch-off run does not show what a deny removes from the tool list: it did not
    exit 0, made no chat request carrying tools, or lacks a tool it did not deny."""
    problems = []
    if run.exit_code != 0:
        problems.append(f"exit code {run.exit_code}")
    if not any(r.get("tools") for r in run.requests):
        problems.append("no chat request carried tools")
    problems += [f"path tool {t!r} not in its tool list" for t in expected_listed if t not in run.tool_list()]
    return problems


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
        if paths.may_be_unavailable(spec, base):
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
    switched_off = h.run({"models": {"m1": [{"text": "idle"}]}}, label="switch-off", rules=plan.base_rules,
                         config=plan.config, files=plan.files, env={SWITCH_OFF_CHANNEL: json.dumps(denied_agent)},
                         cli=("--agent", SWITCH_OFF_AGENT))
    switch_off_invalid = switch_off_problems(switched_off, sorted(covered & set(tool_list)))
    exclusions = {t: _exclusion(t, control, switched_off, unavailable) for t in left_out}
    unreasoned = [t for t, e in exclusions.items() if not e["reason"]]
    not_switchable = [t for t, e in exclusions.items() if not e["absent_from_tool_list_when_denied"]]
    unrefused = [e["id"] for e in entries if e["verdict"] == "unrefused"]
    unconfirmed = [e["id"] for e in entries if e["verdict"] == "unconfirmed"]
    inner = next(e for e in entries if e["id"] == paths.INNER_PATH_ID) \
        if any(s.path_id == paths.INNER_PATH_ID for s in path_specs) else None
    if inner is not None and inner["evidence"]["control"]["executed"]:
        inner["code_mode"] = _code_mode_inner_call(h, control, plan, PRE in inner["refused_by"])
        inner["refused_scope"] = "inner-call" if inner["code_mode"]["outcome"] == "inner-call" else "wrapper-only"
    code_mode_unobserved = inner is None or "code_mode" not in inner \
        or not inner["code_mode"]["inner_control_executed"]
    inner_unstoppable = inner is not None and "code_mode" in inner \
        and inner["code_mode"]["outcome"] == "unstoppable"
    evidence = {
        "exit_codes": {r.label: r.exit_code for r in (control, throw, deny, switched_off)},
        "switch_off_invalid": switch_off_invalid, "inner_call_unstoppable": inner_unstoppable,
        "evaluate_rules": eval_rules, "evaluate_not_called_for": no_eval,
        "marked_executed": bool(unrefused), "unrefused": unrefused, "unconfirmed": unconfirmed,
        "control_not_executed": control_not_executed,
        "tool_list": tool_list, "tool_list_not_accounted_for": unreasoned,
        "exclusions": exclusions, "excluded_not_switchable": not_switchable,
        "unavailable_tools": unavailable, "tool_list_has_unavailable": sorted(set(unavailable) & set(tool_list)),
        "code_mode": _code_mode(control, plan),
        "sessions_with_hooks": sorted({e["input"].get("sessionID") for e in control.probe_entries("tool.execute.before")}),
    }
    if unrefused or inner_unstoppable:
        status = record.FAIL
    elif control_not_executed or unconfirmed or unreasoned or not_switchable or switch_off_invalid \
            or code_mode_unobserved:
        status = record.UNOBSERVED
    else:
        status = record.PASS
    return runner.result(status, evidence, paths=entries)


CODE_MODE_FIELDS = ("before_fires_for_inner_call_under_own_name", "evaluate_fires_for_inner_call_under_own_name",
                    "before_rule_stops_inner_effect", "evaluate_deny_stops_inner_effect",
                    "execute_switchable_by_config", "outcome")


def _code_mode_problems(inner):
    """The inner Code Mode path's ``code_mode`` facts are all there and consistent with its outcome."""
    if inner is None:
        return []
    facts = inner.get("code_mode")
    if not isinstance(facts, dict):
        return [f"path {paths.INNER_PATH_ID!r} has no `code_mode` facts"]
    problems = [f"code_mode lacks {field!r}" for field in CODE_MODE_FIELDS if field not in facts]
    outcome = facts.get("outcome")
    if outcome not in ("inner-call", "wrapper-only"):
        problems.append(f"code_mode outcome {outcome!r} is neither inner-call nor wrapper-only")
    stops = facts.get("before_rule_stops_inner_effect") or facts.get("evaluate_deny_stops_inner_effect")
    if outcome == "inner-call" and not stops:
        problems.append("code_mode outcome is inner-call but no rule stopped the inner effect")
    if outcome == "wrapper-only" and stops:
        problems.append("code_mode outcome is wrapper-only but a rule stopped the inner effect")
    if outcome == "wrapper-only" and inner.get("refused_scope") != "wrapper-only":
        problems.append("code_mode outcome is wrapper-only but the path is not marked refused_scope wrapper-only")
    if facts.get("before_rule_stops_inner_effect") is not None and not facts.get("before_fires_for_inner_call_under_own_name"):
        problems.append("code_mode says a before rule stops the inner call that no before hook saw")
    if facts.get("evaluate_deny_stops_inner_effect") is not None and not facts.get("evaluate_fires_for_inner_call_under_own_name"):
        problems.append("code_mode says an evaluate deny stops the inner call that no evaluate hook saw")
    return problems


def check_record(entry, cases):
    """``paths`` entries with id, tool, ``refused_by`` and a verdict that follows from them; every
    path of the suite is there unless it is a ``paths.OPTIONAL_TOOLS`` tool recorded as unavailable;
    the inner Code Mode path carries its ``code_mode`` facts; every tool of the recorded tool list
    is a path or a fully evidenced exclusion."""
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
    unavailable = set(evidence.get("unavailable_tools") or {})
    problems += [f"tool {t!r} is recorded unavailable but is not an optional tool" for t in sorted(unavailable)
                 if t not in paths.OPTIONAL_TOOLS]
    present = {p.get("id") for p in listed}
    problems += [f"path {s.path_id!r} is missing from `paths`" for s in paths.build_plan().specs
                 if s.path_id not in present and not (s.tool in paths.OPTIONAL_TOOLS and s.tool in unavailable)]
    problems += _code_mode_problems(next((p for p in listed if p.get("id") == paths.INNER_PATH_ID), None))
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
