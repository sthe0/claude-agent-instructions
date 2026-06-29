"""Runtime teardown test for the two cursor spawn wrappers: SIGTERM'ing the
wrapper must reap the whole `agent` subtree, not just the direct child.

Sibling of test_spawn_specialist_teardown.py (which covers the `claude -p`
wrapper). Same runtime axis: import-pass alone does not prove the kill path. Each
wrapper is driven as a real subprocess in --smoke mode against a stub `agent`
that forks a tagged sleep tree and blocks; sending the wrapper SIGTERM (what the
harness and a manual `kill` both deliver) must leave zero tagged descendants.
--timeout-sec 0 drops the optional `timeout` prefix so the stub is the
group-leader child launch_supervised created. Every spawned proc carries a
unique marker so teardown can pkill its own strays even if an assertion fails.
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
SPECIALIST = SCRIPTS_DIR / "spawn-cursor-specialist.py"
ESCAPE = SCRIPTS_DIR / "spawn-cursor-escape.py"


def _unique_marker(tag: str) -> str:
    return f"CURSORTEARDOWN_{tag}_{os.getpid()}_{time.time_ns()}"


def _stub_agent(marker: str) -> str:
    """A stand-in `agent` that ignores its args, execs into a bash carrying
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


def _smoke_argv(wrapper: Path, workspace: Path) -> list[str]:
    # --smoke needs no plan / difficulty inputs; --timeout-sec 0 drops the
    # `timeout` prefix so the stub `agent` is the direct supervised child.
    argv = [
        "python3",
        str(wrapper),
        "--smoke",
        "--timeout-sec",
        "0",
        "--workspace",
        str(workspace),
    ]
    if wrapper == SPECIALIST:
        argv += ["--kind", "developer"]
    return argv


@pytest.mark.parametrize("wrapper", [SPECIALIST, ESCAPE], ids=["specialist", "escape"])
def test_cursor_wrapper_sigterm_reaps_agent_subtree(wrapper, tmp_path):
    marker = _unique_marker(wrapper.stem)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "agent"
    stub.write_text(_stub_agent(marker))
    stub.chmod(0o755)

    env = {
        **os.environ,
        # Prepend so the stub wins over any real `agent`/`cursor-agent` on PATH.
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "CURSOR_API_KEY": "stub-key-for-teardown-test",
        # depth 0 -> child depth 1, well under the recursion cap
        "AGENT_RECURSION_DEPTH": "0",
    }
    cmd = _smoke_argv(wrapper, tmp_path)

    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        alive = _wait_for(marker, 3)
        assert len(alive) >= 3, f"stub agent tree did not start: {alive}"

        # The harness sends SIGTERM before SIGKILL; a manual `kill` sends the same.
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and _marked_pids(marker):
            time.sleep(0.1)
        assert _marked_pids(marker) == [], "wrapper SIGTERM left orphaned agent subtree"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)
