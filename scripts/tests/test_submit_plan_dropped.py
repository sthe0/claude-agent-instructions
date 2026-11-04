"""`agentctl submit-plan` lists the derived grants that were dropped, with stage and reason (P3)."""
from agentctl import plan as plan_mod
from agentctl.plan import dropped_grants_report, load_plan
from agentctl.store import FileStateStore

import test_submit_plan_surfaces as surfaces


def _submit_developer(tmp_path, meta_extra=""):
    store = FileStateStore(tmp_path / "state")
    surfaces._session(store, "dropped")
    plan = surfaces._write_plan(tmp_path, tier="small", meta_extra=meta_extra)
    plan.write_text(plan.read_text().replace('executor = "in_thread"', 'executor = "spawn:developer"'),
                    encoding="utf-8")
    return plan, surfaces.cli.cmd_submit_plan(
        surfaces.ns(session="dropped", plan=str(plan)), store=store)


def test_submit_plan_lists_a_dropped_derivation(tmp_path):
    _plan, d = _submit_developer(tmp_path)
    rows = d.data["dropped_grants"]
    assert [r["stage"] for r in rows] == [1]
    assert "declares no `branch`" in rows[0]["reason"]
    block = [a for a in d.data["advisories"] if a.startswith("dropped grants")]
    assert len(block) == 1 and "- stage 1:" in block[0] and rows[0]["reason"] in block[0]


def test_nothing_dropped_means_no_block(tmp_path):
    store = FileStateStore(tmp_path / "state")
    surfaces._session(store, "none")
    plan = surfaces._write_plan(tmp_path, tier="small")
    d = surfaces.cli.cmd_submit_plan(surfaces.ns(session="none", plan=str(plan)), store=store)
    assert "dropped_grants" not in d.data
    assert not [a for a in d.data["advisories"] if a.startswith("dropped grants")]


def test_report_matches_the_derivation_per_stage(tmp_path):
    plan, _ = _submit_developer(tmp_path)
    doc = load_plan(str(plan))
    got = dropped_grants_report(doc)
    expected = plan_mod._derive_stage_grants(doc.stages[0], plan_mod._venue_for(doc))[1]
    assert [(r["entry"], r["reason"]) for r in got] == [(d["entry"], d["reason"]) for d in expected]
