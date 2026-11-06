"""The snapshot on disk and the identity-keyed caches of the console dashboard (``plan_dashboard.snapshot``).

Plain pytest, standard library only (textual stays in ``test_plan_dashboard_tui_app.py``, which holds the
tests that need the running app: the old marking on a start from a snapshot, the events' order, SIGTERM).

Every kind of source the dashboard reads (``snapshot.SOURCE_KINDS``: state, binding, transcript, ledger) is
put through the same four changes between two refreshes — a different size, a same-size rewrite that moves
only the file's ctime, a creation, a deletion — once with warm caches and once with caches a snapshot seeded.
What a refresh then shows must be the new content, never the cached one. The contract the timing harness
reads (``fixtures/plan_dashboard_contract.json``) fixes the file's layout; ``read_snapshot`` below is a
reader written from it alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from plan_dashboard import attention, model, snapshot as store, tui
from plan_dashboard_fixtures import set_mtime, write_transcript
from plan_dashboard_support import (  # noqa: F401
    ALPHA, BETA, HARNESS, NOW, at, console_root, isolated_home, iso, usage_row,
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
    return store.harness_index()


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


def test_a_file_read_within_one_step_of_its_mtime_is_read_again_next_cycle(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("a", encoding="utf-8")
    blind_to_the_clock(monkeypatch, None)
    monkeypatch.setattr(store, "now_ns", lambda: os.stat(path).st_mtime_ns + store.MTIME_STEP_NS - 1)
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
    monkeypatch.setattr(store, "now_ns", lambda: os.stat(path).st_mtime_ns + store.MTIME_STEP_NS)
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
        monkeypatch.setattr(store, "now_ns", lambda: st.st_mtime_ns + offset)
        store.OBSERVED.note("state", path, st)
        assert (str(path) in {item["path"] for item in store.OBSERVED.sources()}) is recorded


def test_a_ledger_read_inside_the_step_is_finished_from_its_offset_never_read_from_the_start(root, monkeypatch):
    mtime = os.stat(root.cost_log).st_mtime_ns
    clock = [mtime + store.MTIME_STEP_NS - 1]
    monkeypatch.setattr(store, "now_ns", lambda: clock[0])
    bases = []
    real = model._extend_sums
    monkeypatch.setattr(model, "_extend_sums", lambda path, base: (bases.append(base and base["offset"]), real(path, base))[1])
    assert spawn_cost_of(root) == 1.5
    with open(root.cost_log, "a", encoding="utf-8") as handle:
        handle.write(ledger_row(1.0))
    os.utime(root.cost_log, ns=(mtime, mtime))
    assert spawn_cost_of(root) == 2.5
    clock[0] = mtime + store.MTIME_STEP_NS
    assert spawn_cost_of(root) == 2.5
    assert spawn_cost_of(root) == 2.5
    assert bases[0] is None and bases[1] > 0
    first_offset = bases[1]
    assert len(bases) == 3 and bases[2] > first_offset


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
    assert reads == ["bulk-3.json"] and driven["bulk-3"] == [BETA]
    assert index[BETA] == ["bulk-3"] and "bulk-3" not in index[ALPHA]


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


def test_a_write_removes_the_temporaries_a_killed_writer_left(root, tmp_path):
    path = tmp_path / "cache" / store.FILE_NAME
    path.parent.mkdir()
    for pid in (1, 2):
        (path.parent / f"{store.FILE_NAME}.tmp-{pid}").write_text("half", encoding="utf-8")
    save(root, path)
    assert os.listdir(path.parent) == [store.FILE_NAME]


@pytest.fixture
def code_files(tmp_path, monkeypatch):
    files = [tmp_path / "one.py", tmp_path / "two.py"]
    for number, file in enumerate(files):
        file.write_text(f"VALUE = {number}\n", encoding="utf-8")
    monkeypatch.setattr(store, "CODE_FILES", tuple(files))
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


def test_every_module_that_makes_the_stored_values_is_part_of_the_code_version():
    named = {file.name for file in store.CODE_FILES}
    assert {"model.py", "attention.py", "snapshot.py", "tui.py", "tui_app.py", "locate.py",
            Path(store.engine_session_binding.__file__).name} <= named


def test_a_snapshot_written_by_other_code_is_absent_after_the_code_changed(root, tmp_path, code_files):
    path = tmp_path / store.FILE_NAME
    store.write(path, {SELECTION: {"marks": EMPTY_MARKS}}, [])
    assert store.load(path) is not None
    code_files[0].write_text("VALUE = 'fixed'\n", encoding="utf-8")
    store.code_version.cache_clear()
    assert store.load(path) is None


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

    store.OBSERVED.seed([entry("/a", 10), entry("/b", 10), entry("/c", 10)])
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
