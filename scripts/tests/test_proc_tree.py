"""Runtime tests for proc_tree: a kill must reap the whole descendant subtree.

These are process-lifecycle tests, so they spawn real sleep trees. Every spawned
process carries a unique marker in its argv; each test pkills its own marked
strays in teardown so a failed assertion can never leak background sleeps.
"""
import os
import signal
import subprocess
import time

import pytest

import proc_tree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="proc_tree is POSIX-only")


def _unique_marker(tag: str) -> str:
    return f"PROCTREE_TEST_{tag}_{os.getpid()}_{time.time_ns()}"


def _tree_argv(marker: str):
    """A 3-deep tree all tagged with `marker`: a top bash (marker as $0) that
    forks two child bashes (marker as $0), each of which spawns a sleep child.
    `pgrep -f marker` matches the three bash processes; killpg reaps all six.

    The child body has a trailing no-op (`sleep 600; :`) on purpose — bash execs
    the last command of a `-c` body directly, so without something after `sleep`
    the child would exec into sleep and drop the marker argv."""
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


def test_kill_tree_reaps_whole_subtree():
    marker = _unique_marker("subtree")
    try:
        proc = proc_tree.launch_supervised(
            _tree_argv(marker), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        alive = _wait_for(marker, 3)
        assert len(alive) >= 3, f"tree did not start fully: {alive}"

        proc_tree.kill_tree(proc, grace_s=3.0)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _marked_pids(marker):
            time.sleep(0.05)
        assert _marked_pids(marker) == [], "kill_tree left tagged survivors"
    finally:
        _cleanup(marker)


def test_kill_tree_on_dead_proc_is_noop():
    proc = proc_tree.launch_supervised(
        ["bash", "-c", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc.wait()
    # Already exited: must return silently, no exception.
    result = proc_tree.kill_tree(proc)
    assert isinstance(result, set)
    # A bare, never-existent / reserved pid is also a silent no-op.
    assert proc_tree.kill_tree(1) == set()


def _teardown_wrapper(marker: str) -> None:
    """Run in a forked child: launch the tagged tree, install teardown, then
    block. A SIGTERM from the parent must reap the subtree before the child dies.
    """
    proc = proc_tree.launch_supervised(
        _tree_argv(marker), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc_tree.install_teardown(proc)
    time.sleep(600)


def test_install_teardown_sigterm_reaps_subtree():
    import multiprocessing

    marker = _unique_marker("teardown")
    ctx = multiprocessing.get_context("fork")
    child = ctx.Process(target=_teardown_wrapper, args=(marker,))
    try:
        child.start()
        alive = _wait_for(marker, 3)
        assert len(alive) >= 3, f"tree did not start fully under wrapper: {alive}"

        os.kill(child.pid, signal.SIGTERM)
        child.join(timeout=10)
        assert not child.is_alive(), "wrapper did not exit after SIGTERM"

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _marked_pids(marker):
            time.sleep(0.05)
        assert _marked_pids(marker) == [], "teardown handler left orphans"
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=2)
        _cleanup(marker)
