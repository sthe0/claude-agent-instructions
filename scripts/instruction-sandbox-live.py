#!/usr/bin/env python3
"""Launch one tool-less `claude -p` inside an instruction-sandbox root and check
that the instructions it loaded carry the expected marker tokens.

Usage: instruction-sandbox-live.py --root <root> --cwd <dir> --expect <token> [--expect <token> ...]
                                   [--timeout <seconds>] [--model <alias>]

The child runs under the sandbox HOME / agent root, with every built-in tool
disabled, so it can only answer from the instructions the client loaded through
the redirected paths — it cannot read the marker file itself. Credentials are
lent through the environment (never copied into the sandbox).

Output: `found <token>` / `missing <token>` per token; on exit 3 one line
`UNAVAILABLE <reason>`.
Exit:  0  launch completed and every token was found
       1  launch completed (exit 0, no timeout) and a token was absent
       3  UNAVAILABLE: `auth`, `timeout`, `launch-exit=<n>` or `launch-error`;
          a launch that did not complete says nothing about the instructions
       2  usage
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import host_llm  # noqa: E402
from lib.runtime_models import HOST_CLAUDE  # noqa: E402

# The fleet-calibrated `claude -p` bound (docs/operations/advisor-timeout-calibration.md):
# measured on inputs up to ~200k chars, above the ~80k-char always-loaded surface
# a full-CLAUDE.md launch carries.
DEFAULT_TIMEOUT_S = 480
DEFAULT_MODEL = "haiku"

EXIT_PASS, EXIT_MISSING, EXIT_USAGE, EXIT_UNAVAILABLE = 0, 1, 2, 3

# Caller-session variables that would redirect the child's state, identity or
# plugin lookups out of the sandbox, where the canon snapshot cannot see them.
SCRUB_VARS = (
    "CLAUDE_CODE_SESSION_ID",
    "AGENT_LINEAGE_IDS",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_POLICY_LEDGER",
    "CLAUDE_PROJECT_PLUGIN_DIR",
    "CLAUDE_DIFFICULTY_PLUGIN_DIR",
    "CLAUDE_AUTH_PROFILE_DIR",
    "CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR",
    "CLAUDE_INSTRUCTIONS_CANON",
)

PROMPT = (
    "Reply with exactly the value that follows 'SANDBOX-MARKER: ' in your "
    "instructions, one value per line, and nothing else."
)


def build_child_env(caller_env: dict, root: Path) -> tuple[dict, str]:
    """Return (child env, credential status). `caller_env` must be the caller's
    UNMODIFIED environment: `_lend_auth` finds the credentials through
    `harness_config_root()`, i.e. the real CLAUDE_CONFIG_DIR, before any
    sandbox override is applied."""
    env = dict(caller_env)
    # Private seam: the one place that already owns borrow-the-token-into-the-env.
    status = host_llm._lend_auth(env)
    for name in list(env):
        if name in SCRUB_VARS or name.startswith("AGENTCTL_"):
            del env[name]
    home = root / "home"
    agent_root = home / ".claude-agent"
    env.update(
        HOME=str(home),
        CLAUDE_AGENT_HOME=str(agent_root),
        CLAUDE_CONFIG_DIR=str(agent_root),
        CLAUDE_INSTRUCTIONS_REPO=str(root / "core"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
    )
    return env, status


def build_argv(model: str) -> list[str]:
    # `--tools ""` is variadic, so the prompt goes over stdin, not as a trailing arg.
    argv = host_llm.build_launch_argv(HOST_CLAUDE, model, lean=True)
    return argv + ["--tools", ""]


def run_launch(argv: list[str], env: dict, cwd: Path, timeout: float) -> tuple[str, str]:
    """Return ('ok', stdout) or ('unavailable', reason)."""
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
    except OSError:
        return "unavailable", "launch-error"
    try:
        out, _err = proc.communicate(PROMPT, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return "unavailable", "timeout"
    if proc.returncode != 0:
        return "unavailable", f"launch-exit={proc.returncode}"
    return "ok", out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--expect", action="append", required=True, metavar="TOKEN")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_PASS
    root = args.root.resolve()
    if not (root / "home").is_dir() or not args.cwd.is_dir():
        print(f"no sandbox home under {root} or no such cwd {args.cwd}", file=sys.stderr)
        return EXIT_USAGE

    env, status = build_child_env(dict(os.environ), root)
    if status not in host_llm.AUTHENTICATED_TOKEN_STATUSES:
        print(f"credential status: {status}", file=sys.stderr)
        print("UNAVAILABLE auth")
        return EXIT_UNAVAILABLE

    kind, payload = run_launch(build_argv(args.model), env, args.cwd, args.timeout)
    if kind == "unavailable":
        print(f"UNAVAILABLE {payload}")
        return EXIT_UNAVAILABLE

    lines = {line.strip().strip("`'\"") for line in payload.splitlines()}
    absent = False
    for token in args.expect:
        found = token in lines
        absent = absent or not found
        print(f"{'found' if found else 'missing'} {token}")
    return EXIT_MISSING if absent else EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
