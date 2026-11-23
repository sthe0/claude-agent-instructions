"""E12: a scripted ``opencode serve`` session leaves the user's OpenCode config and data alone.

Digests of the user's OpenCode config and data directories are taken before and after one
``serve`` session in an isolated root (a created session and a prompt), together with a scan of
those directories for the probe root's path. Record fields: ``user_config_touched`` (a file was
changed, added or removed, or holds the probe root's path) and ``port_clash`` (something already
listens on OpenCode's default port 4096, where a ``serve`` without ``--port`` would bind). The case
serves on a free port and never binds 4096 itself. pass when the user's files are untouched; fail
when they are touched; unobserved when the session could not be driven.
"""
import socket

from opencode_probe import record, runner, userconfig
from opencode_probe.mechanisms import shared_e as sh

CASE_ID = "E12"
K7 = True
STOPPING_IS_FAIL = True

FIELDS = ("user_config_touched", "port_clash")
DEFAULT_PORT = 4096
SCENARIO = {"models": {"m1": [{"text": "hi"}]}}


def verdict(observation):
    return sh.decide(observation["user_config_touched"], STOPPING_IS_FAIL)


def user_config_change(before, after, leaked):
    """The difference of two ``userconfig.digest_tree`` maps plus the probe-root paths found in the files."""
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    added, removed = sorted(after.keys() - before.keys()), sorted(before.keys() - after.keys())
    return {"changed": changed, "added": added, "removed": removed, "leaked_probe_paths": sorted(leaked),
            "user_config_touched": bool(changed or added or removed or leaked)}


def default_port_in_use():
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", DEFAULT_PORT)) == 0


def run(ctx):
    h = ctx.harness()
    clash = default_port_in_use()
    before = userconfig.digest_tree()
    with h.serve(SCENARIO, label="serve-session") as svc:
        created = svc.call("POST", "/api/session", {"model": {"providerID": "stub", "id": "m1"}})
        session = ((created[1] or {}).get("data") or {}).get("id") if isinstance(created[1], dict) else None
        if not session:
            return runner.result(record.UNOBSERVED, {"create_session": list(created), "port_clash": clash})
        prompted = svc.call("POST", f"/api/session/{session}/prompt", {"text": "go"})[0]
        svc.wait_until(lambda: any(r.get("tools") for r in svc.world.requests()), 20)
        serve_port = int(svc.base.rsplit(":", 1)[1])
    change = user_config_change(before, userconfig.digest_tree(), userconfig.leaked_probe_paths())
    evidence = {**change, "port_clash": clash, "default_port": DEFAULT_PORT, "default_port_bound_by_case": serve_port == DEFAULT_PORT,
                "serve_port": serve_port, "prompt_status": prompted}
    return runner.result(verdict(evidence), evidence)


def check_record(entry, cases):
    return sh.check_entry(entry, CASE_ID, FIELDS, verdict)
