"""Trial mode of scripts/planning-cost-report.py, on sessions the engine wrote through cli.main.

The control arm is an unbound session driven to an approvable plan and left unapproved. The
risk arm is a bound session approved once as a sandbox that then elaborates its at-launch stage
through a refinement replan and a stage-depth review. Each arm's state, history and plan are
copied out of the engine's own store; only the spawn-cost ledger is written here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from agentctl.config import RISK_PLANNING_ENV
from risk_planning_scenario import SESSION, Scenario
from test_lazy_elaboration import KNOWLEDGE, LIKELY_BET, launched, lazy_text
from test_replan_autonomy_boundary import plan_text
from test_risk_bet import stage_review

SCRIPTS = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "planning-cost-report.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "planning_cost_report" / "trial.json"
BASELINE = SCRIPTS.parent / "docs" / "operations" / "planning-cost-baseline.json"
CONTROL_SESSION = "ctl1"
ENGINE_COMMIT = "0" * 40
SKILLS = {"planner": "a" * 64, "thinker": "b" * 64}
EARLY, LATE = "2020-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"
HOUR_MS = 3_600_000
CONCERN = "axis:V the knowledge field does not name the module"
CONTROL_CONCERN = "axis:V the controls are weak"
VOLATILE = ("state_sha256", "history_sha256")

_spec = importlib.util.spec_from_file_location("planning_cost_report", SCRIPT)
pcr = importlib.util.module_from_spec(_spec)
sys.modules["planning_cost_report"] = pcr
_spec.loader.exec_module(pcr)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


def spawn_row(session: str, ts: str, hours: float) -> dict:
    return {"event": "spawn", "kind": "thinker", "ts": ts, "duration_ms": int(hours * HOUR_MS),
            "spawner_session": session}


def write_arm(sc: Scenario, arms: Path, arm: str, sid: str, plan: str, ledger: list[dict]) -> None:
    out = arms / arm
    out.mkdir(parents=True)
    state = sc.state(sid)
    (out / "state.json").write_text(sc.eng.store.path(sid).read_text(encoding="utf-8"), encoding="utf-8")
    (out / "history.jsonl").write_text("".join(json.dumps(e) + "\n" for e in state.history), encoding="utf-8")
    (out / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger), encoding="utf-8")
    (out / "skills.json").write_text(json.dumps(SKILLS), encoding="utf-8")
    shutil.copyfile(plan, out / "plan.toml")


def control_session(sc: Scenario, monkeypatch) -> str:
    monkeypatch.setenv(RISK_PLANNING_ENV, "0")
    plan = sc.write(plan_text(), "control.toml")
    assert sc.submit(plan, CONTROL_SESSION)["marker"] == "PLAN-READY"
    for verdict, concerns in (("revise", (f"blocking: {CONTROL_CONCERN}",)), ("pass", ())):
        d = sc.review(plan, verdict, concerns=concerns, sid=CONTROL_SESSION)
        assert d["ok"] is (verdict == "pass"), d
    return plan


def risk_session(sc: Scenario, monkeypatch) -> str:
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    stage_two = {2: 'verify_command = "true"\n'}
    launched(sc, lazy_text(LIKELY_BET, stage_two))
    elaborated = sc.write(lazy_text(LIKELY_BET, {**KNOWLEDGE, **stage_two}), "elaborated.toml")
    applied = sc.eng.run("replan", session=SESSION, plan=elaborated)
    assert applied["detail"].startswith("refinement applied"), applied
    stage_review(sc, elaborated, 1, "revise", concerns=(f"blocking: {CONCERN}",))
    assert stage_review(sc, elaborated, 1)["ok"] is True
    return elaborated


@pytest.fixture
def arms(tmp_path, monkeypatch) -> Path:
    sc = Scenario(tmp_path)
    control_plan = control_session(sc, monkeypatch)
    risk_plan = risk_session(sc, monkeypatch)
    root = tmp_path / "arms"
    write_arm(sc, root, "control", CONTROL_SESSION, control_plan,
              [spawn_row(CONTROL_SESSION, EARLY, 2.0), spawn_row("other", EARLY, 9.0)])
    write_arm(sc, root, "risk", SESSION, risk_plan,
              [spawn_row(SESSION, EARLY, 0.5), spawn_row(SESSION, LATE, 0.25)])
    return root


def trial(arms: Path, **kw) -> dict:
    return pcr.assemble_trial(pcr.trial_rows(arms), kw.get("cap", pcr.TRIAL_CAP), ENGINE_COMMIT, None)


def mutated(doc: dict, arm: str, **cols) -> dict:
    """The document rebuilt from rows with `cols` set on one arm, so hashes and derived parts stay consistent."""
    rows = copy.deepcopy(doc["rows"])
    row = rows[pcr.ARMS.index(arm)]
    for name, value in cols.items():
        row[pcr.TRIAL_COLUMNS.index(name)] = value
    return pcr.assemble_trial(rows, doc["cap"], doc["engine_commit"], doc["baseline_sha256"])


def run_main(argv, capsys):
    rc = pcr.main([str(a) for a in argv])
    return rc, capsys.readouterr().out


def test_rows_describe_what_the_engine_wrote(arms):
    doc = trial(arms)
    control, risk = doc["arms"]["control"], doc["arms"]["risk"]
    assert (control["bound"], risk["bound"]) == (False, True)
    assert (control["stop_reason"], risk["stop_reason"]) == ("admissible", "elaborated")
    assert control["blockers"] == [] and risk["lazy_pending"] == []
    assert (control["approve_sandbox"], control["approve_elaboration"]) == (0, 0)
    assert (risk["approve_sandbox"], risk["approve_elaboration"]) == (1, 0)
    assert control["dispatch_events"] == risk["dispatch_events"] == 0
    assert control["agent_overrides"] == risk["agent_overrides"] == 0
    assert control["review_acts_total"] == 2 and control["review_acts_deferred"] == 0
    assert risk["review_acts_deferred"] == 2 and risk["review_acts_total"] >= 3
    assert risk["replans"] == 1
    assert control["thinker_spawn_h_total"] == 2.0
    assert (risk["thinker_spawn_h_to_admissible"], risk["thinker_spawn_h_deferred"]) == (0.5, 0.25)
    assert control["skills_planner_sha256"] == risk["skills_planner_sha256"] == SKILLS["planner"]
    assert pcr.check_trial(doc, arms) == []


def test_unelaborated_risk_arm_stops_at_cap(arms):
    sc_state = json.loads((arms / "risk" / "state.json").read_text(encoding="utf-8"))
    sc_state["plan_stage_reviews"] = {}
    (arms / "risk" / "state.json").write_text(json.dumps(sc_state), encoding="utf-8")
    risk = trial(arms)["arms"]["risk"]
    assert risk["lazy_pending"] == [1] and risk["stop_reason"] == "cap"


def test_inconclusive_unless_completed(arms):
    doc = trial(arms)
    assert doc["decision"] == {"completed": True, "verdict": "inconclusive"}
    for arm, reason in (("control", "cap"), ("risk", "cap"), ("risk", "forced")):
        broken = mutated(doc, arm, stop_reason=reason)
        assert broken["decision"] == {"completed": False, "verdict": "inconclusive"}


def judged(doc: dict, control: int, risk: int, **risk_cols) -> dict:
    out = mutated(doc, "control", judge_r=control, judge_v=0)
    return mutated(out, "risk", judge_r=risk, judge_v=0, **risk_cols)


@pytest.mark.parametrize("risk_acts, risk_hours, control_defects, risk_defects, faster, held, verdict", [
    (1, 1.0, 3, 3, True, True, "pass"),
    (1, 1.0, 3, 4, True, False, "fail"),
    (2, 1.0, 3, 3, False, True, "fail"),
    (1, 1.1, 3, 2, False, True, "fail"),
])
def test_decision_rule(arms, risk_acts, risk_hours, control_defects, risk_defects, faster, held, verdict):
    doc = mutated(trial(arms), "control", review_acts_total=2, thinker_spawn_h_total=2.0)
    doc = judged(doc, control_defects, risk_defects, review_acts_total=risk_acts, thinker_spawn_h_total=risk_hours)
    assert doc["comparison"]["faster"] is faster and doc["comparison"]["quality_held"] is held
    assert doc["decision"]["verdict"] == verdict
    assert pcr.check_trial(doc, None) == []


def test_verdict_is_inconclusive_without_a_judge_or_a_denominator(arms):
    doc = trial(arms)
    assert doc["comparison"]["quality_held"] is None and doc["decision"]["verdict"] == "inconclusive"
    empty = mutated(judged(doc, 1, 1), "control", review_acts_total=0)
    assert empty["comparison"]["faster"] is None and empty["decision"]["verdict"] == "inconclusive"


def test_mutated_review_acts_fails_mismatch_arms(arms, tmp_path, capsys):
    doc = trial(arms)
    path = tmp_path / "trial.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert run_main(["--check", path], capsys) == (0, "ok\n")
    doc["rows"][1][pcr.TRIAL_COLUMNS.index("review_acts_total")] += 1
    doc["rows_sha256"] = pcr.digest(doc["rows"])
    path.write_text(json.dumps(doc), encoding="utf-8")
    rc, out = run_main(["--check", path], capsys)
    assert rc == 1 and "MISMATCH: arms" in out.splitlines()


def test_mutated_verdict_fails_mismatch_decision(arms, tmp_path, capsys):
    doc = trial(arms)
    doc["decision"]["verdict"] = "pass"
    path = tmp_path / "trial.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    rc, out = run_main(["--check", path], capsys)
    assert rc == 1 and "MISMATCH: decision" in out.splitlines()


STRUCTURE = [
    ("stop_reason", "risk", {"stop_reason": "cap"}, {}),
    ("dispatch", "risk", {"dispatch_events": 1}, {}),
    ("agent_override", "risk", {"agent_overrides": 1, "stop_reason": "forced"}, {}),
    ("approve_in_control", "control", {"approve_sandbox": 1}, {}),
    ("approve_order", "risk", {"approve_sandbox": 0, "stop_reason": "admissible"}, {}),
    ("bound", "control", {"bound": True}, {}),
    ("skill_hashes", "risk", {"skills_planner_sha256": "c" * 64}, {}),
    ("cap", "risk", {}, {"cap": 0}),
]


@pytest.mark.parametrize("rule, arm, cols, doc_edit", STRUCTURE, ids=[s[0] for s in STRUCTURE])
def test_structural_rule_fails_with_its_own_key(arms, rule, arm, cols, doc_edit):
    doc = trial(arms)
    broken = mutated({**doc, **doc_edit}, arm, **cols)
    assert pcr.check_trial(broken, None) == [f"MISMATCH: structure {rule}"]


def test_check_rejects_a_document_that_is_not_a_trial_table(arms):
    doc = trial(arms)
    del doc["decision"]
    with pytest.raises(pcr.Malformed):
        pcr.check_trial(doc, None)
    doc = trial(arms)
    doc["rows"] = doc["rows"][:1]
    with pytest.raises(pcr.Malformed):
        pcr.check_trial(doc, None)


def test_check_regenerates_from_the_arms_directory(arms):
    doc = trial(arms)
    history = arms / "risk" / "history.jsonl"
    history.write_text(history.read_text(encoding="utf-8") + json.dumps({"event": "dispatch"}) + "\n",
                       encoding="utf-8")
    assert pcr.check_trial(doc, arms) == ["MISMATCH: rows differ from the arms directory"]


def test_concerns_export_is_blind(arms, capsys):
    rc, out = run_main(["concerns", arms], capsys)
    items = json.loads(out)
    assert rc == 0 and {c["text"] for c in items} == {CONTROL_CONCERN, CONCERN}
    assert all(set(c) == {"id", "text"} for c in items)
    assert items == sorted(items, key=lambda c: c["id"])
    for word in ("control", "risk", "axis", "phase", "bet", "deferred"):
        assert word not in out.replace(CONCERN, "").replace(CONTROL_CONCERN, "")


def test_judge_view_shows_the_plan_without_bets_or_phase(arms, capsys):
    rc, out = run_main(["judge-view", arms, "risk"], capsys)
    assert rc == 0 and "how the module is laid out" in out, (arms / "risk" / "plan.toml").read_text()[:2500]
    assert "a risk" not in out and "likelihood" not in out and "phase" not in out.lower()


def test_late_r_and_judge_counts_enter_as_plain_files(arms):
    concerns = {c["text"]: c["id"] for c in pcr.arm_concerns("risk", pcr.read_jsonl(arms / "risk" / "history.jsonl"))}
    (arms / "risk" / "classified.json").write_text(
        json.dumps([{"id": concerns[CONCERN], "axis": "R"}]), encoding="utf-8")
    (arms / "risk" / "judge.json").write_text(json.dumps({"R": 2, "V": 1}), encoding="utf-8")
    risk = trial(arms)["arms"]["risk"]
    assert (risk["late_r"], risk["judge_r"], risk["judge_v"]) == (1, 2, 1)
    assert trial(arms)["arms"]["control"]["late_r"] is None


def strip_volatile(doc: dict) -> dict:
    idx = [pcr.TRIAL_COLUMNS.index(c) for c in VOLATILE]
    rows = [[v for i, v in enumerate(r) if i not in idx] for r in doc["rows"]]
    arms = {a: {k: v for k, v in row.items() if k not in VOLATILE} for a, row in doc["arms"].items()}
    return {**{k: doc[k] for k in ("mode", "cap", "comparison", "decision", "engine_commit")},
            "rows": rows, "arms": arms}


def test_fixture_regenerates_from_session(arms):
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert pcr.check_trial(committed, None) == []
    assert strip_volatile(trial(arms)) == strip_volatile(committed)
    assert committed["decision"] == {"completed": True, "verdict": "inconclusive"}
