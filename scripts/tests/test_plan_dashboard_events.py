"""The moment stream of the console dashboard (``plan_dashboard.events``) and its ``--events`` plumbing.

Plain pytest, standard library only. The vocabulary a reader of the stream may rely on is the contract
fixture; the harness tests read the same file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_dashboard import events, tui

SCRIPTS = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((SCRIPTS / "tests" / "fixtures" / "plan_dashboard_contract.json").read_text(encoding="utf-8"))
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture(autouse=True)
def stream_off():
    events.disable()
    yield
    events.disable()


@pytest.fixture
def pipe():
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def drain(read_fd) -> list[dict]:
    os.set_blocking(read_fd, False)
    try:
        data = os.read(read_fd, 1 << 16)
    except BlockingIOError:
        return []
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def test_the_stream_is_off_until_enabled(pipe):
    read_fd, write_fd = pipe
    assert not events.enabled()
    events.emit("cycle_start", cycle=1)
    events.enable(write_fd)
    assert events.enabled()
    events.disable()
    events.emit("cycle_start", cycle=2)
    assert drain(read_fd) == []


def test_an_event_is_one_json_line_with_name_clock_and_payload(pipe):
    read_fd, write_fd = pipe
    events.enable(write_fd)
    before = time.monotonic_ns()
    events.emit("mark_confirmed", task="alpha", applied=["/a/b.json"])
    after = time.monotonic_ns()
    (line,) = drain(read_fd)
    assert line["ev"] == "mark_confirmed"
    assert line["task"] == "alpha"
    assert line["applied"] == ["/a/b.json"]
    assert before <= line["t_ns"] <= after


def test_events_keep_their_order_and_clock_is_monotonic(pipe):
    read_fd, write_fd = pipe
    events.enable(write_fd)
    for cycle in range(1, 4):
        events.emit("cycle_start", cycle=cycle)
    lines = drain(read_fd)
    assert [line["cycle"] for line in lines] == [1, 2, 3]
    assert [line["t_ns"] for line in lines] == sorted(line["t_ns"] for line in lines)


def test_a_closed_descriptor_does_not_break_the_dashboard(pipe):
    read_fd, write_fd = pipe
    events.enable(write_fd)
    os.close(read_fd)
    os.close(write_fd)
    events.emit("cycle_start", cycle=1)


def test_the_stream_goes_to_the_named_pipe_and_not_to_the_screen_descriptor(tmp_path):
    fifo = tmp_path / "events.fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        events.enable_path(fifo)
        events.emit("cycle_start", cycle=1)
        (line,) = drain(reader)
    finally:
        os.close(reader)
    assert (line["ev"], line["cycle"]) == ("cycle_start", 1)
    assert events._fd not in (0, 1, 2)


def test_the_contract_names_every_event_with_its_origin():
    vocabulary = CONTRACT["events"]["vocabulary"]
    assert {name for name, spec in vocabulary.items() if not spec["new_only"]} == {
        "rows_painted", "mark_painted", "mark_confirmed", "live_check_end", "cycle_start", "cycle_end"}
    assert {name for name, spec in vocabulary.items() if spec["new_only"]} == {
        "first_screen_confirmed", "last_old_cleared", "snapshot_written"}


def test_the_contract_fixes_the_snapshot_path_and_the_old_marking():
    assert CONTRACT["snapshot"]["path_rule"] == "${XDG_CACHE_HOME:-$HOME/.cache}/plan-dashboard/snapshot.json"
    assert CONTRACT["snapshot"]["temp_name"] == "snapshot.json.tmp-<pid>"
    assert CONTRACT["screen"]["old_marking"]["text"] == "~"
    assert CONTRACT["screen"]["unread_marking"]["text"] == tui.WAIT_MARKS[tui.UNREAD]
    assert set(CONTRACT["screen"]["row_marks"].values()) == {tui.WAIT_MARKS[k] for k in ("choice", "permission", "idle")}


def launch_args(**overrides):
    base = dict(state_root=Path("/s"), window_hours=24, interval=2.0, cost_log=Path("/c"), include_all=False,
                sessions=[], transcripts_root=None, plugin_dir=None, locator=None)
    return SimpleNamespace(**{**base, **overrides})


def test_launch_argv_is_unchanged_when_the_flag_is_off():
    assert "--events" not in tui.launch_argv(launch_args())
    assert tui.launch_argv(launch_args(events=None)) == tui.launch_argv(launch_args())


def test_launch_argv_passes_the_pipe_path_on():
    argv = tui.launch_argv(launch_args(events=Path("/s/events.fifo")))
    assert argv[argv.index("--events") + 1] == "/s/events.fifo"


def test_the_tui_subcommand_takes_the_pipe_path_and_defaults_it_off():
    parser = cli.build_parser()
    assert parser.parse_args(["tui"]).events is None
    assert parser.parse_args(["tui", "--events", "/s/events.fifo"]).events == Path("/s/events.fifo")
