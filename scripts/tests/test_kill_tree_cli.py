"""Runtime tests for the kill-tree.py CLI: it must reap the whole subtree and
refuse the init/self-kill guard path.

Like test_proc_tree.py these spawn real sleep trees tagged with a unique marker;
teardown pkills the marker so a failed assertion never leaks background sleeps.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import proc_tree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="kill-tree.py is POSIX-only")

CLI = Path(__file__).resolve().parent.parent / "kill-tree.py"


def _unique_marker(tag: str) -> str:
    return f"KILLTREECLI_TEST_{tag}_{os.getpid()}_{time.time_ns()}"


def _tree_argv(marker: str):
    inner = f"bash -c 'sleep 600; :' {marker}"
    script = f"{inner} & {inner} & wait"
    return ["bash", "-c", script, marker]


def _marked_pids(marker: str) -> list[int]:
    res = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return [int(x) for x in res.stdout.split()]


def _wait_for(marker: str, count: int, timeout: float = 5.0) -> list[int]:
    deadline = time.monotonic() + timeout
    pids: list[int] = []
    while time.monotonic() < deadline:
        pids = _marked_pids(marker)
        if len(pids) >= count:
            return pids
        time.sleep(0.05)
    return pids


def _cleanup(marker: str) -> None:
    subprocess.run(["pkill", "-9", "-f", marker], capture_output=True)


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=str(CLI.parent),  # put scripts/ on sys.path[0] so `import proc_tree` resolves
    )


def test_cli_reaps_whole_subtree():
    marker = _unique_marker("subtree")
    try:
        proc = proc_tree.launch_supervised(
            _tree_argv(marker), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        alive = _wait_for(marker, 3)
        assert len(alive) >= 3, f"tree did not start fully: {alive}"

        res = _run_cli(str(proc.pid), "--grace", "3")
        assert res.returncode == 0, f"CLI exited {res.returncode}: {res.stderr}"
        assert res.stdout.startswith("reaped:"), res.stdout

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _marked_pids(marker):
            time.sleep(0.05)
        assert _marked_pids(marker) == [], "CLI left tagged survivors"
    finally:
        _cleanup(marker)


def test_cli_refuses_init_pid():
    # pid <= 1 must be refused without signalling anything.
    res = _run_cli("1")
    assert res.returncode != 0, "CLI must refuse pid<=1"
    assert "refus" in res.stderr.lower()
    assert "reaped:" not in res.stdout
