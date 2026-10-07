"""Reviewer memory for the topological pair review.

A pair that already has a recorded review is re-reviewed with that review in front of
the reviewer: `agentctl plan-review-pair-history` reports the pair's earlier verdicts,
their concerns (stable id, raw and effective severity, part, whether still an
unresolved blocker) and the parts changed since the last record; the driver hands it
to `spawn-specialist.py --review-topo-history`, and the bundle renders it.

New symbols are imported inside the tests so this file stays collectable on a tree that
predates them.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.plan import load_plan

SCRIPTS = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "pair_bundle_golden"
PAIR = "2-1"
SID = "mem"
ID_IN_BUNDLE = re.compile(r"`(\d+-\d+#\d+\.c\d+)`")


def ns(**kw):
    return Namespace(**kw)


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


@pytest.fixture(autouse=True)
def private_escalation_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(cli.ESCALATION_LEDGER_ENV, str(tmp_path / "escalations.jsonl"))


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def drv():
    return _load_script("plan_review_topological_memory", "plan-review-topological.py")


@pytest.fixture
def spawner():
    return _load_script("spawn_specialist_memory", "spawn-specialist.py")


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def session(store, fixtures_dir, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text((fixtures_dir / "plan_two_stage_substantive.toml").read_text())
    cli.cmd_start(ns(session=SID, task="demo", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=SID, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=SID), store=store)
    cli.cmd_submit_plan(ns(session=SID, plan=str(plan)), store=store)
    return plan


def _retitle(plan: Path, stage_title: str) -> None:
    plan.write_text(plan.read_text().replace(f'title = "{stage_title}', f'title = "{stage_title}+'))


def _review_pair(store, plan, verdict, concerns=None, *, pair=PAIR):
    return cli.cmd_plan_review(
        ns(session=SID, target=None, scope=f"topo:{pair}", verdict=verdict, reviewer="thinker",
           concerns=concerns, concern_ids=None, note="", regression_command=None,
           plan_digest=_sha(plan)),
        store=store)


def _history(store, plan, pair=PAIR) -> dict:
    directive = cli.cmd_plan_review_pair_history(
        ns(session=SID, target=str(plan), pair=pair), store=store)
    assert directive.ok, directive.detail
    return directive.data


def _bundle(plan: Path, history, *, pair=PAIR) -> str:
    from agentctl.render import render_pair_review_bundle
    return render_pair_review_bundle(
        load_plan(str(plan)), pair, plan_sha256=_sha(plan), view_dir="/view", history=history)


# --- the verb ---------------------------------------------------------------------

def test_history_lists_both_records_and_exactly_the_changed_part(store, session):
    plan = session
    first = _review_pair(store, plan, "revise", ["blocking: C1: stage 1 scaffold is wrong"])
    second = _review_pair(store, plan, "revise", ["note: C2: wording could be tighter"])
    assert first.ok and second.ok
    _retitle(plan, "Scaffold module")

    data = _history(store, plan)
    assert data["pair"] == PAIR
    assert data["changed_parts_since_last"] == ["stage:1"]
    assert [r["record_seq"] for r in data["records"]] == sorted(r["record_seq"] for r in data["records"])
    assert len(data["records"]) == 2
    old, new = data["records"]
    assert old["reviewer_verdict"] == "revise" and new["reviewer_verdict"] == "revise"
    (blocker,) = old["concerns"]
    assert blocker["id"] == first.data["concern_ids"][0]
    assert (blocker["severity"], blocker["effective_severity"]) == ("blocking", "blocking")
    assert "scaffold is wrong" in blocker["text"]
    (remark,) = new["concerns"]
    assert remark["id"] == second.data["concern_ids"][0] and remark["severity"] == "note"
    assert remark["effective_severity"] in ("note", "advisory")


def test_history_does_not_list_a_part_outside_the_pairs_own_parts(store, session):
    plan = session
    assert _review_pair(store, plan, "pass").ok
    _retitle(plan, "Wire CI")
    assert _history(store, plan)["changed_parts_since_last"] == []
    _retitle(plan, "Add tests")
    assert _history(store, plan)["changed_parts_since_last"] == ["stage:2"]


def test_history_marks_an_open_blocker_unresolved(store, session):
    plan = session
    _review_pair(store, plan, "revise", ["blocking: C1: still wrong"])
    (concern,) = _history(store, plan)["records"][0]["concerns"]
    assert concern["unresolved"] is True and concern["parts"]


def test_history_is_read_only(store, session, tmp_path):
    plan = session
    _review_pair(store, plan, "revise", ["blocking: C1: still wrong"])

    def snapshot():
        return {p: p.read_bytes() for p in (tmp_path / "state").rglob("*") if p.is_file()}

    before = snapshot()
    assert before
    _history(store, plan)
    _history(store, plan, pair="3-2")
    assert snapshot() == before


def test_history_of_a_pair_never_reviewed_is_empty(store, session):
    data = _history(store, session)
    assert data["records"] == [] and data["changed_parts_since_last"] == []


def test_verb_is_not_a_user_authority_verb():
    assert "plan-review-pair-history" not in (SCRIPTS / "lib" / "widening_targets.py").read_text()


# --- the bundle -------------------------------------------------------------------

def test_bundle_without_history_is_the_golden_snapshot_byte_for_byte():
    from agentctl.render import render_pair_review_bundle
    doc = load_plan(str(GOLDEN / "plan.toml"))
    pair = (GOLDEN / "pair").read_text().strip()
    text = render_pair_review_bundle(doc, pair, plan_sha256="0" * 64, view_dir="/golden/view")
    assert text == (GOLDEN / "bundle.md").read_text()


def test_bundle_with_history_carries_the_prior_review_and_the_rule(store, session):
    plan = session
    first = _review_pair(store, plan, "revise", ["blocking: C1: stage 1 scaffold is wrong"])
    _retitle(plan, "Scaffold module")
    data = _history(store, plan)
    text = _bundle(plan, data)

    assert "## Prior review of this pair" in text
    assert "## Changed since the prior verdict" in text
    (concern_id,) = first.data["concern_ids"]
    assert f"`{concern_id}`" in text
    assert "blocking (effective: blocking)" in text and "UNRESOLVED BLOCKER" in text
    assert "scaffold is wrong" in text
    assert "`stage:1`" in text.split("## Changed since the prior verdict", 1)[1].split("##", 1)[0]
    assert text.index("## Prior review of this pair") < text.index("Plan digest:")
    protocol = text.split("## Review protocol", 1)[1]
    assert "Block only on a part listed under `## Changed since the prior verdict`" in " ".join(protocol.split())
    assert "part that still carries an unresolved blocker listed under `## Prior review of this pair`" in " ".join(
        protocol.split())
    assert "re:<concern-id>" in protocol


def test_bundle_without_changes_says_so(store, session):
    plan = session
    _review_pair(store, plan, "pass")
    text = _bundle(plan, _history(store, plan))
    assert "Nothing changed since the prior verdict." in text


def test_an_id_taken_from_the_bundle_resolves_in_plan_review(store, session):
    plan = session
    _review_pair(store, plan, "revise", ["blocking: C1: stage 1 scaffold is wrong"])
    text = _bundle(plan, _history(store, plan))
    (quoted,) = set(ID_IN_BUNDLE.findall(text))

    again = _review_pair(store, plan, "revise", [f"blocking: re:{quoted} C1: still wrong"])
    assert again.ok, again.detail
    assert again.data["concern_ids"] and quoted != again.data["concern_ids"][0]
    assert store.load(SID).plan_pair_reviews[PAIR].effective_severities == ["blocking"]

    unknown = _review_pair(store, plan, "revise", ["blocking: re:2-1#99.c9 C1: no such concern"])
    assert unknown.ok is False


# --- the spawner ------------------------------------------------------------------

def _spawn_argv(plan: Path, *extra: str) -> list[str]:
    return ["--kind", "thinker", "--plan", str(plan), "--done-criterion", "review",
            "--criterion-type", "acceptance-review", "--complexity", "high", "--effort", "high",
            "--dry-run", *extra]


def test_spawner_refuses_the_history_flag_without_review_topo(spawner, tmp_path, monkeypatch, capsys,
                                                              fixtures_dir):
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "costs.jsonl")
    history = tmp_path / "h.json"
    history.write_text(json.dumps({"pair": PAIR, "records": [], "changed_parts_since_last": []}))
    rc = spawner.main(_spawn_argv(fixtures_dir / "plan_two_stage.toml", "--review-topo-history", str(history)))
    assert rc == 2
    assert "--review-topo-history" in capsys.readouterr().err


def test_spawner_renders_the_history_into_the_bundle(spawner, store, session, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo"))
    plan = session
    _review_pair(store, plan, "revise", ["blocking: C1: stage 1 scaffold is wrong"])
    data = _history(store, plan)
    history = tmp_path / "h.json"
    history.write_text(json.dumps(data))

    rc = spawner.main(_spawn_argv(plan, "--review-topo", PAIR, "--review-topo-history", str(history)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Prior review of this pair" in out and data["records"][0]["concerns"][0]["id"] in out

    rc = spawner.main(_spawn_argv(plan, "--review-topo", PAIR))
    assert rc == 0
    assert "## Prior review of this pair" not in capsys.readouterr().out


def test_spawner_refuses_an_unreadable_history_file(spawner, session, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(spawner, "COST_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setenv("AGENTCTL_TOPO_UNITS_DIR", str(tmp_path / "topo"))
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    rc = spawner.main(_spawn_argv(session, "--review-topo", PAIR, "--review-topo-history", str(bad)))
    assert rc == 2


# --- the driver -------------------------------------------------------------------

class _Engine:
    def __init__(self, records):
        self.records = records
        self.calls: list[list[str]] = []

    def run(self, argv, env):
        self.calls.append(list(argv))
        if argv[0] == "plan-review-pair-history":
            return {"ok": True, "data": {"pair": PAIR, "records": self.records,
                                         "changed_parts_since_last": ["stage:1"]}}
        raise AssertionError(argv[0])


def _driver(drv, monkeypatch, plan, records):
    engine = _Engine(records)
    spawned: list[list[str]] = []
    monkeypatch.setattr(drv, "run_agentctl", engine.run)
    monkeypatch.setattr(drv, "cost_log_size", lambda: 0)

    def spawn(argv):
        spawned.append(list(argv))
        flag_at = argv.index("--review-topo-history") if "--review-topo-history" in argv else None
        if flag_at is not None:
            spawned_history.append(json.loads(Path(argv[flag_at + 1]).read_text()))
        return 0, "", ""

    spawned_history: list[dict] = []
    monkeypatch.setattr(drv, "spawn_pair", spawn)
    args = Namespace(session=SID, pairs=None, model=None, complexity="high", early_stop=False,
                     parallel=None, dry_run=False)
    return drv.Driver(args, str(plan), {}), engine, spawned, spawned_history


RECORD = {"record_seq": 1, "plan_sha256": "a" * 64, "reviewer_verdict": "revise",
          "effective_verdict": "revise", "concerns": []}


def test_driver_passes_the_history_flag_only_for_a_pair_with_a_record(drv, monkeypatch, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    driver, engine, spawned, histories = _driver(drv, monkeypatch, plan, [RECORD])
    try:
        driver.launch({"pair": PAIR, "status": "revise"})
        driver.launch({"pair": "3-2", "status": "missing"})
    finally:
        driver.cleanup_history()
    assert "--review-topo-history" in spawned[0] and "--review-topo-history" not in spawned[1]
    assert histories[0]["records"] == [RECORD] and histories[0]["changed_parts_since_last"] == ["stage:1"]
    assert [c[0] for c in engine.calls] == ["plan-review-pair-history"]
    assert all("--session" not in argv for argv in spawned)


def test_driver_omits_the_flag_when_the_verb_reports_no_records(drv, monkeypatch, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    driver, _, spawned, _ = _driver(drv, monkeypatch, plan, [])
    driver.launch({"pair": PAIR, "status": "stale"})
    assert "--review-topo-history" not in spawned[0]


def test_driver_removes_its_history_files_afterwards(drv, monkeypatch, tmp_path):
    plan = tmp_path / "plan.toml"
    plan.write_text("x")
    driver, _, spawned, _ = _driver(drv, monkeypatch, plan, [RECORD])
    driver.launch({"pair": PAIR, "status": "revise"})
    path = Path(spawned[0][spawned[0].index("--review-topo-history") + 1])
    assert path.exists()
    driver.cleanup_history()
    assert not path.exists()
