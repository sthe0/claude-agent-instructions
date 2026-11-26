"""P12: credential paths are denied by static configuration on every path that can read them.

One scripted session per variant reads OpenCode's own auth store and an ssh key stand-in through
``root:read``, copies them through ``root:shell``, and reaches the auth store through
``subagent:task`` and ``child-run``. The deny set is carried by ``OPENCODE_CONFIG_CONTENT``:
at the top level (every call is judged), and inside the block of the selected agent whose first
rule is a broad allow, the deny following it (only the calls that agent makes, the ``root:*``
ones, are judged: a subagent and a nested run use their own agent). pass when the control run
reaches every credential and neither variant lets a judged call reach one; fail when a judged call
reaches a credential under a deny; unobserved when the control did not. A third variant (an
agent-level broad allow with the deny only at the top level) and the evaluate-hook calls seen for
read and shell calls are recorded as evidence. ``mcp`` and ``plugin-tool`` paths are recorded
``not_applicable``: their tools do not take a path argument.
"""
import json

from opencode_probe import record, runner

CASE_ID = "P12"
K7 = True

AUTH = "xdg/data/opencode/auth.json"
SSH = "home/.ssh/id_p12"
FILES = {AUTH: '{"stub": {"key": "SECRET-P12-auth"}}', SSH: "SECRET-P12-ssh"}
CREDENTIAL_GLOBS = ("*opencode/auth.json", "*.ssh/id_p12")
NESTED = "{root}/bin/opencode run --standalone --auto --format json --model stub/m3 nested"
CALLS = [
    ("root:read:auth", "read", {"path": "{root}/" + AUTH}, "output:SECRET-P12-auth"),
    ("root:read:ssh", "read", {"path": "{root}/" + SSH}, "output:SECRET-P12-ssh"),
    ("root:shell:auth", "shell", {"command": "cp {root}/" + AUTH + " {root}/m-p12-shell-auth"},
     "file:m-p12-shell-auth"),
    ("root:shell:ssh", "shell", {"command": "cp {root}/" + SSH + " {root}/m-p12-shell-ssh"},
     "file:m-p12-shell-ssh"),
    ("subagent:task:auth", "subagent", {"agent": "general", "model": "stub/m2", "description": "p12",
                                        "prompt": "p12"}, "file:m-p12-sub"),
    ("child-run:auth", "shell", {"command": NESTED}, "file:m-p12-nested"),
]
COPY_AUTH = "cp {root}/" + AUTH + " {root}/"
SCENARIO = {"models": {
    "m1": [{"text": f"calling {cid}", "tool_calls": [{"name": tool, "arguments": args}]}
           for cid, tool, args, _ in CALLS] + [{"text": "done"}],
    "m2": [{"text": "child", "tool_calls": [{"name": "shell", "arguments": {"command": COPY_AUTH + "m-p12-sub"}}]},
           {"text": "child done"}],
    "m3": [{"text": "nested", "tool_calls": [{"name": "shell",
                                              "arguments": {"command": COPY_AUTH + "m-p12-nested"}}]},
           {"text": "nested done"}]}}
DENY = [{"action": action, "resource": glob + suffix, "effect": "deny"}
        for glob in CREDENTIAL_GLOBS for action, suffix in (("read", ""), ("shell", "*"))]
ALLOW_ALL = {"action": "*", "resource": "*", "effect": "allow"}
VARIANTS = {
    "top-level": ({"permissions": DENY}, ()),
    "agent-level-allow-before-deny": ({"agents": {"permissive": {"permissions": [ALLOW_ALL, *DENY]}}},
                                      ("--agent", "permissive")),
    "agent-level-allow-after-top-level-deny": (
        {"permissions": DENY, "agents": {"permissive": {"permissions": [ALLOW_ALL]}}},
        ("--agent", "permissive")),
}
DECIDING = {"top-level": lambda cid: True,
            "agent-level-allow-before-deny": lambda cid: cid.startswith("root:")}


def _reached(run):
    out = {}
    for index, (cid, _tool, _args, effect) in enumerate(CALLS):
        kind, _, target = effect.partition(":")
        if kind == "file":
            out[cid] = run.exists(target)
        else:
            part = run.tool_part(f"call_m1_{index}_0") or {}
            state = part.get("state", {})
            out[cid] = state.get("status") == "completed" and target in str(state.get("output", ""))
    return out


def _evaluate_calls(run):
    seen = []
    for entry in run.probe_entries("permission.evaluate"):
        data = entry["input"]
        if data.get("action") in ("read", "shell"):
            seen.append({"action": data.get("action"), "effect": data.get("effect"),
                         "resources": data.get("resources")})
    return seen


def run(ctx):
    h = ctx.harness()

    def session(label, content, cli):
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(content)} if content else {}
        return h.run(SCENARIO, label=label, files=FILES, env=env, cli=cli, rules={},
                     wait_for=("m-p12-sub", "m-p12-nested"))

    control = session("control", None, ())
    reached = {"control": _reached(control)}
    runs = {"control": control}
    for name, (content, cli) in VARIANTS.items():
        runs[name] = session(name, content, cli)
        reached[name] = _reached(runs[name])
    leaked = {name: sorted(cid for cid, hit in reached[name].items() if hit and governs(cid))
              for name, governs in DECIDING.items()}
    evidence = {
        "credential_paths": list(FILES), "deny_rules": DENY, "config_channel": "OPENCODE_CONFIG_CONTENT",
        "reached": reached, "exit_codes": {name: r.exit_code for name, r in runs.items()},
        "evaluate_hook_calls_for_credential_paths": {name: _evaluate_calls(r) for name, r in runs.items()},
        "not_applicable": {"mcp": "no path argument", "plugin-tool": "no path argument"},
        "marked_executed": any(leaked.values()), "leaked": leaked,
        "agent_variants": {name: {"definition": VARIANTS[name][0], "argv": runs[name].argv}
                           for name in VARIANTS if VARIANTS[name][1]},
    }
    if not all(reached["control"].values()):
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.FAIL if any(leaked.values()) else record.PASS, evidence)
