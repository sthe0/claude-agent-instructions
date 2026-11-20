"""The execution paths the mechanisms cases exercise, and how a run shows one of them executed.

One scenario drives every path in one session of the stub model ``m1`` (one scripted call per
path, in ``PATH_IDS`` order); the subagent's child runs on ``m2`` and the nested ``child-run``
process on ``m3``. A path counts as executed by its effect (a marker file, a changed file, a
request the stub saw, a completed read), never by the event log alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .harness import REFUSAL_TEXT, Harness, Run

NESTED_PROMPT = "nested"


@dataclass(frozen=True)
class PathSpec:
    path_id: str
    tool: str
    arguments: dict
    token: str
    effect: str
    throw_contains: str | None = None
    refusal_call_id: str | None = None


UNAVAILABLE_TEXT = "No tool named"
NESTED_CALL_ID = "call_m3_0_0"


def _specs() -> list[PathSpec]:
    return [
        PathSpec("root:shell", "shell", {"command": "touch {root}/m-root-shell"},
                 "m-root-shell", "file:m-root-shell", "m-root-shell"),
        PathSpec("root:write", "write", {"path": "{root}/m-write.txt", "content": "written"},
                 "m-write", "file:m-write.txt", "m-write"),
        PathSpec("root:edit", "edit", {"path": "{root}/m-edit.txt", "oldString": "before",
                                       "newString": "after"}, "m-edit", "content:m-edit.txt=after", "m-edit"),
        PathSpec("root:read", "read", {"path": "{root}/m-read.txt"}, "m-read", "output:read-body", "m-read"),
        PathSpec("root:glob", "glob", {"pattern": "m-glob-*", "path": "{root}"}, "m-glob",
                 "output:m-glob-1", "m-glob"),
        PathSpec("root:grep", "grep", {"pattern": "m-grep-token", "path": "{root}/m-grep.txt"},
                 "m-grep", "output:m-grep-token", "m-grep"),
        PathSpec("root:webfetch", "webfetch", {"url": "http://127.0.0.1:{port}/m-webfetch"},
                 "m-webfetch", "stub-get:/m-webfetch", "m-webfetch"),
        PathSpec("subagent:task", "subagent", {"agent": "general", "model": "stub/m2",
                                               "description": "m-subagent", "prompt": "m-subagent"},
                 "m-subagent", "file:m-subagent", "m-subagent"),
        PathSpec("root:execute", "execute",
                 {"code": "await fetch('http://127.0.0.1:{port}/m-exec-fetch'); return 'm-exec-fetch done'"},
                 "m-exec-fetch", "stub-get:/m-exec-fetch", "m-exec-fetch"),
        PathSpec("root:execute-inner", "execute",
                 {"code": "const r = await tools.opencode.worktree_list(); "
                          "return 'm-exec-inner ' + JSON.stringify(r)"},
                 "m-exec-inner", "output:m-exec-inner", "m-exec-inner"),
        PathSpec("root:skill", "skill", {"id": "m-skill"}, "m-skill", "output:m-skill-body", "m-skill"),
        PathSpec("root:experimental_image_generate", "experimental_image_generate",
                 {"prompt": "m-image"}, "m-image", "dispatched", "m-image"),
        PathSpec("mcp:probesrv:touch", "probesrv_touch", {}, "m-mcp", "file:m-mcp"),
        PathSpec("plugin-tool:probe_tool", "probe_tool", {}, "m-plugin-tool", "file:m-plugin-tool"),
        PathSpec("child-run", "shell", {"command": "{root}/bin/opencode run --standalone --auto --format json "
                                                   f"--model stub/m3 {NESTED_PROMPT}"},
                 "m-child-run", "file:m-child-run", "m-child-run", NESTED_CALL_ID),
        PathSpec("root:question", "question",
                 {"questions": [{"question": "m-question", "header": "m",
                                 "options": [{"label": "a", "description": "a"}]}]},
                 "m-question", "dispatched", "m-question"),
    ]


PATH_IDS = tuple(s.path_id for s in _specs())
FILES = {"m-edit.txt": "before", "m-read.txt": "read-body", "m-glob-1.txt": "x",
         "m-grep.txt": "line with m-grep-token",
         ".claude/skills/m-skill/SKILL.md": "---\nname: m-skill\ndescription: probe skill\n---\nm-skill-body\n"}


@dataclass
class Plan:
    specs: list[PathSpec]
    scenario: dict
    files: dict[str, str]
    config: dict
    base_rules: dict
    call_ids: dict[str, str]

    def spec(self, path_id: str) -> PathSpec:
        return next(s for s in self.specs if s.path_id == path_id)


def build_plan(include: tuple[str, ...] | None = None) -> Plan:
    specs = [s for s in _specs() if include is None or s.path_id in include]
    m1, call_ids = [], {}
    for spec in specs:
        call_ids[spec.path_id] = f"call_m1_{len(m1)}_0"
        m1.append({"text": f"calling {spec.path_id}", "tool_calls": [{"name": spec.tool,
                                                                    "arguments": spec.arguments}]})
    m1.append({"text": "all paths done"})
    scenario = {"models": {
        "m1": m1,
        "m2": [{"text": "child working", "tool_calls": [{"name": "shell",
                                                          "arguments": {"command": "touch {root}/m-subagent"}}]},
               {"text": "child done"}],
        "m3": [{"text": "nested working", "tool_calls": [{"name": "shell",
                                                            "arguments": {"command": "touch {root}/m-child-run"}}]},
               {"text": "nested done"}]}}
    config = {"mcp": {"probesrv": {"type": "local", "codemode": False,
                                   "command": ["python3", str(_mcp_server()), "{root}/m-mcp"]}}}
    return Plan(specs, scenario, dict(FILES), config, {"tool": "probe_tool"}, call_ids)


def _mcp_server():
    from .core import PROBE_DIR

    return PROBE_DIR / "assets" / "mcp_server.py"


def executed(run: Run, plan: Plan, spec: PathSpec) -> bool:
    kind, _, target = spec.effect.partition(":")
    part = run.tool_part(plan.call_ids[spec.path_id])
    if kind == "file":
        return run.exists(target)
    if kind == "content":
        rel, _, wanted = target.partition("=")
        return (run.read(rel) or "").strip() == wanted
    if kind == "output":
        output = (part or {}).get("state", {}).get("output", "")
        return (part or {}).get("state", {}).get("status") == "completed" and target in str(output)
    if kind == "stub-get":
        return any(r.get("get") == target for r in run.requests)
    if kind == "dispatched":
        state = (part or {}).get("state") or {}
        error = str(state.get("error") or "")
        refused = any(text in error for text in REFUSAL_TEXT.values()) or UNAVAILABLE_TEXT in error
        return state.get("status") in ("completed", "error") and not refused
    raise ValueError(spec.effect)


def outcome(run: Run, plan: Plan, spec: PathSpec, mechanism: str | None = None) -> dict:
    """What the run shows of the path's call; with ``mechanism`` also the positive refusal event
    of that mechanism for the call (None when the run has none). The call is the path's own call
    id, or the nested process's call id for ``child-run``, whose effect happens in that call."""
    state = (run.tool_part(plan.call_ids[spec.path_id]) or {}).get("state", {})
    out = {"executed": executed(run, plan, spec), "status": state.get("status"),
           "error": state.get("error")}
    if mechanism:
        out["refusal_event"] = run.refusal_event(mechanism, spec.refusal_call_id or plan.call_ids[spec.path_id])
    return out


def wait_markers(plan: Plan) -> tuple[str, ...]:
    return tuple(f"{s.effect.split(':', 1)[1]}" for s in plan.specs
                 if s.path_id in ("subagent:task", "child-run"))


def throw_rule(spec: PathSpec) -> dict:
    rule = {"name": spec.tool}
    if spec.throw_contains:
        rule["contains"] = spec.throw_contains
    return rule


def eval_rule(run: Run, plan: Plan, spec: PathSpec) -> dict | None:
    """A ``permission.evaluate`` deny rule for the path, built from the evaluate input the
    control run logged for it; None when the hook was never called for that path."""
    call_id = plan.call_ids[spec.path_id]
    for entry in run.probe_entries("permission.evaluate"):
        data = entry["input"]
        token_seen = spec.token in json.dumps(data.get("resources"))
        by_call = (data.get("source") or {}).get("id") == call_id and spec.path_id != "child-run"
        if token_seen or by_call:
            rule = {"name": data["action"]}
            if token_seen:
                rule["contains"] = spec.token
            return rule
    return None


def exercise(harness: Harness, plan: Plan, label: str, *, rules: dict | None = None,
             config: dict | None = None, cli: tuple[str, ...] = (), env: dict | None = None,
             stand_ins: bool = False, files: dict | None = None) -> Run:
    merged_rules = {**plan.base_rules, **(rules or {})}
    merged_config = {**plan.config, **(config or {})} if config else plan.config
    return harness.run(plan.scenario, label=label, rules=merged_rules, config=merged_config,
                       files={**plan.files, **(files or {})}, cli=cli, env=env, stand_ins=stand_ins,
                       wait_for=wait_markers(plan))
