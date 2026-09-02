#!/usr/bin/env python3
"""Recount `samples/published-text-gate/in-harness-observation.json`, from the
committed transcript extracts it cites -- not from its own self-reported fields.

Difficulty removed: every control on this hook up to this point fed it a
payload on stdin, which exercises its decision logic and says nothing about
whether a real Claude Code harness ever actually ROUTES a Bash call to it.
Stage 6 observed real routing with two `claude -p` children, but a developer's
own harness observation is corroborating evidence, not proof -- so this
checker treats the JSON record as a claim and RECOMPUTES it from the two
committed extracts (`samples/published-text-gate/transcripts/{deny,allow}.jsonl`)
plus a fresh re-run of the hook's own current source, the same way
check-live-run-evidence.py recomputes live-run-evidence.md rather than trusting
it.

What is recomputed, and why each one can otherwise be inflated:

  - the DESIRED registration row, read out of install-reminder-hooks.sh by
    `ast.literal_eval` (no import, no side effects) -- so the record cannot
    quote a matcher the installer does not actually wire.
  - the deny arm's decision, by re-running the CURRENT hook source against a
    freshly-built payload carrying the deny extract as `transcript_path` and a
    body reconstructed from the record's own `published_body` field (never
    from an ephemeral /tmp scratch file, which need not exist on another
    checkout) -- so a stale extract or a since-loosened hook is caught rather
    than assumed still denying.
  - the allow arm's Skill-before-Write-before-publish ordering, by sorting the
    extract's own `in_child_acts` timestamps -- never by reading the record's
    prose `order` field.
  - the allow arm's binding strength, by calling `writer_pass.bind()` -- the
    hook's own existential-witness function -- against the committed extract
    as the transcript and the Write act's own `content` as the body. This
    reuses the mechanism under test as the check, so the allow claim is
    proven by the same logic the hook runs, not by a second hand-rolled
    parser.
  - that the hook's source carries no transcript-path override env: such an
    env would be a mintable bypass of the entire binding, available to
    exactly the actor the binding exists to constrain.

Exit 0 when every claim survives; 1 with a per-failure list otherwise. Give it
verify-all's argv convention: defaults its own path when argv is empty, and
tolerates --staged (ignored; there is exactly one committed record).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import writer_pass as wp  # noqa: E402

RECORD_PATH = ROOT / "samples" / "published-text-gate" / "in-harness-observation.json"
INSTALL_SCRIPT = ROOT / "scripts" / "install-reminder-hooks.sh"
HOOK_SCRIPT = ROOT / "scripts" / "hook-published-text-writer-gate.py"
HOOK_NAME = "hook-published-text-writer-gate.py"
_BANNED_OVERRIDE_ENVS = ("TRANSCRIPT_PATH_OVERRIDE", "TRANSCRIPT_OVERRIDE", "FORCE_TRANSCRIPT")


def _desired_row(fails: list[str]) -> list | None:
    """The DESIRED tuple for this hook, read out of install-reminder-hooks.sh
    by parsing the literal list -- never imported, so reading it has no side
    effect and cannot be fooled by a stale in-repo cache of the module."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    start = text.find("DESIRED = [")
    if start < 0:
        fails.append(f"{INSTALL_SCRIPT}: no 'DESIRED = [' block found")
        return None
    open_bracket = text.index("[", start)
    depth = 0
    end = None
    for i in range(open_bracket, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        fails.append(f"{INSTALL_SCRIPT}: unterminated DESIRED block")
        return None
    try:
        rows = ast.literal_eval(text[open_bracket:end])
    except (SyntaxError, ValueError) as exc:
        fails.append(f"{INSTALL_SCRIPT}: DESIRED block does not parse: {exc}")
        return None
    for row in rows:
        if len(row) >= 3 and row[2] == HOOK_NAME:
            return list(row)
    fails.append(f"{INSTALL_SCRIPT}: DESIRED carries no row for {HOOK_NAME}")
    return None


def _seam_env(td: Path) -> dict[str, str]:
    """A self-contained seam file naming `stub-publish.sh` as a bash_verb
    publication -- the same seam shape the real runs used, rebuilt fresh so
    the re-run never depends on the ephemeral scratch venue the live runs
    were launched from (which need not exist on another checkout)."""
    seam_path = td / "publication-tools.local"
    seam_path.write_text(json.dumps([{"name": "stub-publish.sh", "kind": "bash_verb"}]), encoding="utf-8")
    env = dict(os.environ)
    env["CLAUDE_PUBLICATION_TOOLS_FILE"] = str(seam_path)
    return env


def _run_hook(command: str, transcript_path: Path, cwd: Path, env: dict[str, str]) -> dict:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "check-in-harness-observation",
        "transcript_path": str(transcript_path),
        "cwd": str(cwd),
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    if not proc.stdout.strip():
        # No directive printed at all == decide() returned ("allow", "").
        return {"hookSpecificOutput": {"permissionDecision": "allow"}}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"_raw_stdout": proc.stdout, "_stderr": proc.stderr, "_returncode": proc.returncode}


def _permission_decision(out: dict) -> str | None:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def _check_arm_present(rec: dict, fails: list[str]) -> dict[str, dict] | None:
    runs = rec.get("runs")
    if not isinstance(runs, list):
        fails.append("rec['runs'] is not a list -- the final verify command's "
                      "'{r[\"arm\"]: r for r in rec[\"runs\"]}' would crash on this record")
        return None
    by_arm: dict[str, dict] = {}
    for r in runs:
        arm = r.get("arm")
        if arm not in ("deny", "allow"):
            fails.append(f"run entry has no recognized 'arm' key: {r.get('arm')!r}")
            continue
        by_arm[arm] = r
    if not {"deny", "allow"} <= set(by_arm):
        fails.append(f"runs must cover both arms, found: {sorted(by_arm)}")
        return None
    return by_arm


def check_deny(rec: dict, d: dict, desired_row: list | None, fails: list[str]) -> None:
    if d.get("harness_verdict") != "deny":
        fails.append(f"deny arm harness_verdict is {d.get('harness_verdict')!r}, not 'deny'")
    if HOOK_NAME not in json.dumps(d):
        fails.append("deny run does not name the hook anywhere in its record")
    if d.get("stub_executed") is not False:
        fails.append("deny run does not record stub_executed: false -- "
                      "the deny must have preceded any external effect")
    matcher = d.get("matcher")
    if not matcher:
        fails.append("deny run's routing observation is not bound to a matcher")
    elif desired_row is not None and list(matcher) != desired_row:
        fails.append(f"deny matcher {matcher} != DESIRED row {desired_row}")

    extract = ROOT / d.get("transcript_extract", "")
    if not (extract.exists() and str(d.get("transcript_extract", "")).startswith("samples/")):
        fails.append(f"deny run is not backed by a committed extract under samples/: {extract}")
        return
    lines = [json.loads(ln) for ln in extract.read_text(encoding="utf-8").splitlines() if ln.strip()]
    tool_use_ids = {
        item["id"]
        for ln in lines
        for item in (ln.get("message", {}).get("content") or [])
        if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Bash"
    }
    denials = [
        item for ln in lines
        for item in (ln.get("message", {}).get("content") or [])
        if isinstance(item, dict) and item.get("type") == "tool_result"
        and item.get("is_error") and item.get("tool_use_id") in tool_use_ids
    ]
    if not denials:
        fails.append(f"{extract}: no error tool_result tied to a Bash tool_use -- "
                      "the extract does not carry a real denial")
    elif not any(d.get("hook_reason", "") in (den.get("content") or "") for den in denials):
        fails.append(f"{extract}: no denial's content contains this record's hook_reason")

    body = d.get("published_body")
    command = d.get("command")
    if not body or not command:
        fails.append("deny run has no published_body/command to rebuild a fresh payload from")
        return
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        body_path = tdp / "body.txt"
        body_path.write_text(body, encoding="utf-8")
        fresh_command = f"{tdp}/stub-publish.sh --body-file {body_path} ."
        out = _run_hook(fresh_command, extract, tdp, _seam_env(tdp))
        decision = _permission_decision(out)
        if decision != "deny":
            fails.append(f"re-running the CURRENT hook source against a fresh deny-shaped "
                          f"payload (extract as transcript_path) decided {decision!r}, not deny: {out}")


def check_allow(a: dict, desired_row: list | None, fails: list[str]) -> None:
    if a.get("harness_verdict") == "deny":
        fails.append("allow arm harness_verdict is 'deny'")
    matcher = a.get("matcher")
    if not matcher:
        fails.append("allow run's routing observation is not bound to a matcher")
    elif desired_row is not None and list(matcher) != desired_row:
        fails.append(f"allow matcher {matcher} != DESIRED row {desired_row}")

    acts = a.get("in_child_acts") or []
    names = [x.get("act") for x in acts]
    if names[:3] != ["skill:tech-writer", "write", "publish"]:
        fails.append(f"allow arm did not perform the compliant flow in order: {names}")
    grants = set(a.get("permission_grants") or [])
    if not {"Skill", "Write"} <= grants:
        fails.append(f"allow child was not granted Skill and Write: {sorted(grants)}")
    timed = [x for x in acts if x.get("timestamp")]
    if len(timed) != 3:
        fails.append(f"allow arm acts are not each timestamped: {timed}")
        return
    recomputed_order = [x["act"] for x in sorted(timed, key=lambda x: x["timestamp"])]
    if recomputed_order != ["skill:tech-writer", "write", "publish"]:
        fails.append(f"recomputed order from the acts' own timestamps is {recomputed_order}, "
                      "not Skill-before-Write-before-publish")

    extract_rel = a.get("transcript_extract", "")
    extract = ROOT / extract_rel
    if not (extract.exists() and str(extract_rel).startswith("samples/")):
        fails.append(f"allow run is not backed by a committed extract under samples/: {extract}")
        return

    write_acts = [x for x in acts if x.get("act") == "write"]
    allow_body = write_acts[0].get("content", "") if write_acts else ""
    if not allow_body:
        fails.append("allow arm's write act carries no content to recompute the binding from")
        return
    binding = wp.bind(allow_body, extract)
    print(f"recomputed binding against the committed extract: {binding.strength}")
    if binding.strength == "NONE":
        fails.append("writer_pass.bind() against the committed extract itself does not "
                      "reproduce a bound allow -- the record is self-reported, not proven")

    # Re-run the current hook against a fresh payload naming the same
    # committed extract as transcript_path, and a body file carrying the
    # exact write-act content -- confirms the current source still allows.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        body_path = tdp / "body.txt"
        body_path.write_text(allow_body, encoding="utf-8")
        fresh_command = f"{tdp}/stub-publish.sh --body-file {body_path}"
        out = _run_hook(fresh_command, extract, tdp, _seam_env(tdp))
        decision = _permission_decision(out)
        if decision == "deny":
            fails.append(f"re-running the CURRENT hook source against a fresh allow-shaped "
                          f"payload (extract as transcript_path) denied: {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="ignored; accepted for verify-all uniformity")
    parser.parse_args(argv)

    if not RECORD_PATH.exists():
        print(f"FAIL: {RECORD_PATH} not found", file=sys.stderr)
        return 1
    rec = json.loads(RECORD_PATH.read_text(encoding="utf-8"))

    fails: list[str] = []

    if not rec.get("channel_probe"):
        fails.append("the settings-hooks channel was asserted rather than probed")

    desired_row = _desired_row(fails)
    if desired_row is not None and rec.get("desired_registration_row") != desired_row:
        fails.append(
            f"recorded desired_registration_row {rec.get('desired_registration_row')} != "
            f"the DESIRED row install-reminder-hooks.sh actually carries {desired_row}")

    by_arm = _check_arm_present(rec, fails)
    if by_arm is not None:
        check_deny(rec, by_arm["deny"], desired_row, fails)
        check_allow(by_arm["allow"], desired_row, fails)

    src = HOOK_SCRIPT.read_text(encoding="utf-8")
    for banned in _BANNED_OVERRIDE_ENVS:
        if banned in src:
            fails.append(f"a transcript-path override would be a mintable bypass "
                          f"of the whole binding: {banned}")

    if "mcp" not in json.dumps(rec).lower():
        fails.append("the MCP-route residual is not recorded either way")

    if fails:
        print(f"FAIL — {len(fails)} claim(s) did not survive the recount:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("OK — deny and allow arms recomputed from committed extracts and a fresh "
          "hook re-run; both match the record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
