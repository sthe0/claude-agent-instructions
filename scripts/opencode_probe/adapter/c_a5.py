"""A5: every built-in tool OpenCode offers the model is either named for the gates or switched off.

The adapter maps a tool id to the Claude tool whose PreToolUse rows must see it, and refuses a tool id it
knows nothing about. A tool that writes files or runs commands under an id the adapter does not map would
pass every ``Edit|Write`` and ``Bash`` row. The ids are therefore taken from what the pinned binary offers,
not from a list in the adapter: one idle session per stub model id of each model family (OpenCode offers
a different file-editing tool to some families), each run with the gate shim loaded, and the tools of the
stub's chat requests are collected; the registry the server reports is read too when it answers.

pass when every offered tool id is a tool the adapter maps (``opencode_events.BUILTIN_TOOLS``), a path id the
mechanisms record lists (``PATH_TOOL_NAMES``), or switched off through ``DISABLED_PATHS`` with a reason, and the
adapter's own list of mutating built-ins that are switched off is not offered; fail when an offered tool id is
unaccounted for; unobserved when a run did not offer any tool. The record carries, per model id, the tools the
model was offered, and the registry's answer.
"""
from opencode_probe import record, runner
from opencode_probe.adapter import shared_a as sa

CASE_ID = "A5"
K7 = True

FAMILIES = ("m1", "gpt-5", "gpt-5-codex", "o4-mini", "claude-sonnet-5", "gemini-2.5-pro", "qwen3-coder",
            "grok-4", "kimi-k2")
REGISTRY_PATHS = ("/experimental/tool/ids", "/experimental/tool", "/api/tool", "/api/tools")
MARKERS = ("PlainText", "hello")


PROBE_TOOLS = frozenset({"probe_tool", "probesrv_touch", "probecm_touch"})


def _accounted(tool):
    from lib import opencode_events as oe

    return tool in PROBE_TOOLS or oe.claude_tool(tool) is not None


def run(ctx):
    h = ctx.harness()
    config = sa.gate_config({"providers": {"stub": {"models": {m: {"name": m} for m in FAMILIES}}}})
    files = {**sa.gate_files(inert=ctx.inert), "extra-rows.toml": ""}
    offered = {}
    for model in FAMILIES:
        run_ = h.run({"models": {model: [{"text": MARKERS[1]}]}}, label=f"offer-{model}", config=config,
                     files=files, env=sa.gate_env(), model=f"stub/{model}", timeout=90)
        offered[model] = {"tools": run_.tool_list(), "exit_code": run_.exit_code}
    registry = {}
    with h.serve({"models": {"m1": [{"text": MARKERS[1]}]}}, label="registry", config=config, files=files,
                 env=sa.gate_env()) as svc:
        for path in REGISTRY_PATHS:
            status, body = svc.call("GET", path)
            registry[path] = {"status": status, "body": body if status == 200 else None}
    union = sorted({t for entry in offered.values() for t in entry["tools"]})
    unaccounted = [t for t in union if not _accounted(t)]
    evidence = {"offered": offered, "union": union, "registry": registry, "unaccounted": unaccounted}
    if not union:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.FAIL if unaccounted else record.PASS, evidence)
