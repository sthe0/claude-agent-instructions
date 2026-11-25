"""A5: every built-in tool OpenCode offers the model is mapped for the gates or refused, and every argument is classified.

The adapter maps a tool id to the Claude tool whose PreToolUse rows must see it, and refuses a tool id it
knows nothing about. A tool that writes files or runs commands under an id the adapter does not map would
pass every ``Edit|Write`` and ``Bash`` row. The ids are therefore taken from what the pinned binary offers,
not from a list in the adapter: one idle session per stub model id of each model family (OpenCode offers
a different file-editing tool to some families), each run with the gate shim loaded, and the tools of the
stub's chat requests are collected, with the argument names of each tool's input schema; the registry the
server reports is read too when it answers.

A second set of runs, for a gpt-family and another model id, records what denying a permission action does to
the editing tools they are offered: the action ``patch`` and the action ``edit``. It is the evidence that
``patch`` cannot be switched off alone (``opencode_events.REFUSED_TOOLS``).

pass when every offered tool id is a tool the adapter maps (``opencode_events.BUILTIN_TOOLS``), a path id the
mechanisms record lists (``PATH_TOOL_NAMES``) or a tool it refuses (``REFUSED_TOOLS``), and the argument names
the schemas carry for the mapped tools are exactly the ones ``opencode_events.ARGUMENT_CLASSES`` classifies, with
the JSON types and required sets the bridge checks (``oe.unclassified_arguments``);
fail when an offered tool id is unaccounted for, or an argument is unclassified, retyped, no longer in the schema
or required differently;
unobserved when a run did not offer any tool. The record carries, per model id, the tools the model was
offered, each tool's argument names, the switch-off runs and the registry's answer.
"""
import json

from opencode_probe import record, runner
from opencode_probe.adapter import shared_a as sa

CASE_ID = "A5"
K7 = True

FAMILIES = ("m1", "gpt-5", "gpt-5-codex", "o4-mini", "claude-sonnet-5", "gemini-2.5-pro", "qwen3-coder",
            "grok-4", "kimi-k2")
REGISTRY_PATHS = ("/experimental/tool/ids", "/experimental/tool", "/api/tool", "/api/tools")
MARKERS = ("PlainText", "hello")
SWITCH_OFF_MODELS = ("gpt-5", "m1")
SWITCH_OFF_ACTIONS = ("patch", "edit")

PROBE_TOOLS = frozenset({"probe_tool", "probesrv_touch", "probecm_touch"})
EDITING_TOOLS = ("edit", "write", "patch")


def _accounted(tool):
    from lib import opencode_events as oe

    return tool in PROBE_TOOLS or oe.claude_tool(tool) is not None or tool in oe.REFUSED_TOOLS


def argument_names(schema):
    """The argument names of one tool's input schema as the model's chat request carries it."""
    node = schema if isinstance(schema, dict) else {}
    node = node.get("parameters") or node.get("input_schema") or node
    props = node.get("properties") if isinstance(node, dict) else None
    return sorted(props) if isinstance(props, dict) else []


def input_schema(schema):
    """One tool's input schema as ``{"properties": {name: spec}, "required": [...]}``, from the chat request's form."""
    node = schema if isinstance(schema, dict) else {}
    node = node.get("parameters") or node.get("input_schema") or node
    props = node.get("properties") if isinstance(node, dict) else None
    required = node.get("required") if isinstance(node, dict) else None
    return {"properties": dict(props) if isinstance(props, dict) else {},
            "required": sorted(required) if isinstance(required, list) else []}


def merge_schemas(into, more):
    """Fold one family's schema of a tool into the union: every property seen, every argument any family requires."""
    into.setdefault("properties", {}).update({k: v for k, v in more["properties"].items()
                                              if k not in into["properties"]})
    into["required"] = sorted(set(into.get("required", [])) | set(more["required"]))
    return into


def _switch_off(h, config, files, c_p1):
    out = {}
    for action in SWITCH_OFF_ACTIONS:
        agent = {"agents": {c_p1.SWITCH_OFF_AGENT: {"permissions": [{"action": action, "resource": "*",
                                                                     "effect": "deny"}]}}}
        for model in SWITCH_OFF_MODELS:
            off = h.run({"models": {model: [{"text": MARKERS[1]}]}}, label=f"off-{action}-{model}", config=config,
                        files=files, env=sa.gate_env(**{c_p1.SWITCH_OFF_CHANNEL: json.dumps(agent)}),
                        model=f"stub/{model}", timeout=90, cli=("--agent", c_p1.SWITCH_OFF_AGENT))
            out[f"{action}/{model}"] = {"exit_code": off.exit_code, "tools": off.tool_list(),
                                        "editing_tools": [t for t in EDITING_TOOLS if t in off.tool_list()]}
    return out


def run(ctx):
    from lib import opencode_events as oe
    from opencode_probe.mechanisms import c_p1

    h = ctx.harness()
    config = sa.gate_config({"providers": {"stub": {"models": {m: {"name": m} for m in FAMILIES}}}})
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": ""}
    offered, arguments = {}, {}
    for model in FAMILIES:
        scenario = {"models": {model: [{"text": MARKERS[1]}]}}
        run_ = h.run(scenario, label=f"offer-{model}", config=config, files=files, env=sa.gate_env(),
                     model=f"stub/{model}", timeout=90)
        offered[model] = {"tools": run_.tool_list(), "exit_code": run_.exit_code}
        for tool in run_.tool_list():
            merge_schemas(arguments.setdefault(tool, {}), input_schema(run_.tool_schema(tool)))
    switch_off = _switch_off(h, config, files, c_p1)
    registry = {}
    with h.serve({"models": {"m1": [{"text": MARKERS[1]}]}}, label="registry", config=config, files=files,
                 env=sa.gate_env()) as svc:
        for path in REGISTRY_PATHS:
            status, body = svc.call("GET", path)
            registry[path] = {"status": status, "body": body if status == 200 else None}
    union = sorted({t for entry in offered.values() for t in entry["tools"]})
    unaccounted = [t for t in union if not _accounted(t)]
    schema = {tool: spec for tool, spec in sorted(arguments.items()) if tool not in PROBE_TOOLS}
    argument_problems = oe.unclassified_arguments(schema)
    patch_alone = bool(switch_off["patch/gpt-5"]["tools"]) and "patch" not in switch_off["patch/gpt-5"]["tools"]
    evidence = {"offered": offered, "union": union, "registry": registry, "unaccounted": unaccounted,
                "arguments": schema, "argument_problems": argument_problems, "switch_off": switch_off,
                "patch_switchable_alone": patch_alone}
    if not union or any(entry["exit_code"] != 0 or not entry["tools"] for entry in offered.values()) \
            or any(entry["exit_code"] != 0 or not entry["tools"] for entry in switch_off.values()):
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.FAIL if unaccounted or argument_problems else record.PASS, evidence)
