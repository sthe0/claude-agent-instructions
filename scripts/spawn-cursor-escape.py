#!/usr/bin/env python3
"""Wrap `agent -p` spawn for overcome-difficulty recursive escape in Cursor.

Cursor equivalent of overcome-difficulty/SKILL.md § Recursive escape (which uses
`claude -p`). The wrapper handles process concerns so the coordinator only
supplies the difficulty declaration:

  1. Validate args; enforce max-recursion-depth from config.md.
  2. Resolve CURSOR_API_KEY from env or --api-key-file.
  3. Assemble the self-contained overcome-difficulty escape prompt.
  4. Spawn `agent -p` with trust/force flags.
  5. Validate the first non-empty line carries RESOLVED:/INVESTIGATION:/LOOP_DETECTED:
     (else prefix MALFORMED:).
  6. Append a JSONL row to ~/.local/log/cursor-spawn-costs.jsonl.

Use --dry-run to print the assembled prompt and command without spawning.
Use --smoke for a minimal RESOLVED: ping installation check.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import proc_tree  # sibling module in scripts/; supervised launch + recursive teardown

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_MD = REPO_ROOT / "config.md"
DEFAULT_API_KEY_FILE = Path.home() / ".cursor_api_key"
COST_LOG = Path.home() / ".local" / "log" / "cursor-spawn-costs.jsonl"

RETURN_MARKERS = ("RESOLVED", "INVESTIGATION", "LOOP_DETECTED")
MARKER_RE = re.compile(rf"^({'|'.join(RETURN_MARKERS)}):")

CONFIG_KEY_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|")


def parse_config_md() -> dict[str, str]:
    """Extract `key` -> `value` from the markdown table in config.md."""
    constants: dict[str, str] = {}
    for line in CONFIG_MD.read_text(encoding="utf-8").splitlines():
        m = CONFIG_KEY_RE.match(line)
        if m:
            constants[m.group(1)] = m.group(2)
    return constants


def recursion_max(constants: dict[str, str]) -> int:
    key = "max-recursion-depth"
    if key not in constants:
        raise SystemExit(f"error: {key} not defined in config.md")
    return int(constants[key])


def find_agent_binary() -> str | None:
    return shutil.which("agent") or shutil.which("cursor-agent")


def resolve_api_key(api_key_file: Path) -> str | None:
    env_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if env_key:
        return env_key
    if api_key_file.is_file():
        return api_key_file.read_text(encoding="utf-8").strip()
    return None


def assemble_prompt(
    *,
    depth: int,
    expected: str,
    actual: str,
    mismatch: str,
    tried: list[str],
) -> str:
    tried_lines = "\n".join(f"- {item}" for item in tried) if tried else "- (none recorded)"
    sections = [
        f"AGENT_RECURSION_DEPTH={depth}",
        "",
        "You have been spawned as a fresh root coordinator to resolve a difficulty "
        "in isolation from any parent conversation. There is no prior history; treat "
        "the description below as a self-contained task.",
        "",
        "Difficulty (in declaration form):",
        f"- Expected: {expected}",
        f"- Actual: {actual}",
        f"- Mismatch: {mismatch}",
        "",
        "What has been tried so far (concise; do not retry blindly):",
        tried_lines,
        "",
        "What you are asked to do:",
        "1. Work through overcome-difficulty (declaration → investigation → critique).",
        "2. Resolve the difficulty if you can.",
        "3. If you yourself hit an unyielding sub-difficulty, escalate with the same "
        "mechanism (this prompt template, AGENT_RECURSION_DEPTH+1).",
        "",
        "Reply with one of these exact markers on the first non-empty line of your final output:",
        "- RESOLVED: <one paragraph resolution + concrete next action for the caller>",
        "- INVESTIGATION: <findings + what you would try next, if you investigated but could not resolve>",
        "- LOOP_DETECTED: <how this task mirrors an ancestor's task you noticed, if "
        "AGENT_RECURSION_DEPTH is at or above loop-sensitivity-depth (see ~/.claude/config.md) "
        "and the pattern repeats>",
    ]
    return "\n".join(sections)


def validate_marker(result_text: str) -> tuple[str, bool]:
    """Return (text, ok). If marker is missing, prepend MALFORMED: and ok=False."""
    for line in result_text.splitlines():
        if line.strip():
            if MARKER_RE.match(line.strip()):
                return result_text, True
            break
    return (
        "MALFORMED: escape output did not start with a known marker "
        f"({', '.join(RETURN_MARKERS)}).\n\n{result_text}",
        False,
    )


def log_cost_entry(entry: dict) -> None:
    COST_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_refused(reason: str, extra: dict) -> None:
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "refused",
        "reason": reason,
        **extra,
    }
    log_cost_entry(entry)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--expected", help="what the plan declared the result should be")
    p.add_argument("--actual", help="what actually happened")
    p.add_argument("--mismatch", help="one or two sentences naming the gap")
    p.add_argument(
        "--tried",
        action="append",
        default=[],
        help="approach tried and why it failed (repeatable)",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace directory for the agent (default: cwd)",
    )
    p.add_argument("--model", default="composer-2.5", help="agent model id (default: composer-2.5)")
    p.add_argument(
        "--timeout-sec",
        type=int,
        default=600,
        help="wall-clock timeout in seconds when `timeout` is on PATH (default: 600)",
    )
    p.add_argument(
        "--api-key-file",
        type=Path,
        default=DEFAULT_API_KEY_FILE,
        help=f"path to API key file (default: {DEFAULT_API_KEY_FILE})",
    )
    p.add_argument("--dry-run", action="store_true", help="print prompt and command, then exit")
    p.add_argument(
        "--smoke",
        action="store_true",
        help="installation smoke: minimal prompt expecting RESOLVED: ping",
    )
    return p


def build_agent_cmd(
    agent_bin: str,
    prompt: str,
    workspace: Path,
    model: str,
    timeout_sec: int,
) -> list[str]:
    cmd = [
        agent_bin,
        "-p",
        prompt,
        "--trust",
        "--force",
        "--approve-mcps",
        "--workspace",
        str(workspace.resolve()),
        "--output-format",
        "text",
        "--model",
        model,
    ]
    timeout_bin = shutil.which("timeout")
    if timeout_bin and timeout_sec > 0:
        cmd = [timeout_bin, str(timeout_sec)] + cmd
    return cmd


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.smoke:
        prompt = "Reply with exactly one line: RESOLVED: ping"
        timeout_sec = min(args.timeout_sec, 90)
    else:
        missing = [
            name
            for name, val in (
                ("expected", args.expected),
                ("actual", args.actual),
                ("mismatch", args.mismatch),
            )
            if not val
        ]
        if missing:
            print(f"error: --{missing[0]} required unless --smoke", file=sys.stderr)
            return 1
        timeout_sec = args.timeout_sec

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1

    constants = parse_config_md()
    depth_now = int(os.environ.get("AGENT_RECURSION_DEPTH", "0"))
    depth_next = depth_now + 1
    cap = recursion_max(constants)
    if depth_next > cap:
        print(
            f"error: spawn would push AGENT_RECURSION_DEPTH to {depth_next}, "
            f"above max-recursion-depth={cap}. Stop and escalate to the user "
            f"per overcome-difficulty § Safeguards.",
            file=sys.stderr,
        )
        log_refused("recursion-cap", {"depth_attempted": depth_next, "cap": cap})
        return 3

    if not args.smoke:
        prompt = assemble_prompt(
            depth=depth_next,
            expected=args.expected or "",
            actual=args.actual or "",
            mismatch=args.mismatch or "",
            tried=args.tried,
        )

    agent_bin = find_agent_binary()
    cmd = build_agent_cmd(agent_bin or "agent", prompt, workspace, args.model, timeout_sec)

    if args.dry_run:
        print("=== assembled prompt ===")
        print(prompt)
        print("\n=== command (not executed) ===")
        if cmd[0] == "timeout":
            printable = cmd[:3] + [f"<prompt {len(prompt)} chars>"] + cmd[-5:]
        else:
            printable = cmd[:2] + [f"<prompt {len(prompt)} chars>"] + cmd[-5:]
        print(" ".join(repr(c) if " " in c else c for c in printable))
        api_key = resolve_api_key(args.api_key_file)
        print(f"\nCURSOR_API_KEY={'set' if api_key else 'missing'}")
        return 0

    if agent_bin is None:
        print(
            "error: neither `agent` nor `cursor-agent` on PATH; cannot spawn. "
            "Re-run with --dry-run to inspect the prompt.",
            file=sys.stderr,
        )
        return 4

    api_key = resolve_api_key(args.api_key_file)
    if not api_key:
        print(
            "error: CURSOR_API_KEY not set and api key file missing or empty: "
            f"{args.api_key_file}",
            file=sys.stderr,
        )
        return 1

    env = {**os.environ, "AGENT_RECURSION_DEPTH": str(depth_next), "CURSOR_API_KEY": api_key}
    started = time.monotonic()
    # launch_supervised makes the `agent` child a session/process-group leader;
    # install_teardown then reaps that whole group if this wrapper is killed (the
    # harness sends SIGTERM ~5s before SIGKILL, and a manual `kill` lands the same
    # SIGTERM), so the agent subtree is never orphaned.
    proc = proc_tree.launch_supervised(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    proc_tree.install_teardown(proc)
    try:
        stdout_str, stderr_str = proc.communicate()
    finally:
        proc_tree.kill_tree(proc)
    completed = subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout_str, stderr=stderr_str
    )
    duration_ms = int((time.monotonic() - started) * 1000)

    result_text = completed.stdout
    if not result_text.strip() and completed.stderr.strip():
        result_text = completed.stderr

    forwarded, ok = validate_marker(result_text)
    parsed_marker: str | None = None
    if ok:
        first = forwarded.splitlines()[0].strip()
        parsed_marker = first.split(":", 1)[0]

    sys.stdout.write(forwarded)
    if not forwarded.endswith("\n"):
        sys.stdout.write("\n")

    log_cost_entry({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "event": "spawn",
        "kind": "cursor-escape-smoke" if args.smoke else "cursor-escape",
        "depth": depth_next,
        "model": args.model,
        "workspace": str(workspace),
        "duration_ms": duration_ms,
        "return_marker": parsed_marker,
        "exit_code": completed.returncode,
        "malformed": not ok,
    })

    summary_bits = [
        "spawn-cursor-escape:",
        f"depth={depth_next}",
        f"model={args.model}",
        f"duration_ms={duration_ms}",
    ]
    if parsed_marker:
        summary_bits.append(f"marker={parsed_marker}")
    if not ok:
        summary_bits.append("MALFORMED")
    print(" ".join(summary_bits), file=sys.stderr)

    if not ok:
        return 1
    return 0 if completed.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
