"""Regression test: spawn-specialist.py must feed the assembled prompt to the
child on stdin, not as an argv element.

Linux caps a SINGLE argv element at MAX_ARG_STRLEN (32 pages = 131072 bytes),
independent of the far larger total-argv budget. `assemble_prompt()` inlines
the entire `--plan` file, so any plan past ~128 KiB made `cmd.append(prompt)`
followed by `execve` fail with `OSError: [Errno 7] Argument list too long` and
the spawn never started (see the working plan this test accompanies).

We drive the REAL wrapper as a subprocess against a stub `claude` that reads
all of stdin, records what it received, and prints a valid result JSON so the
wrapper's marker validation is satisfied. Only a real launch proves execve did
not choke on the prompt.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
WRAPPER = SCRIPTS_DIR / "spawn-specialist.py"

PLAN_BYTES_FLOOR = 150_000
SENTINEL = "SPAWNSTDIN_SENTINEL_2b6f1c4a"


def _big_plan_text() -> str:
    filler = ("x" * 998 + "\n") * (PLAN_BYTES_FLOOR // 999 + 2)
    text = f"# Plan\n\n{SENTINEL}\n\n**<<this step>>** do the thing.\n\n{filler}"
    assert len(text.encode("utf-8")) >= PLAN_BYTES_FLOOR
    return text


def _stub_claude_script(report_path: Path) -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "data = sys.stdin.read()\n"
        f"report = {json.dumps(str(report_path))}\n"
        "with open(report, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'stdin_len': len(data.encode('utf-8')),\n"
        f"               'sentinel_present': {json.dumps(SENTINEL)} in data}}, f)\n"
        "print(json.dumps({'result': 'COMPLETED: ok', 'total_cost_usd': 0}))\n"
    )


@pytest.mark.skipif(os.name != "posix", reason="stub relies on POSIX shebang exec")
def test_big_plan_prompt_reaches_child_on_stdin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    report_path = tmp_path / "report.json"
    stub.write_text(_stub_claude_script(report_path))
    stub.chmod(0o755)

    plan = tmp_path / "plan.md"
    plan.write_text(_big_plan_text())

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "AGENT_RECURSION_DEPTH": "0",
    }
    cmd = [
        "python3",
        str(WRAPPER),
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "stub reflects stdin length",
        "--criterion-type", "measurable",
        "--budget", "small",
        "--complexity", "low",
    ]

    start = time.monotonic()
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"wrapper exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert report_path.exists(), (
        f"stub claude never wrote its report (elapsed={elapsed:.1f}s)\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    report = json.loads(report_path.read_text())
    assert report["stdin_len"] >= PLAN_BYTES_FLOOR, report
    assert report["sentinel_present"] is True, report


def test_dry_run_never_puts_the_prompt_on_a_single_argv_element(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(_big_plan_text())

    env = {**os.environ, "AGENT_RECURSION_DEPTH": "0"}
    cmd = [
        "python3",
        str(WRAPPER),
        "--kind", "developer",
        "--plan", str(plan),
        "--done-criterion", "stub reflects stdin length",
        "--criterion-type", "measurable",
        "--dry-run",
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    marker_idx = next(i for i, line in enumerate(lines) if line.startswith("=== command (not executed"))
    command_line = lines[marker_idx + 1]
    for token in command_line.split(" "):
        assert len(token) < 131072, f"argv element >= MAX_ARG_STRLEN found in dry-run output: {len(token)} bytes"
