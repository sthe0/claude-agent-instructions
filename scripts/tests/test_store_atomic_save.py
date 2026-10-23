"""FileStateStore.save replaces the state file atomically: a failed write leaves the
previous file and the directory untouched."""
import os
import resource
import signal
from argparse import Namespace

import pytest

from agentctl import cli


def _start(store, sid):
    cli.cmd_start(Namespace(session=sid, task="t", goal="g", done_criterion="dc",
                            criterion_type="measurable", recursion_depth=0,
                            cost_log=None), store=store)


def test_failed_write_leaves_previous_state_intact(store):
    _start(store, "s1")
    path = store.path("s1")
    before = path.read_bytes()
    listing = sorted(os.listdir(store.root))
    state = store.load("s1")
    state.goal = "x" * (len(before) + 200_000)

    old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (len(before) + 1000, hard))
    try:
        with pytest.raises(OSError):
            store.save(state)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, old_handler)

    assert path.read_bytes() == before
    assert sorted(os.listdir(store.root)) == listing


def test_save_round_trips(store):
    _start(store, "s2")
    state = store.load("s2")
    state.goal = "new goal"
    store.save(state)
    assert store.load("s2").goal == "new goal"
    assert sorted(os.listdir(store.root)) == ["s2.json"]
