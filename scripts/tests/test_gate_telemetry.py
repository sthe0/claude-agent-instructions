"""Gate telemetry: `cli._log_gate` appends one JSONL row per gate evaluation to
GATE_LOG, fail-open (an I/O error never blocks a transition). Covers the shape of
the appended row, fail-open behaviour on an unwritable path, and a walkthrough
that counts exactly one log line per gate evaluation across a realistic session
(plan_approval, resolution x2, difficulty_blockers, replan_coverage)."""
import json
from argparse import Namespace

from agentctl import cli
from agentctl.state import Node, StageStatus


def ns(**kw):
    return Namespace(**kw)


def _read_gate_log(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _start(store, sid, **over):
    base = dict(session=sid, task="t", goal="g", done_criterion="dc",
                criterion_type="measurable", recursion_depth=0)
    base.update(over)
    return cli.cmd_start(Namespace(**base), store=store)


def _to_executing_stage1(store, sid, plan):
    cli.cmd_start(ns(session=sid, task="demo-two-stage", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


# --- _log_gate: shape + fail-open -------------------------------------------

def test_log_gate_appends_expected_shape(store, monkeypatch, tmp_path):
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    _start(store, "s1")
    state = store.load("s1")

    cli._log_gate(state, "plan_approval", ["missing approval"], passed=False)

    rows = _read_gate_log(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"ts", "session", "node", "gate", "blockers", "passed"}
    assert row["session"] == "s1"
    assert row["node"] == state.node
    assert row["gate"] == "plan_approval"
    assert row["blockers"] == ["missing approval"]
    assert row["passed"] is False


def test_log_gate_appends_one_line_per_call(store, monkeypatch, tmp_path):
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    _start(store, "s2")
    state = store.load("s2")

    cli._log_gate(state, "plan_approval", [], passed=True)
    cli._log_gate(state, "resolution", ["x"], passed=False)
    cli._log_gate(state, "resolution", [], passed=True)

    rows = _read_gate_log(log_path)
    assert len(rows) == 3
    assert [r["gate"] for r in rows] == ["plan_approval", "resolution", "resolution"]
    assert [r["passed"] for r in rows] == [True, False, True]


def test_log_gate_fails_open_on_unwritable_path(store, monkeypatch, tmp_path):
    """GATE_LOG's parent is a plain file, not a directory: mkdir(parents=True)
    raises NotADirectoryError (an OSError subclass). _log_gate must swallow it."""
    blocker_file = tmp_path / "not-a-dir"
    blocker_file.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(cli, "GATE_LOG", blocker_file / "gate-log.jsonl")
    _start(store, "s3")
    state = store.load("s3")

    cli._log_gate(state, "plan_approval", [], passed=True)  # must not raise

    assert not (blocker_file / "gate-log.jsonl").exists()


# --- walkthrough: one event per real gate evaluation -------------------------

def test_walkthrough_logs_one_event_per_gate_evaluation(store, monkeypatch, tmp_path, fixtures_dir):
    """Drive a full session through approve -> verify-final -> resolve, then
    confirm exactly the expected chokepoints fired, each exactly once (except
    resolution, which is evaluated twice: once at verify-final, once at resolve)."""
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    plan = str(fixtures_dir / "plan_two_stage.toml")
    sid = "w1"
    _to_executing_stage1(store, sid, plan)

    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok", observation=""), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)
    cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                             control="reviewed: ok", observation=""), store=store)
    cli.cmd_verify_final(ns(session=sid), store=store)
    # the experience plugin auto-activates on the substantive spine and gates
    # resolve until its phases are recorded — complete them like a real session
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched",
                             note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="walkthrough fixture, nothing to record"), store=store)
    cli.cmd_resolve(ns(session=sid, by="Fedor", quality=5, quality_by="user-confirmed",
                       quality_note=None), store=store)

    rows = _read_gate_log(log_path)
    gates_fired = [r["gate"] for r in rows]
    assert gates_fired == ["plan_approval", "resolution", "resolution"]
    assert [r["passed"] for r in rows] == [True, True, True]
    assert store.load(sid).node == Node.RESOLVED.value


def test_walkthrough_logs_difficulty_and_replan_coverage_gates(store, monkeypatch, tmp_path, fixtures_dir):
    """A failed stage enters DIAGNOSING; a premature replan logs one blocked
    difficulty_blockers evaluation, and the completed cycle logs a second (passed)
    difficulty_blockers evaluation plus one replan_coverage evaluation."""
    log_path = tmp_path / "gate-log.jsonl"
    monkeypatch.setattr(cli, "GATE_LOG", log_path)
    plan = str(fixtures_dir / "plan_two_stage.toml")
    refined = str(fixtures_dir / "plan_two_stage_refined.toml")
    sid = "w2"
    _to_executing_stage1(store, sid, plan)

    cli.cmd_record_result(ns(session=sid, status="failed", actual="boom"), store=store)
    assert store.load(sid).node == Node.DIAGNOSING.value

    # premature replan: blocked by the incomplete difficulty record
    blocked = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert blocked.ok is False

    cli.cmd_declare(ns(session=sid, expected="e", actual="a", mismatch="m"), store=store)
    cli.cmd_investigate(ns(session=sid, localized_expectation="le", localized_actual="la",
                           hypotheses=["h1", "h2"]), store=store)
    cli.cmd_critique(ns(session=sid, functional_ground="fg", replanning_task="rt"), store=store)

    d = cli.cmd_replan(ns(session=sid, plan=refined), store=store)
    assert d.ok is True

    rows = _read_gate_log(log_path)
    gates_fired = [r["gate"] for r in rows]
    assert gates_fired == ["plan_approval", "difficulty_blockers",
                           "difficulty_blockers", "replan_coverage"]
    assert rows[1]["passed"] is False  # premature: record incomplete
    assert rows[2]["passed"] is True   # cycle complete
    assert rows[3]["passed"] is True   # coverage satisfied
