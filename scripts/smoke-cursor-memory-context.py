#!/usr/bin/env python3
"""Live smoke: Cursor sessionStart injects memory, config, and skill catalog.

Creates a temporary workspace with .cursor/hooks.json and a project MEMORY.md
containing a unique sentinel, then runs `agent -p` twice (90s each) asking for
the sentinel, the small-change-max-lines constant, and a known skill name.
Fails clearly when the agent CLI or API key is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

MEMORY_HOOK = SCRIPTS_DIR / "hook-cursor-memory-context.py"
DEFAULT_API_KEY_FILE = Path.home() / ".cursor_api_key"
ATTEMPT_TIMEOUT_SEC = 90
ATTEMPTS = 2
KNOWN_SKILL_NAME = "overcome-difficulty"
CONFIG_KEY = "small-change-max-lines"


def find_agent_binary() -> str | None:
    return shutil.which("agent") or shutil.which("cursor-agent")


def resolve_api_key(api_key_file: Path) -> str | None:
    env_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if env_key:
        return env_key
    if api_key_file.is_file():
        return api_key_file.read_text(encoding="utf-8").strip()
    return None


def build_agent_cmd(agent_bin: str, workspace: Path, timeout_sec: int) -> list[str]:
    cmd = [
        agent_bin,
        "-p",
        "--trust",
        "--force",
        "--workspace",
        str(workspace.resolve()),
        "--output-format",
        "text",
    ]
    timeout_bin = shutil.which("timeout")
    if timeout_bin and timeout_sec > 0:
        cmd = [timeout_bin, str(timeout_sec)] + cmd
    return cmd


def _read_config_constant(key: str) -> str | None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from agentctl.config import parse_config_md

    for config_path in (
        Path.home() / ".claude-agent" / "config.md",
        SCRIPTS_DIR.parent / "config.md",
    ):
        if config_path.is_file():
            constants = parse_config_md(config_path)
            if key in constants:
                return constants[key]
    return None


def _write_workspace(workspace: Path, sentinel: str) -> None:
    hook_command = str(MEMORY_HOOK.resolve())
    hooks_doc = {
        "version": 1,
        "hooks": {
            "sessionStart": [
                {
                    "command": hook_command,
                    "type": "command",
                    "timeout": 5,
                }
            ]
        },
    }
    (workspace / ".cursor").mkdir(parents=True)
    (workspace / ".cursor" / "hooks.json").write_text(
        json.dumps(hooks_doc, indent=2) + "\n",
        encoding="utf-8",
    )
    memory_dir = workspace / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        f"# Smoke project memory\n\nSmoke sentinel: {sentinel}\n",
        encoding="utf-8",
    )


def _run_attempt(
    agent_bin: str,
    workspace: Path,
    prompt: str,
    api_key: str,
    attempt: int,
) -> tuple[bool, str]:
    cmd = build_agent_cmd(agent_bin, workspace, ATTEMPT_TIMEOUT_SEC)
    env = {**os.environ, "CURSOR_API_KEY": api_key}
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except Exception as exc:
        return False, f"attempt {attempt}: spawn failed: {exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, (
            f"attempt {attempt}: agent exited {proc.returncode}\n{output.strip()}"
        )
    if not output.strip():
        return False, f"attempt {attempt}: empty agent output"
    return True, output


def main() -> int:
    agent_bin = find_agent_binary()
    if agent_bin is None:
        print(
            "smoke-cursor-memory-context: FAIL — neither `agent` nor `cursor-agent` "
            "on PATH; install the Cursor CLI",
            file=sys.stderr,
        )
        return 1

    api_key = resolve_api_key(DEFAULT_API_KEY_FILE)
    if not api_key:
        print(
            "smoke-cursor-memory-context: FAIL — CURSOR_API_KEY unset and "
            f"{DEFAULT_API_KEY_FILE} missing or empty",
            file=sys.stderr,
        )
        return 1

    sentinel = f"CURSOR_MEMORY_SMOKE_{uuid.uuid4().hex[:12]}"
    config_value = _read_config_constant(CONFIG_KEY)
    if not config_value:
        print(
            f"smoke-cursor-memory-context: FAIL — could not read `{CONFIG_KEY}` from config.md",
            file=sys.stderr,
        )
        return 1

    prompt = (
        "From your session context, reply on three lines only:\n"
        f"1) memory sentinel\n2) value of {CONFIG_KEY}\n3) one skill name from the catalog\n"
        "Use exactly these labels:\n"
        "sentinel: <value>\n"
        f"{CONFIG_KEY}: <value>\n"
        "skill: <name>"
    )

    workspace = Path(tempfile.mkdtemp(prefix="cursor-memory-smoke-"))
    try:
        _write_workspace(workspace, sentinel)
        for attempt in range(1, ATTEMPTS + 1):
            ok, detail = _run_attempt(agent_bin, workspace, prompt, api_key, attempt)
            if not ok:
                print(f"smoke-cursor-memory-context: FAIL — {detail}", file=sys.stderr)
                return 1
            missing: list[str] = []
            if sentinel not in detail:
                missing.append(f"sentinel {sentinel!r}")
            if config_value not in detail:
                missing.append(f"{CONFIG_KEY}={config_value!r}")
            if KNOWN_SKILL_NAME not in detail:
                missing.append(f"skill {KNOWN_SKILL_NAME!r}")
            if missing:
                print(
                    "smoke-cursor-memory-context: FAIL — "
                    f"attempt {attempt} output missing: {', '.join(missing)}\n"
                    f"{detail.strip()}",
                    file=sys.stderr,
                )
                return 1
            print(f"smoke-cursor-memory-context: attempt {attempt} OK")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    print("smoke-cursor-memory-context: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
