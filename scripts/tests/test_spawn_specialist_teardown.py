"""Runtime teardown test for spawn-specialist.py: SIGTERM'ing the wrapper must
reap the whole `claude -p` subtree, not just the direct child.

This is the runtime axis the stage's done criterion names — import-pass alone
does not prove the kill path. We drive the REAL wrapper as a subprocess against a
stub `claude` that forks a tagged sleep tree and blocks; sending the wrapper
SIGTERM (what the harness and a manual `kill` both deliver) must leave zero
tagged descendants. Every spawned proc carries a unique marker so teardown can
pkill its own strays even if an assertion fails.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="teardown is POSIX-only")

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
WRAPPER = SCRIPTS_DIR / "spawn-specialist.py"


def _unique_marker(tag: str) -> str:
    return f"SPAWNTEARDOWN_{tag}_{os.getpid()}_{time.time_ns()}"


def _stub_claude(marker: str) -> str:
    """A stand-in `claude` that ignores its args, execs into a bash carrying
    `marker` as $0 which forks two more marker-tagged bashes (each with a sleep
    child), then waits. `pgrep -f marker` matches the three bashes; the stub is
    the group leader launch_supervised created, so killpg reaps the whole tree.
    The trailing `:` keeps each child from exec'ing into sleep and dropping the
    marker argv."""
    inner = "bash -c 'sleep 600; :' %s" % marker
    body = "%s & %s & wait" % (inner, inner)
    return "#!/bin/bash\nexec bash -c %r %s\n" % (body, marker)


def _marked_pids(marker: str) -> list[int]:
    res = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [int(x) for x in res.stdout.split()]


def _wait_for(marker: str, count: int, timeout: float = 15.0) -> list[int]:
    deadline = time.monotonic() + timeout
    pids: list[int] = []
    while time.monotonic() < deadline:
        pids = _marked_pids(marker)
        if len(pids) >= count:
            return pids
        time.sleep(0.1)
    return pids


def test_wrapper_sigterm_reaps_claude_subtree(tmp_path):
    marker = _unique_marker("subtree")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_stub_claude(marker))
    stub.chmod(0o755)

    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**<<this step>>** do the thing.\n")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        # depth 0 -> child depth 1, well under the recursion cap
        "AGENT_RECURSION_DEPTH": "0",
    }
    cmd = [
        "python3",
        str(WRAPPER),
        "--kind",
        "developer",
        "--plan",
        str(plan),
        "--done-criterion",
        "stub does nothing",
        "--criterion-type",
        "measurable",
    ]

    wrapper = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        alive = _wait_for(marker, 3)
        assert len(alive) >= 3, f"stub claude tree did not start: {alive}"

        # The harness sends SIGTERM before SIGKILL; a manual `kill` sends the same.
        wrapper.send_signal(signal.SIGTERM)
        wrapper.wait(timeout=15)

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and _marked_pids(marker):
            time.sleep(0.1)
        assert _marked_pids(marker) == [], "wrapper SIGTERM left orphaned claude subtree"
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
        subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)
