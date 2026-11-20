"""P2: the integration's permissions prevail over a project-level allow and a global allow.

The integration delivers its permissions through ``OPENCODE_CONFIG_CONTENT`` (the channel P7
uses for the agent definition). A project config in the working directory allows ``shell`` and
so does a stand-in global config; the integration denies every ``shell`` action. pass when a
scripted ``shell`` call executes in the control run (the two allows, no integration deny) and does
not execute with the integration deny present; fail when it executes despite the deny; unobserved
when the control did not execute it or the deny does not refuse even alone. The two other
channels (``OPENCODE_CONFIG`` file, ``OPENCODE_CONFIG_DIR``) are run the same way and recorded as
evidence of which layer wins; they do not decide the status.
"""
import json

from opencode_probe import record, runner, userconfig

CASE_ID = "P2"
K7 = True
INTEGRATION_CHANNEL = "OPENCODE_CONFIG_CONTENT"

ALLOW = json.dumps({"permissions": [{"action": "shell", "resource": "*", "effect": "allow"}]})
DENY = {"permissions": [{"action": "shell", "resource": "*", "effect": "deny"}]}
LAYERS = {"project/opencode.json": ALLOW, "xdg/config/opencode/opencode.json": ALLOW}
SCENARIO = {"models": {"m1": [{"text": "p2", "tool_calls": [
    {"name": "shell", "arguments": {"command": "touch {root}/m-p2"}}]}, {"text": "done"}]}}


def _channel(h, channel: str, files: dict) -> dict:
    kwargs = {"cwd": "project", "files": dict(files)}
    if channel == "OPENCODE_CONFIG":
        kwargs["config"] = DENY
    elif channel == "OPENCODE_CONFIG_CONTENT":
        kwargs["env"] = {"OPENCODE_CONFIG_CONTENT": json.dumps(DENY)}
    else:
        kwargs["files"]["opencode-config/opencode.json"] = json.dumps(DENY)
    run = h.run(SCENARIO, label=f"{channel}", **kwargs)
    return {"executed": run.exists("m-p2"), "exit_code": run.exit_code}


def run(ctx):
    h = ctx.harness()
    control = h.run(SCENARIO, label="control", files=LAYERS, cwd="project")
    channels = {}
    for channel in ("OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        channels[channel] = {"with_allows": _channel(h, channel, LAYERS), "alone": _channel(h, channel, {})}
    evidence = {"control_executed": control.exists("m-p2"), "channel": INTEGRATION_CHANNEL,
                "channels": channels, "project_layer": "project/opencode.json (cwd)",
                "global_layer": "XDG_CONFIG_HOME/opencode/opencode.json",
                "real_config_key_names": userconfig.real_config_key_names()}
    chosen = channels[INTEGRATION_CHANNEL]
    if not control.exists("m-p2") or chosen["alone"]["executed"]:
        return runner.result(record.UNOBSERVED, evidence)
    return runner.result(record.FAIL if chosen["with_allows"]["executed"] else record.PASS, evidence)
