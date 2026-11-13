"""How the dashboard learns of changes without asking the file system (``plan_dashboard.watch``).

Plain pytest, standard library only: nothing here imports textual, so the system interpreter runs it. The
inotify tests use the kernel's own descriptor on directories under ``tmp_path`` and wait for the descriptor
with a bounded ``select``, so a missing event fails in seconds instead of hanging. ``PLAN_DASHBOARD_REQUIRE_INOTIFY=1``
turns the skip a machine without inotify would get into a failure. A queue overflow, a watch that cannot be
added and a missing inotify are injected through the module's seams (``watch.Inotify`` and an ``Inotify`` stand-in
handed to ``Watcher``); the sweep runs on a fake monotonic clock.
"""
from __future__ import annotations

import errno
import os
import select
from pathlib import Path

import pytest

from plan_dashboard import snapshot as store, watch
from plan_dashboard_support import isolated_home  # noqa: F401

REQUIRE_INOTIFY = os.environ.get("PLAN_DASHBOARD_REQUIRE_INOTIFY") == "1"
WAIT_S = 5.0


@pytest.fixture
def inotify():
    try:
        watch.Inotify().close()
    except watch.Unavailable as exc:
        if REQUIRE_INOTIFY:
            pytest.fail(f"PLAN_DASHBOARD_REQUIRE_INOTIFY=1 but inotify is not usable here: {exc.reason}")
        pytest.skip(f"inotify is not usable here: {exc.reason}")


class Layout:
    def __init__(self, base: Path):
        self.state, self.bindings, self.projects = base / "state", base / "bindings", base / "projects"
        self.ledger_dir, self.listing_dir = base / "ledger", base / "listing"
        for directory in (self.state, self.bindings, self.projects / "proj-a", self.projects / "proj-empty",
                          self.ledger_dir, self.listing_dir):
            directory.mkdir(parents=True)
        self.ledger, self.listing = self.ledger_dir / "costs.jsonl", self.listing_dir / "events.jsonl"

    def sources(self) -> list[watch.Source]:
        return watch.sources_for(self.state, self.bindings, [self.projects], self.ledger, self.listing)

    def file_of(self, kind: str) -> Path:
        return {"state": self.state / "task.json", "binding": self.bindings / "bound.json",
                "transcript": self.projects / "proj-a" / "chat.jsonl", "ledger": self.ledger,
                "locator_listing": self.listing}[kind]


@pytest.fixture
def layout(tmp_path):
    return Layout(tmp_path / "watched")


class Started:
    def __init__(self, watcher: watch.Watcher):
        self.watcher = watcher

    def named(self) -> watch.Drained:
        """The events of the writes made so far; fails when the descriptor shows none within ``WAIT_S``."""
        ready, _, _ = select.select([self.watcher.inotify.fd], [], [], WAIT_S)
        assert ready, f"no inotify event within {WAIT_S} s"
        return self.watcher.drain()


@pytest.fixture
def started(inotify, layout):
    watcher = watch.start(layout.sources())
    yield Started(watcher)
    watcher.close()


class FakeInotify:
    """An ``Inotify`` that holds the records handed to it, logs each watch added, and can refuse them."""

    def __init__(self, records=(), refuse: int | None = None, log: list | None = None):
        self.records, self.refuse, self.log, self.count = list(records), refuse, log if log is not None else [], 0

    def add(self, path) -> int:
        if self.refuse is not None:
            raise OSError(self.refuse, os.strerror(self.refuse), str(path))
        self.count += 1
        self.log.append(("watch", str(path)))
        return self.count

    def remove(self, wd) -> None:
        pass

    def ready(self) -> bool:
        return bool(self.records)

    def read(self):
        records, self.records = self.records, []
        return records

    def close(self) -> None:
        pass


class Steps:
    """A monotonic clock that moves ``step`` seconds at every reading."""

    def __init__(self, step: float = 0.0):
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def populated_view(layout: Layout, files: int) -> tuple[store.KeptView, list[Path]]:
    """A kept view that has stat'ed ``files`` transcripts and listed the directories above them."""
    kept, paths = store.KeptView(), []
    for number in range(files):
        path = layout.projects / "proj-a" / f"chat-{number:03d}.jsonl"
        path.write_text("x", encoding="utf-8")
        paths.append(path)
    kept.scandir(layout.projects)
    kept.scandir(layout.projects / "proj-a")
    for path in paths:
        kept.stat(path)
    return kept, paths


@pytest.mark.parametrize("kind", ["state", "binding", "transcript", "ledger", "locator_listing"])
def test_each_kind_of_write_is_named_by_an_event(started, layout, kind):
    path = layout.file_of(kind)
    steps = (
        ("created", lambda: path.write_text("one", encoding="utf-8")),
        ("appended", lambda: path.open("a", encoding="utf-8").write("two")),
        ("touched", lambda: os.utime(path, (1, 1))),
        ("replaced", lambda: (path.with_name("other.tmp").write_text("new", encoding="utf-8"),
                              os.replace(path.with_name("other.tmp"), path))),
        ("deleted", path.unlink),
    )
    for step, write in steps:
        write()
        assert str(path) in started.named().paths, f"the file {step} is not named by an event"


def test_a_directory_created_while_running_is_watched_before_it_is_read(started, layout):
    new = layout.projects / "proj-b"
    new.mkdir()
    found = started.named()
    assert str(new) in found.added and str(new) in started.watcher.watched
    (new / "chat.jsonl").write_text("one", encoding="utf-8")
    assert str(new / "chat.jsonl") in started.named().paths


def test_a_directory_created_under_a_non_source_ancestor_is_watched_before_it_is_read(started, layout):
    assert not list(layout.projects.glob("*.jsonl"))
    new = layout.projects / "proj-c"
    new.mkdir()
    (new / "early.jsonl").write_text("one", encoding="utf-8")
    kept = store.KeptView()
    found = started.named()
    kept.mark(found.paths, found.directories, by="event")
    assert str(new) in started.watcher.watched and str(new) in kept.dirty
    (new / "later.jsonl").write_text("two", encoding="utf-8")
    assert str(new / "later.jsonl") in started.named().paths


def test_a_source_in_an_existing_directory_that_held_no_source_is_watched(started, layout):
    empty = layout.projects / "proj-empty"
    assert str(empty) in started.watcher.watched
    (empty / "chat.jsonl").write_text("one", encoding="utf-8")
    assert str(empty / "chat.jsonl") in started.named().paths


def test_a_queue_overflow_brings_one_full_check_in_slices(layout):
    kept, paths = populated_view(layout, 30)
    kept.trust(None)
    watcher = watch.Watcher(layout.sources(), inotify=FakeInotify([(-1, watch.IN_Q_OVERFLOW, 0, "")]))
    found = watch.begin_cycle(watcher, kept, lambda: None)
    assert watch.needs_sweep(found) and found.reasons
    changed = paths[17]
    changed.write_text("grown", encoding="utf-8")
    sweep, clock, taken, slices = watch.Sweep(kept), Steps(0.01), 0, 0
    sweep.begin_one_off()
    size = sweep.size()
    while sweep.one_off:
        result = watch.sweep_slice(sweep, None, 0.05, clock)
        taken, slices = taken + result.entries, slices + 1
    assert taken == size and slices > 1
    assert kept.found[str(changed)] == "overflow-sweep"
    assert set(kept.dirty) == {str(changed)}


def test_a_watch_that_cannot_be_added_puts_the_run_on_the_stat_path(layout, monkeypatch):
    monkeypatch.setattr(watch, "Inotify", lambda: FakeInotify(refuse=errno.ENOSPC))
    said = []
    assert watch.choose_path(layout.sources(), lambda ev, **payload: said.append((ev, payload))) is None
    assert [ev for ev, _ in said] == ["detection"]
    assert said[0][1]["path"] == "stat" and "ENOSPC" in said[0][1]["reason"]


def test_without_inotify_the_stat_path_runs_as_today(layout, monkeypatch):
    def missing():
        raise watch.Unavailable("inotify is not available: no libc")

    monkeypatch.setattr(watch, "Inotify", missing)
    said = []
    emit = lambda ev, **payload: said.append((ev, payload))  # noqa: E731
    assert watch.choose_path(layout.sources(), emit) is None
    assert watch.choose_path(layout.sources(), emit, enabled=False) is None
    assert [(ev, p["path"]) for ev, p in said] == [("detection", "stat"), ("detection", "stat")]
    assert said[0][1]["reason"].startswith("inotify is not available")
    assert said[1][1]["reason"] == "PLAN_DASHBOARD_WATCH=off"
    kept, paths = populated_view(layout, 3)
    calls = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda path, *a, **kw: (calls.append(str(path)), real_stat(path, *a, **kw))[1])
    for _ in range(2):
        kept.stat(paths[0])
    assert calls.count(str(paths[0])) == 2


def test_an_event_path_cycle_reads_only_what_events_named(started, layout, monkeypatch):
    kept, paths = populated_view(layout, 12)
    started.watcher.drain()
    kept.trust(started.watcher.covers)
    paths[4].write_text("grown", encoding="utf-8")
    ready, _, _ = select.select([started.watcher.inotify.fd], [], [], WAIT_S)
    assert ready
    watch.begin_cycle(started.watcher, kept, lambda: None)
    assert set(kept.dirty) == {str(paths[4])}
    calls = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda path, *a, **kw: (calls.append(str(path)), real_stat(path, *a, **kw))[1])
    for path in paths:
        kept.stat(path)
    assert calls == [str(paths[4])]


def test_the_reconciliation_checks_every_source_once_per_30_s(layout, monkeypatch):
    kept, _ = populated_view(layout, 120)
    kept.trust(None)
    checked = []
    real = kept.differs
    monkeypatch.setattr(kept, "differs", lambda item: (checked.append(item), real(item))[1])
    sweep, clock = watch.Sweep(kept), Steps()
    size = sweep.size()
    per_tick = watch.slice_entries(size)
    for _ in range(int(watch.RECONCILE_S / watch.TICK_S)):
        watch.sweep_slice(sweep, per_tick, watch.TICK_S, clock)
    assert size > per_tick > 1
    assert sorted(checked[:size]) == sorted(kept.sweep_items())
    assert set(checked) == set(kept.sweep_items()) and len(checked) == 60 * per_tick


def test_a_write_before_the_cycle_start_stamp_is_in_that_cycle(started, layout):
    kept, _ = populated_view(layout, 2)
    started.watcher.drain()
    kept.trust(started.watcher.covers)
    written = layout.file_of("state")
    written.write_text("one", encoding="utf-8")
    ready, _, _ = select.select([started.watcher.inotify.fd], [], [], WAIT_S)
    assert ready
    at_stamp = []
    watch.begin_cycle(started.watcher, kept, lambda: at_stamp.append(started.watcher.pending()))
    assert at_stamp == [True]
    assert str(written) in kept.dirty


def test_the_event_path_detection_follows_the_last_start_up_watch(layout, monkeypatch):
    log: list = []
    monkeypatch.setattr(watch, "Inotify", lambda: FakeInotify(log=log))
    said = []

    def emit(ev, **payload):
        log.append((ev, payload["path"]))
        said.append(ev)

    watcher = watch.choose_path(layout.sources(), emit)
    assert watcher is not None and said == ["detection"]
    assert log[-1] == ("detection", "event")
    assert len(log) > 1 and all(entry[0] == "watch" for entry in log[:-1])
