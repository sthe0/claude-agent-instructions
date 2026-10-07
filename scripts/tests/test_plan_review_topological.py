"""Coverage for scripts/plan-review-topological.py, the driver that walks a plan's
reliance pairs, spawns one thinker per pair, records each verdict and composes the pass.

The driver is hyphenated, so it is loaded by path inside a fixture, never at module scope:
on a tree without it the tests fail at run time (pytest exit 1), not at collection.

Two engines stand behind the driver's `run_agentctl` seam: `RealEngine` runs the real
`agentctl` CLI in-process against a `FileStateStore` (record/walk/compose semantics and the
condition-4 ledger are the engine's), and `FakeEngine` scripts statuses for the
orchestration cases (early stop, waiting, exit codes) that need a state the real engine
cannot be steered into cheaply. The spawner is always a stub (`Rig.spawn`).
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import sys
import time
from pathlib import Path

import pytest
from test_plan_review_topo import SID, Env, _data, _sha, _write, make_env  # noqa: F401

from agentctl import cli, plan
from agentctl.render import render_pair_review_bundle, topo_pair_view_dirname
from lib import planner_plan_check

SCRIPTS = Path(__file__).resolve().parent.parent
SATISFIED = ("current", "override")
PAIR_LINE = re.compile(
    r"^TOPO-PAIR: pair=(?P<pair>\S+) verdict=(?P<verdict>\S+) digest=(?P<digest>[0-9a-f]{64}) "
    r"cost_usd=(?P<cost>\S+) duration_ms=(?P<duration>\S+) pulls=(?P<pulls>\S+) "
    r"transcript=(?P<transcript>\S+)$"
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def drv():
    return _load("plan_review_topological", "plan-review-topological.py")


def flag(argv, name):
    return argv[argv.index(name) + 1]


def flags(argv, name):
    return [argv[i + 1] for i, a in enumerate(argv) if a == name]


def write_transcript(path: Path, tool_uses) -> Path:
    path.write_text(
        "\n".join(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})
            for tid, name, tool_input in tool_uses
        ) + "\n",
        encoding="utf-8",
    )
    return path


def review_original(verdict: str, digest: str, concerns=()) -> str:
    return "\n".join(["REVIEW:", f"Verdict: {verdict}", f"Plan digest: {digest}", *concerns])


def canonical_stdout(original: str) -> str:
    return planner_plan_check.canonicalize("REVIEW", "one-line digest", None, original)


def summary_line(duration=1000, cost=0.5, marker="REVIEW") -> str:
    parts = ["spawn-specialist: kind=thinker budget=3.00 depth=1", f"duration_ms={duration}"]
    if cost is not None:
        parts.append(f"cost_usd={cost}")
    if marker:
        parts.append(f"marker={marker}")
    return " ".join(parts)


class FakeEngine:
    def __init__(self, levels, prereqs=None, status=None):
        self.levels = levels
        self.prereqs = prereqs or {}
        self.status = {p: "missing" for lvl in levels for p in lvl}
        self.status.update(status or {})
        self.calls: list = []
        self.events: list = []

    def verbs(self, verb):
        return [argv for argv, _ in self.calls if argv[0] == verb]

    def run(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        verb = argv[0]
        if verb == "plan-review-walk":
            self.events.append(("walk",))
            levels = []
            for depth, pairs in enumerate(self.levels):
                rows = []
                for pair in pairs:
                    waiting = [p for p in self.prereqs.get(pair, []) if self.status.get(p) not in SATISFIED]
                    rows.append({"pair": pair, "base": "b", "service": "s", "level": depth,
                                 "status": self.status[pair], "ready": not waiting,
                                 "waiting": waiting, "spawn": None, "record": None})
                levels.append(rows)
            return {"ok": True, "data": {"plan_path": flag(argv, "--target"), "discharges": [],
                                         "levels": levels}}
        if verb == "plan-review":
            pair = flag(argv, "--scope").split(":", 1)[1]
            self.events.append(("record", pair))
            self.status[pair] = "current" if flag(argv, "--verdict") == "pass" else "revise"
            return {"ok": True, "data": {"pair": pair}}
        if verb == "plan-review-pair-history":
            pair = flag(argv, "--pair")
            return {"ok": True, "data": {"pair": pair, "records": [], "changed_parts_since_last": []}}
        if verb == "plan-review-compose":
            failing = {p: s for p, s in self.status.items() if s not in SATISFIED}
            if failing:
                return {"ok": False, "detail": "blocked", "data": {"failing": failing}}
            return {"ok": True, "data": {}}
        raise AssertionError(f"unexpected agentctl verb {verb}")


class RealEngine:
    def __init__(self, state_root: Path, monkeypatch):
        self.state_root = state_root
        self.monkeypatch = monkeypatch
        self.calls: list = []
        self.events: list = []

    def verbs(self, verb):
        return [argv for argv, _ in self.calls if argv[0] == verb]

    def run(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        if argv[0] == "plan-review-walk":
            self.events.append(("walk",))
        if argv[0] == "plan-review":
            self.events.append(("record", flag(argv, "--scope").split(":", 1)[1]))
        ledger = env.get("AGENTCTL_ESCALATION_LEDGER")
        if ledger:
            self.monkeypatch.setenv("AGENTCTL_ESCALATION_LEDGER", ledger)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["--state-root", str(self.state_root), *argv])
        return json.loads(buf.getvalue())


class Rig:
    def __init__(self, drv, tmp_path, monkeypatch, capsys, engine, plan_path):
        self.drv = drv
        self.capsys = capsys
        self.engine = engine
        self.plan = plan_path
        self.cost_log = tmp_path / "costs.jsonl"
        self.events = engine.events
        self.spawns: list = []
        self.specs: dict = {}
        monkeypatch.setattr(drv, "spawn_pair", self.spawn)
        monkeypatch.setattr(drv, "run_agentctl", engine.run)

    @property
    def sha(self) -> str:
        return hashlib.sha256(Path(self.plan).read_bytes()).hexdigest()

    def append_cost_row(self, **row) -> None:
        with self.cost_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "spawn", **row}) + "\n")

    def spawn(self, argv):
        self.spawns.append(list(argv))
        pair = flag(argv, "--review-topo")
        if "--dry-run" in argv:
            return 0, (f"TOPO-VIEW: /v/{pair} files=node-1.md\n=== assembled prompt ===\nx\n"
                       "# stdin: <prompt 4321 chars>\n"), ""
        self.events.append(("spawn-start", pair))
        spec = self.specs.get(pair, {})
        if spec.get("delay"):
            time.sleep(spec["delay"])
        if spec.get("on_spawn"):
            spec["on_spawn"](pair)
        sha = hashlib.sha256(Path(flag(argv, "--plan")).read_bytes()).hexdigest()
        verdict = spec.get("verdict", "pass")
        concerns = spec.get("concerns", ["blocking: C1: default concern"] if verdict == "revise" else [])
        stdout = spec.get("stdout")
        if stdout is None:
            stdout = canonical_stdout(review_original(verdict, spec.get("digest", sha), concerns))
        stderr = spec.get("stderr")
        if stderr is None:
            stderr = summary_line(spec.get("duration", 1000), spec.get("cost", 0.5)) + "\n"
            if spec.get("transcript"):
                stderr += f"spawn-specialist: transcript={spec['transcript']}\n"
        self.events.append(("spawn-end", pair))
        return spec.get("rc", 0), stdout, stderr

    def run(self, *extra, plan=None):
        rc = self.drv.main(["--session", SID, "--plan", str(plan or self.plan), *extra])
        return rc, self.capsys.readouterr().out.splitlines()

    def spawned(self) -> list:
        return [flag(a, "--review-topo") for a in self.spawns if "--dry-run" not in a]


@pytest.fixture
def make_rig(drv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(drv, "COST_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo"))

    def make(engine, plan_path=None):
        if plan_path is None:
            plan_path = tmp_path / "plan.toml"
            plan_path.write_text("plan bytes\n", encoding="utf-8")
        return Rig(drv, tmp_path, monkeypatch, capsys, engine, plan_path)

    return make


@pytest.fixture
def real(make_env, make_rig, tmp_path, monkeypatch):
    env = make_env()
    return env, make_rig(RealEngine(tmp_path / "state", monkeypatch), env.plan)


def single(make_rig, pair="3-1"):
    return make_rig(FakeEngine([[pair]]))


def test_td1_dry_run_lists_commands_and_prompt_sizes_and_spawns_nothing(make_rig):
    engine = FakeEngine([["a"], ["b"]], prereqs={"b": ["a"]})
    rig = make_rig(engine)
    rc, out = rig.run("--dry-run")
    assert rc == 0
    dry = [ln for ln in out if ln.startswith("TOPO-DRY: ")]
    assert [ln.split()[1] for ln in dry] == ["pair=a", "pair=b"]
    for line in dry:
        assert re.fullmatch(r"TOPO-DRY: pair=\S+ prompt_chars=4321 view_files=[^,\s]+", line), line
    assert any(ln.startswith("level 0:") for ln in out) and any(ln.startswith("level 1:") for ln in out)
    assert all("--dry-run" in argv for argv in rig.spawns)
    assert rig.spawned() == []
    assert engine.verbs("plan-review") == [] and engine.verbs("plan-review-compose") == []


def test_td2_levels_run_in_walk_order_and_the_walk_is_reread_per_level(real):
    env, rig = real
    first = rig.engine.run(["plan-review-walk", "--session", SID, "--target", str(env.plan),
                            "--format", "json"], {})
    levels = [[row["pair"] for row in level] for level in first["data"]["levels"]]
    assert len(levels) >= 2
    rig.events.clear()
    rc, out = rig.run()
    assert rc == 0 and "COMPOSE: pass" in out
    spawned = rig.spawned()
    start = 0
    for level in levels:
        assert sorted(spawned[start:start + len(level)]) == sorted(level)
        start += len(level)
    assert start == len(spawned)
    for depth in range(1, len(levels)):
        last_record = max(i for i, e in enumerate(rig.events)
                          if e[0] == "record" and e[1] in levels[depth - 1])
        first_spawn = min(i for i, e in enumerate(rig.events)
                          if e[0] == "spawn-start" and e[1] in levels[depth])
        assert any(e == ("walk",) for e in rig.events[last_record:first_spawn]), depth


def test_td2_parallel_one_keeps_exact_walk_order(real):
    env, rig = real
    first = rig.engine.run(["plan-review-walk", "--session", SID, "--target", str(env.plan),
                            "--format", "json"], {})
    levels = [[row["pair"] for row in level] for level in first["data"]["levels"]]
    rig.events.clear()
    rc, out = rig.run("--parallel", "1")
    assert rc == 0 and "COMPOSE: pass" in out
    assert rig.spawned() == [p for level in levels for p in level]


def test_early_stop_flags_exclusive(drv):
    with pytest.raises(SystemExit) as exc:
        drv.main(["--session", SID, "--plan", "x", "--early-stop", "--no-early-stop"])
    assert exc.value.code == 2


def test_td2_parallel_never_starts_a_level_before_the_previous_one_returned(make_rig):
    engine = FakeEngine([["a", "b"], ["c", "d"]])
    rig = make_rig(engine)
    rig.specs = {p: {"delay": 0.05} for p in "abcd"}
    rc, out = rig.run("--parallel", "2")
    assert rc == 0
    ev = rig.events
    assert ev.index(("spawn-start", "b")) < ev.index(("spawn-end", "a"))
    for later in "cd":
        for earlier in "ab":
            assert ev.index(("spawn-start", later)) > ev.index(("spawn-end", earlier))
    assert [e[1] for e in ev if e[0] == "record"] == ["a", "b", "c", "d"]


def max_concurrency(events) -> int:
    running = peak = 0
    for kind, *_ in events:
        if kind == "spawn-start":
            running += 1
            peak = max(peak, running)
        elif kind == "spawn-end":
            running -= 1
    return peak


def run_level_with_cap(make_rig, monkeypatch, drv, cap, pairs, *extra):
    constants = {} if cap is None else {"review-parallel-max": str(cap)}
    monkeypatch.setattr(drv, "parse_config_md", lambda: constants, raising=False)
    rig = make_rig(FakeEngine([list(pairs)]))
    rig.specs = {p: {"delay": 0.05} for p in pairs}
    rc, _ = rig.run(*extra)
    assert rc == 0
    return rig


def test_default_parallel_level_width(make_rig, drv, monkeypatch):
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 6, "abc")
    assert max_concurrency(rig.events) == 3


def test_default_parallel_cap_binds(make_rig, drv, monkeypatch):
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 2, "abcd")
    assert max_concurrency(rig.events) == 2


def test_default_parallel_explicit_override(make_rig, drv, monkeypatch):
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 3, "abc")
    assert max_concurrency(rig.events) == 3
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 3, "abc", "--parallel", "2")
    assert max_concurrency(rig.events) == 2
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 3, "abc", "--parallel", "1")
    assert max_concurrency(rig.events) == 1


def test_default_parallel_missing_key(make_rig, drv, monkeypatch):
    rig = run_level_with_cap(make_rig, monkeypatch, drv, 3, "ab")
    assert max_concurrency(rig.events) == 2
    rig = run_level_with_cap(make_rig, monkeypatch, drv, None, "ab")
    assert max_concurrency(rig.events) == 1


def test_default_parallel_same_level(make_rig, drv, monkeypatch):
    def recorded(extra):
        monkeypatch.setattr(drv, "parse_config_md", lambda: {"review-parallel-max": "4"}, raising=False)
        rig = make_rig(FakeEngine([["a", "b", "c"]]))
        rig.specs = {"a": {"delay": 0.05}, "b": {"delay": 0.05, "verdict": "revise"}, "c": {"delay": 0.05}}
        rig.run(*extra)
        return rig, [(argv[argv.index("--scope") + 1], argv[argv.index("--verdict") + 1])
                     for argv in rig.engine.verbs("plan-review")], dict(rig.engine.status)

    default_rig, default_records, default_status = recorded([])
    assert max_concurrency(default_rig.events) == 3
    sequential_rig, sequential_records, sequential_status = recorded(["--parallel", "1"])
    assert max_concurrency(sequential_rig.events) == 1
    assert default_records == sequential_records
    assert default_status == sequential_status == {"a": "current", "b": "revise", "c": "current"}


def test_default_continue_past_revise(make_rig):
    rig = make_rig(FakeEngine([["a"], ["b"]]))
    rig.specs = {"a": {"verdict": "revise"}}
    rc, out = rig.run()
    assert rig.spawned() == ["a", "b"] and not any(ln.startswith("TOPO-WAITING") for ln in out)
    assert rc == 1

    rig = make_rig(FakeEngine([["a"], ["b"]], prereqs={"b": ["a"]}))
    rig.specs = {"a": {"verdict": "revise"}}
    rc, out = rig.run()
    assert rig.spawned() == ["a", "b"] and not any(ln.startswith("TOPO-WAITING") for ln in out)


def test_td3_early_stop_waiting_and_never_a_second_spawn(make_rig):
    engine = FakeEngine([["a", "c"], ["b"]])
    rig = make_rig(engine)
    rig.specs = {"a": {"verdict": "revise"}}
    rc, out = rig.run("--early-stop")
    assert rc == 1 and sorted(rig.spawned()) == ["a", "c"]
    assert "COMPOSE: blocked b,a" in out or "COMPOSE: blocked a,b" in out

    engine = FakeEngine([["a"], ["b"]], prereqs={"b": ["x"]}, status={"x": "revise"})
    rig = make_rig(engine)
    rc, out = rig.run("--early-stop")
    assert "TOPO-WAITING: pair=b waiting=x" in out and rig.spawned() == ["a"]
    assert rc == 1

    engine = FakeEngine([["a"], ["b"]], prereqs={"b": ["x"]}, status={"x": "revise"})
    rig = make_rig(engine)
    rc, out = rig.run("--no-early-stop")
    assert rig.spawned() == ["a", "b"] and not any(ln.startswith("TOPO-WAITING") for ln in out)

    engine = FakeEngine([["a"], ["a", "c"]])
    rig = make_rig(engine)
    rig.specs = {"a": {"verdict": "revise"}}
    rc, out = rig.run("--no-early-stop")
    assert rig.spawned() == ["a", "c"]


def test_td4_a_child_without_a_marker_or_with_a_wrong_digest_is_never_recorded(make_rig):
    for spec in ({"stdout": "just some prose, no review block\n"}, {"digest": "f" * 64},
                 {"stderr": "spawn-specialist: kind=thinker budget=3.00 depth=1 duration_ms=5\n"}):
        rig = single(make_rig)
        rig.specs = {"3-1": spec}
        rc, out = rig.run()
        assert rc == 2
        assert any(ln.startswith("TOPO-REFUSED: pair=3-1 ") for ln in out), (spec, out)
        assert rig.engine.verbs("plan-review") == []


def test_td5_a_ceiling_refusal_is_a_refused_pair_naming_the_exits(make_rig):
    rig = single(make_rig)
    rig.specs = {"3-1": {"rc": 2, "stdout": "", "stderr": (
        "error: assembled prompt is 99999 chars, exceeding the ceiling for --review-topo 3-1; "
        "a user-authored override is the only other exit\n")}}
    rc, out = rig.run()
    refused = [ln for ln in out if ln.startswith("TOPO-REFUSED: pair=3-1 ")]
    assert rc == 2 and len(refused) == 1
    assert "assembled prompt is 99999 chars" in refused[0]
    assert "split" in refused[0] and "override" in refused[0]
    assert rig.engine.verbs("plan-review") == []


def test_td6_pairs_scope_spawns_only_the_named_pairs_and_skips_compose(real):
    env, rig = real
    rc, out = rig.run("--pairs", "3-1")
    assert rig.spawned() == ["3-1"] and rc == 0
    assert any(ln.startswith("COMPOSE: skipped scoped run; not current: ") for ln in out)
    assert rig.engine.verbs("plan-review-compose") == []
    assert env.status("3-1") == "current"

    rc, out = rig.run("--pairs", "3-1")
    assert rig.spawned() == ["3-1"] and rc == 3
    assert "TOPO-CURRENT: pair=3-1" in out
    assert rig.engine.verbs("plan-review-compose") == []

    before = len(rig.spawns)
    for bad in ("3", "order", "9-9"):
        with pytest.raises(SystemExit) as exc:
            rig.run("--pairs", bad)
        assert exc.value.code == 2
    assert len(rig.spawns) == before


def test_td6_a_named_pair_is_spawned_even_when_it_is_not_ready(make_rig):
    engine = FakeEngine([["a"], ["b"]], prereqs={"b": ["a"]})
    rig = make_rig(engine)
    rc, out = rig.run("--pairs", "b")
    assert rig.spawned() == ["b"] and rc == 0
    assert "COMPOSE: skipped scoped run; not current: a" in out


def test_td7_ledger_flag_reaches_every_agentctl_call_and_receives_the_condition4_line(real, tmp_path):
    env, rig = real
    other = tmp_path / "nested" / "gap-ledger.jsonl"
    rig.specs = {"3-1": {"verdict": "revise", "concerns": ["1. blocking: **C4:** the **stage 2** result omits `x`"]}}
    rc, out = rig.run("--ledger", str(other), "--no-early-stop")
    assert other.exists()
    assert rig.engine.calls and all(
        e.get("AGENTCTL_ESCALATION_LEDGER") == str(other.resolve()) for _, e in rig.engine.calls)
    lines = [json.loads(ln) for ln in other.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and "the stage 2 result omits x" in json.dumps(lines[0])
    assert env.ledger_lines() == []


def test_td8_every_pair_line_carries_all_seven_fields_and_unknowns_print_na(make_rig, tmp_path, drv):
    rig = make_rig(FakeEngine([["a", "b", "c"]]))
    view = {p: Path(rig.drv.view_dir_for(p, rig.sha)) for p in "abc"}
    ta = write_transcript(tmp_path / "ta.jsonl", [("t1", "Read", {"file_path": str(view["a"] / "node-1.md")})])
    tb = write_transcript(tmp_path / "tb.jsonl", [("t1", "Read", {"file_path": str(view["b"] / "node-1.md")})])
    rig.specs = {
        "a": {"transcript": str(ta), "cost": None,
              "on_spawn": lambda pair: rig.append_cost_row(
                  transcript_path=str(ta), review_pair="a", plan_sha256=rig.sha, duration_ms=777)},
        "b": {"transcript": str(tb)},
        "c": {"transcript": "<not-found-within-10s>"},
    }
    rc, out = rig.run()
    rows = {m["pair"]: m for m in map(PAIR_LINE.match, out) if m}
    assert set(rows) == {"a", "b", "c"}
    assert (rows["a"]["cost"], rows["a"]["duration"], rows["a"]["pulls"]) == ("NA", "777", "1")
    assert rows["a"]["transcript"] == str(ta)
    assert (rows["b"]["cost"], rows["b"]["pulls"], rows["b"]["transcript"]) == ("0.5000", "1", str(tb))
    assert (rows["c"]["pulls"], rows["c"]["transcript"]) == ("NA", "NA")
    summary = next(ln for ln in out if ln.startswith("TOPO-SUMMARY: "))
    assert "cost_unknown=1" in summary and "pulls_unknown=1" in summary


def test_td9_pull_counting_is_separator_safe_and_deduplicated(drv, tmp_path):
    sha = "c" * 64
    view = str(tmp_path / "topo" / sha / "view-3-1")
    path = write_transcript(tmp_path / "t.jsonl", [
        ("t1", "Read", {"file_path": f"{view}/node-1.md"}),
        ("t1", "Read", {"file_path": f"{view}/node-1.md"}),
        ("t2", "Grep", {"path": view, "pattern": "x"}),
        ("t3", "Glob", {"pattern": f"{view}/*.md"}),
        ("t4", "Bash", {"command": f"cat {view}/node-1.md"}),
        ("t5", "Read", {"file_path": "/elsewhere/node-1.md"}),
        ("t7", "Read", {"file_path": str(tmp_path / "topo" / sha / "view-3-10" / "node-1.md")}),
        ("t8", "Bash", {"command": f"cat {view}-extra/node-1.md"}),
        ("t9", "Grep", {"path": view, "pattern": f"{view}/node-1.md"}),
    ])
    assert drv.count_pulls(str(path), view) == 5
    none = write_transcript(tmp_path / "n.jsonl", [("t1", "Read", {"file_path": "/elsewhere"})])
    assert drv.count_pulls(str(none), view) == 0
    assert drv.count_pulls(str(tmp_path / "missing.jsonl"), view) is None
    assert drv.count_pulls(None, view) is None


def test_td10_summary_totals_equal_the_sums_over_the_pair_lines(make_rig, tmp_path):
    rig = make_rig(FakeEngine([["a", "b", "c"]]))
    view = {p: Path(rig.drv.view_dir_for(p, rig.sha)) for p in "ab"}
    ta = write_transcript(tmp_path / "ta.jsonl", [
        ("1", "Read", {"file_path": str(view["a"] / "n.md")}), ("2", "Bash", {"command": f"ls {view['a']}"})])
    tb = write_transcript(tmp_path / "tb.jsonl", [("1", "Read", {"file_path": "/x"})])
    rig.specs = {"a": {"cost": 1.25, "duration": 1000, "transcript": str(ta)},
                 "b": {"cost": 2.5, "duration": 2000, "transcript": str(tb)},
                 "c": {"cost": None, "duration": 3000}}
    rc, out = rig.run()
    rows = [m for m in map(PAIR_LINE.match, out) if m]
    summary = dict(kv.split("=") for kv in next(ln for ln in out if ln.startswith("TOPO-SUMMARY: ")).split()[1:])
    assert summary["pairs"] == str(len(rows)) == "3"
    assert float(summary["cost_usd"]) == pytest.approx(sum(float(r["cost"]) for r in rows if r["cost"] != "NA"))
    assert int(summary["duration_ms"]) == sum(int(r["duration"]) for r in rows)
    assert int(summary["pairs_pulled"]) == sum(1 for r in rows if r["pulls"] not in ("NA", "0")) == 1
    assert int(summary["cost_unknown"]) == sum(1 for r in rows if r["cost"] == "NA") == 1
    assert int(summary["pulls_unknown"]) == sum(1 for r in rows if r["pulls"] == "NA") == 1


@pytest.mark.parametrize("extra,expected", [
    ([], ["--complexity", "high"]),
    (["--complexity", "medium"], ["--complexity", "medium"]),
    (["--model", "m-x"], ["--model", "m-x"]),
])
def test_td11_model_selector(make_rig, extra, expected):
    for dry in (False, True):
        rig = single(make_rig)
        rc, _ = rig.run(*extra, *(["--dry-run"] if dry else []))
        argv = rig.spawns[0]
        assert expected[0] in argv and flag(argv, expected[0]) == expected[1]
        assert flag(argv, "--effort") == "high"
        assert ("--model" in argv) == (expected[0] == "--model")
        assert ("--complexity" in argv) == (expected[0] == "--complexity")
        assert flag(argv, "--done-criterion").strip() and "3-1" in flag(argv, "--done-criterion")
        assert flag(argv, "--criterion-type") == "acceptance-review"
        assert flag(argv, "--review-topo") == "3-1" and "--plan-brief" in argv
        assert ("--dry-run" in argv) == dry


def test_td11_both_selectors_are_an_argument_error(make_rig):
    rig = single(make_rig)
    with pytest.raises(SystemExit) as exc:
        rig.run("--complexity", "low", "--model", "m-x")
    assert exc.value.code == 2 and rig.spawns == []


@pytest.mark.parametrize("extra", [[], ["--complexity", "low"], ["--model", "m-x"]])
def test_td12_the_spawn_argv_parses_under_the_real_spawner_parser(make_rig, extra):
    spawner = _load("spawn_specialist_for_td12", "spawn-specialist.py")
    for dry in (False, True):
        rig = single(make_rig)
        rig.run(*extra, *(["--dry-run"] if dry else []))
        args = spawner.build_parser().parse_args(rig.spawns[0])
        assert args.review_topo == "3-1" and args.kind == "thinker"


def run_one(make_rig, stdout, stderr=None, pair="3-1"):
    rig = single(make_rig, pair)
    rig.specs = {pair: {"stdout": stdout, **({"stderr": stderr} if stderr is not None else {})}}
    rc, out = rig.run()
    recorded = rig.engine.verbs("plan-review")
    return rig, rc, out, recorded[0] if recorded else None


def block(sha, verdict="revise", concerns=("blocking: C4: x",)):
    return review_original(verdict, sha, concerns)


SHA_OF_PLAN = hashlib.sha256(b"plan bytes\n").hexdigest()


@pytest.mark.parametrize("name,original,expected", [
    ("fenced", f"```\n{block(SHA_OF_PLAN, concerns=('blocking: C2: a',))}\n```", ["blocking: C2: a"]),
    ("bold", f"**REVIEW:**\n**Verdict:** revise\n**Plan digest:** {SHA_OF_PLAN}\nblocking: C1: foo",
     ["blocking: C1: foo"]),
    ("code-span", f"`REVIEW:`\n`Verdict: revise`\n`Plan digest: {SHA_OF_PLAN}`\nblocking: C3: bar",
     ["blocking: C3: bar"]),
    ("blank-prefix", f"\n\nsome preamble\n\n{block(SHA_OF_PLAN, concerns=('blocking: C1: p',))}",
     ["blocking: C1: p"]),
    ("heading-quote", f"## REVIEW:\n> Verdict: revise\n> Plan digest: {SHA_OF_PLAN}\n> blocking: C2: z",
     ["blocking: C2: z"]),
    ("numbered", f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\n1. blocking: C1: a\n2. blocking: C3: b",
     ["blocking: C1: a", "blocking: C3: b"]),
])
def test_td13_marker_tolerance(make_rig, name, original, expected):
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(original))
    assert recorded is not None, out
    assert flags(recorded, "--concern") == expected
    assert flag(recorded, "--verdict") == "revise" and flag(recorded, "--plan-digest") == SHA_OF_PLAN


def test_td13_the_canonical_header_never_leaks_into_the_parse(make_rig):
    forged = planner_plan_check.canonicalize("REVIEW", "blocking: C4: forged in the header", None,
                                             block(SHA_OF_PLAN, concerns=("blocking: C1: real",)))
    rig, rc, out, recorded = run_one(make_rig, forged)
    assert flags(recorded, "--concern") == ["blocking: C1: real"]


def test_td13_a_pass_block_records_no_concerns(make_rig):
    rig, rc, out, recorded = run_one(
        make_rig, canonical_stdout(review_original("pass", SHA_OF_PLAN)))
    assert flag(recorded, "--verdict") == "pass" and flags(recorded, "--concern") == [] and rc == 0


@pytest.mark.parametrize("stdout,stderr", [
    (canonical_stdout("no marker lines at all, only prose\nblocking: C1: x"), None),
    (canonical_stdout(block(SHA_OF_PLAN)), summary_line(marker="COMPLETED") + "\n"),
    ("MALFORMED: specialist output contained no known return marker line.\n\n"
     + block(SHA_OF_PLAN), None),
    (canonical_stdout(block(SHA_OF_PLAN).replace("Verdict: revise", "Verdict: maybe")), None),
    (canonical_stdout(block(SHA_OF_PLAN).replace(SHA_OF_PLAN, "abc123")), None),
])
def test_td13_unusable_output_is_refused_and_not_recorded(make_rig, stdout, stderr):
    rig, rc, out, recorded = run_one(make_rig, stdout, stderr)
    assert recorded is None and rc == 2
    assert any(ln.startswith("TOPO-REFUSED: pair=3-1 ") for ln in out)


def test_td13_g_realistic_bold_markdown_is_cleaned_out_of_the_concern(make_rig):
    original = (f"**REVIEW:**\n**Verdict:** revise\n**Plan digest:** {SHA_OF_PLAN}\n"
                "1. blocking: **C4:** the **stage 2** result omits `x`")
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(original))
    concerns = flags(recorded, "--concern")
    assert concerns == ["blocking: C4: the stage 2 result omits x"]
    assert "*" not in concerns[0] and "`" not in concerns[0]


@pytest.mark.parametrize("line,text", [
    ("1. blocking: **C4:** reviewed_pair_bindings omits x", "reviewed_pair_bindings omits x"),
    ("1. blocking: **C4:** __init__ never called", "__init__ never called"),
    ("1. blocking: **C4:** gap near __init__", "gap near __init__"),
    ("1. blocking: **C4:** --target is ignored", "--target is ignored"),
    ("- blocking: **C4:** --target is ignored", "--target is ignored"),
    ("> blocking: C4: gap near __init__", "gap near __init__"),
])
def test_td13_g2_underscores_hyphens_hashes_and_angles_survive_in_concern_values(make_rig, line, text):
    original = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\n{line}"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(original))
    assert flags(recorded, "--concern") == [f"blocking: C4: {text}"]


def test_td13_g2_hash_and_angle_inside_a_value_are_kept(make_rig):
    original = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\nblocking: C4: see #12 and <pair> placeholder"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(original))
    assert flags(recorded, "--concern") == ["blocking: C4: see #12 and <pair> placeholder"]


def test_td13_h_only_the_last_block_counts(make_rig):
    original = ("**REVIEW: revise**\nblocking: C4: prose gap that is not real\nmore prose\n"
                + block(SHA_OF_PLAN, concerns=("blocking: C1: real concern",)))
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(original))
    assert flags(recorded, "--concern") == ["blocking: C1: real concern"]
    assert "prose gap" not in json.dumps(recorded)

    closing = block(SHA_OF_PLAN, concerns=("blocking: C1: real concern",)) + "\nREVIEW: revise"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(closing))
    assert flags(recorded, "--concern") == ["blocking: C1: real concern"]

    disagreeing = block(SHA_OF_PLAN, concerns=("blocking: C1: real concern",)) + "\nREVIEW: pass"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(disagreeing))
    assert recorded is None and rc == 2 and "marker disagrees with verdict" in "\n".join(out)


def test_td13_i_wrapped_concerns_fold_and_unprefixed_ones_are_refused(make_rig):
    wrapped = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\nblocking: C4: first half\n    second half\nblocking: C1: other"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(wrapped))
    assert flags(recorded, "--concern") == ["blocking: C4: first half second half", "blocking: C1: other"]

    prose = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\nthe plan is bad\nreally"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(prose))
    assert recorded is None and "no condition-prefixed concerns" in "\n".join(out)

    second = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\nblocking: C4: a\n- b has no prefix"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(second))
    assert recorded is None and "unprefixed concern" in "\n".join(out)

    leading = f"REVIEW:\nVerdict: revise\nPlan digest: {SHA_OF_PLAN}\nsome words first\nblocking: C4: a"
    rig, rc, out, recorded = run_one(make_rig, canonical_stdout(leading))
    assert recorded is None and "unprefixed concern" in "\n".join(out)


def test_td14_the_cost_log_is_the_spawners(drv):
    spawner = _load("spawn_specialist_for_td14", "spawn-specialist.py")
    assert Path(spawner.COST_LOG) == drv.COST_LOG


def cost_run(make_rig, spec_builder, preexisting=()):
    rig = single(make_rig)
    for row in preexisting:
        rig.append_cost_row(**row)
    rig.specs = {"3-1": spec_builder(rig)}
    rc, out = rig.run()
    return rc, out


def row_for(rig, **extra):
    return {"review_pair": "3-1", "plan_sha256": rig.sha, **extra}


def test_td14_exact_transcript_match_beats_a_same_pair_same_sha_neighbour(make_rig, tmp_path):
    mine, other = str(tmp_path / "mine.jsonl"), str(tmp_path / "other.jsonl")

    def spec(rig):
        def on_spawn(pair):
            rig.append_cost_row(**row_for(rig, transcript_path=other, cost_usd=9.0, duration_ms=1))
            rig.append_cost_row(**row_for(rig, transcript_path=mine, cost_usd=1.5, duration_ms=222))
            rig.append_cost_row(**row_for(rig, transcript_path=mine, cost_usd=7.0, duration_ms=3, event="refused"))
        return {"transcript": mine, "on_spawn": on_spawn}

    rc, out = cost_run(make_rig, spec)
    line = next(m for m in map(PAIR_LINE.match, out) if m)
    assert (line["cost"], line["duration"]) == ("1.5000", "222")


def test_td14_fallback_reads_only_rows_appended_after_the_spawn_started(make_rig):
    def spec(rig):
        return {"transcript": "<not-found-within-10s>",
                "on_spawn": lambda pair: rig.append_cost_row(**row_for(rig, cost_usd=2.5, duration_ms=444))}

    stale = [{"review_pair": "3-1", "plan_sha256": SHA_OF_PLAN, "cost_usd": 9.0, "duration_ms": 1}]
    rc, out = cost_run(make_rig, spec, preexisting=stale)
    line = next(m for m in map(PAIR_LINE.match, out) if m)
    assert (line["cost"], line["duration"]) == ("2.5000", "444")


def test_td14_two_matching_rows_in_the_window_are_ambiguous(make_rig):
    def spec(rig):
        def on_spawn(pair):
            rig.append_cost_row(**row_for(rig, cost_usd=1.0, duration_ms=1))
            rig.append_cost_row(**row_for(rig, cost_usd=2.0, duration_ms=2))
        return {"transcript": "<not-found-within-10s>", "on_spawn": on_spawn}

    rc, out = cost_run(make_rig, spec)
    assert rc == 2
    assert any(ln.startswith("TOPO-REFUSED: pair=3-1 ") and "ambiguous" in ln for ln in out)


def test_td15_exit_codes(make_rig):
    rig = make_rig(FakeEngine([["a"], ["b"]]))
    rc, out = rig.run()
    assert rc == 0 and "COMPOSE: pass" in out

    rig = make_rig(FakeEngine([["a"], ["b"]]))
    rig.specs = {"a": {"verdict": "revise"}}
    rc, out = rig.run("--early-stop")
    assert rc == 1 and rig.spawned() == ["a"]
    assert any(ln.startswith("COMPOSE: blocked") for ln in out)

    rig = make_rig(FakeEngine([["a"], ["b"]]))
    rig.specs = {"a": {"stdout": "prose only\n"}}
    rc, out = rig.run("--early-stop")
    assert rc == 2 and rig.spawned() == ["a"]
    assert not any(ln.startswith("COMPOSE") for ln in out)
    assert rig.engine.verbs("plan-review-compose") == []

    rig = make_rig(FakeEngine([["a"], ["b"]], status={"a": "current", "b": "override"}))
    rc, out = rig.run()
    assert rc == 3 and rig.spawned() == [] and "COMPOSE: pass" in out

    rig = make_rig(FakeEngine([["a"]]))
    rc, out = rig.run("--dry-run")
    assert rc == 0


def test_td15_a_refusal_and_a_revise_in_one_run_exit_2(make_rig):
    for no_early_stop in (False, True):
        rig = make_rig(FakeEngine([["a", "b"], ["c"]]))
        rig.specs = {"a": {"stdout": "prose only\n"}, "b": {"verdict": "revise"}}
        rc, out = rig.run(*([] if no_early_stop else ["--early-stop"]))
        assert rc == 2
        compose_calls = len(rig.engine.verbs("plan-review-compose"))
        if no_early_stop:
            assert compose_calls == 1 and sorted(rig.spawned()) == ["a", "b", "c"]
            assert any(ln.startswith("COMPOSE: blocked") for ln in out)
        else:
            assert compose_calls == 0 and sorted(rig.spawned()) == ["a", "b"]


def test_td16_the_parse_reads_protocol_tokens_from_the_plan_module_at_call_time(drv, monkeypatch):
    assert drv.parse_review_output(
        review_original("revise", SHA_OF_PLAN, ["blocking: C4: a gap"])).concerns == ["blocking: C4: a gap"]
    monkeypatch.setattr(plan, "CONDITION_MARKERS", ("C1:", "C2:", "C3:", "K9:"))
    parsed = drv.parse_review_output(review_original("revise", SHA_OF_PLAN, ["blocking: K9: the gap"]))
    assert parsed.concerns == ["blocking: K9: the gap"]
    with pytest.raises(drv.TopoRefused):
        drv.parse_review_output(review_original("revise", SHA_OF_PLAN, ["blocking: C4: the old gap marker"]))


def test_td16_the_stage1_bundle_carries_every_token_the_driver_parses(drv, make_env, tmp_path):
    env = make_env()
    bundle = render_pair_review_bundle(env.doc(), "3-1", plan_sha256=_sha(env.plan), view_dir=tmp_path / "v")
    for token in (plan.REVIEW_MARKER, plan.VERDICT_MARKER, plan.PLAN_DIGEST_MARKER, *plan.CONDITION_MARKERS):
        assert token in bundle, token
    assert "Concerns:" not in bundle and not hasattr(plan, "CONCERNS_MARKER")
    assert "Concerns:" not in (SCRIPTS / "plan-review-topological.py").read_text(encoding="utf-8")
    reply = "\n".join([plan.REVIEW_MARKER, f"{plan.VERDICT_MARKER} revise",
                       f"{plan.PLAN_DIGEST_MARKER} {_sha(env.plan)}",
                       f"{plan.SEVERITY_BLOCKING}: {plan.CONDITION_MARKERS[1]} a concern"])
    parsed = drv.parse_review_output(reply)
    assert parsed.concerns == [f"{plan.SEVERITY_BLOCKING}: {plan.CONDITION_MARKERS[1]} a concern"] and parsed.digest == _sha(env.plan)


def test_td17_every_agentctl_call_targets_the_plan_argument_not_the_session_plan(
        make_env, make_rig, tmp_path, monkeypatch):
    env = make_env()
    other = _write(tmp_path / "other.toml", {**_data(), "meta": {**_data()["meta"], "goal": "another goal"}})
    monkeypatch.chdir(tmp_path)
    rig = make_rig(RealEngine(tmp_path / "state", monkeypatch), other)
    rc, out = rig.run(plan="other.toml")
    assert rc == 0 and "COMPOSE: pass" in out
    verbs = {argv[0] for argv, _ in rig.engine.calls}
    assert verbs == {"plan-review-walk", "plan-review", "plan-review-compose"}
    for argv, _ in rig.engine.calls:
        assert flag(argv, "--session") == SID and flag(argv, "--target") == str(other)
        assert str(env.plan) not in argv
        if argv[0] == "plan-review":
            assert flag(argv, "--reviewer") == "thinker" and flag(argv, "--scope").startswith("topo:")
            assert "--regression-command" not in argv
    assert env.status("3-1", other) == "current" and env.status("3-1") == "missing"
    assert all(m["transcript"] == "NA" for m in map(PAIR_LINE.match, out) if m)

    spawned = len(rig.spawned())
    rc, out = rig.run(plan="other.toml")
    assert rc == 3 and len(rig.spawned()) == spawned and "COMPOSE: pass" in out
    rc, out = rig.run("--pairs", "3-1", plan="other.toml")
    assert rc == 3 and len(rig.spawned()) == spawned
    assert len(rig.engine.verbs("plan-review-compose")) == 3 and "COMPOSE: pass" in out


def test_td17_a_refused_pair_stops_the_run_after_its_level_unless_told_otherwise(make_rig):
    for no_early_stop, expected in ((False, ["a"]), (True, ["a", "b"])):
        rig = make_rig(FakeEngine([["a"], ["b"]]))
        rig.specs = {"a": {"stdout": "prose only\n"}}
        rc, out = rig.run(*([] if no_early_stop else ["--early-stop"]))
        assert rig.spawned() == expected and rc == 2


def test_td17_the_driver_never_passes_a_regression_command(make_rig):
    rig = make_rig(FakeEngine([["a"]]))
    rig.specs = {"a": {"verdict": "revise"}}
    rig.run()
    assert all("--regression-command" not in argv for argv, _ in rig.engine.calls)
