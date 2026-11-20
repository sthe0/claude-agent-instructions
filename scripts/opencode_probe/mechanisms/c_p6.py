"""P6: what OpenCode reports about a run's outcome and its usage, for a cost ledger to read.

Characterization, always recorded: the key sets of the ``run --format json`` events and the exit
codes of a successful run, a model error (the stub answers HTTP 400) and a killed run (SIGTERM and
SIGKILL during a model call that hangs). Capability items, copied into the evidence because the
runner removes the roots of a passing case: the usage store's path relative to the isolated XDG data dir, the schema of
its session table and one sample row, the token counts against the usage the stub reported, the cost
stored for the uncatalogued stub model, a model id and a timestamp on each message across one
in-session model switch (driven through ``serve``), and the child session of one scripted
``subagent`` call with its link to the parent. pass when every item is observed; absent when some
are not, with ``missing`` naming each (``store``, ``schema``, ``tokens``, ``per-message-model``,
``child-session``) and ``missing_observations`` holding the observation that showed it.
"""
import json
import signal
import sqlite3
import time

from opencode_probe import record, runner
from opencode_probe.harness import wait_until

CASE_ID = "P6"
K7 = False

DB_REL = "xdg/data/opencode/opencode.db"
XDG_DATA_REL = "xdg/data"
USAGE_TABLE = "session_v2"
USAGE_COLUMNS = {"cost", "tokens_input", "tokens_output", "parent_id", "model"}
STUB_INPUT, STUB_OUTPUT = 11, 7
MISSING_ITEMS = ("store", "schema", "tokens", "per-message-model", "child-session")

SUCCESS = {"models": {"m1": [{"text": "hello"}]}}
MODEL_ERROR = {"models": {"m1": [{"error": 400}]}}
HANG = {"models": {"m1": [{"hang": 40, "text": "late"}]}}
SWITCH = {"models": {"m1": [{"text": "from m1"}], "m2": [{"text": "from m2"}]}}
CHILD = {"models": {
    "m1": [{"text": "sub", "tool_calls": [{"name": "subagent", "arguments": {
        "agent": "general", "model": "stub/m2", "description": "p6", "prompt": "p6-child"}}]},
           {"text": "done"}],
    "m2": [{"text": "child", "tool_calls": [{"name": "shell", "arguments": {"command": "touch {root}/m-p6-child"}}]},
           {"text": "child done"}]}}
SWITCH_BODIES = ({"model": {"providerID": "stub", "id": "m2"}}, {"providerID": "stub", "id": "m2"})


def _keys(events):
    out = {}
    for event in events:
        entry = out.setdefault(str(event.get("type")), {"keys": set(), "part_keys": set()})
        entry["keys"].update(event)
        if isinstance(event.get("part"), dict):
            entry["part_keys"].update(event["part"])
    return {kind: {k: sorted(v) for k, v in entry.items()} for kind, entry in sorted(out.items())}


def _rows(root, sql, args=()):
    path = root / DB_REL
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _clip(row):
    return {k: (v[:160] if isinstance(v, str) else v) for k, v in row.items()}


def _usage_requests(run, model):
    return [r for r in run.requests if r.get("model") == model]


def _characterize(h):
    success = h.run(SUCCESS, label="success", timeout=60)
    errored = h.run(MODEL_ERROR, label="model-error", timeout=60)
    terminated = h.run(HANG, label="kill-sigterm", timeout=25, signal_after=(4, signal.SIGTERM))
    killed = h.run(HANG, label="kill-sigkill", timeout=25, signal_after=(4, signal.SIGKILL))
    return success, {
        "event_schema": _keys(success.events),
        "exit_codes": {"success": success.exit_code, "model_error": errored.exit_code,
                       "sigterm": terminated.exit_code, "sigkill": killed.exit_code},
        "sigterm_timed_out": terminated.timed_out,
        "model_error_events": sorted({str(e.get("type")) for e in errored.events}),
        "model_error_stderr": errored.stderr[-300:],
        "model_error_event_sample": next((e for e in errored.events if e.get("type") == "error"), None),
        "killed_event_types": sorted({str(e.get("type")) for e in killed.events}),
    }


def _store(success):
    root = success.root
    evidence = {"path_relative_to_xdg_data": None, "table": USAGE_TABLE}
    sessions = _rows(root, f"select * from {USAGE_TABLE}")
    if sessions is None:
        return evidence, {"store": f"no readable {DB_REL} under the isolated root"}, None
    evidence["path_relative_to_xdg_data"] = DB_REL[len(XDG_DATA_REL) + 1:]
    info = _rows(root, f"pragma table_info({USAGE_TABLE})") or []
    columns = [r["name"] for r in info]
    tables = [r["name"] for r in _rows(root, "select name from sqlite_master where type='table'") or []]
    evidence["tables"] = tables
    evidence["columns"] = {r["name"]: r["type"] for r in info}
    missing = {}
    if not USAGE_COLUMNS <= set(columns):
        missing["schema"] = f"{USAGE_TABLE} columns {columns} lack {sorted(USAGE_COLUMNS - set(columns))}"
    if not sessions:
        missing["store"] = f"{USAGE_TABLE} has no row after a completed run"
        return evidence, missing, None
    row = sessions[0]
    evidence["sample_row"] = _clip(row)
    evidence["cost_value"] = row.get("cost")
    evidence["cost_sqlite_type"] = (_rows(root, f"select typeof(cost) as t from {USAGE_TABLE}") or [{}])[0].get("t")
    expected_in = STUB_INPUT * len(_usage_requests(success, "m1"))
    expected_out = STUB_OUTPUT * len(_usage_requests(success, "m1"))
    seen = {"input": row.get("tokens_input"), "output": row.get("tokens_output")}
    evidence["tokens"] = {"stored": seen, "stub_reported": {"input": expected_in, "output": expected_out},
                          "stub_model_requests": len(_usage_requests(success, "m1"))}
    if not (seen["input"] and seen["output"] and seen["input"] == expected_in and seen["output"] == expected_out):
        missing["tokens"] = f"stored {seen}, the stub reported input {expected_in} output {expected_out}"
    return evidence, missing, row


def _switch(h):
    evidence, missing = {}, None
    with h.serve(SWITCH, label="switch") as svc:
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        sid = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not sid:
            return {"create": list(created)}, "could not create a session under serve"

        def assistants():
            body = svc.call("GET", f"/api/session/{sid}/message")[1] or {}
            return sorted((m for m in body.get("data", []) if m.get("type") == "assistant"),
                          key=lambda m: (m.get("time") or {}).get("created", 0))

        svc.call("POST", f"/api/session/{sid}/prompt", {"text": "one"})
        wait_until(lambda: any(c.get("type") == "text" for m in assistants() for c in m.get("content", [])), 30)
        time.sleep(2.0)
        for body in SWITCH_BODIES:
            code, data = svc.call("POST", f"/api/session/{sid}/model", body)
            evidence["switch_request"] = {"body": body, "status": code, "response": data}
            if code in (200, 204):
                break
        svc.call("POST", f"/api/session/{sid}/prompt", {"text": "two"})
        wait_until(lambda: len(assistants()) >= 2 and any(
            c.get("text") == "from m2" for c in assistants()[-1].get("content", [])), 30)
        time.sleep(1.0)
        per_message = [{"id": m.get("id"), "model": (m.get("model") or {}).get("id"),
                        "time_created": (m.get("time") or {}).get("created")} for m in assistants()]
        evidence["assistant_messages"] = per_message
        evidence["session_row"] = _clip((_rows(svc.world.root, f"select * from {USAGE_TABLE} where id = ?", (sid,))
                                         or [{}])[0])
        models = [m["model"] for m in per_message]
        if not (len(per_message) >= 2 and all(m["model"] and m["time_created"] for m in per_message)
                and len(set(models)) >= 2):
            missing = f"assistant messages {per_message} do not show two model ids with timestamps"
    return evidence, missing


def _child(h):
    run = h.run(CHILD, label="child", wait_for=("m-p6-child",), settle=12.0)
    root_id = (run.session_ids() or [None])[0]
    rows = wait_until(lambda: [r for r in (_rows(run.root, f"select * from {USAGE_TABLE} where parent_id is not null")
                                           or []) if r.get("tokens_input")], 15) or []
    parent = (_rows(run.root, f"select * from {USAGE_TABLE} where id = ?", (root_id,)) or [{}])[0]
    jobs = _rows(run.root, "select session_id, child_session_id, kind, status from job") or []
    evidence = {"root_session": root_id, "child_rows": [_clip(r) for r in rows], "parent_row": _clip(parent),
                "job_rows": jobs, "child_marker": run.exists("m-p6-child"),
                "stub_model_requests": {m: len(_usage_requests(run, m)) for m in ("m1", "m2")}}
    linked = [r for r in rows if r.get("parent_id") == root_id]
    if not linked:
        return evidence, f"no session row with parent_id {root_id} and non-zero tokens after a scripted subagent call"
    return evidence, None


def run(ctx):
    h = ctx.harness()
    success, evidence = _characterize(h)
    if success.exit_code != 0 or not success.events:
        evidence["success_stderr"] = success.stderr[-300:]
        return runner.result(record.UNOBSERVED, evidence)
    store, missing, _ = _store(success)
    evidence["store"] = store
    switch, switch_missing = _switch(h)
    evidence["model_switch"] = switch
    if switch_missing:
        missing["per-message-model"] = switch_missing
    child, child_missing = _child(h)
    evidence["child_session"] = child
    if child_missing:
        missing["child-session"] = child_missing
    if "store" in missing:
        missing.setdefault("schema", missing["store"])
        missing.setdefault("tokens", missing["store"])
    evidence["missing"] = [item for item in MISSING_ITEMS if item in missing]
    evidence["missing_observations"] = {item: missing[item] for item in evidence["missing"]}
    return runner.result(record.ABSENT if missing else record.PASS, json.loads(json.dumps(evidence, default=str)))


def check_record(entry, cases):
    """The event schema, the exit codes, and a status that follows from the content: pass needs
    ``missing == []``, the usage-store columns and tokens, a sample row and two distinct model ids
    across the switch; absent needs a non-empty ``missing`` within ``MISSING_ITEMS``, each item with
    the observation that showed it."""
    evidence = entry.get("evidence", {})
    problems = [f"evidence lacks {key}" for key in ("event_schema", "exit_codes") if not evidence.get(key)]
    status = entry.get("status")
    if status not in (record.PASS, record.ABSENT):
        return problems
    store = evidence.get("store") or {}
    missing = evidence.get("missing")
    if not isinstance(missing, list):
        return problems + ["evidence has no `missing` list"]
    if status == record.PASS:
        if missing:
            problems.append(f"status pass but missing is {missing}")
        if not (store.get("columns") and store.get("tokens")):
            problems.append("status pass but the usage-store columns or tokens are not recorded")
        if not store.get("sample_row"):
            problems.append("status pass but no sample row is recorded")
        models = {m.get("model") for m in (evidence.get("model_switch") or {}).get("assistant_messages") or []}
        if len(models - {None}) < 2:
            problems.append(f"status pass but the switch shows model ids {sorted(map(str, models))}")
    else:
        if not missing:
            problems.append("status absent but missing is empty")
        problems += [f"missing item {item!r} is not one of {list(MISSING_ITEMS)}"
                     for item in missing if item not in MISSING_ITEMS]
        observed = evidence.get("missing_observations") or {}
        problems += [f"missing item {item!r} has no observation" for item in missing if not observed.get(item)]
    return problems
