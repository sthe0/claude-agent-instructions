"""A1: with the gate shim loaded, a denying gate refuses the call on every path id the mechanisms record lists.

The path ids are the ``paths`` of the pinned mechanisms record's P1 entry. One run of the scripted
session that calls each path once loads the probe plugin (passive, no rule) and the gate shim, with a
denying stand-in row (matcherless, exits 2) added through ``AGENT_GATE_EXTRA_ROWS``; Core's own
denying rows deny only on engine state, which a stub-driven session has none of. The Code Mode inner
call is run on its own with a row whose matcher fits only the inner tool, so the wrapper is not
what stops it; a ``patch`` call (the file-editing tool of the gpt family) is run on its own under the denying row; the nested ``child-run`` process is likewise run alone with a row that refuses only
its own ``touch`` command, so the outer call that launches it passes. A path is refused when its effect did not happen and the shim's log holds a refusal
for that call (the inner call: for a tool of the inner namespace). pass when every listed path is
refused; fail when some path's effect happened (``marked_executed``); unobserved when the pinned
record or a listed path is missing, or a path was neither executed nor logged as refused.
"""
from opencode_probe import core, paths, record, runner
from opencode_probe.adapter import shared_a as sa

CASE_ID = "A1"
K7 = True
REST_ON_PLUGIN_DENY = True

INNER_MATCHER = f"mcp__{paths.INNER_NAMESPACE}__.*"
PATCH_PATH_ID = "root:patch"
PATCH_MODEL = "gpt-5"
PATCH_TARGET = "m-patch"


def pinned_path_ids(state):
    pin = core.read_pin().get("last_pass", {}).get("mechanisms")
    found = record.pinned_record(state, pin)
    if found is None:
        return None
    entry = (found[1].get("cases") or {}).get("P1") or {}
    return [p["id"] for p in entry.get("paths", [])]


def _rows(matcher=None, script=sa.DENY_ALL):
    row = {"event": "PreToolUse", "script": str(script), "timeout_s": 10}
    if matcher:
        row["matcher"] = matcher
    return sa.extra_rows([row])


def _gated(h, plan, label, ctx, rows_text):
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": rows_text}
    return paths.exercise(h, plan, label, config=sa.gate_config(), files=files,
                          env=sa.gate_env(AGENT_GATE_PLUGIN_TOOLS="probe_tool"))


def _path_entry(run, plan, spec, entries):
    out = paths.outcome(run, plan, spec)
    call_id = spec.refusal_call_id or plan.call_ids[spec.path_id]
    if spec.path_id == paths.INNER_PATH_ID:
        logged = sa.refusal_logged(entries, tool_prefix=f"{paths.INNER_NAMESPACE}_")
    else:
        logged = sa.refusal_logged(entries, call_id=call_id)
    error = str(out.get("error") or "")
    carried_out = out["executed"]
    if spec.effect == "dispatched" and sa.DENY_REASON in error:
        carried_out = False  # a dispatched-only effect shows no side effect; the gate's reason in the error is the refusal
    if carried_out:
        verdict = "unrefused"
    elif logged:
        verdict = "refused"
    else:
        verdict = "unconfirmed"
    return {"id": spec.path_id, "tool": spec.tool, "verdict": verdict, "refusal_logged": logged,
            "reason_in_error": sa.DENY_REASON in error, "status": out["status"]}


def _patch_path(h, ctx):
    """The gpt family is offered ``patch`` in place of edit and write, a path the mechanisms record does not
    list (its stub model is not the one the P1 scenario drives): a patch that adds a file runs under the
    denying row, and the file must not appear."""
    patch = ("*** Begin Patch\n*** Add File: {root}/" + PATCH_TARGET + "\n+x\n*** End Patch")
    scenario = {"models": {PATCH_MODEL: [{"tool_calls": [{"name": "patch", "arguments": {"patchText": patch}}]},
                                         {"text": "done"}]}}
    config = sa.gate_config({"providers": {"stub": {"models": {PATCH_MODEL: {"name": PATCH_MODEL}}}}})
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": _rows()}
    run = h.run(scenario, label="gated-patch", config=config, files=files, env=sa.gate_env(),
                model=f"stub/{PATCH_MODEL}", timeout=120)
    logged = sa.refusal_logged(sa.gate_log(run), tool_prefix="patch")
    executed = run.exists(PATCH_TARGET)
    verdict = "unrefused" if executed else "refused" if logged else "unconfirmed"
    return run, {"id": PATCH_PATH_ID, "tool": "patch", "verdict": verdict, "refusal_logged": logged,
                 "reason_in_error": sa.DENY_REASON in run.stdout + run.stderr, "status": run.exit_code}


def run(ctx):
    ids = pinned_path_ids(ctx.state or core.state_dir())
    if not ids:
        return runner.result(record.UNOBSERVED, {"error": "the pinned mechanisms record lists no P1 paths"})
    h = ctx.harness()
    missing = [i for i in ids if i not in paths.PATH_IDS]
    if missing:
        return runner.result(record.UNOBSERVED, {"error": "pinned path ids unknown to this suite", "missing": missing})
    alone = {paths.INNER_PATH_ID: ("gated-inner", _rows(INNER_MATCHER)),
             "child-run": ("gated-child", _rows(script=sa.DENY_TOUCH))}
    outer_plan = paths.build_plan(include=tuple(i for i in ids if i not in alone))
    outer = _gated(h, outer_plan, "gated", ctx, _rows())
    entries = [_path_entry(outer, outer_plan, spec, sa.gate_log(outer)) for spec in outer_plan.specs]
    runs = [outer]
    for path_id in (i for i in ids if i in alone):
        label, rows_text = alone[path_id]
        plan = paths.build_plan(include=(path_id,))
        run = _gated(h, plan, label, ctx, rows_text)
        runs.append(run)
        entries.append(_path_entry(run, plan, plan.specs[0], sa.gate_log(run)))
    patch_run, patch_entry = _patch_path(h, ctx)
    runs.append(patch_run)
    entries.append(patch_entry)
    unrefused = [e["id"] for e in entries if e["verdict"] == "unrefused"]
    unconfirmed = [e["id"] for e in entries if e["verdict"] == "unconfirmed"]
    evidence = {"path_ids": ids, "exit_codes": {r.label: r.exit_code for r in runs},
                "marked_executed": bool(unrefused), "unrefused": unrefused, "unconfirmed": unconfirmed,
                "gate_log_events": sorted({e.get("at") for r in runs for e in sa.gate_log(r)}),
                "inert_shim": bool(ctx.inert)}
    if unrefused:
        status = record.FAIL
    elif unconfirmed:
        status = record.UNOBSERVED
    else:
        status = record.PASS
    return runner.result(status, evidence, paths=entries)
