"""The snapshot on disk and the identity-keyed caches of the console dashboard (``plan_dashboard.snapshot``).

Plain pytest, standard library only (textual stays in ``test_plan_dashboard_tui_app.py``, which holds the
tests that need the running app: the old marking on a start from a snapshot, the events' order, SIGTERM). The
one test here that runs the app, the code-version path on the event stream, borrows that file's helpers and
skips, or fails, with it.

Every kind of source the dashboard reads (``snapshot.SOURCE_KINDS``: state, binding, transcript, ledger) is
put through the same four changes between two refreshes — a different size, a same-size rewrite that moves
only the file's ctime, a creation, a deletion — once with warm caches and once with caches a snapshot seeded.
What a refresh then shows must be the new content, never the cached one. The contract the timing harness
reads (``fixtures/plan_dashboard_contract.json``) fixes the file's layout; ``read_snapshot`` below is a
reader written from it alone.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_dashboard import attention, model, snapshot as store, tui
from plan_dashboard_fixtures import set_mtime, write_transcript
from plan_dashboard_support import (  # noqa: F401
    ALPHA, BETA, HARNESS, NOW, ONE_HOUR_NS, STALE, at, console_root, isolated_home, iso, make_state, usage_row,
)

TESTS = Path(__file__).resolve().parent
CONTRACT = json.loads((TESTS / "fixtures" / "plan_dashboard_contract.json").read_text(encoding="utf-8"))
SELECTION = store.selection_key(True, 24, ())
EMPTY_MARKS = {"tasks": {}, "sessions": []}
PAST_NS = int(NOW.timestamp() * 1_000_000_000)
OTHER_ALPHA = "alpha-0000-bbbb"
JUMP_TARGET = {"argv": ["placeholder-no-op", "win-1"], "label": "window one", "return_hint": "use the terminal's tab keys"}


@pytest.fixture(autouse=True)
def cold_caches():
    attention.clear_cache()
    model.clear_cache()
    yield
    attention.clear_cache()
    model.clear_cache()


@pytest.fixture
def root(tmp_path):
    fixture = console_root(tmp_path)
    for path in bindings_dir(fixture).glob("*.json"):
        set_mtime(path, NOW)
    return fixture


def bindings_dir(root) -> Path:
    return root.home / "agentctl" / "engine-session-bindings"


def read_all(root) -> dict:
    """What one refresh reads: the states and the ledger, the binding scan, the transcript listing and tails."""
    snapshot = model.collect(root.state_root, now=NOW, include_all=True, cost_log=root.cost_log)
    store.BINDINGS.read()
    attention.transcript_index(root.transcripts_root)
    for path in sorted(root.transcripts_root.glob("*/*.jsonl")):
        attention.read_tail(path)
    return snapshot


def save(root, path: Path, key: str = SELECTION, marks=EMPTY_MARKS) -> Path:
    return tui.write_snapshot(path, key, tui.snapshot_section(read_all(root), marks, {}, NOW))


def restart(root, tmp_path) -> Path:
    """Save the snapshot a dashboard leaves at exit, forget every cache, and start from it."""
    path = tmp_path / "cache" / store.CACHE_DIRNAME / store.FILE_NAME
    save(root, path)
    attention.clear_cache()
    model.clear_cache()
    assert tui.restore_snapshot(path, SELECTION) is not None
    return path


CTIME_TICKS: dict[int, int] = {}


@pytest.fixture(autouse=True)
def ticking_ctime(monkeypatch):
    """A ctime that moves with every ``rewrite`` whatever the file system's tick: the identity of a file
    rewritten at its old size and mtime then differs, as it does on a file system with a fine clock."""
    CTIME_TICKS.clear()
    real = store.identity_of

    def identity(st):
        dev, ino, size, mtime_ns, ctime_ns = real(st)
        return (dev, ino, size, mtime_ns, ctime_ns + CTIME_TICKS.get(ino, 0))

    monkeypatch.setattr(store, "identity_of", identity)
    yield
    CTIME_TICKS.clear()


def rewrite(path: Path, text: str, *, mtime_ns: int) -> None:
    """Write ``text`` with the mtime ``mtime_ns`` and a later ctime than the file had."""
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    ino = os.stat(path).st_ino
    CTIME_TICKS[ino] = CTIME_TICKS.get(ino, 0) + 1


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    rewrite(path, text.replace(old, new, 1), mtime_ns=os.stat(path).st_mtime_ns)


def task_row(root, session_id) -> dict | None:
    return {task["session_id"]: task for task in read_all(root)["tasks"]}.get(session_id)


# --- one scenario per kind of source and kind of change ---------------------------------------------------
# A scenario is ``setup(root) -> (observe, before, mutate, after)``: what ``observe(root)`` shows before and
# after ``mutate()`` ran between two refreshes.

def state_path(root) -> Path:
    return root.state_root / f"{ALPHA}.json"


def node_of(root):
    row = task_row(root, ALPHA)
    return row and row["node"]


def spawn_cost_of(root):
    return task_row(root, ALPHA)["totals"]["spawn_cost_usd"]


def binding_index(root):
    return as_lists(store.harness_index())


def as_lists(index) -> dict:
    return {key: list(values) for key, values in index.items()}


def transcript_path(root) -> Path:
    return next(root.transcripts_root.glob(f"*/{HARNESS}.jsonl"))


def last_ts(root):
    tail = attention.read_tail(transcript_path(root))
    return tail and tail.last_ts


def listed_transcripts(root):
    return sorted(attention.transcript_index(root.transcripts_root))


def ledger_row(cost: float) -> str:
    return json.dumps({"spawner_session": ALPHA, "stage_index": 1, "ts": iso(at(minutes=25)), "cost_usd": cost}) + "\n"


def binding_text(*engines) -> str:
    return json.dumps({"driven": [{"session": e, "ts": "2026-10-01T00:00:00+00:00"} for e in engines]})


def state_change(root):
    return node_of, "EXECUTING", lambda: edit(state_path(root), '"node": "EXECUTING"', '"node": "PLAN_READY"'), "PLAN_READY"


def state_same_size(root):
    return node_of, "EXECUTING", lambda: edit(state_path(root), '"node": "EXECUTING"', '"node": "XXECUTING"'), "XXECUTING"


def state_create(root):
    path = state_path(root)
    text = path.read_text(encoding="utf-8")
    path.unlink()
    return node_of, None, lambda: rewrite(path, text, mtime_ns=PAST_NS), "EXECUTING"


def state_delete(root):
    return node_of, "EXECUTING", lambda: state_path(root).unlink(), None


def ledger_change(root):
    def grow():
        with open(root.cost_log, "a", encoding="utf-8") as handle:
            handle.write(ledger_row(1.0))
        os.utime(root.cost_log, ns=(PAST_NS, PAST_NS))
    return spawn_cost_of, 1.5, grow, 2.5


def ledger_same_size(root):
    return spawn_cost_of, 1.5, lambda: edit(root.cost_log, '"cost_usd": 1.5', '"cost_usd": 2.5'), 2.5


def ledger_create(root):
    root.cost_log.unlink()
    return spawn_cost_of, 0.0, lambda: rewrite(root.cost_log, ledger_row(1.5), mtime_ns=PAST_NS), 1.5


def ledger_delete(root):
    return spawn_cost_of, 1.5, lambda: root.cost_log.unlink(), 0.0


def binding_change(root):
    path = bindings_dir(root) / f"{HARNESS}.json"
    return (binding_index, {ALPHA: [HARNESS]},
            lambda: rewrite(path, binding_text(ALPHA, BETA), mtime_ns=PAST_NS), {ALPHA: [HARNESS], BETA: [HARNESS]})


def binding_same_size(root):
    path = bindings_dir(root) / f"{HARNESS}.json"
    return binding_index, {ALPHA: [HARNESS]}, lambda: edit(path, ALPHA, OTHER_ALPHA), {OTHER_ALPHA: [HARNESS]}


def binding_create(root):
    path = bindings_dir(root) / "harness-2.json"
    return (binding_index, {ALPHA: [HARNESS]}, lambda: rewrite(path, binding_text(BETA), mtime_ns=PAST_NS),
            {ALPHA: [HARNESS], BETA: ["harness-2"]})


def binding_delete(root):
    return binding_index, {ALPHA: [HARNESS]}, lambda: (bindings_dir(root) / f"{HARNESS}.json").unlink(), {}


def transcript_change(root):
    def append():
        line = json.dumps({"type": "user", "timestamp": iso(at(minutes=1)), "message": {"content": "go on"}}) + "\n"
        with open(transcript_path(root), "a", encoding="utf-8") as handle:
            handle.write(line)
        os.utime(transcript_path(root), ns=(PAST_NS, PAST_NS))
    return last_ts, iso(at(minutes=25)), append, iso(at(minutes=1))


def transcript_same_size(root):
    return (last_ts, iso(at(minutes=25)), lambda: edit(transcript_path(root), iso(at(minutes=25)), iso(at(minutes=24))),
            iso(at(minutes=24)))


def transcript_create(root):
    def create():
        set_mtime(write_transcript(root.transcripts_root, "other-9", [usage_row("m9", at(minutes=2), 1)]), NOW)
    return listed_transcripts, [HARNESS], create, [HARNESS, "other-9"]


def transcript_delete(root):
    return listed_transcripts, [HARNESS], lambda: transcript_path(root).unlink(), []


SCENARIOS = {
    ("state", "change"): state_change, ("state", "same-size"): state_same_size,
    ("state", "create"): state_create, ("state", "delete"): state_delete,
    ("ledger", "change"): ledger_change, ("ledger", "same-size"): ledger_same_size,
    ("ledger", "create"): ledger_create, ("ledger", "delete"): ledger_delete,
    ("binding", "change"): binding_change, ("binding", "same-size"): binding_same_size,
    ("binding", "create"): binding_create, ("binding", "delete"): binding_delete,
    ("transcript", "change"): transcript_change, ("transcript", "same-size"): transcript_same_size,
    ("transcript", "create"): transcript_create, ("transcript", "delete"): transcript_delete,
}


def test_every_kind_of_source_is_put_through_every_change():
    assert {kind for kind, _ in SCENARIOS} == set(store.SOURCE_KINDS)
    assert {change for _, change in SCENARIOS} == {"change", "same-size", "create", "delete"}


@pytest.mark.parametrize("behind_snapshot", [False, True], ids=["warm", "behind-snapshot"])
@pytest.mark.parametrize("kind, change", sorted(SCENARIOS))
def test_a_change_between_two_refreshes_shows_on_the_next_one(root, tmp_path, kind, change, behind_snapshot):
    observe, before, mutate, after = SCENARIOS[kind, change](root)
    read_all(root)
    if behind_snapshot:
        restart(root, tmp_path)
    assert observe(root) == before
    mutate()
    assert observe(root) == after


def watch(root, kind: str) -> None:
    """The cycle's stat of the kind's source, as it is after a change made since the source was read."""
    if kind == "ledger":
        store.OBSERVED.ledger(root.cost_log)
        return
    path = {"state": state_path(root), "binding": bindings_dir(root) / f"{HARNESS}.json",
            "transcript": transcript_path(root)}[kind]
    store.OBSERVED.note(kind, path, os.stat(path))


@pytest.mark.parametrize("change", ["change", "same-size"])
@pytest.mark.parametrize("kind", ["state", "binding", "transcript", "ledger"])
def test_a_value_read_before_its_source_changed_is_not_saved_under_the_identity_it_changed_to(
        root, tmp_path, kind, change):
    """The race of a read and its stat: a value read under one identity, a write, then the stat that the
    snapshot records. The next start must show the new content, never the value of the old read."""
    observe, before, mutate, after = SCENARIOS[kind, change](root)
    read = read_all(root)
    assert observe(root) == before
    mutate()
    watch(root, kind)
    path = tmp_path / "cache" / store.CACHE_DIRNAME / store.FILE_NAME
    tui.write_snapshot(path, SELECTION, tui.snapshot_section(read, EMPTY_MARKS, {}, NOW))
    attention.clear_cache()
    model.clear_cache()
    assert tui.restore_snapshot(path, SELECTION) is not None
    assert observe(root) == after


def test_only_values_read_under_a_recorded_identity_are_exported():
    cache = store.IdentityCache("state")
    cache.put("/a", (1, 2, 3, 4, 5), "a")
    cache.put("/b", (1, 2, 3, 4, 5), "b")
    cache.put("/c", (1, 2, 3, 4, 5), "c")
    identities = {"/a": (1, 2, 3, 4, 5), "/b": (1, 2, 4, 4, 5)}
    assert store.export_values(cache, identities) == {"/a": "a"}


# --- the resolution step ------------------------------------------------------------------------------

def blind_to_the_clock(monkeypatch, st):
    """A file system whose clock the identity cannot see: a second write of the tick leaves the identity as it
    was, and the read happens ``read_after_ns`` after the write."""
    monkeypatch.setattr(store, "identity_of", lambda s: (s.st_dev, s.st_ino, s.st_size, 0, 0))


def stamp(path) -> int:
    """The later of a file's mtime and ctime: the moment it last changed, whoever set its mtime."""
    st = os.stat(path)
    return max(st.st_mtime_ns, st.st_ctime_ns)


def test_a_file_read_within_one_step_of_its_mtime_is_read_again_next_cycle(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("a", encoding="utf-8")
    blind_to_the_clock(monkeypatch, None)
    monkeypatch.setattr(store, "now_ns", lambda: stamp(path) + store.MTIME_STEP_NS - 1)
    cache, reads = store.IdentityCache("state"), []

    def load(p):
        reads.append(p)
        return Path(p).read_text(encoding="utf-8")

    assert cache.fetch(path, os.stat(path), load) == "a"
    path.write_text("b", encoding="utf-8")
    assert cache.fetch(path, os.stat(path), load) == "b"
    assert len(reads) == 2


def test_a_file_read_a_step_after_its_mtime_is_served_from_the_cache(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("a", encoding="utf-8")
    monkeypatch.setattr(store, "now_ns", lambda: stamp(path) + store.MTIME_STEP_NS)
    cache, reads = store.IdentityCache("state"), []

    def load(p):
        reads.append(p)
        return "a"

    for _ in range(3):
        assert cache.fetch(path, os.stat(path), load) == "a"
    assert len(reads) == 1


def test_an_unsettled_source_is_not_recorded_in_the_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("a", encoding="utf-8")
    st = os.stat(path)
    for offset, recorded in ((store.MTIME_STEP_NS - 1, False), (store.MTIME_STEP_NS, True)):
        store.OBSERVED.reset()
        monkeypatch.setattr(store, "now_ns", lambda: stamp(path) + offset)
        store.OBSERVED.note("state", path, st)
        assert (str(path) in {item["path"] for item in store.OBSERVED.sources()}) is recorded


def test_a_ledger_read_inside_the_step_is_finished_from_its_offset_never_read_from_the_start(root, monkeypatch):
    mtime = os.stat(root.cost_log).st_mtime_ns
    clock = [stamp(root.cost_log) + store.MTIME_STEP_NS - 1]
    monkeypatch.setattr(store, "now_ns", lambda: clock[0])
    bases = []
    real = model._extend_sums
    monkeypatch.setattr(model, "_extend_sums", lambda path, base: (bases.append(base and base["offset"]), real(path, base))[1])
    assert spawn_cost_of(root) == 1.5
    with open(root.cost_log, "a", encoding="utf-8") as handle:
        handle.write(ledger_row(1.0))
    os.utime(root.cost_log, ns=(mtime, mtime))
    clock[0] = stamp(root.cost_log) + store.MTIME_STEP_NS - 1
    assert spawn_cost_of(root) == 2.5
    clock[0] = stamp(root.cost_log) + store.MTIME_STEP_NS
    assert spawn_cost_of(root) == 2.5
    assert spawn_cost_of(root) == 2.5
    assert bases[0] is None and bases[1] > 0
    first_offset = bases[1]
    assert len(bases) == 3 and bases[2] > first_offset


def test_a_transcript_read_inside_the_step_that_did_not_grow_is_finished_from_its_offset_never_parsed_again(
        root, monkeypatch):
    path = transcript_path(root)
    clock = [stamp(path) + store.MTIME_STEP_NS - 1]
    monkeypatch.setattr(store, "now_ns", lambda: clock[0])
    starts, real = [], attention._first_read
    monkeypatch.setattr(attention, "_first_read", lambda handle, size: (starts.append(size), real(handle, size))[1])
    first = attention.read_tail(path)
    assert first.ident is None and len(starts) == 1
    second = attention.read_tail(path)
    assert len(starts) == 1 and second.ident is None
    assert second.offset == first.offset and second.last_ts == first.last_ts
    clock[0] = stamp(path) + store.MTIME_STEP_NS
    third = attention.read_tail(path)
    assert len(starts) == 1 and third.ident is not None
    assert attention.read_tail(path) is third


def test_a_transcript_that_shrank_or_kept_its_size_under_a_moved_identity_is_read_from_the_start(root):
    path = transcript_path(root)
    attention.read_tail(path)
    starts, real = [], attention._first_read
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(attention, "_first_read", lambda handle, size: (starts.append(size), real(handle, size))[1])
        text = path.read_text(encoding="utf-8")
        rewrite(path, text, mtime_ns=os.stat(path).st_mtime_ns)
        attention.read_tail(path)
        assert len(starts) == 1
        rewrite(path, text[: len(text) // 2], mtime_ns=os.stat(path).st_mtime_ns)
        attention.read_tail(path)
        assert len(starts) == 2


def test_a_read_ten_milliseconds_after_the_last_change_is_not_settled(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("a", encoding="utf-8")
    ten_milliseconds = 10_000_000
    assert ten_milliseconds < store.MTIME_STEP_NS
    assert not store.settled(os.stat(path), stamp(path) + ten_milliseconds)
    monkeypatch.setattr(store, "now_ns", lambda: stamp(path) + ten_milliseconds)
    cache, reads = store.IdentityCache("state"), []

    def load(p):
        reads.append(p)
        return Path(p).read_text(encoding="utf-8")

    assert cache.fetch(path, os.stat(path), load) == "a" and cache.fetch(path, os.stat(path), load) == "a"
    assert len(reads) == 2


def test_a_file_whose_ctime_is_newer_than_its_restored_mtime_is_not_settled_until_a_step_after_the_ctime():
    st = SimpleNamespace(st_mtime_ns=1_000, st_ctime_ns=50 * store.MTIME_STEP_NS)
    assert not store.settled(st, st.st_ctime_ns + store.MTIME_STEP_NS - 1)
    assert store.settled(st, st.st_ctime_ns + store.MTIME_STEP_NS)


# --- the binding scan ------------------------------------------------------------------------------------

def test_an_unchanged_binding_tree_is_not_read_again_on_the_next_cycle(root, monkeypatch):
    for number in range(20):
        path = bindings_dir(root) / f"bulk-{number}.json"
        rewrite(path, binding_text(ALPHA), mtime_ns=PAST_NS)
    reads = []
    real = store._driven
    monkeypatch.setattr(store, "_driven", lambda path: (reads.append(path), real(path))[1])
    store.BINDINGS.read()
    assert len(reads) == 21
    reads.clear()
    store.BINDINGS.read()
    assert reads == []


def test_a_changed_binding_file_is_the_only_one_read_on_the_next_cycle(root, monkeypatch):
    for number in range(5):
        rewrite(bindings_dir(root) / f"bulk-{number}.json", binding_text(ALPHA), mtime_ns=PAST_NS)
    store.BINDINGS.read()
    reads = []
    real = store._driven
    monkeypatch.setattr(store, "_driven", lambda path: (reads.append(Path(path).name), real(path))[1])
    rewrite(bindings_dir(root) / "bulk-3.json", binding_text(BETA), mtime_ns=PAST_NS)
    index, driven = store.BINDINGS.read()
    assert reads == ["bulk-3.json"] and list(driven["bulk-3"]) == [BETA]
    assert list(index[BETA]) == ["bulk-3"] and "bulk-3" not in index[ALPHA]


# --- a start from the snapshot reads only what changed since --------------------------------------------

def test_a_start_from_a_snapshot_reads_each_changed_source_once_and_the_others_not_at_all(root, tmp_path, monkeypatch):
    restart(root, tmp_path)
    loads, sums, tails = [], [], []
    real_load, real_sums = model._load_digest, model._extend_sums
    monkeypatch.setattr(model, "_load_digest", lambda path, moment: (loads.append(Path(path).name), real_load(path, moment))[1])
    monkeypatch.setattr(model, "_extend_sums", lambda *a, **k: (sums.append(1), real_sums(*a, **k))[1])
    edit(state_path(root), '"node": "EXECUTING"', '"node": "PLAN_READY"')
    read_all(root)
    assert loads == [f"{ALPHA}.json"] and sums == []
    changed = store.OBSERVED.peek()
    assert str(state_path(root)) in changed
    assert str(root.cost_log) not in changed and str(transcript_path(root)) not in changed


def test_an_unread_ledger_figure_stays_unknown_across_a_start_from_a_snapshot(root, tmp_path):
    root.cost_log.unlink()
    root.cost_log.mkdir()
    assert spawn_cost_of(root) is None
    restart(root, tmp_path)
    assert spawn_cost_of(root) is None


# --- R5: unknown and partial figures --------------------------------------------------------------------

def test_a_ledger_that_cannot_be_read_is_unknown_and_never_zero(root):
    root.cost_log.unlink()
    root.cost_log.mkdir()
    totals = task_row(root, ALPHA)["totals"]
    assert totals["spawn_cost_usd"] is None and totals["cost_usd"] is None and totals["partial"] is True
    assert tui.cost_text(totals) == tui.NOT_COUNTED


def test_an_absent_ledger_is_zero_spent_not_unknown(root):
    root.cost_log.unlink()
    totals = task_row(root, ALPHA)["totals"]
    assert totals["spawn_cost_usd"] == 0.0


def test_a_total_with_an_unread_part_is_partial_and_a_lower_bound():
    spawn_only = model.totals(wall_clock_s=60, spawn_cost_usd=1.5, root_cost_usd=None, root_counted=False)
    root_only = model.totals(wall_clock_s=60, spawn_cost_usd=None, root_cost_usd=2.0, root_counted=True)
    both = model.totals(wall_clock_s=60, spawn_cost_usd=1.5, root_cost_usd=2.0, root_counted=True)
    neither = model.totals(wall_clock_s=60, spawn_cost_usd=None, root_cost_usd=None, root_counted=False)
    assert [t["partial"] for t in (spawn_only, root_only, both, neither)] == [True, True, False, True]
    assert (root_only["spawn_cost_usd"], neither["cost_usd"]) == (None, None)
    assert tui.cost_text(root_only).startswith("≥") and tui.cost_text(neither) == tui.NOT_COUNTED
    assert not tui.cost_text(both).startswith("≥")


# --- the file -------------------------------------------------------------------------------------------

def read_snapshot(path: Path) -> dict | None:
    """A reader of the file written from the contract alone: None for every case the contract calls absent."""
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    schema = CONTRACT["snapshot"]["schema"]
    if not isinstance(document, dict) or document.get("version") != schema["version"] or document["version"] is True:
        return None
    body = json.dumps(document.get("sections"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if document.get("body_sha256") != hashlib.sha256(body).hexdigest():
        return None
    return document


def test_the_snapshot_follows_the_contract(root, tmp_path):
    path = tmp_path / "cache" / store.CACHE_DIRNAME / store.FILE_NAME
    save(root, path)
    tui.write_snapshot(path, store.selection_key(False, 24, ()), tui.snapshot_section(read_all(root), EMPTY_MARKS, {}, NOW))
    document = read_snapshot(path)
    assert document is not None and (
        set(document) == set(CONTRACT["snapshot"]["schema"]) | {"sections", "code", "sources_sha256"})
    assert set(document["sections"]) == {SELECTION, store.selection_key(False, 24, ())}
    wanted = set(CONTRACT["snapshot"]["schema"]["sources"][0])
    assert document["sources"] and {kind for kind in (s["kind"] for s in document["sources"])} <= set(store.SOURCE_KINDS)
    for source in document["sources"]:
        assert set(source) == wanted
        assert all(isinstance(source[key], int) for key in store.IDENTITY_KEYS)
        assert source["path"].startswith(str(root.home))
    assert sorted(os.listdir(path.parent)) == [store.FILE_NAME]
    assert store.load(path) == document


def test_only_the_last_selection_keeps_the_values_the_start_seeds_the_caches_with(root, tmp_path):
    path = tmp_path / store.FILE_NAME
    live = store.selection_key(False, 24, ())
    save(root, path, SELECTION)
    save(root, path, live)
    sections = store.load(path)["sections"]
    assert "values" in sections[live] and "values" not in sections[SELECTION]
    assert tui.restore_snapshot(path, SELECTION) is not None
    assert tui.restore_snapshot(path, "all:1:nobody") is None


def test_the_path_is_under_the_cache_home_whatever_the_environment():
    assert store.default_path({"XDG_CACHE_HOME": "/x/cache", "HOME": "/h"}) == Path("/x/cache/plan-dashboard/snapshot.json")
    assert store.default_path({"HOME": "/h"}) == Path("/h/.cache/plan-dashboard/snapshot.json")
    assert store.default_path({"XDG_CACHE_HOME": "", "HOME": "/h"}) == Path("/h/.cache/plan-dashboard/snapshot.json")


def test_a_failed_write_leaves_the_old_snapshot_and_no_temporary_file(root, tmp_path, monkeypatch):
    path = tmp_path / "cache" / store.FILE_NAME
    save(root, path)
    before = path.read_bytes()

    def refuse(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        save(root, path)
    assert path.read_bytes() == before and os.listdir(path.parent) == [store.FILE_NAME]


def test_text_is_stored_as_utf8_and_reads_back_unchanged(tmp_path):
    path = tmp_path / "cache" / store.FILE_NAME
    sections = {"live:24:": {"goal": "Ускорить запуск", "tail": "half \ud83d pair"}}
    store.write(path, sections, [])
    raw = path.read_bytes()
    assert "Ускорить запуск".encode("utf-8") in raw and b"\\u0423" not in raw
    assert b"\\ud83d" in raw
    stored = store.load(path)["sections"]["live:24:"]
    assert {key: stored[key] for key in ("goal", "tail")} == sections["live:24:"]


def a_dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    child.wait()
    return child.pid


def test_a_write_removes_the_temporaries_a_killed_writer_left(root, tmp_path):
    path = tmp_path / "cache" / store.FILE_NAME
    path.parent.mkdir()
    for suffix in (a_dead_pid(), a_dead_pid(), "not-a-pid"):
        (path.parent / f"{store.FILE_NAME}.tmp-{suffix}").write_text("half", encoding="utf-8")
    save(root, path)
    assert os.listdir(path.parent) == [store.FILE_NAME]


def test_a_write_leaves_the_temporary_of_a_live_writer_alone(root, tmp_path):
    path = tmp_path / "cache" / store.FILE_NAME
    path.parent.mkdir()
    live = path.parent / f"{store.FILE_NAME}.tmp-{os.getppid()}"
    live.write_text("being written", encoding="utf-8")
    save(root, path)
    assert live.read_text(encoding="utf-8") == "being written"
    assert sorted(os.listdir(path.parent)) == sorted([store.FILE_NAME, live.name])


def test_a_failure_while_clearing_stale_temporaries_leaves_no_temporary_of_this_process(root, tmp_path, monkeypatch):
    path = tmp_path / "cache" / store.FILE_NAME
    path.parent.mkdir()
    own = path.parent / f"{store.FILE_NAME}.tmp-{os.getpid()}"
    own.write_text("half", encoding="utf-8")

    def fails(_):
        raise OSError("a directory that cannot be listed")

    monkeypatch.setattr(store, "_remove_stale_temporaries", fails)
    with pytest.raises(OSError):
        save(root, path)
    assert not own.exists() and not path.exists()


@pytest.fixture
def code_files(tmp_path, monkeypatch):
    files = [tmp_path / "one.py", tmp_path / "two.py"]
    for number, file in enumerate(files):
        file.write_text(f"VALUE = {number}\n", encoding="utf-8")
    monkeypatch.setattr(store, "code_files", lambda: tuple(files))
    store.code_version.cache_clear()
    yield files
    monkeypatch.undo()
    store.code_version.cache_clear()


def test_the_code_version_follows_the_content_of_every_module_that_makes_the_values(code_files):
    first = store.code_version()
    for file in code_files:
        file.write_text(file.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        store.code_version.cache_clear()
        second = store.code_version()
        assert second != first
        first = second


def repo_modules_loaded_by(*modules: str) -> set[str]:
    """The files under ``scripts/`` a fresh interpreter has loaded after importing ``modules``: what the
    interpreter itself says those modules pull in."""
    program = (
        "import sys, json; sys.path.insert(0, sys.argv[1])\n"
        "for name in sys.argv[2:]: __import__(name)\n"
        "print(json.dumps(sorted(m.__file__ for m in list(sys.modules.values())"
        " if getattr(m, '__file__', None) and m.__file__.startswith(sys.argv[1]))))\n")
    done = subprocess.run([sys.executable, "-I", "-c", program, str(store.SCRIPTS), *modules],
                          capture_output=True, text=True, check=True)
    return {Path(name).resolve().relative_to(store.SCRIPTS).as_posix() for name in json.loads(done.stdout)}


def derived() -> set[str]:
    return {file.relative_to(store.SCRIPTS).as_posix() for file in store.code_files()}


def test_the_code_version_covers_every_repo_module_the_values_pass_through():
    assert {"agentctl/effort.py", "lib/transcript_cost.py", "lib/engine_session_binding.py",
            "plan_dashboard/model.py", "plan_dashboard/attention.py", "plan_dashboard/tui.py",
            "plan_dashboard/snapshot.py", "plan_dashboard/locate.py", "plan_dashboard/tui_app.py"} <= derived()


def test_the_derived_code_files_include_every_module_an_interpreter_loads_for_the_dashboard_modules():
    loaded = repo_modules_loaded_by("plan_dashboard.model", "plan_dashboard.attention", "plan_dashboard.tui",
                                    "plan_dashboard.snapshot", "plan_dashboard.locate")
    assert loaded - {"plan_dashboard/__init__.py", "lib/__init__.py", "agentctl/__init__.py"} <= derived()


def test_the_derived_code_files_follow_relative_and_nested_imports(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    (package / "sub").mkdir(parents=True)
    for name, text in {"root.py": "from . import sibling\n", "sibling.py": "def f():\n    from .sub import leaf\n",
                       "sub/__init__.py": "", "sub/leaf.py": "import pkg.other\n", "other.py": "",
                       "unrelated.py": ""}.items():
        (package / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(store, "SCRIPTS", tmp_path)
    monkeypatch.setattr(store, "CODE_ROOTS", ("pkg.root",))
    monkeypatch.setattr(store, "code_files", store.code_files.__wrapped__)
    assert {file.relative_to(tmp_path).as_posix() for file in store.code_files()} == {
        "pkg/root.py", "pkg/sibling.py", "pkg/sub/__init__.py", "pkg/sub/leaf.py", "pkg/other.py"}


def test_a_read_ends_at_its_next_source_once_the_app_is_leaving(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    loaded = []
    store.STOP.set()
    try:
        with pytest.raises(store.Stopped):
            store.IdentityCache("state").fetch(source, source.stat(), loaded.append)
    finally:
        store.STOP.clear()
    assert loaded == [] and not issubclass(store.Stopped, Exception)


BLOCKS_WITH_AN_IMPORT = '''
import top
if cond:
    import in_if
else:
    import in_else
try:
    import in_try
except ImportError:
    import in_except
else:
    import in_try_else
finally:
    import in_finally
try:
    pass
except* OSError:
    import in_except_star
with ctx:
    import in_with
for item in items:
    import in_for
else:
    import in_for_else
while cond:
    import in_while
else:
    import in_while_else
class Holder:
    import in_class
    def method(self):
        import in_method
        def inner():
            import in_inner
async def coroutine():
    async with ctx:
        import in_async_with
    async for item in items:
        import in_async_for
match value:
    case 1:
        import in_case
x = lambda: (yield)
'''


def test_the_imports_of_a_module_are_found_in_every_kind_of_block(tmp_path, monkeypatch):
    import ast

    source = tmp_path / "pkg" / "module.py"
    source.parent.mkdir()
    source.write_text(BLOCKS_WITH_AN_IMPORT, encoding="utf-8")
    monkeypatch.setattr(store, "SCRIPTS", tmp_path)
    everywhere = {alias.name for node in ast.walk(ast.parse(BLOCKS_WITH_AN_IMPORT)) if isinstance(node, ast.Import)
                  for alias in node.names}
    assert len(everywhere) == 19 and store._imported_names(source) == everywhere


def test_the_imports_found_in_the_blocks_are_the_imports_a_walk_of_every_node_finds_in_every_repo_module():
    import ast

    for path in store.SCRIPTS.rglob("*.py"):
        try:
            tree = ast.parse(path.read_bytes())
        except SyntaxError:
            continue
        in_blocks = [node for node in store._statements(tree.body) if isinstance(node, (ast.Import, ast.ImportFrom))]
        everywhere = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        assert sorted(map(ast.dump, in_blocks)) == sorted(map(ast.dump, everywhere)), path


def test_a_missing_or_unparsable_code_file_gives_a_version_no_other_process_shares(tmp_path, monkeypatch):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "root.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(store, "SCRIPTS", tmp_path)
    monkeypatch.setattr(store, "code_files", store.code_files.__wrapped__)
    versions = []
    for roots in (("pkg.root",), ("pkg.missing",)):
        monkeypatch.setattr(store, "CODE_ROOTS", roots)
        store.code_version.cache_clear()
        versions.append(store.code_version())
    store.code_version.cache_clear()
    assert all(version.startswith("unreadable-") for version in versions)


def test_a_snapshot_written_by_other_code_is_absent_after_the_code_changed(root, tmp_path, code_files):
    path = tmp_path / store.FILE_NAME
    store.write(path, {SELECTION: {"marks": EMPTY_MARKS}}, [])
    assert store.load(path) is not None
    code_files[0].write_text("VALUE = 'fixed'\n", encoding="utf-8")
    store.code_version.cache_clear()
    assert store.load(path) is None


@pytest.fixture
def code_tree(tmp_path, monkeypatch):
    """A code tree of its own: ``pkg.root`` imports ``pkg.helper`` and ``pkg.later``, which does not exist yet.
    Every file and directory carries a past mtime, so a later write moves its identity whatever the clock's tick."""
    scripts = tmp_path / "scripts"
    package = scripts / "pkg"
    package.mkdir(parents=True)
    (package / "root.py").write_text("from . import helper\nimport pkg.later\n", encoding="utf-8")
    (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    for path in (package / "root.py", package / "helper.py", package, scripts):
        os.utime(path, ns=(PAST_NS, PAST_NS))
    monkeypatch.setattr(store, "SCRIPTS", scripts)
    monkeypatch.setattr(store, "CODE_ROOTS", ("pkg.root",))
    monkeypatch.setattr(store, "code_files", store.code_files.__wrapped__)
    store.code_version.cache_clear()
    yield package
    monkeypatch.undo()
    store.code_version.cache_clear()


def written_by_another_process(path: Path) -> Path:
    """A snapshot written, then the code version forgotten, as a new process starts without it."""
    store.write(path, {SELECTION: {"marks": EMPTY_MARKS}}, [])
    store.code_version.cache_clear()
    return path


def count_parses(monkeypatch) -> list:
    import ast

    parsed, original = [], ast.parse
    monkeypatch.setattr(ast, "parse", lambda *args, **kwargs: parsed.append(args) or original(*args, **kwargs))
    return parsed


def test_an_unchanged_code_tree_is_accepted_without_parsing(code_tree, tmp_path, monkeypatch):
    path = written_by_another_process(tmp_path / store.FILE_NAME)
    parsed = count_parses(monkeypatch)
    assert store.load(path) is not None
    assert len(parsed) == 0


def test_a_changed_code_file_invalidates_the_snapshot(code_tree, tmp_path):
    same_size, other_size = "VALUE = 2\n", "VALUE = 1234\n"
    assert len(same_size) == len((code_tree / "helper.py").read_text(encoding="utf-8")) != len(other_size)
    for rewrite in (same_size, other_size):
        path = written_by_another_process(tmp_path / store.FILE_NAME)
        (code_tree / "helper.py").write_text(rewrite, encoding="utf-8")
        assert store.load(path) is None
        assert store.code_check_report()["code_version_path"] == "full"
        store.code_version.cache_clear()


def test_a_module_newly_resolving_an_import_invalidates_the_snapshot(code_tree, tmp_path):
    path = written_by_another_process(tmp_path / store.FILE_NAME)
    (code_tree / "later.py").write_text("LATER = True\n", encoding="utf-8")
    assert store.load(path) is None
    store.code_version.cache_clear()
    later_package = code_tree / "later"
    (code_tree / "later.py").unlink()
    os.utime(code_tree, ns=(PAST_NS, PAST_NS))
    path = written_by_another_process(tmp_path / store.FILE_NAME)
    later_package.mkdir()
    (later_package / "__init__.py").write_text("LATER = True\n", encoding="utf-8")
    assert store.load(path) is None


def rewritten_record(path: Path, record) -> Path:
    document = json.loads(path.read_text(encoding="utf-8"))
    for section in document["sections"].values():
        if record is None:
            section.pop("code_identities")
        else:
            section["code_identities"] = record
    document["body_sha256"] = store.body_sha256(document["sections"])
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_snapshot_without_code_identities_takes_the_full_path(code_tree, tmp_path, monkeypatch):
    path = written_by_another_process(tmp_path / store.FILE_NAME)
    record = next(iter(json.loads(path.read_text(encoding="utf-8"))["sections"].values()))["code_identities"]
    malformed = (None, "not a record", {**record, "files": {"pkg/root.py": [1, 2]}}, {**record, "dirs": None},
                 {**record, "taken_ns": "soon"}, {**record, "root": str(tmp_path)})
    for damaged in malformed:
        rewritten_record(written_by_another_process(path), damaged)
        parsed = count_parses(monkeypatch)
        assert store.load(path) is not None, damaged
        assert parsed, damaged
        reason = "identity differs" if damaged is malformed[-1] else "no record"
        assert store.code_check_report() == {"code_version_path": "full", "code_version_reason": reason}, damaged
        store.code_version.cache_clear()


def test_a_code_file_written_within_one_resolution_step_of_the_walk_takes_the_full_path(code_tree, tmp_path, monkeypatch):
    changed = max(max(p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in (code_tree, *code_tree.iterdir()))
    monkeypatch.setattr(store, "now_ns", lambda: changed + store.MTIME_STEP_NS // 2)
    path = written_by_another_process(tmp_path / store.FILE_NAME)
    assert store.load(path) is not None
    assert store.code_check_report() == {"code_version_path": "full", "code_version_reason": "write within resolution step"}


def test_the_code_version_path_is_reported_on_the_event_stream(code_tree, tmp_path):
    import test_plan_dashboard_tui_app as app_tests  # skips, or fails under PLAN_DASHBOARD_REQUIRE_TEXTUAL=1, without textual
    from plan_dashboard import events

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    events.enable(write_fd)
    try:
        root = app_tests.attention_root(tmp_path)
        path = app_tests.snapshot_file(tmp_path)
        app_tests.leave_a_snapshot(root, path, locator=app_tests.attention_locator())
        written = [e for e in app_tests.drain(read_fd) if e["ev"] == "snapshot_written"]
        assert written and all("code_version_path" in e for e in written)
        paths = []
        for change in (None, "VALUE = 2\n"):
            if change is not None:
                (code_tree / "helper.py").write_text(change, encoding="utf-8")
            store.code_version.cache_clear()
            attention.clear_cache()
            model.clear_cache()
            app_tests.run(app_tests.nothing, root, snapshot_path=path, locator=app_tests.attention_locator())
            painted = [e for e in app_tests.drain(read_fd) if e["ev"] == "rows_painted"]
            assert painted
            paths.append({(e["code_version_path"], e.get("code_version_reason")) for e in painted})
        assert paths == [{("fast", None)}, {("full", "identity differs")}]
    finally:
        events.disable()
        os.close(read_fd)
        os.close(write_fd)


def damage(path: Path, how: str) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    if how == "damaged":
        path.write_text("not json at all", encoding="utf-8")
    elif how == "half-written":
        path.write_text(text[: len(text) // 2], encoding="utf-8")
    elif how == "foreign-version":
        path.write_text(json.dumps({**document, "version": 2}), encoding="utf-8")
    elif how == "boolean-version":
        path.write_text(json.dumps({**document, "version": True}), encoding="utf-8")
    elif how == "tampered":
        section = next(iter(document["sections"].values()))
        section["snapshot"]["tasks"][0]["goal"] = "someone else's goal"
        path.write_text(json.dumps(document), encoding="utf-8")
    elif how == "foreign-code":
        path.write_text(json.dumps({**document, "code": "0" * 64}), encoding="utf-8")
    elif how == "old-shape":
        document.pop("code")
        document.pop("sources_sha256")
        path.write_text(json.dumps(document), encoding="utf-8")
    elif how == "tampered-sources":
        document["sources"][0]["size"] += 1
        path.write_text(json.dumps(document), encoding="utf-8")
    elif how == "bad-source":
        document["sources"][0].pop("ino")
        path.write_text(json.dumps(document), encoding="utf-8")
    elif how == "unreadable":
        path.unlink()
        path.mkdir()
    elif how == "missing":
        path.unlink()


@pytest.mark.parametrize("how", ["missing", "damaged", "half-written", "foreign-version", "boolean-version",
                                 "tampered", "bad-source", "unreadable", "foreign-code", "old-shape",
                                 "tampered-sources"])
def test_a_snapshot_that_cannot_be_trusted_is_absent(root, tmp_path, how):
    path = tmp_path / store.FILE_NAME
    save(root, path)
    assert read_snapshot(path) is not None and store.load(path) is not None
    damage(path, how)
    assert store.load(path) is None
    attention.clear_cache()
    model.clear_cache()
    assert tui.restore_snapshot(path, SELECTION) is None
    assert store.OBSERVED.peek() == []


def test_a_section_without_its_rows_is_not_restored(root, tmp_path):
    path = tmp_path / store.FILE_NAME
    store.write(path, {SELECTION: {"marks": EMPTY_MARKS}}, [])
    assert store.load(path) is not None and tui.restore_snapshot(path, SELECTION) is None


# --- what the screen shows ------------------------------------------------------------------------------

def test_a_row_read_from_the_snapshot_wears_the_old_marking_until_it_is_read_again(root):
    snapshot = read_all(root)
    overlay = {"tasks": {ALPHA: {"waiting": "choice"}, BETA: {"waiting": "idle"}}, "sessions": []}
    old = {row["session_id"]: row["cells"][1] for row in tui.overview_rows(snapshot, NOW, overlay, old={ALPHA})}
    live = {row["session_id"]: row["cells"][1] for row in tui.overview_rows(snapshot, NOW, overlay)}
    assert (old[ALPHA], old[BETA]) == (tui.OLD_MARK + "Q", "…")
    assert (live[ALPHA], live[BETA]) == ("Q", "…")
    assert tui.old_marking("Q") == "~Q" == CONTRACT["screen"]["old_marking"]["text"] + "Q"


def test_a_source_belongs_to_the_tasks_the_contract_names():
    assert set(CONTRACT["source_to_task"]) == {"state", "binding", "transcript", "ledger", "tmux_listing"}
    driven, tasks = {"h1": ["t1", "t2"]}, ["t1", "t2", "t3", "h1"]
    belongs = {kind: store.tasks_of_source(f"/x/{stem}", kind, driven, tasks)
               for kind, stem in (("state", "t1"), ("binding", "h1"), ("transcript", "h1"), ("ledger", "cost"))}
    assert belongs == {"state": {"t1"}, "binding": {"t1", "t2"}, "transcript": {"t1", "t2", "h1"}, "ledger": set(tasks)}


def test_a_cycle_reports_what_was_created_changed_deleted_or_read_since_the_snapshot():
    def entry(path, size):
        return {"path": path, "kind": "state", "dev": 1, "ino": 2, "size": size, "mtime_ns": 3, "ctime_ns": 4}

    def stat(size):
        return SimpleNamespace(st_dev=1, st_ino=2, st_size=size, st_mtime_ns=3, st_ctime_ns=4)

    store.seed_observation([entry("/a", 10), entry("/b", 10), entry("/c", 10)], None)
    store.OBSERVED.scanned("state", {"/a": (stat(10), 0), "/b": (stat(11), 0), "/d": (stat(10), 0)})
    assert store.OBSERVED.peek() == ["/b", "/c", "/d"]
    store.OBSERVED.note_read("/a")
    changed, kinds, _ = store.observation()
    assert changed == ["/a", "/b", "/c", "/d"] and kinds["/c"] == "state"


def test_a_jump_from_an_old_row_whose_window_is_gone_finds_a_live_window_of_the_task(root, tmp_path):
    marks = {"tasks": {ALPHA: {"waiting": "none", "harness_session": "h-gone"}}, "sessions": []}
    path = tmp_path / store.FILE_NAME
    save(root, path, marks=marks)
    stored = tui.restore_snapshot(path, SELECTION)["marks"]
    tried = []

    def jump_target(harness):
        tried.append(harness)
        return JUMP_TARGET if harness == HARNESS else None

    ran = []
    status = tui.jump_to(("task", ALPHA), stored, SimpleNamespace(NAME="fake", jump_target=jump_target),
                         runner=lambda *args, **kwargs: ran.append(args) or subprocess.CompletedProcess(args[0], 0, "", ""))
    assert tried == ["h-gone", HARNESS] and len(ran) == 1
    assert status.startswith("window one — return:")


# --- the snapshot records what the window shows, and the live check still sees the rest --------------------

WINDOW = store.selection_key(False, 24, ())
OUT_OF_WINDOW_NS = PAST_NS - 72 * ONE_HOUR_NS


def read_window(root) -> dict:
    snapshot = model.collect(root.state_root, now=NOW, include_all=False, cost_log=root.cost_log)
    store.BINDINGS.read()
    attention.transcript_index(root.transcripts_root)
    return snapshot


def restart_window(root, tmp_path) -> dict:
    """Save the window's snapshot, forget every cache and start from it; the saved document is returned."""
    path = tmp_path / "cache" / store.CACHE_DIRNAME / store.FILE_NAME
    tui.write_snapshot(path, WINDOW, tui.snapshot_section(read_window(root), EMPTY_MARKS, {}, NOW))
    attention.clear_cache()
    model.clear_cache()
    assert tui.restore_snapshot(path, WINDOW) is not None
    return json.loads(path.read_text(encoding="utf-8"))


def out_of_window_state(root, name: str) -> Path:
    path = root.state_root / f"{name}.json"
    rewrite(path, (root.state_root / f"{STALE}.json").read_text(encoding="utf-8"), mtime_ns=OUT_OF_WINDOW_NS)
    return path


def recorded_states(document) -> list[str]:
    return [source["path"] for source in document["sources"] if source["kind"] == "state"]


def test_a_new_state_file_behind_the_snapshot_is_detected(root, tmp_path):
    os.utime(root.state_root, ns=(OUT_OF_WINDOW_NS, OUT_OF_WINDOW_NS))   # its creation then moves the marker
    restart_window(root, tmp_path)
    created = out_of_window_state(root, "late-3333-dddd")
    read_window(root)
    assert str(created) in store.observation()[0]


def test_the_snapshot_does_not_grow_with_state_files_outside_the_window(root, tmp_path):
    for n in range(2):
        out_of_window_state(root, f"old-{n}-eeee")
    few = recorded_states(restart_window(root, tmp_path))
    for n in range(2, 12):
        out_of_window_state(root, f"old-{n}-eeee")
    many = restart_window(root, tmp_path)
    assert len(recorded_states(many)) == len(few)
    assert str(root.state_root / f"{STALE}.json") not in recorded_states(many)


def test_a_changed_state_file_outside_the_window_is_applied_by_the_live_check(root, tmp_path):
    stale = root.state_root / f"{STALE}.json"
    assert str(stale) not in recorded_states(restart_window(root, tmp_path))
    rewrite(stale, stale.read_text(encoding="utf-8") + " ", mtime_ns=os.stat(stale).st_mtime_ns)
    CTIME_TICKS[os.stat(stale).st_ino] += ONE_HOUR_NS                 # its ctime on the dashboard's clock
    read_window(root)
    assert str(stale) in store.observation()[0]


# --- a transcript of a task the dashboard does not show the task from, and a source rewritten in place ------

def read_as_the_app_does(root) -> dict:
    """What a refresh reads of the transcripts: through each task's binding, as the app reads them."""
    snapshot = model.collect(root.state_root, now=NOW, include_all=True, cost_log=root.cost_log)
    attention.collect([task["session_id"] for task in snapshot["tasks"]], None, root.transcripts_root)
    return snapshot


def restart_from_an_app_read(root, tmp_path) -> Path:
    path = tmp_path / "cache" / store.CACHE_DIRNAME / store.FILE_NAME
    tui.write_snapshot(path, SELECTION, tui.snapshot_section(read_as_the_app_does(root), EMPTY_MARKS, {}, NOW))
    attention.clear_cache()
    model.clear_cache()
    assert tui.restore_snapshot(path, SELECTION) is not None
    return path


def grow(path: Path) -> None:
    rewrite(path, path.read_text(encoding="utf-8") + "\n", mtime_ns=os.stat(path).st_mtime_ns)


def test_a_same_named_transcript_of_a_task_is_applied_by_the_live_check_when_the_task_shows_another(root, tmp_path):
    own = write_transcript(root.transcripts_root, ALPHA, [usage_row("o1", at(minutes=40), 1)])
    set_mtime(own, at(minutes=40))
    [bound] = root.transcripts_root.glob(f"*/{HARNESS}.jsonl")
    assert os.stat(bound).st_mtime_ns > os.stat(own).st_mtime_ns
    restart_from_an_app_read(root, tmp_path)
    grow(own)
    read_as_the_app_does(root)
    changed, kinds, driven = store.observation()
    assert str(own) in changed
    assert store.applied_by_task([str(own)], kinds, driven, [ALPHA, BETA]) == {ALPHA: [str(own)], BETA: []}


def test_the_harness_counts_a_changed_transcript_of_a_shown_task_from_a_snapshot_the_dashboard_wrote(root, tmp_path):
    import plan_dashboard_harness as harness

    path = restart_from_an_app_read(root, tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    [bound] = root.transcripts_root.glob(f"*/{HARNESS}.jsonl")
    grow(bound)
    taken_when_no_state_file_is_in_the_window = (NOW + dt.timedelta(days=10)).isoformat()
    copy = {"file": str(path), "taken_at": taken_when_no_state_file_is_in_the_window, "sources": document["sources"]}

    def counted() -> list[str]:
        return [item["path"] for item in harness.contract_changes(copy, harness.live_sources())
                if item["kind"] == "transcript"]

    assert counted() == [str(bound)]
    for section in document["sections"].values():
        for task in section["snapshot"]["tasks"]:
            del task["session_id"]
    path.write_text(json.dumps(document), encoding="utf-8")
    assert counted() == []


def test_an_out_of_window_state_file_rewritten_in_place_is_applied_by_the_live_check(root, tmp_path):
    stale = root.state_root / f"{STALE}.json"
    assert str(stale) not in recorded_states(restart_window(root, tmp_path))
    marker = store.directory_marker(root.state_root)
    rewrite(stale, stale.read_text(encoding="utf-8") + " ", mtime_ns=os.stat(stale).st_mtime_ns)
    CTIME_TICKS[os.stat(stale).st_ino] += ONE_HOUR_NS
    assert store.directory_marker(root.state_root) == marker
    read_window(root)
    assert str(stale) in store.observation()[0]


# --- a stat-path cycle reads each directory once and each path once ---------------------------------------

FAR_FUTURE_NS = 36_000 * 1_000_000_000


@pytest.fixture
def settled_clock(monkeypatch):
    """A clock far past every file's last change: every directory key and every stat is settled."""
    monkeypatch.setattr(store, "now_ns", lambda: time.time_ns() + FAR_FUTURE_NS)


def count_reads(monkeypatch):
    """``(listings, stats)``: the paths ``os.scandir`` and ``os.stat`` are asked for from now on."""
    listings, stats = [], []
    real_scandir, real_stat = os.scandir, os.stat
    monkeypatch.setattr(os, "scandir", lambda path, *a, **k: (listings.append(str(path)), real_scandir(path, *a, **k))[1])
    monkeypatch.setattr(os, "stat", lambda path, *a, **k: (stats.append(str(path)), real_stat(path, *a, **k))[1])
    return listings, stats


def stat_cycle(view, directory, by_directory_time=True):
    """One stat-path cycle that lists ``directory`` and stats each entry: ``{name: (is_dir, stat)}``."""
    view.begin_stat_cycle()
    try:
        return {entry.name: (entry.is_dir(), entry.stat()) for entry in view.scandir(directory, by_directory_time)}
    finally:
        view.end_stat_cycle()


def move_directory_key(directory):
    """Touch the directory until its mtime or ctime moves, whatever the file system's tick."""
    scratch = Path(directory) / ".scratch"

    def times():
        seen = os.stat(directory)
        return seen.st_mtime_ns, seen.st_ctime_ns

    start = times()
    while times() == start:
        scratch.write_text("x", encoding="utf-8")
        scratch.unlink()


def test_a_stat_path_cycle_reads_one_listing_and_one_stat_per_path(root, monkeypatch):
    store.reset()
    listings, stats = count_reads(monkeypatch)
    store.KEPT.begin_stat_cycle()
    model.collect(root.state_root, now=NOW, include_all=True, cost_log=root.cost_log)
    attention.transcript_index(root.transcripts_root)
    store.harness_index()
    model.child_index(root.state_root)
    store.KEPT.end_stat_cycle()
    mine = (str(root.home), str(root.state_root), str(root.transcripts_root))
    ours = lambda paths: [path for path in paths if path.startswith(mine)]  # noqa: E731
    assert ours(listings) and ours(stats)
    assert [path for path, n in Counter(ours(listings)).items() if n > 1] == []
    assert [path for path, n in Counter(ours(stats)).items() if n > 1] == []
    store.reset()


def test_an_unchanged_directory_is_listed_once_over_several_stat_cycles(tmp_path, settled_clock, monkeypatch):
    directory = tmp_path / "bindings"
    directory.mkdir()
    files = sorted(str(directory / f"{number}.json") for number in range(5))
    for path in files:
        Path(path).write_text("{}", encoding="utf-8")
    listings, stats = count_reads(monkeypatch)
    view = store.KeptView()
    first = stat_cycle(view, directory)
    for _ in range(3):
        assert stat_cycle(view, directory) == first
    assert listings.count(str(directory)) == 1
    assert sorted(path for path in stats if path.endswith(".json")) == files


def test_a_replace_over_an_existing_name_is_seen_by_the_next_stat_cycle(tmp_path, settled_clock):
    directory, aside = tmp_path / "bindings", tmp_path / "aside"
    directory.mkdir()
    aside.mkdir()
    (directory / "a.json").write_text("{}", encoding="utf-8")
    view = store.KeptView()
    before = stat_cycle(view, directory)
    assert before["a.json"][0] is False
    (tmp_path / "link").symlink_to(aside)
    os.replace(tmp_path / "link", directory / "a.json")
    move_directory_key(directory)
    after = stat_cycle(view, directory)
    assert after["a.json"][0] is True and after["a.json"][1].st_ino != before["a.json"][1].st_ino


def test_a_change_between_the_key_stat_and_the_listing_is_seen_by_the_next_cycle(tmp_path, settled_clock, monkeypatch):
    directory = tmp_path / "bindings"
    directory.mkdir()
    (directory / "a.json").write_text("{}", encoding="utf-8")
    real, done = store.read_directory, []

    def listing_then_a_creation(root):
        names = real(root)
        if not done:
            done.append(1)
            (directory / "b.json").write_text("{}", encoding="utf-8")
            move_directory_key(root)
        return names

    monkeypatch.setattr(store, "read_directory", listing_then_a_creation)
    view = store.KeptView()
    assert set(stat_cycle(view, directory)) == {"a.json"}
    assert set(stat_cycle(view, directory)) == {"a.json", "b.json"}


def test_a_listing_under_an_unsettled_key_is_not_reused(tmp_path, monkeypatch):
    directory = tmp_path / "bindings"
    directory.mkdir()
    (directory / "a.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(store, "now_ns", lambda: stamp(directory) + store.MTIME_STEP_NS - 1)
    listings, _ = count_reads(monkeypatch)
    view = store.KeptView()
    stat_cycle(view, directory)
    stat_cycle(view, directory)
    assert listings.count(str(directory)) == 2


def test_an_in_place_rewrite_in_a_memo_only_directory_is_seen(tmp_path, settled_clock):
    directory = tmp_path / "states"
    directory.mkdir()
    path = directory / "a.json"
    path.write_text("{}", encoding="utf-8")
    view = store.KeptView()
    assert stat_cycle(view, directory, by_directory_time=False)["a.json"][1].st_size == 2
    path.write_text("{ }  ", encoding="utf-8")
    assert stat_cycle(view, directory, by_directory_time=False)["a.json"][1].st_size == 5


def test_a_stat_is_not_kept_past_the_cycle_that_made_it(tmp_path, settled_clock):
    path = tmp_path / "a.json"
    path.write_text("{}", encoding="utf-8")
    view = store.KeptView()
    view.begin_stat_cycle()
    assert view.stat(path).st_size == 2
    path.write_text("{ }  ", encoding="utf-8")
    assert view.stat(path).st_size == 2
    view.end_stat_cycle()
    assert view.stat(path).st_size == 5


@contextlib.contextmanager
def a_stat_cycle():
    store.KEPT.begin_stat_cycle()
    try:
        yield
    finally:
        store.KEPT.end_stat_cycle()


def count_passes(monkeypatch) -> list[str]:
    """The kind of every full directory pass ``snapshot.listed`` makes from now on."""
    kinds, real = [], store.listed
    monkeypatch.setattr(store, "listed", lambda kind, *a, **k: (kinds.append(kind), real(kind, *a, **k))[1])
    return kinds


def count_globs(monkeypatch) -> list[str]:
    roots, real = [], store.KEPT.glob
    monkeypatch.setattr(store.KEPT, "glob", lambda root, pattern: (roots.append(str(root)), real(root, pattern))[1])
    return roots


def replace_whole_file(path: Path, text: str) -> None:
    """What ``store.save`` and ``record_drive`` do: write a temporary name, rename it over ``path``."""
    scratch = path.with_name(path.name + ".tmp9")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)
    move_directory_key(path.parent)


def collect_in_a_stat_cycle(root, now=NOW):
    with a_stat_cycle():
        return model.collect(root.state_root, now=now, include_all=True, cost_log=root.cost_log)


def linked_child(session: str, parent: str = ALPHA) -> str:
    return make_state(session, parent_link={"parent_session": parent, "stage_index": 1}).to_json()


def test_an_unchanged_bindings_directory_is_not_walked_again(root, settled_clock, monkeypatch):
    store.reset()
    passes = count_passes(monkeypatch)
    results = []
    for _ in range(3):
        with a_stat_cycle():
            results.append(store.BINDINGS.read())
    assert passes.count("binding") == 1
    assert results[0][0] and results[2] == results[0]


def test_a_replaced_binding_is_seen_by_the_next_stat_cycle(root, settled_clock):
    store.reset()
    with a_stat_cycle():
        before = store.BINDINGS.read()[0]
    assert before.get(ALPHA) == [HARNESS]
    replace_whole_file(bindings_dir(root) / f"{HARNESS}.json",
                       json.dumps({"driven": [{"session": "gamma-engine", "ts": "2026-10-22T11:00:00"}]}))
    with a_stat_cycle():
        after = store.BINDINGS.read()[0]
    assert after.get("gamma-engine") == [HARNESS] and ALPHA not in after


def test_one_binding_pass_per_stat_cycle(root, monkeypatch):
    monkeypatch.setattr(store, "MTIME_STEP_NS", 10 ** 30)
    store.reset()
    passes = count_passes(monkeypatch)
    with a_stat_cycle():
        store.BINDINGS.read()
        store.harness_index()
        store.harness_sessions_driving(ALPHA)
        store.BINDINGS.read()
    assert passes.count("binding") == 1
    with a_stat_cycle():
        store.harness_index()
    assert passes.count("binding") == 2


def test_a_presumed_lookup_outside_the_cover_reads_in_full(root, settled_clock):
    store.reset()
    store.BINDINGS.presume({BETA}, store.Recorded({}))
    with a_stat_cycle():
        index = store.harness_index()
        assert isinstance(index, store.PresumedIndex)
        assert index.get(ALPHA) == [HARNESS]
        assert store.BINDINGS.presumption is None
    with a_stat_cycle():
        assert type(store.BINDINGS.read()[0]) is dict


def test_an_unchanged_state_directory_is_not_listed_again(root, settled_clock, monkeypatch):
    store.reset()
    passes = count_passes(monkeypatch)
    results = [collect_in_a_stat_cycle(root) for _ in range(3)]
    assert passes.count("state") == 1
    assert results[0]["tasks"] and results[2]["tasks"] == results[0]["tasks"]


def test_a_replaced_state_file_is_seen_by_the_next_stat_cycle(root, settled_clock):
    store.reset()
    goals = lambda snapshot: {task["session_id"]: task["goal"] for task in snapshot["tasks"]}  # noqa: E731
    assert goals(collect_in_a_stat_cycle(root))[ALPHA] == "Build the thing [with brackets]"
    replace_whole_file(root.state_root / f"{ALPHA}.json", make_state(ALPHA, goal="Changed goal").to_json())
    assert goals(collect_in_a_stat_cycle(root))[ALPHA] == "Changed goal"


def test_the_window_moves_with_now_over_a_kept_listing(root, settled_clock, monkeypatch):
    store.reset()
    for session, age in ((ALPHA, 1), (BETA, 23), (STALE, 48)):
        set_mtime(root.state_root / f"{session}.json", NOW - dt.timedelta(hours=age))
    passes = count_passes(monkeypatch)
    paths = {session: str(root.state_root / f"{session}.json") for session in (ALPHA, BETA, STALE)}
    for now, loaded in ((NOW, {ALPHA, BETA}), (NOW + dt.timedelta(hours=2), {ALPHA})):
        with a_stat_cycle():
            model.collect(root.state_root, now=now, cost_log=root.cost_log)
        assert {s for s, path in paths.items() if path in store.OBSERVED.loaded} == loaded
    assert passes.count("state") == 1


def test_an_unchanged_state_directory_is_not_indexed_for_children_again(root, settled_clock, monkeypatch):
    replace_whole_file(root.state_root / "child-one.json", linked_child("child-one"))
    store.reset()
    globs = count_globs(monkeypatch)
    results = []
    for _ in range(3):
        with a_stat_cycle():
            results.append(model.child_index(root.state_root))
    assert [child["session_id"] for child in results[0][ALPHA]] == ["child-one"]
    assert results[1] == results[0] and results[2] == results[0]
    assert globs.count(str(root.state_root)) == 1


def test_a_new_linked_child_is_seen_by_the_next_stat_cycle(root, settled_clock):
    replace_whole_file(root.state_root / "child-one.json", linked_child("child-one"))
    store.reset()
    with a_stat_cycle():
        assert [c["session_id"] for c in model.child_index(root.state_root)[ALPHA]] == ["child-one"]
    replace_whole_file(root.state_root / "child-two.json", linked_child("child-two"))
    with a_stat_cycle():
        assert sorted(c["session_id"] for c in model.child_index(root.state_root)[ALPHA]) == ["child-one", "child-two"]


def test_a_changed_since_rebuilds_the_child_index(root, settled_clock, monkeypatch):
    path = root.state_root / "child-one.json"
    replace_whole_file(path, linked_child("child-one"))
    set_mtime(path, NOW - dt.timedelta(hours=5))
    store.reset()
    globs = count_globs(monkeypatch)
    with a_stat_cycle():
        assert model.child_index(root.state_root, since=(NOW - dt.timedelta(hours=5)).timestamp())
    model._CHILDREN.clear()
    with a_stat_cycle():
        assert model.child_index(root.state_root, since=NOW.timestamp()) == {}
    assert globs.count(str(root.state_root)) == 2


def transcript_index_in_a_stat_cycle(root, callers: int = 1):
    with a_stat_cycle():
        for _ in range(callers):
            found = attention.transcript_index(root.transcripts_root)
    return found


def test_one_transcript_index_pass_per_stat_cycle(root, monkeypatch):
    monkeypatch.setattr(store, "MTIME_STEP_NS", 10 ** 30)
    store.reset()
    with a_stat_cycle():
        attention.transcript_index(root.transcripts_root)
        store.TRANSCRIPTS.index(store.transcript_roots(root.transcripts_root))
        attention.transcript_index(root.transcripts_root)
    assert store.TRANSCRIPTS.passes == 1
    transcript_index_in_a_stat_cycle(root)
    assert store.TRANSCRIPTS.passes == 2


def test_an_unchanged_transcript_tree_is_not_indexed_again(root, settled_clock):
    store.reset()
    results = [transcript_index_in_a_stat_cycle(root, callers=2) for _ in range(3)]
    assert store.TRANSCRIPTS.passes == 1
    assert HARNESS in results[0] and results[2] == results[0]


def test_a_new_transcript_is_seen_by_the_next_stat_cycle(root, settled_clock):
    store.reset()
    assert "other-9" not in transcript_index_in_a_stat_cycle(root)
    directory = transcript_path(root).parent
    (directory / "other-9.jsonl").write_text("{}\n", encoding="utf-8")
    move_directory_key(directory)
    assert list(transcript_index_in_a_stat_cycle(root)["other-9"]) == [directory / "other-9.jsonl"]


def test_a_kept_transcript_index_is_not_changed_by_its_callers(root, settled_clock):
    store.reset()
    transcript_index_in_a_stat_cycle(root)
    kept = transcript_index_in_a_stat_cycle(root)
    before = {stem: list(paths) for stem, paths in kept.items()}
    for edit in (lambda: kept[HARNESS].append("x"), lambda: kept.__setitem__("x", ()), lambda: kept.pop(HARNESS)):
        try:
            edit()
        except (TypeError, AttributeError):
            pass
    after = transcript_index_in_a_stat_cycle(root)
    assert {stem: list(paths) for stem, paths in after.items()} == before


def test_a_transcript_index_under_an_unsettled_key_is_not_reused(root, monkeypatch):
    far = [time.time_ns() + FAR_FUTURE_NS]
    monkeypatch.setattr(store, "now_ns", lambda: far[0])
    store.reset()
    transcript_index_in_a_stat_cycle(root)
    directory = transcript_path(root).parent
    (directory / "other-9.jsonl").write_text("{}\n", encoding="utf-8")
    move_directory_key(directory)
    far[0] = stamp(directory) + store.MTIME_STEP_NS - 1
    passes = store.TRANSCRIPTS.passes
    transcript_index_in_a_stat_cycle(root)
    transcript_index_in_a_stat_cycle(root)
    assert store.TRANSCRIPTS.passes == passes + 2


def test_a_reused_stat_cycle_records_the_same_sources(root, settled_clock):
    store.reset()
    store.OBSERVED.reset()

    def one_cycle():
        with a_stat_cycle():
            model.collect(root.state_root, now=NOW, include_all=True, cost_log=root.cost_log)
            store.BINDINGS.read()
        return store.OBSERVED.sources()

    full = one_cycle()
    store.OBSERVED.reset()
    assert {item["kind"] for item in full} >= {"state", "binding"}
    assert one_cycle() == full


class Cover:
    """A cover test of the kept view: covers what lies under ``prefix`` except the names in ``outside``."""

    def __init__(self, prefix, outside=()):
        self.prefix, self.outside = str(prefix), tuple(outside)

    def __call__(self, path) -> bool:
        return str(path).startswith(self.prefix) and not str(path).endswith(self.outside)


@pytest.fixture
def listing(tmp_path, monkeypatch):
    """A trusting kept view over a directory of two settled files, on a clock far past them."""
    store.reset()
    monkeypatch.setattr(store, "now_ns", lambda: time.time_ns() + FAR_FUTURE_NS)
    root = tmp_path / "listed"
    root.mkdir()
    for name in ("a", "b"):
        (root / f"{name}.json").write_text("{}", encoding="utf-8")
    cover = Cover(tmp_path)
    store.KEPT.trust(cover)
    yield SimpleNamespace(root=root, cover=cover)
    store.reset()


def list_state(listing):
    return store.listed("state", listing.root, ".json")


def count_kept_reads(monkeypatch) -> SimpleNamespace:
    """Counters on ``KeptView.scandir`` calls and ``KeptEntry`` constructions."""
    counts = SimpleNamespace(scandir=0, entries=0)
    real_scandir, real_entry = store.KeptView.scandir, store.KeptEntry

    def scandir(view, *args, **kwargs):
        counts.scandir += 1
        return real_scandir(view, *args, **kwargs)

    class Counted(real_entry):
        __slots__ = ()

        def __init__(self, *args):
            counts.entries += 1
            super().__init__(*args)

    monkeypatch.setattr(store.KeptView, "scandir", scandir)
    monkeypatch.setattr(store, "KeptEntry", Counted)
    return counts


def test_kept_stat_decides_coverage_outside_the_lock(listing):
    held = []
    store.KEPT.trust(lambda path: held.append(store.KEPT._lock._is_owned()) or True)
    store.KEPT.stat(listing.root / "a.json")
    assert held == [False]


def test_kept_scandir_decides_coverage_outside_the_lock(listing):
    held = []
    store.KEPT.trust(lambda path: held.append(store.KEPT._lock._is_owned()) or True)
    store.KEPT.scandir(listing.root)
    assert held == [False]


def test_a_trust_swap_during_a_stat_answers_live(listing, monkeypatch):
    path = listing.root / "a.json"
    store.KEPT.stat(path)
    live, real_stat = [], os.stat
    monkeypatch.setattr(os, "stat", lambda target, *args, **kwargs: (live.append(str(target)), real_stat(target, *args, **kwargs))[1])
    other = Cover(listing.root.parent)

    def swapping(_path) -> bool:
        store.KEPT.trust(other)
        return True

    store.KEPT.trust(swapping)
    store.KEPT.stat(path)
    assert live == [str(path)]


def test_an_unchanged_directory_is_not_relisted_on_the_event_path(listing, monkeypatch):
    first = list_state(listing)
    counts = count_kept_reads(monkeypatch)
    second = list_state(listing)
    assert (counts.scandir, counts.entries) == (0, 0)
    assert second == first and set(second) == {"a", "b"}


def test_a_marked_child_is_seen_by_the_next_listing(listing):
    first = list_state(listing)
    (listing.root / "a.json").write_text('{"grown": true}', encoding="utf-8")
    store.KEPT.mark([listing.root / "a.json"])
    assert list_state(listing)["a"][1].st_size != first["a"][1].st_size


def test_a_relisted_directory_is_listed_again(listing, monkeypatch):
    list_state(listing)
    store.KEPT.mark(directories=[listing.root])
    list_state(listing)
    counts = count_kept_reads(monkeypatch)
    list_state(listing)
    assert counts.scandir == 1


def test_distrust_drops_listing_reuse(listing, monkeypatch):
    list_state(listing)
    store.KEPT.distrust()
    store.KEPT.trust(listing.cover)
    counts = count_kept_reads(monkeypatch)
    list_state(listing)
    assert counts.scandir == 1


def test_a_mark_during_the_pass_is_not_reused(listing, monkeypatch):
    passes, real_scandir = [], store.KeptView.scandir

    def marking(view, root, *args, **kwargs):
        entries = real_scandir(view, root, *args, **kwargs)
        if not passes:
            view.mark([Path(root) / "a.json"])
        passes.append(root)
        return entries

    monkeypatch.setattr(store.KeptView, "scandir", marking)
    list_state(listing)
    list_state(listing)
    assert len(passes) == 2


def test_listing_reuse_needs_every_child_covered(listing, monkeypatch):
    store.KEPT.trust(Cover(listing.root.parent, outside=("b.json",)))
    list_state(listing)
    counts = count_kept_reads(monkeypatch)
    list_state(listing)
    assert counts.scandir == 1


def test_a_reused_listing_is_a_fresh_copy(listing):
    first = list_state(listing)
    first.clear()
    second = list_state(listing)
    assert set(second) == {"a", "b"}
    second.clear()
    assert set(list_state(listing)) == {"a", "b"}


def test_a_reused_listing_replays_its_observation(listing):
    list_state(listing)
    recorded = store.OBSERVED.capture("state")
    assert recorded[0]
    store.OBSERVED.scanned("state", {})
    list_state(listing)
    assert store.OBSERVED.capture("state") == recorded


def test_a_directory_missing_at_the_first_listing_is_listed_once_it_exists(listing):
    later = listing.root.parent / "later"
    assert store.listed("state", later, ".json") == {}
    later.mkdir()
    (later / "c.json").write_text("{}", encoding="utf-8")
    assert set(store.listed("state", later, ".json")) == {"c"}


def test_unsettled_entries_are_not_reused(listing, monkeypatch):
    start = time.time_ns()
    clock, real_marker = [start], store.directory_marker
    monkeypatch.setattr(store, "now_ns", lambda: clock[0])

    def marker_after_the_clock_moves(path):
        clock[0] = start + FAR_FUTURE_NS
        return real_marker(path)

    monkeypatch.setattr(store, "directory_marker", marker_after_the_clock_moves)
    list_state(listing)
    assert store.OBSERVED.unsettled("state")
    counts = count_kept_reads(monkeypatch)
    list_state(listing)
    assert counts.scandir == 1


@pytest.fixture
def bound(root, tmp_path, monkeypatch):
    """A trusting kept view over the fixture root's bindings directory, on a clock far past its files."""
    store.reset()
    monkeypatch.setattr(store, "now_ns", lambda: time.time_ns() + FAR_FUTURE_NS)
    store.KEPT.trust(Cover(tmp_path))
    yield bindings_dir(root)
    store.reset()


def test_an_unchanged_bindings_directory_is_not_reread_on_the_event_path(bound, monkeypatch):
    first = store.BINDINGS.read()
    counts = count_kept_reads(monkeypatch)
    fetched, real_fetch = [], store.IdentityCache.fetch

    def counting(cache, *args, **kwargs):
        fetched.append(args[0])
        return real_fetch(cache, *args, **kwargs)

    monkeypatch.setattr(store.IdentityCache, "fetch", counting)
    second = store.BINDINGS.read()
    assert (counts.scandir, counts.entries, fetched) == (0, 0, [])
    assert second == first and first[0]


def test_a_bindings_directory_holding_unwatched_files_is_not_reread_on_the_event_path(bound, monkeypatch):
    (bound / "stray.lock").write_text("", encoding="utf-8")
    store.KEPT.trust(lambda path: str(path) == str(bound) or str(path).endswith(".json"))
    store.BINDINGS.read()
    passes, real_pass = [], store.BINDINGS._pass

    def counting(presumption):
        passes.append(presumption)
        return real_pass(presumption)

    monkeypatch.setattr(store.BINDINGS, "_pass", counting)
    store.BINDINGS.read()
    assert passes == []


def test_a_marked_binding_is_seen_by_the_next_read(bound):
    assert as_lists(store.BINDINGS.read()[0]) == {ALPHA: [HARNESS]}
    path = bound / f"{HARNESS}.json"
    path.write_text(json.dumps({"driven": [{"session": BETA, "ts": "2026-10-01T00:00:00+00:00"}]}), encoding="utf-8")
    store.KEPT.mark([path])
    assert as_lists(store.BINDINGS.read()[0]) == {BETA: [HARNESS]}


# --- the event path keeps one record per entry and rebuilds only the entries a mark names -------------------

def test_marked_since_names_the_entries_marked_after_the_token(listing):
    store.KEPT.mark([listing.root / "a.json"])
    token = store.KEPT.mark_token()
    store.KEPT.mark([listing.root / "b.json"])
    assert store.KEPT.marked_since(listing.root, token) == {"b.json"}
    assert store.KEPT.marked_since(listing.root, store.KEPT.mark_token()) == set()


def test_marked_since_is_none_after_a_trust_change(listing):
    token = store.KEPT.mark_token()
    store.KEPT.mark([listing.root / "a.json"])
    store.KEPT.trust(Cover(listing.root.parent))
    assert store.KEPT.marked_since(listing.root, token) is None


def count_fetches(monkeypatch) -> list[str]:
    """The paths ``IdentityCache.fetch`` is asked for from now on."""
    fetched, real_fetch = [], store.IdentityCache.fetch

    def counting(cache, *args, **kwargs):
        fetched.append(str(args[0]))
        return real_fetch(cache, *args, **kwargs)

    monkeypatch.setattr(store.IdentityCache, "fetch", counting)
    return fetched


def bulk_bindings(directory: Path, count: int = 3) -> list[Path]:
    paths = [directory / f"bulk-{number}.json" for number in range(count)]
    for path in paths:
        rewrite(path, binding_text(BETA), mtime_ns=PAST_NS)
    return paths


def both_reads(index_and_driven) -> tuple[dict, dict]:
    index, driven = index_and_driven
    return as_lists(index), as_lists(driven)


def test_a_renamed_binding_rereads_only_that_entry_on_the_event_path(bound, monkeypatch):
    path = bulk_bindings(bound)[1]
    store.BINDINGS.read()
    fetched = count_fetches(monkeypatch)
    aside = bound / "bulk-1.tmp"
    rewrite(aside, binding_text(ALPHA), mtime_ns=PAST_NS)
    os.replace(aside, path)
    store.KEPT.mark([aside, path], [bound])
    read = both_reads(store.BINDINGS.read())
    assert fetched == [str(path)]
    assert read == both_reads(store.BindingScan().read())
    assert read[1]["bulk-1"] == [ALPHA]


def test_a_vanished_binding_is_dropped_by_the_next_read(bound):
    gone = bulk_bindings(bound, 1)[0]
    assert "bulk-0" in store.BINDINGS.read()[1]
    gone.unlink()
    store.KEPT.mark([gone], [bound])
    index, driven = both_reads(store.BINDINGS.read())
    assert "bulk-0" not in driven and "bulk-0" not in index.get(BETA, [])


def test_an_unsettled_binding_is_reread_by_the_next_read(bound, monkeypatch):
    fresh = bound / "fresh.json"
    changed = time.time_ns() + FAR_FUTURE_NS
    rewrite(fresh, binding_text(BETA), mtime_ns=changed)
    clock = [changed + store.MTIME_STEP_NS - 1]
    monkeypatch.setattr(store, "now_ns", lambda: clock[0])
    store.BINDINGS.read()
    clock[0] = changed + store.MTIME_STEP_NS
    fetched = count_fetches(monkeypatch)
    read = both_reads(store.BINDINGS.read())
    assert fetched == [str(fresh)]
    assert read[0] == {ALPHA: [HARNESS], BETA: ["fresh"]}


def test_a_caller_mutating_a_binding_read_does_not_change_the_next_read(bound):
    index, driven = store.BINDINGS.read()
    before = both_reads((index, driven))
    for edit in (lambda: index[ALPHA].append("x"), lambda: index.__setitem__("x", ()), lambda: driven.pop(HARNESS),
                 lambda: driven[HARNESS].append("x")):
        try:
            edit()
        except (TypeError, AttributeError):
            pass
    assert both_reads(store.BINDINGS.read()) == before


def test_event_path_reuse_is_counted_in_the_cycle_census(bound):
    store.BINDINGS.read()
    token = store.REUSED.begin_event_cycle()
    store.BINDINGS.read()
    assert store.REUSED.names(token, ("binding",)) == ["binding"]


@pytest.fixture
def transcribed(root, tmp_path, monkeypatch):
    """A trusting kept view over the fixture's transcripts: two project directories on a clock far past them."""
    store.reset()
    monkeypatch.setattr(store, "now_ns", lambda: time.time_ns() + FAR_FUTURE_NS)
    other = root.transcripts_root / "other-project"
    other.mkdir()
    (other / "kept-1.jsonl").write_text("{}\n", encoding="utf-8")
    store.KEPT.trust(Cover(tmp_path))
    attention.transcript_index(root.transcripts_root)
    yield SimpleNamespace(root=root, changed=transcript_path(root).parent, unchanged=other)
    store.reset()


def test_an_unchanged_transcript_subdirectory_is_not_rescanned_on_the_event_path(transcribed, monkeypatch):
    fetched = count_fetches(monkeypatch)
    (transcribed.changed / "new-1.jsonl").write_text("{}\n", encoding="utf-8")
    move_directory_key(transcribed.changed)
    store.KEPT.mark([transcribed.changed / "new-1.jsonl"], [transcribed.changed])
    attention.transcript_index(transcribed.root.transcripts_root)
    assert fetched == [str(transcribed.changed)]


def test_a_new_transcript_is_seen_by_the_next_event_index(transcribed):
    path = transcribed.changed / "new-1.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    move_directory_key(transcribed.changed)
    store.KEPT.mark([path], [transcribed.changed])
    assert list(attention.transcript_index(transcribed.root.transcripts_root)["new-1"]) == [path]
