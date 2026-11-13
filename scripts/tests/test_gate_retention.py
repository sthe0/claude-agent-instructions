"""The suite gate's leftovers: sandboxes removed by their owner or the next run, a bounded kept store.

Difficulty removed: every gate run left its sandbox clone in /tmp (a killed run's for good) and kept
records, baselines and logs without limit. These tests pin the owner lock and the sweep, the evidence
of a failing run, and the least-recently-used trim; ``gate_speedup_control.py disk-series`` shows the
same on real gate runs in a private /tmp.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import gate_speedup_control as control
from lib import gate_retention as retention
from lib import suite_gate as gate
from test_suite_gate import FakeRunner, build_world

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPTS_DIR.parent
PREFIX = "instruction-sandbox."
MB = retention.MB
TINY = 0.01
NEW_FAILURE = "scripts/tests/test_y.py::test_one"

HOLDER = """\
import sys, time
from lib import gate_retention

with gate_retention.sandbox(sys.argv[1], sys.argv[2]) as root:
    print(root, flush=True)
    time.sleep(600)
"""


@pytest.fixture
def common(tmp_path):
    path = tmp_path / "common"
    path.mkdir()
    return path


@pytest.fixture
def parent(tmp_path):
    path = tmp_path / "tmp"
    path.mkdir()
    return path


@pytest.fixture
def holder(tmp_path, parent):
    """Start a process that holds a sandbox; yields (process, sandbox root). Kills the process afterwards."""
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), str(parent), PREFIX],
                            stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(SCRIPTS_DIR)})
    root = Path(proc.stdout.readline().strip())
    yield proc, root
    proc.kill()
    proc.wait()


# ── sandboxes: owner lock, sweep ──────────────────────────────────────────

def test_a_sandbox_is_removed_with_its_owner_file_when_the_block_ends(parent):
    with retention.sandbox(parent, PREFIX) as root:
        assert root.is_dir() and retention.owner_path(root).is_file()
        assert root.name.startswith(PREFIX)
    assert list(parent.iterdir()) == []


def test_a_sandbox_is_kept_when_the_keep_variable_is_set(parent, monkeypatch):
    monkeypatch.setenv(retention.KEEP_ENV, "1")
    with retention.sandbox(parent, PREFIX) as root:
        pass
    assert root.is_dir() and retention.owner_path(root).is_file()


def test_the_sandbox_is_removed_when_the_block_raises(parent):
    with pytest.raises(RuntimeError):
        with retention.sandbox(parent, PREFIX):
            raise RuntimeError("boom")
    assert list(parent.iterdir()) == []


def test_a_killed_runs_sandbox_is_removed_by_the_next_sweep(holder, parent):
    proc, root = holder
    (root / "clone").mkdir()
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert root.is_dir()
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == [root.name]
    assert list(parent.iterdir()) == []


def test_a_live_sandbox_is_kept_by_the_sweep(holder, parent):
    _, root = holder
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == []
    assert root.is_dir() and retention.owner_path(root).is_file()


def test_a_sandbox_being_created_is_kept_by_a_racing_sweep(parent, monkeypatch):
    """A sweep from any repository's run, at the moment the creator takes the owner lock, finds no owner file."""
    swept: list[list[str]] = []
    real_flock = fcntl.flock

    def flock_after_a_sweep(target, operation):
        if isinstance(target, int) and operation == fcntl.LOCK_EX:
            swept.append(retention.sweep_dead_sandboxes(parent, PREFIX))
        return real_flock(target, operation)

    monkeypatch.setattr(retention.fcntl, "flock", flock_after_a_sweep)
    with retention.sandbox(parent, PREFIX) as root:
        assert swept == [[]]
        assert root.is_dir() and retention.owner_path(root).is_file()
        assert retention.sweep_dead_sandboxes(parent, PREFIX) == []


def test_a_failure_while_creating_the_owner_leaves_nothing_and_leaks_no_descriptor(parent, monkeypatch):
    def refuse(target, operation):
        raise OSError("no locks here")

    before = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(retention.fcntl, "flock", refuse)
    with pytest.raises(OSError):
        with retention.sandbox(parent, PREFIX):
            pass
    assert list(parent.iterdir()) == []
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_sandbox_without_an_owner_file_is_never_removed(parent):
    stray = parent / f"{PREFIX}old-gate"
    stray.mkdir()
    other = parent / "someone-elses"
    other.mkdir()
    (parent / f"{PREFIX}x.owner").write_text("", encoding="utf-8")
    (parent / "other.owner").write_text("", encoding="utf-8")
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == [f"{PREFIX}x"]
    assert stray.is_dir() and other.is_dir() and (parent / "other.owner").is_file()


def test_an_owner_symlink_is_not_followed(parent, tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (parent / f"{PREFIX}link").symlink_to(target)
    (parent / f"{PREFIX}link.owner").symlink_to(tmp_path / "nowhere")
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == []
    assert target.is_dir()


def test_a_read_only_directory_does_not_make_a_sandbox_a_leftover(parent):
    root = parent / f"{PREFIX}ro"
    (root / "objects" / "ab").mkdir(parents=True)
    (root / "objects" / "ab" / "obj").write_text("x", encoding="utf-8")
    (root / "objects" / "ab").chmod(0o500)
    retention.owner_path(root).write_text("", encoding="utf-8")
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == [root.name]
    assert list(parent.iterdir()) == []


def test_a_sandbox_that_cannot_be_removed_keeps_its_owner_file_unlocked_for_the_next_sweep(parent, monkeypatch, capsys):
    monkeypatch.setattr(retention, "_remove_tree", lambda root: None)
    with retention.sandbox(parent, PREFIX) as root:
        pass
    owner = retention.owner_path(root)
    assert root.is_dir() and owner.is_file()
    assert "cannot remove the sandbox" in capsys.readouterr().err
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == []
    monkeypatch.undo()
    assert retention.sweep_dead_sandboxes(parent, PREFIX) == [root.name]
    assert list(parent.iterdir()) == []


# ── the kept store: least recently used, bounded ──────────────────────────

def put(path: Path, mb: float = 1, age: float = 0) -> Path:
    """A file of ``mb`` MB whose mtime is ``age`` seconds ago; a larger age is less recently used."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(int(mb * MB)))
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def sha(n: int) -> str:
    return f"{n:040x}"


def test_the_least_recently_used_baseline_goes_first(common):
    for n, age in ((1, 300), (2, 200), (3, 100)):
        put(common / retention.BASELINE_DIR / f"{sha(n)}.json", age=age)
    result = retention.enforce(common, max_mb=3, runs=3)
    assert result.evicted == (f"baseline:{sha(1)}",)
    assert not (common / retention.BASELINE_DIR / f"{sha(1)}.json").exists()
    assert (common / retention.BASELINE_DIR / f"{sha(2)}.json").exists()
    assert result.after_mb <= 3 < result.before_mb


def test_a_baseline_is_evicted_whole(common):
    base = common / retention.BASELINE_DIR
    for suffix in (".json", ".junit.xml", ".tests.jsonl", ".log"):
        put(base / f"{sha(1)}{suffix}", mb=0.25, age=500)
    (base / f"{sha(1)}.lock").write_text("", encoding="utf-8")
    put(base / f"{sha(2)}.json", mb=2, age=10)
    retention.enforce(common, max_mb=3, runs=3)
    assert sorted(p.name for p in base.iterdir()) == [f"{sha(1)}.lock", f"{sha(2)}.json"]


def test_an_evicted_groups_lock_file_stays_so_runs_keep_locking_one_inode(common):
    base = common / retention.BASELINE_DIR
    lock = base / f"{sha(1)}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    inode = lock.stat().st_ino
    put(base / f"{sha(1)}.json", age=500)
    put(base / f"{sha(2)}.json", age=10)
    assert retention.enforce(common, max_mb=1.5, runs=3).evicted == (f"baseline:{sha(1)}",)
    assert lock.stat().st_ino == inode


def test_a_group_that_a_run_rewrote_after_it_was_listed_is_not_evicted(tmp_path):
    lock = tmp_path / f"{sha(1)}.lock"
    data = put(tmp_path / f"{sha(1)}.json", age=500)
    listed_at = time.time() - 500
    os.utime(data)
    assert retention._evict_group(lock, [data], listed_at) is False
    assert data.is_file()
    assert retention._evict_group(lock, [data], time.time() + 5) is True
    assert not data.exists() and lock.is_file()


def test_a_baseline_cache_hit_counts_as_use(common):
    base = common / retention.BASELINE_DIR
    older = put(base / f"{sha(1)}.json", age=500)
    put(base / f"{sha(2)}.json", age=100)
    put(base / f"{sha(3)}.json", age=300)
    retention.touch(older)
    result = retention.enforce(common, max_mb=3, runs=3)
    assert result.evicted == (f"baseline:{sha(3)}",)


def test_touching_a_missing_file_is_not_an_error(common):
    retention.touch(common / "absent.json")


RUN_SUFFIXES = (".json", ".log", ".junit.xml", ".tests.jsonl")


def run_files(common: Path, n: int, age: float, mb: float = 1) -> list[Path]:
    """The files one candidate run keeps at the top of the store."""
    return [put(common / retention.STORE_DIR / f"{sha(n)}{suffix}", mb=mb / len(RUN_SUFFIXES), age=age)
            for suffix in RUN_SUFFIXES]


def test_old_candidate_run_files_are_evicted_least_recently_used(common):
    files = {n: run_files(common, n, age) for n, age in ((1, 300), (2, 200), (3, 100))}
    result = retention.enforce(common, max_mb=2.5, runs=3)
    assert result.evicted == (f"run:{sha(1)}",)
    assert not any(f.exists() for f in files[1]) and all(f.is_file() for f in files[2] + files[3])
    assert result.after_mb <= 2.5 and not result.over_bound


def test_the_candidate_and_tip_run_files_are_never_evicted(common):
    candidate = run_files(common, 9, age=10_000)
    tip = run_files(common, 8, age=9_000)
    other = run_files(common, 1, age=100)
    put(common / retention.BASELINE_DIR / f"{sha(2)}.json", age=100)
    result = retention.enforce(common, max_mb=TINY, runs=3, protect=(sha(9), sha(8)))
    assert all(f.is_file() for f in candidate + tip)
    assert not any(f.exists() for f in other)
    assert set(result.evicted) == {f"run:{sha(1)}", f"baseline:{sha(2)}"}


def test_a_candidate_run_in_progress_keeps_its_files(common):
    lock = common / retention.STORE_DIR / f"{sha(1)}.lock"
    kept = run_files(common, 1, age=900)
    lock.write_text("", encoding="utf-8")
    with open(lock) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = retention.enforce(common, max_mb=TINY, runs=3)
    assert all(f.is_file() for f in kept) and result.evicted == () and result.over_bound


def test_a_store_still_over_the_bound_says_so(common, capsys):
    run_files(common, 9, age=10, mb=2)
    result = retention.enforce(common, max_mb=1, runs=3, protect=(sha(9),))
    assert result.over_bound and result.evicted == ()
    assert "over the bound of 1 MB" in capsys.readouterr().err


def test_a_store_within_the_bound_stays_quiet(common, capsys):
    run_files(common, 9, age=10)
    assert not retention.enforce(common, max_mb=10, runs=3).over_bound
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bound", [0, -1])
def test_a_non_positive_bound_is_refused_not_taken_as_evict_everything(common, bound):
    kept = run_files(common, 1, age=10)
    with pytest.raises(ValueError):
        retention.enforce(common, max_mb=bound, runs=3)
    assert all(f.is_file() for f in kept)


def test_the_tip_and_candidate_baselines_are_never_evicted(common):
    base = common / retention.BASELINE_DIR
    tip = put(base / f"{sha(1)}.json", age=900)
    tip_junit = put(base / f"{sha(1)}.junit.xml", age=900)
    candidate = put(base / f"{sha(2)}.json", age=800)
    other = put(base / f"{sha(3)}.json", age=10)
    result = retention.enforce(common, max_mb=TINY, runs=3, protect=(sha(1), sha(2)))
    assert tip.is_file() and tip_junit.is_file() and candidate.is_file()
    assert not other.exists()
    assert result.evicted == (f"baseline:{sha(3)}",)


def test_a_baseline_being_built_is_kept(common):
    base = common / retention.BASELINE_DIR
    lock = put(base / f"{sha(1)}.lock", mb=0.01, age=900)
    kept = put(base / f"{sha(1)}.json", age=900)
    other = put(base / f"{sha(2)}.json", age=10)
    with open(lock) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        retention.enforce(common, max_mb=TINY, runs=3)
    assert kept.is_file() and not other.exists()


def evidence(common: Path, n: int, age: float) -> Path:
    path = common / retention.EVIDENCE_DIR / sha(n)
    put(path / "log.txt", age=age)
    os.utime(path, (time.time() - age,) * 2)
    return path


def test_the_last_failing_runs_evidence_survives_while_it_fits(common):
    entries = [evidence(common, n, age=1000 - 100 * n) for n in range(1, 6)]
    result = retention.enforce(common, max_mb=4, runs=3)
    assert result.evicted == (f"evidence:{sha(1)}", f"evidence:{sha(2)}")
    assert [e.exists() for e in entries] == [False, False, True, True, True]


def test_the_newest_evidence_is_evicted_oldest_first_when_it_alone_exceeds_the_bound(common):
    entries = [evidence(common, n, age=1000 - 100 * n) for n in range(1, 6)]
    result = retention.enforce(common, max_mb=2, runs=3)
    assert result.evicted == tuple(f"evidence:{sha(n)}" for n in (1, 2, 3, 4))
    assert [e.exists() for e in entries] == [False, False, False, False, True]


def test_evidence_goes_after_everything_else_is_gone(common):
    base = put(common / retention.BASELINE_DIR / f"{sha(1)}.json", age=10)
    kept = evidence(common, 2, age=5000)
    result = retention.enforce(common, max_mb=2, runs=3)
    assert result.evicted == (f"baseline:{sha(1)}",) and not base.exists() and kept.is_dir()


def partial_copy(common: Path, n: int, pid: int) -> Path:
    path = common / retention.EVIDENCE_DIR / f"{retention.EVIDENCE_TMP_PREFIX}{sha(n)}-{pid}"
    put(path / "log.txt")
    return path


def test_a_partial_evidence_copy_of_a_live_run_is_left_alone(common):
    partial = partial_copy(common, 1, os.getpid())
    retention.enforce(common, max_mb=TINY, runs=3)
    assert partial.is_dir()


def test_a_partial_evidence_copy_of_a_dead_run_is_removed(common):
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    partial = partial_copy(common, 1, done.pid)
    live = partial_copy(common, 2, os.getpid())
    result = retention.enforce(common, max_mb=100, runs=3)
    assert not partial.exists() and live.is_dir()
    assert result.before_mb < 1.5 and result.evicted == ()


def test_a_registered_store_is_trimmed_least_recently_used_first(common, tmp_path):
    store = tmp_path / "other-store"
    for name, age in (("a", 300), ("b", 200), ("c", 100)):
        put(store / name / "data", age=age)
        os.utime(store / name, (time.time() - age,) * 2)
    retention.register_store("probe", store, shutil.rmtree)
    try:
        result = retention.enforce(common, max_mb=3, runs=3)
    finally:
        retention.unregister_store("probe")
    assert result.evicted == ("probe:a",)
    assert sorted(p.name for p in store.iterdir()) == ["b", "c"]


def test_a_registered_store_given_as_a_function_of_the_common_dir(common):
    retention.register_store("lazy", lambda c: c / "lazy-store", lambda child: child.unlink())
    try:
        put(common / "lazy-store" / "x", age=1)
        result = retention.enforce(common, max_mb=TINY, runs=3)
    finally:
        retention.unregister_store("lazy")
    assert result.evicted == ("lazy:x",)


@pytest.mark.parametrize("own", [retention.STORE_DIR, retention.BASELINE_DIR, retention.EVIDENCE_DIR])
def test_a_store_registered_on_the_gates_own_directories_is_not_listed_twice(common, own):
    kept = put(common / retention.BASELINE_DIR / f"{sha(1)}.json", age=100)
    retention.register_store("dup", lambda c: c / own, shutil.rmtree)
    try:
        result = retention.enforce(common, max_mb=TINY, runs=3)
    finally:
        retention.unregister_store("dup")
    assert result.evicted == (f"baseline:{sha(1)}",) and not kept.exists()


def test_a_store_within_the_bound_loses_nothing(common):
    put(common / retention.BASELINE_DIR / f"{sha(1)}.json")
    assert retention.enforce(common, max_mb=10, runs=3).evicted == ()


def test_an_empty_store_is_fine(common):
    result = retention.enforce(common, max_mb=1, runs=3)
    assert result.evicted == () and result.before_mb < 0.1


def test_failure_evidence_holds_the_log_the_failures_the_junit_and_the_core_tree(common, tmp_path):
    core = tmp_path / "core"
    (core / "scripts").mkdir(parents=True)
    (core / "scripts" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (core / "tmp").mkdir()
    (core / "tmp" / "scratch").write_text("big", encoding="utf-8")
    (core / "scripts" / "tmp").mkdir()
    junit = tmp_path / "junit.xml"
    junit.write_text("<testsuite/>", encoding="utf-8")
    target = retention.keep_failure_evidence(common, sha(1), log_tail="tail\n", junit_path=str(junit),
                                             failures=["b::t", "a::t"], core_tree=core)
    assert target == common / retention.EVIDENCE_DIR / sha(1)
    assert (target / "log.txt").read_text(encoding="utf-8") == "tail\n"
    assert (target / "failures.txt").read_text(encoding="utf-8") == "a::t\nb::t\n"
    assert (target / "junit.xml").read_text(encoding="utf-8") == "<testsuite/>"
    assert (target / "core" / "scripts" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (target / "core" / "tmp").exists() and (target / "core" / "scripts" / "tmp").is_dir()
    assert [p.name for p in target.parent.iterdir()] == [sha(1)]


def test_failure_evidence_that_cannot_be_written_costs_the_evidence_not_the_run(common, tmp_path, capsys):
    (common / "suite-gate").write_text("a file where the directory should be", encoding="utf-8")
    result = retention.keep_failure_evidence(common, sha(1), log_tail="x", junit_path="", failures=[],
                                             core_tree=tmp_path / "core")
    assert result is None and "cannot keep the evidence" in capsys.readouterr().err


# ── through the gate ──────────────────────────────────────────────────────

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "hermetic_unavailable", lambda: "off in these tests")
    return build_world(tmp_path, monkeypatch)


def evidence_dir(world, sha_):
    return world.common / retention.EVIDENCE_DIR / sha_


def test_a_clean_pass_leaves_the_sandbox_parent_empty_and_no_evidence(world):
    candidate = world.code_commit()
    outcome = world.run(FakeRunner({candidate: ()}))
    assert outcome.record["status"] == gate.PASS
    assert list(world.sandbox.iterdir()) == []
    assert not evidence_dir(world, candidate).exists()


def test_a_failing_run_keeps_its_evidence_and_leaves_the_sandbox_parent_empty(world):
    candidate = world.code_commit()
    outcome = world.run(FakeRunner({candidate: (NEW_FAILURE,), world.base: ()}))
    assert outcome.record["status"] == gate.FAIL
    assert list(world.sandbox.iterdir()) == []
    kept = evidence_dir(world, candidate)
    assert (kept / "log.txt").is_file()
    assert (kept / "failures.txt").read_text(encoding="utf-8") == NEW_FAILURE + "\n"


def test_the_gate_removes_a_dead_runs_sandbox_before_it_starts(world):
    dead = world.sandbox / f"{gate.SANDBOX_PREFIX}dead"
    dead.mkdir()
    retention.owner_path(dead).write_text("", encoding="utf-8")
    candidate = world.code_commit()
    world.run(FakeRunner({candidate: ()}))
    assert list(world.sandbox.iterdir()) == []


def test_the_gate_leaves_an_ownerless_sandbox_alone(world):
    stray = world.sandbox / f"{gate.SANDBOX_PREFIX}old-gate"
    stray.mkdir()
    candidate = world.code_commit()
    world.run(FakeRunner({candidate: ()}))
    assert stray.is_dir()


def test_a_run_trims_the_store_but_not_its_own_record_or_the_tips_baseline(world, monkeypatch):
    monkeypatch.setattr(retention, "keep_max_mb", lambda config_path=None: 1)
    old = put(world.common / retention.BASELINE_DIR / f"{sha(7)}.json", mb=3, age=100_000)
    candidate = world.code_commit()
    world.run(FakeRunner({candidate: (NEW_FAILURE,), world.base: ()}))
    assert not old.exists()
    assert world.stored(candidate) is not None
    assert (world.common / retention.BASELINE_DIR / f"{world.base}.json").is_file()


# ── the disk-series control ───────────────────────────────────────────────

def test_disk_series_builds_every_gate_command_through_cap_run():
    command = control.series_command(Path("/repo"), "c" * 40, "t" * 40)
    assert command[:2] == [str(SCRIPTS_DIR / "cap-run.sh"), "--"]
    assert command[3] == str(SCRIPTS_DIR / "suite-gate.py") and command[4] == "run"
    assert command[command.index("--ref") + 1] == "c" * 40
    assert command[command.index("--remote-sha") + 1] == "t" * 40


def test_disk_series_runs_only_known_kinds(tmp_path, capsys):
    assert control.main(["disk-series", "--runs", "pass,oops", "--out", str(tmp_path / "o.json")]) == 2
    assert "oops" in capsys.readouterr().out


# ── configuration ─────────────────────────────────────────────────────────

def config_rows():
    from agentctl.config import parse_config_md

    return parse_config_md(REPO_DIR / "config.md")


def test_the_defaults_equal_the_config_rows():
    rows = config_rows()
    assert int(rows[retention.KEEP_MAX_KEY]) == retention.DEFAULT_KEEP_MAX_MB == 2048
    assert int(rows[retention.EVIDENCE_RUNS_KEY]) == retention.DEFAULT_EVIDENCE_RUNS == 3
    assert retention.keep_max_mb() == 2048 and retention.evidence_runs() == 3


def test_each_config_row_has_a_rationale_row():
    text = (REPO_DIR / "docs" / "operations" / "config-rationale.md").read_text(encoding="utf-8")
    assert f"| `{retention.KEEP_MAX_KEY}` | `2048` |" in text
    assert f"| `{retention.EVIDENCE_RUNS_KEY}` | `3` |" in text


@pytest.mark.parametrize("value", ["abc", "0", "-5", " "])
def test_a_bad_row_falls_back_to_the_default(tmp_path, value):
    config = tmp_path / "config.md"
    config.write_text(f"| `{retention.KEEP_MAX_KEY}` | `{value}` | meaning |\n", encoding="utf-8")
    assert retention.keep_max_mb(config) == retention.DEFAULT_KEEP_MAX_MB
