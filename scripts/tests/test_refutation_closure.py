"""A refuted prescription is closed in three steps, not by an edit of the rule file alone.

A critique that routes a fault to a rule file (a `principle/v1` or `leaf/v1`) snapshots what the rule
rests on. The obligation then stays open until the picture (a new experience or case enters the
`induced_from` of a regularity the rule is grounded in), the condition (a regularity joined its
grounds, or a grounded regularity's claim changed) and the prescription (the rule file itself
differs from its baseline) all hold. A target that is not a prescription closes on the edit alone,
as before. Each test builds a small memory under tmp_path and drives the gate functions, or the
critique and replan commands, in-process, so replacing one seam turns exactly its tests red by
assertion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentctl import gates, grounding
from agentctl.plan import load_plan
from agentctl.state import SessionState
from lib import principle_provenance as pp

from test_failure_address_consequence import (
    _blockers, _critique, _diagnosing, _normalize, _plan, _replan, _stages,
)

DATE = "2026-10-29"


def _fm(*lines: str) -> str:
    return "---\n" + "".join(f"{line}\n" for line in lines) + "---\n"


def regularity_text(claim="claim v1", induced=("e1",)) -> str:
    return _fm("name: r", "description: d", "type: reference", "schema: regularity/v1", "generality: 2",
               f"induced_from: {pp.format_list(induced)}", f"created: {DATE}", f"last_verified: {DATE}"
               ) + f"# R\n\n## Regularity\n\n{claim}\n"


def experience_text() -> str:
    return _fm("name: e", "description: d", "type: reference", "schema: difficulty/v1") + "# E\n"


def principle_text(grounded=("R1",), rule="rule v1") -> str:
    return _fm("name: p", "description: d", "type: reference", "schema: principle/v1", "generality: 2",
               f"grounded_in: {pp.format_list(['regularity:' + g for g in grounded])}",
               f"created: {DATE}", f"last_verified: {DATE}") + f"# P\n\n## Principle\n\n{rule}\n"


class Memory:
    """A global `leaves/` tree and a project memory dir, with the grounding resolver pointed at them."""

    def __init__(self, tmp: Path) -> None:
        self.dirs = {"global": tmp / "mem" / "leaves", "project": tmp / "proj" / ".claude" / "agent-memory"}
        for base in self.dirs.values():
            for sub in ("experience", "regularities", "principles"):
                (base / sub).mkdir(parents=True)

    def write(self, tier: str, sub: str, slug: str, text: str) -> str:
        path = self.dirs[tier] / sub / f"{slug}.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def edit(self, path: str, text: str) -> None:
        Path(path).write_text(text, encoding="utf-8")

    def seed(self) -> None:
        """R1 (induced from e1) and R2 (from e2) are regularities; e1, e2 and e3 are experience leaves."""
        for slug in ("e1", "e2", "e3"):
            self.write("global", "experience", slug, experience_text())
        self.r1 = self.write("global", "regularities", "R1", regularity_text(induced=("e1",)))
        self.r2 = self.write("global", "regularities", "R2", regularity_text("claim two", ("e2",)))
        self.rule = self.write("global", "principles", "P", principle_text())


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setenv("AGENTCTL_SCRATCH_ROOTS", str(tmp_path / "scratch-elsewhere"))
    memory = Memory(tmp_path)
    monkeypatch.setattr(grounding, "engine_leaves_root", lambda: memory.dirs["global"])
    memory.seed()
    return memory


@pytest.fixture
def plans(tmp_path):
    def make(name, stages=None):
        return _plan(tmp_path / "work" / f"{name}.toml", stages or _stages())
    return make


def _entry(target: str, stage: int = 1) -> dict:
    return gates.renorm_record(target, gates.file_norm_digest(target), stage, 0,
                               gates.prescription_snapshot(target))


def _state(entry: dict) -> SessionState:
    state = SessionState(session_id="rc", task_id="rc")
    state.pending_renorms = [entry]
    return state


def _edit_rule(mem: Memory) -> None:
    mem.edit(mem.rule, principle_text(rule="rule v2"))


def _refine_claim(mem: Memory) -> None:
    mem.edit(mem.r1, regularity_text("claim v2", ("e1",)))


def _add_picture(mem: Memory) -> None:
    mem.edit(mem.r1, regularity_text("claim v1", ("e1", "e3")))


def test_prescription_target_snapshotted(mem, store, plans):
    _diagnosing(store, "s1", plans("base"))
    assert _critique(store, "s1", "normative", [mem.rule]).ok is True
    targets = store.load("s1").difficulty.norm_targets
    assert len(targets) == 1 and "prescription" in targets[0], \
        "BEHAVIOUR: a prescription target recorded no snapshot of its grounds"
    snap = targets[0]["prescription"]
    assert snap["grounded_in"] == ["regularity:R1"]
    assert set(snap["regularities"]) == {"regularity:R1", "regularity:R2"}
    assert snap["sources"] == {"regularity:R1": ["e1"], "regularity:R2": ["e2"]}


def test_nonprescription_target_unchanged(mem, store, plans):
    assert gates.prescription_snapshot(mem.r1) is None
    _diagnosing(store, "s2", plans("base"))
    assert _critique(store, "s2", "normative", [mem.r1]).ok is True
    (target,) = store.load("s2").difficulty.norm_targets
    assert "prescription" not in target, "BEHAVIOUR: a non-prescription target got a snapshot"
    entry = _entry(mem.r1)
    assert gates.open_renorms(_state(entry)) == [entry]
    _refine_claim(mem)
    assert gates.open_renorms(_state(entry)) == [], \
        "BEHAVIOUR: a non-prescription target stayed open after its edit"


def test_prescription_only_edit_stays_open(mem):
    entry = _entry(mem.rule)
    assert gates.refutation_steps_missing(entry) == ["picture", "condition", "prescription"]
    _edit_rule(mem)
    assert gates.refutation_steps_missing(entry) == ["picture", "condition"]
    assert gates.open_renorms(_state(entry)) == [entry], \
        "BEHAVIOUR: an edit of the rule file alone closed the obligation"


def test_condition_without_picture_stays_open(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    _refine_claim(mem)
    assert gates.refutation_steps_missing(entry) == ["picture"]
    assert gates.open_renorms(_state(entry)) == [entry], \
        "BEHAVIOUR: a refined regularity without a new instance closed the obligation"


def test_picture_without_condition_stays_open(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    _add_picture(mem)
    assert gates.refutation_steps_missing(entry) == ["condition"], \
        "BEHAVIOUR: a new instance without a change of the ground satisfied the condition"
    assert gates.open_renorms(_state(entry)) == [entry]


def test_three_steps_discharge(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    mem.edit(mem.r1, regularity_text("claim v2", ("e1", "e3")))
    assert gates.refutation_steps_missing(entry) == []
    assert gates.open_renorms(_state(entry)) == [], \
        "BEHAVIOUR: the obligation stayed open after all three steps were recorded"


def test_existing_regularity_link_stays_open(mem):
    entry = _entry(mem.rule)
    mem.edit(mem.rule, principle_text(grounded=("R1", "R2")))
    assert gates.condition_met(entry) is True
    assert gates.refutation_steps_missing(entry) == ["picture"], \
        "BEHAVIOUR: linking a regularity that already held its refs counted as a new instance"
    assert gates.open_renorms(_state(entry)) == [entry]


def test_picture_alone_keeps_regularity_digest(mem):
    before = gates.regularity_digest(regularity_text(induced=("e1",)))
    assert gates.regularity_digest(regularity_text(induced=("e1", "e3"))) == before, \
        "BEHAVIOUR: a new induced_from ref changed the regularity's digest"
    dashed = regularity_text().replace("induced_from: [e1]", "induced_from:\n  - e1\n  - e3")
    assert gates.regularity_digest(dashed) == before
    assert gates.regularity_digest(regularity_text("claim v2")) != before
    assert gates.regularity_digest(regularity_text() + "more\n") != before
    assert gates.regularity_digest(regularity_text().replace("generality: 2", "generality: 3")) != before


def test_stamp_alone_keeps_regularity_digest(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    before = gates.regularity_digest(Path(mem.r1).read_text(encoding="utf-8"))
    stamped = regularity_text().replace(
        f"last_verified: {DATE}",
        "last_verified: 2026-11-01\ngrounding_judged: sha256:abc\ngrounding_unjudged: down\n"
        "grounding_override:\n  - reason: x")
    mem.edit(mem.r1, stamped)
    assert gates.regularity_digest(stamped) == before, \
        "BEHAVIOUR: a stamp or restamp changed the regularity's digest"
    assert "condition" in gates.refutation_steps_missing(entry), \
        "BEHAVIOUR: restamping a ground satisfied the condition"


def test_project_ground_snapshotted(mem):
    p1 = mem.write("project", "regularities", "p1", regularity_text("project claim", ("e1",)))
    mem.write("global", "regularities", "g1", regularity_text("global claim", ("e2",)))
    rule = mem.write("project", "principles", "PP", principle_text(grounded=("p1", "g1")))
    assert gates.snapshot_tiers(rule) == ("project", "global")
    assert gates.snapshot_tiers(mem.rule) == ("global",)
    snap = gates.prescription_snapshot(rule)
    assert snap is not None
    assert snap["grounded_in"] == ["project:regularity:p1", "regularity:g1"], \
        "BEHAVIOUR: a project rule's snapshot lost its project or its fallback ground"
    assert {"project:regularity:p1", "regularity:g1", "regularity:R1"} <= set(snap["regularities"])
    assert p1 and "project:regularity:p1" in snap["sources"]


def _stage_blocked(entry: dict) -> str:
    return " | ".join(gates.pending_renorm_stage_blockers(_state(entry), 1))


def test_stage_pass_names_missing_step(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    text = _stage_blocked(entry)
    assert "picture" in text and "condition" in text, \
        f"BEHAVIOUR: the stage refusal does not name the missing steps: {text!r}"
    assert "prescription missing" not in text


def test_final_gate_names_missing_step(mem):
    entry = _entry(mem.rule)
    _edit_rule(mem)
    text = " | ".join(gates.pending_renorm_final_blockers(_state(entry)))
    assert "picture" in text and "condition" in text, \
        f"BEHAVIOUR: the final refusal does not name the missing steps: {text!r}"
    assert "prescription missing" not in text


def _bound(store, plans, mem, sid):
    """Session `sid` with a prescription renorm of `mem.rule` bound to stage 1, through a closing replan."""
    _diagnosing(store, sid, plans("base"))
    assert _critique(store, sid, "normative", [mem.rule]).ok is True
    _normalize(store, sid)
    assert _replan(store, sid, plans("renorm", _stages(s1={"renorms": [mem.rule]}))).ok is True
    return store.load(sid)


def test_snapshot_survives_rebind(mem, store, plans):
    at_critique = gates.prescription_snapshot(mem.rule)
    (entry,) = _bound(store, plans, mem, "b1").pending_renorms
    assert entry.get("prescription") == at_critique, "BEHAVIOUR: the replan's rebind dropped the snapshot"
    _refine_claim(mem)
    again = plans("again", _stages(s1={"renorms": [mem.rule], "method": "m4"}))
    assert _replan(store, "b1", again).ok is True
    (entry,) = store.load("b1").pending_renorms
    assert entry["prescription"] == at_critique, \
        "BEHAVIOUR: a later rebind re-took the snapshot instead of keeping the critique-time one"


def test_replan_closure_keeps_edited_prescription_open(mem, store, plans):
    _bound(store, plans, mem, "b2")
    _edit_rule(mem)
    d = _replan(store, "b2", plans("again", _stages(s1={"renorms": [mem.rule], "method": "m2"})))
    assert d.ok is True, f"BEHAVIOUR: a replan naming the renorm was refused: {d.detail}"
    assert [e["target"] for e in store.load("b2").pending_renorms] == [mem.rule], \
        "BEHAVIOUR: a replan after an edit of the rule file alone closed the obligation"


def test_replan_names_missing_step(mem, store, plans):
    _bound(store, plans, mem, "b3")
    _edit_rule(mem)
    d = _replan(store, "b3", plans("dropped", _stages(s1={"method": "m3"})))
    assert d.ok is False, "BEHAVIOUR: a replan dropped an open prescription renorm"
    text = _blockers(d)
    assert "picture" in text and "condition" in text, f"BEHAVIOUR: the refusal names no step: {text!r}"


def test_deleted_rule_file_keeps_prescription_open(mem):
    entry = _entry(mem.rule)
    Path(mem.rule).unlink()
    assert gates.refutation_steps_missing(entry) == ["file"]
    assert gates.open_renorms(_state(entry)) == [entry], \
        "BEHAVIOUR: deleting the rule file closed a prescription obligation"
    for text in (_stage_blocked(entry), " | ".join(gates.pending_renorm_final_blockers(_state(entry)))):
        assert "gone" in text and "restore" in text, f"BEHAVIOUR: the refusal does not say the file is gone: {text!r}"


def test_deleted_nonprescription_target_still_closes(mem):
    entry = _entry(mem.r1)
    Path(mem.r1).unlink()
    assert gates.open_renorms(_state(entry)) == [], \
        "BEHAVIOUR: a deleted non-prescription target stayed open"


def test_renamed_rule_file_stays_open_until_restored(mem):
    entry = _entry(mem.rule)
    moved = Path(mem.rule).with_name("P-renamed.md")
    Path(mem.rule).rename(moved)
    assert gates.open_renorms(_state(entry)) == [entry], \
        "BEHAVIOUR: renaming the rule file closed the obligation"
    moved.rename(mem.rule)
    assert gates.refutation_steps_missing(entry) == ["picture", "condition", "prescription"]


def test_carried_passed_stage_with_open_prescription_is_refused(mem, store, plans):
    _diagnosing(store, "c1", plans("base"), fail_stage=2)
    assert _critique(store, "c1", "normative", [mem.rule]).ok is True
    _normalize(store, "c1")
    _edit_rule(mem)
    new_plan = load_plan(plans("on-passed", _stages(s1={"renorms": [mem.rule]})), strict=False)
    out = gates.normative_edit_blockers(store.load("c1"), new_plan, {2})
    text = " | ".join(out)
    assert "PASSED" in text and "picture" in text and "condition" in text, \
        f"BEHAVIOUR: an edited rule with picture and condition open was admitted on a carried PASSED stage: {out}"
    assert gates.normative_edit_blockers(store.load("c1"), new_plan, {1, 2}) == []


def test_project_rule_under_a_leaves_dir_is_project_tier(mem):
    mem.write("project", "regularities", "p1", regularity_text("project claim", ("e1",)))
    nested = mem.dirs["project"] / "leaves"
    nested.mkdir()
    rule = nested / "x.md"
    rule.write_text(principle_text(grounded=("p1",)).replace("principle/v1", "leaf/v1"), encoding="utf-8")
    assert gates.snapshot_tiers(str(rule)) == ("project", "global")
    snap = gates.prescription_snapshot(str(rule))
    assert snap is not None and "project:regularity:p1" in snap["regularities"], \
        "BEHAVIOUR: a project rule under a leaves/ dir was read as global"


def test_project_rule_discharges_in_three_steps(mem):
    mem.write("project", "regularities", "p1", regularity_text("project claim", ("e1",)))
    rule = mem.write("project", "principles", "PP", principle_text(grounded=("p1",)))
    entry = _entry(rule)
    assert gates.refutation_steps_missing(entry) == ["picture", "condition", "prescription"]
    mem.edit(rule, principle_text(grounded=("p1",), rule="rule v2"))
    mem.edit(str(mem.dirs["project"] / "regularities" / "p1.md"),
             regularity_text("project claim v2", ("e1", "e3")))
    assert gates.refutation_steps_missing(entry) == [], \
        "BEHAVIOUR: a project-tier rule never reached the discharged state"


def test_unparsed_induced_from_does_not_make_later_refs_a_picture(mem):
    broken = regularity_text().replace("induced_from: [e1]", "induced_from: [e1")
    mem.edit(mem.r1, broken)
    snap = gates.prescription_snapshot(mem.rule)
    assert snap["unparsed"] == ["regularity:R1"]
    entry = _entry(mem.rule)
    mem.edit(mem.r1, regularity_text("claim v1", ("e1", "e3")))
    assert "picture" in gates.refutation_steps_missing(entry), \
        "BEHAVIOUR: refs of a regularity unparsed at snapshot time counted as a new picture"
