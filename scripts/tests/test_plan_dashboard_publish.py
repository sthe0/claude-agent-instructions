"""Status digest of the plan dashboard: the text, the change-driven and rate-bounded send, the
plugin seam and the ``publish`` subcommand.

Plain pytest, standard library only, fake plugins only: no test reaches a network. The mutation
plugin's publish family (``plan_dashboard_mutations.PUBLISH_MUTATIONS``) breaks them one by one.
Sends run under ``read_only_guard`` over the fixture root, with the publish state kept outside it.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import socket
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_dashboard import model, publish
from plan_dashboard_fixtures import read_only_guard
from plan_dashboard_support import ALPHA, BETA, NOW, console_root, isolated_home  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
DOC = REPO / "docs" / "components" / "plan-dashboard.md"
_spec = importlib.util.spec_from_file_location("plan_dashboard_cli", SCRIPTS / "plan-dashboard.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


REAL_FLOCK = fcntl.flock


@contextlib.contextmanager
def guarded_root(home):
    """``read_only_guard`` with locking allowed: publishing locks a file beside its state, outside the
    guarded root, and the guard refuses every lock request."""
    with read_only_guard(home):
        fcntl.flock = REAL_FLOCK
        yield


class FakePublisher:
    NAME = "fake"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def publish(self, key, text, handle):
        if self.fail:
            raise RuntimeError("transport down")
        self.calls.append((key, text, handle))
        return handle or f"msg-{len(self.calls)}"

    def health(self):
        return True


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def collect_options(root):
    return {"cost_log": root.cost_log, "window_hours": 24, "include_all": True}


def one_pass(root, plugin, state_path, clock):
    return publish.run_once(root.state_root, plugin, state_path, now=NOW, clock=clock, **collect_options(root))


def task(n, goal="goal"):
    return {"session_id": f"session-{n:05d}-xxxx", "readable": True, "liveness": "active", "node": "EXECUTING",
            "stages_done": 1, "stages_total": 3, "open_difficulty": None, "goal": goal, "last_activity": f"2026-10-22T12:{n % 60:02d}:00+00:00",
            "totals": {"wall_clock_s": 60, "cost_usd": 1.0, "partial": False, "estimate": {"cost_usd": False, "wall_clock_s": False}}}


def test_the_digest_lists_each_task_and_carries_no_clock(tmp_path):
    root = console_root(tmp_path)
    first = model.collect(root.state_root, now=NOW, include_all=True, cost_log=root.cost_log)
    text = publish.digest(first)
    assert text.splitlines()[0].endswith(": 4 tasks")
    assert ALPHA[:8] in text and BETA[:8] in text and "Build the thing" in text
    later = {**first, "generated_at": "2030-01-01T00:00:00+00:00"}
    assert publish.digest(later) == text


def test_an_empty_snapshot_says_so():
    text = publish.digest({"hostname": "box", "tasks": []})
    assert text == "box: 0 tasks\nno live tasks"


def test_a_long_list_is_cut_to_the_message_limit_with_a_count_of_the_rest():
    snapshot = {"hostname": "box", "tasks": [task(n, "g" * 200) for n in range(200)]}
    text = publish.digest(snapshot)
    assert 0 < len(text) <= publish.TEXT_LIMIT
    last = text.splitlines()[-1]
    assert last.startswith("… and ") and last.endswith(" more")
    shown = len(text.splitlines()) - 2
    assert last == f"… and {200 - shown} more"


def test_an_unreadable_task_is_listed_not_dropped():
    broken = {"session_id": "broken-0000", "readable": False, "liveness": "idle", "reason": "x", "last_activity": None}
    assert "broken-0 idle unreadable" in publish.digest({"hostname": "box", "tasks": [broken]})


def test_should_send_is_change_driven_and_rate_bounded():
    entry = {"sha": publish.text_sha("old"), "sent_at": 1000.0}
    assert publish.should_send(None, "old", 1000.0)
    assert not publish.should_send(entry, "old", 1000.0 + 10 * publish.MIN_INTERVAL_S)
    assert not publish.should_send(entry, "new", 1000.0 + publish.MIN_INTERVAL_S - 1)
    assert publish.should_send(entry, "new", 1000.0 + publish.MIN_INTERVAL_S)
    assert publish.should_send(entry, "new", 1000.5, min_interval_s=0)


def test_run_once_sends_then_edits_the_same_message_and_skips_an_unchanged_text(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    plugin, clock = FakePublisher(), Clock()
    with guarded_root(root.home):
        assert one_pass(root, plugin, state_path, clock) == "sent"
        clock.now += 1000
        assert one_pass(root, plugin, state_path, clock) == "unchanged"
        assert len(plugin.calls) == 1
        key = publish.key_for(socket.gethostname())
        state = publish.read_state(state_path)
        assert state[key]["handle"] == "msg-1"
        state[key]["sha"] = "stale"
        publish.write_state(state_path, state)
        clock.now += 1000
        assert one_pass(root, plugin, state_path, clock) == "edited"
    sent_key, text, handle = plugin.calls[1]
    assert sent_key == key and handle == "msg-1" and ALPHA[:8] in text


def test_a_changed_text_inside_the_rate_bound_waits(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    plugin, clock = FakePublisher(), Clock()
    assert one_pass(root, plugin, state_path, clock) == "sent"
    key = publish.key_for(socket.gethostname())
    state = publish.read_state(state_path)
    state[key]["sha"] = "different"
    publish.write_state(state_path, state)
    clock.now += publish.MIN_INTERVAL_S - 1
    assert one_pass(root, plugin, state_path, clock) == "skipped"
    clock.now += 1
    assert one_pass(root, plugin, state_path, clock) == "edited"


class RateLimited(RuntimeError):
    retry_after = 120


def test_a_failing_transport_keeps_the_handle_but_records_the_attempt(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    clock = Clock()
    with pytest.raises(RuntimeError):
        one_pass(root, FakePublisher(fail=True), state_path, clock)
    entry = next(iter(publish.read_state(state_path).values()))
    assert entry == {"attempt_at": 1000.0, "retry_until": 0}


def test_a_failed_attempt_is_throttled_by_the_rate_bound(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    clock = Clock()
    with pytest.raises(RuntimeError):
        one_pass(root, FakePublisher(fail=True), state_path, clock)
    healthy = FakePublisher()
    clock.now += publish.MIN_INTERVAL_S - 1
    assert one_pass(root, healthy, state_path, clock) == "skipped"
    assert healthy.calls == []
    clock.now += 1
    assert one_pass(root, healthy, state_path, clock) == "sent"


def test_a_retry_after_from_the_plugin_is_honoured(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    clock = Clock()

    class Limited(FakePublisher):
        def publish(self, key, text, handle):
            raise RateLimited("slow down")

    with pytest.raises(RateLimited):
        one_pass(root, Limited(), state_path, clock)
    healthy = FakePublisher()
    clock.now += RateLimited.retry_after - 1
    assert one_pass(root, healthy, state_path, clock) == "skipped"
    clock.now += 1
    assert one_pass(root, healthy, state_path, clock) == "sent"


def test_an_unsavable_state_after_a_send_fails_loudly(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    plugin = FakePublisher()

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(publish, "write_state", refuse)
    with pytest.raises(publish.StatePersistError, match="duplicate"):
        one_pass(root, plugin, state_path, Clock())
    assert len(plugin.calls) == 1


def test_a_second_process_cannot_send_while_the_lock_is_held(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    plugin = FakePublisher()
    with publish.single_instance(state_path):
        with pytest.raises(publish.PublisherBusy):
            one_pass(root, plugin, state_path, Clock())
    assert plugin.calls == []
    assert one_pass(root, plugin, state_path, Clock()) == "sent"


def test_the_backoff_doubles_is_capped_and_never_below_a_retry_after():
    assert [publish.backoff_s(60, n) for n in (1, 2, 3)] == [60, 120, 240]
    assert publish.backoff_s(60, 20) == publish.MAX_BACKOFF_S
    assert publish.backoff_s(60, 1, retry_after=500) == 500


def test_watch_backs_off_and_gives_up_after_consecutive_failures(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    clock = Clock()
    sleeps, outcomes = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += 1000

    with pytest.raises(publish.PublishAborted):
        publish.watch(root.state_root, FakePublisher(fail=True), state_path, interval=7, sleep=sleep,
                      now=lambda: NOW, clock=clock, report=outcomes.append, **collect_options(root))
    assert sleeps == [7 * 2 ** n for n in range(publish.MAX_CONSECUTIVE_FAILURES - 1)]
    assert len(outcomes) == publish.MAX_CONSECUTIVE_FAILURES


def test_watch_stops_at_once_when_the_state_cannot_be_saved(tmp_path, monkeypatch):
    root = console_root(tmp_path)
    monkeypatch.setattr(publish, "write_state", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    with pytest.raises(publish.StatePersistError):
        publish.watch(root.state_root, FakePublisher(), tmp_path / "s.json", interval=1, sleep=lambda s: None,
                      now=lambda: NOW, clock=Clock(), **collect_options(root))


def test_publishing_writes_nothing_under_the_state_root_or_the_ledger(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    with guarded_root(root.home):
        one_pass(root, FakePublisher(), state_path, Clock())
    assert state_path.is_file()
    assert not any(p.name.startswith(".") for p in root.state_root.iterdir())


def test_watch_keeps_passing_and_survives_a_failing_pass(tmp_path):
    root = console_root(tmp_path)
    state_path = tmp_path / "pub" / "state.json"
    plugin, clock = FakePublisher(), Clock()
    outcomes, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += 1000
        if len(sleeps) == 1:
            plugin.fail = True
            state = publish.read_state(state_path)
            for entry in state.values():
                entry["sha"] = "stale"
            publish.write_state(state_path, state)
        elif len(sleeps) == 2:
            plugin.fail = False

    publish.watch(root.state_root, plugin, state_path, interval=7, sleep=sleep, stop=lambda: len(sleeps) >= 3,
                  now=lambda: NOW, clock=clock, report=outcomes.append, **collect_options(root))
    assert sleeps == [7, 7, 7]
    assert outcomes[0] == "sent"
    assert outcomes[1].startswith("failed: RuntimeError")
    assert outcomes[2] == "edited"
    assert len(outcomes) == 3


def write_plugin(directory: Path, name: str, body: str) -> None:
    target = directory / publish.PUBLISHER_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


GOOD = """
    NAME = "good"
    def publish(key, text, handle): return handle or "h"
    def health(): return True, "fine"
"""


def test_exactly_one_plugin_is_used(tmp_path):
    with pytest.raises(publish.PublisherError, match="no publisher"):
        publish.select_publisher(tmp_path / "empty")
    write_plugin(tmp_path, "a", GOOD)
    assert publish.select_publisher(tmp_path).NAME == "good"
    write_plugin(tmp_path, "b", GOOD.replace("good", "other"))
    with pytest.raises(publish.PublisherError, match="several"):
        publish.select_publisher(tmp_path)


@pytest.mark.parametrize("body, message", [
    ("raise ImportError('nope')", "does not load"),
    ("NAME = 'x'\ndef health(): return True", "lacks publish"),
])
def test_a_broken_plugin_is_an_error_not_a_skip(tmp_path, body, message):
    write_plugin(tmp_path, "bad", body)
    with pytest.raises(publish.PublisherError, match=message):
        publish.select_publisher(tmp_path)


def test_health_runs_with_the_network_and_process_spawning_stubbed(tmp_path):
    def opens_a_socket():
        socket.socket().close()
        return True

    def raises():
        raise ValueError("config missing")

    assert publish.check_health(SimpleNamespace(health=lambda: (True, "fine"))) == (True, "fine")
    assert publish.check_health(SimpleNamespace(health=lambda: False)) == (False, "")
    ok, detail = publish.check_health(SimpleNamespace(health=opens_a_socket))
    assert not ok and "stay local" in detail
    ok, detail = publish.check_health(SimpleNamespace(health=raises))
    assert not ok and "config missing" in detail
    socket.socket().close()


# --- the publish subcommand ------------------------------------------------------------------

def args_for(root, *extra):
    return ["publish", "--state-root", str(root.state_root), "--cost-log", str(root.cost_log), "--all", *extra]


def test_dry_run_prints_the_digest_loads_no_plugin_and_writes_no_state(tmp_path, capsys):
    root = console_root(tmp_path)
    write_plugin(tmp_path / "plugins", "boom", "raise SystemExit('loaded')")
    state_path = tmp_path / "pub" / "state.json"
    with read_only_guard(root.home):
        code = cli.main(args_for(root, "--dry-run", "--publish-state", str(state_path),
                                 "--plugin-dir", str(tmp_path / "plugins")))
    assert code == 0
    out = capsys.readouterr().out
    assert ALPHA[:8] in out and 0 < len(out.rstrip("\n")) <= publish.TEXT_LIMIT
    assert not state_path.exists() and not state_path.parent.exists()


def test_dry_run_names_a_session_that_does_not_exist(tmp_path, capsys):
    root = console_root(tmp_path)
    assert cli.main(args_for(root, "--dry-run", "--session", "nope")) == cli.EXIT_NO_SUCH_SESSION
    assert "no such session: nope" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["--once", "--watch"])
def test_sending_modes_refuse_a_session_that_does_not_exist(tmp_path, capsys, mode):
    root = console_root(tmp_path)
    write_plugin(tmp_path / "plugins", "a", GOOD)
    state_path = tmp_path / "pub" / "state.json"
    code = cli.main(args_for(root, mode, "--session", "nope", "--plugin-dir", str(tmp_path / "plugins"),
                             "--publish-state", str(state_path)))
    assert code == cli.EXIT_NO_SUCH_SESSION
    assert "no such session: nope" in capsys.readouterr().err
    assert not state_path.exists()


def test_check_publisher_reports_health_and_a_missing_plugin(tmp_path, capsys):
    root = console_root(tmp_path)
    write_plugin(tmp_path / "plugins", "a", GOOD)
    assert cli.main(args_for(root, "--check-publisher", "--plugin-dir", str(tmp_path / "plugins"))) == 0
    assert "publisher good: ok: fine" in capsys.readouterr().out
    assert cli.main(args_for(root, "--check-publisher", "--plugin-dir", str(tmp_path / "none"))) == cli.EXIT_PUBLISHER
    assert "no publisher plugin" in capsys.readouterr().err


def test_a_mode_is_required(tmp_path):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["publish"])


def test_once_sends_through_the_plugin(tmp_path, capsys):
    root = console_root(tmp_path)
    write_plugin(tmp_path / "plugins", "a", GOOD)
    state_path = tmp_path / "pub" / "state.json"
    code = cli.main(args_for(root, "--once", "--plugin-dir", str(tmp_path / "plugins"), "--publish-state", str(state_path)))
    assert code == 0 and capsys.readouterr().out.strip() == "sent"
    assert next(iter(publish.read_state(state_path).values()))["handle"] == "h"


def test_the_doc_section_names_the_plugin_dir_and_the_watch_command():
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Status digest", 1)[1].split("\n## ", 1)[0]
    assert "--plugin-dir" in section and "publish --watch" in section
