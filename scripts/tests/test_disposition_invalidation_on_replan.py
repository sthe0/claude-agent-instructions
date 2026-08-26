"""Disposition invalidation on replan: when a stage field changes, questions
disposed against that field are marked stale so the mismatch is visible in
question-list output before the next approve (#123).

Covers:
- premise.invalidate_stale_dispositions: pure-logic unit tests (bag + stage_keys).
- cmd_replan wires the helper: a disposed question whose cited stage field changed
  surfaces stale_note in question-list after the replan.
- The disposition itself is preserved (audit trail).
- The stale note is cleared when the stage field is restored.
- An open or retired question is never marked stale.
- The plain question-list format shows [stale] for stale questions.
- The --format md output shows the stale note in the disposition cell.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, plugins, plugins_premise
from agentctl.plan import load_plan, stage_element_keys
from agentctl.premise import (
    STALE_DISPOSITION_NOTE,
    Question,
    invalidate_stale_dispositions,
    questions_from_dicts,
    questions_to_dicts,
)
from agentctl.state import SessionState, WeightClass

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
depends_on = []
output_artifacts = ["s{i}.py"]
"""


def _write_plan(path, stages, *, goal="exercise invalidation"):
    body = [
        "[meta]",
        'task_id = "demo-invalidation"',
        f'goal = "{goal}"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
        'weight_class = "small_change"',
        "",
    ]
    prev = None
    for i, img in stages:
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(f"""[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
""")
        prev = i
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def ns(**kw):
    return Namespace(**kw)


# ---------------------------------------------------------------------------
# Pure unit tests for premise.invalidate_stale_dispositions
# ---------------------------------------------------------------------------

class TestInvalidateStaleDispositionsPure:
    def _bag(self, questions):
        return {"questions": questions_to_dicts(questions)}

    def _stage_keys(self, doc, index):
        s = next(s for s in doc.stages if s.index == index)
        return {index: stage_element_keys(s)}

    def test_stale_note_set_when_stage_field_changed(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        # Build a question disposed against a key that LOOKS different from the current one
        q = Question(
            id="Q1", target="stage:1.means", question="why?",
            disposition="researched", own_research="looked it up",
            answer="because X", source="docs", derivation="matches the spec",
            disposed_at_key="stale-key-that-will-not-match",
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is True
        q_after = questions_from_dicts(bag["questions"])[0]
        assert q_after.stale_note == STALE_DISPOSITION_NOTE

    def test_stale_note_cleared_when_key_is_again_valid(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        current_key = sk[1].get("means") or sk[1].get("")
        # Pick a key that IS valid for stage 1
        from agentctl.premise import _accepted_keys
        valid_key = next(k for k in _accepted_keys(sk[1], "means") if k)
        q = Question(
            id="Q1", target="stage:1.means", question="why?",
            disposition="researched", own_research="looked it up",
            answer="because X", source="docs", derivation="matches",
            disposed_at_key=valid_key,
            stale_note=STALE_DISPOSITION_NOTE,  # previously stale, now cleared
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is True
        q_after = questions_from_dicts(bag["questions"])[0]
        assert q_after.stale_note == ""

    def test_open_question_is_not_marked_stale(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        q = Question(
            id="Q1", target="stage:1.means", question="why?",
            disposition="open", disposed_at_key="whatever",
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is False
        assert questions_from_dicts(bag["questions"])[0].stale_note == ""

    def test_retired_question_is_not_marked_stale(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        q = Question(
            id="Q1", target="stage:1.means", question="why?",
            disposition="retired", reason="stage removed",
            disposed_at_key="stale-key",
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is False
        assert questions_from_dicts(bag["questions"])[0].stale_note == ""

    def test_goal_target_is_not_marked_stale(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        q = Question(
            id="Q1", target="plan.goal", question="why?",
            disposition="researched", own_research="checked",
            answer="fits", source="docs", derivation="obvious",
            disposed_at_key="stale-key",
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is False

    def test_dangling_stage_target_is_skipped(self, tmp_path):
        """A question bound to a stage that no longer exists is a dangling edge
        that validate_questions handles separately; invalidate_stale_dispositions
        skips it rather than marking it stale."""
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        q = Question(
            id="Q1", target="stage:99.means", question="why?",
            disposition="researched", own_research="checked",
            answer="fits", source="docs", derivation="obvious",
            disposed_at_key="stale-key",
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is False

    def test_no_change_when_all_keys_still_valid(self, tmp_path):
        plan_path = _write_plan(tmp_path / "plan.toml", [(1, "img-one")])
        doc = load_plan(plan_path)
        sk = {s.index: stage_element_keys(s) for s in doc.stages}
        from agentctl.premise import _accepted_keys
        valid_key = next(k for k in _accepted_keys(sk[1], "means") if k)
        q = Question(
            id="Q1", target="stage:1.means", question="why?",
            disposition="researched", own_research="checked",
            answer="fits", source="docs", derivation="makes sense",
            disposed_at_key=valid_key,
        )
        bag = self._bag([q])
        changed = invalidate_stale_dispositions(bag, sk)
        assert changed is False
        assert bag["questions"][0].get("stale_note", "") == ""

    def test_returns_false_on_empty_question_bag(self, tmp_path):
        bag = {"questions": []}
        changed = invalidate_stale_dispositions(bag, {1: {}})
        assert changed is False


# ---------------------------------------------------------------------------
# Integration tests: cmd_replan wires the helper
# ---------------------------------------------------------------------------

def _setup_session_with_disposed_question(store, sid, plan_path):
    """Drive a session to EXECUTING stage 1 with one disposed question."""
    cli.cmd_start(ns(session=sid, task="stale-disp-test", goal="",
                     done_criterion="", criterion_type="measurable",
                     recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan_path), store=store)
    assert "premise" in store.load(sid).plugins

    # Raise and dispose a question against stage 1's result field.
    cli.cmd_question_raise(
        ns(session=sid, id="Q1", target="stage:1.result",
           question="why this target image?", own_research="", control=None),
        store=store)
    cli.cmd_question_research(
        ns(session=sid, id="Q1", attempted="verified via docs"), store=store)
    cli.cmd_question_dispose(
        ns(session=sid, id="Q1", to="researched",
           answer="because X", source="docs", derivation="matches spec",
           basis="", risk="", plan=None),
        store=store)
    cli.cmd_order_raise(ns(session=sid, id="O1", element="covers this task"), store=store)
    cli.cmd_order_dispose(ns(session=sid, id="O1", as_="covered", stage=1, reason=""),
                          store=store)
    cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                               runner=lambda *a, **kw:
                               __import__('types').SimpleNamespace(
                                   returncode=0, stdout="", stderr=""))
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(ns(session=sid), store=store)


class TestDispositionInvalidationOnReplan:
    def test_stale_note_set_in_question_list_after_replan_changes_stage_field(
            self, store, tmp_path, monkeypatch):
        """A disposed question whose cited stage field changed on replan surfaces
        stale_note in question-list output — the mismatch is visible before the
        approve gate fires (#123)."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        plan_path = _write_plan(tmp_path / "plan.toml",
                                [(1, "img-one"), (2, "img-two")])
        sid = "stale-disp-1"
        _setup_session_with_disposed_question(store, sid, str(plan_path))

        # Verify the question is disposed and currently NOT stale.
        d = cli.cmd_question_list(ns(session=sid, format=None), store=store)
        assert "Q1=researched" in d.detail
        assert "[stale]" not in d.detail

        # Replan with a changed expected_result_image on stage 1, which moves the
        # stage's result-element key that Q1 was disposed against.
        corrected = _write_plan(tmp_path / "corrected.toml",
                                [(1, "img-one-EDITED"), (2, "img-two")])
        cli.cmd_declare(ns(session=sid, symptom="img changed", diagnosis="img moved"),
                        store=store)
        cli.cmd_critique(ns(session=sid, similarities="same approach",
                            differences="img updated",
                            failure_address="not_applicable"), store=store)
        cli.cmd_replan(ns(session=sid, plan=str(corrected),
                          normalization_waiver="one-off img change",
                          coverage_waiver=None), store=store)

        # question-list now shows the stale note for Q1.
        d_after = cli.cmd_question_list(ns(session=sid, format=None), store=store)
        assert "Q1=researched [stale]" in d_after.detail

    def test_stale_note_in_md_format(self, store, tmp_path, monkeypatch):
        """The --format md output includes the stale note in the disposition cell."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        plan_path = _write_plan(tmp_path / "plan.toml",
                                [(1, "img-one"), (2, "img-two")])
        sid = "stale-disp-md"
        _setup_session_with_disposed_question(store, sid, str(plan_path))

        corrected = _write_plan(tmp_path / "corrected-md.toml",
                                [(1, "img-one-EDITED"), (2, "img-two")])
        cli.cmd_declare(ns(session=sid, symptom="img changed", diagnosis="img moved"),
                        store=store)
        cli.cmd_critique(ns(session=sid, similarities="same approach",
                            differences="img updated",
                            failure_address="not_applicable"), store=store)
        cli.cmd_replan(ns(session=sid, plan=str(corrected),
                          normalization_waiver="one-off img change",
                          coverage_waiver=None), store=store)

        d = cli.cmd_question_list(ns(session=sid, format="md"), store=store)
        assert STALE_DISPOSITION_NOTE in d.detail
        # The stale note is in the disposition column, not a separate row.
        assert "researched" in d.detail

    def test_stale_note_cleared_by_second_replan_restoring_the_field(
            self, store, tmp_path, monkeypatch):
        """If a second replan reverts the stage-field change, the stale note is
        cleared — the disposition is valid again."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        plan_path = _write_plan(tmp_path / "plan.toml",
                                [(1, "img-one"), (2, "img-two")])
        sid = "stale-disp-clear"
        _setup_session_with_disposed_question(store, sid, str(plan_path))

        # First replan: change the stage field, making Q1 stale.
        corrected = _write_plan(tmp_path / "corrected.toml",
                                [(1, "img-one-EDITED"), (2, "img-two")])
        cli.cmd_declare(ns(session=sid, symptom="img changed", diagnosis="img moved"),
                        store=store)
        cli.cmd_critique(ns(session=sid, similarities="same approach",
                            differences="img updated",
                            failure_address="not_applicable"), store=store)
        cli.cmd_replan(ns(session=sid, plan=str(corrected),
                          normalization_waiver="one-off", coverage_waiver=None),
                       store=store)
        d1 = cli.cmd_question_list(ns(session=sid, format=None), store=store)
        assert "[stale]" in d1.detail

        # Second replan: revert the image, restoring the original key.
        reverted = _write_plan(tmp_path / "reverted.toml",
                               [(1, "img-one"), (2, "img-two")])
        cli.cmd_declare(ns(session=sid, symptom="reverted", diagnosis="back to original"),
                        store=store)
        cli.cmd_critique(ns(session=sid, similarities="same approach",
                            differences="img reverted",
                            failure_address="not_applicable"), store=store)
        cli.cmd_question_enumerate(ns(session=sid, plan=None), store=store,
                                   runner=lambda *a, **kw:
                                   __import__('types').SimpleNamespace(
                                       returncode=0, stdout="", stderr=""))
        cli.cmd_replan(ns(session=sid, plan=str(reverted),
                          normalization_waiver="one-off", coverage_waiver=None),
                       store=store)

        d2 = cli.cmd_question_list(ns(session=sid, format=None), store=store)
        assert "[stale]" not in d2.detail
        assert "Q1=researched" in d2.detail

    def test_disposition_itself_is_preserved_not_re_opened(
            self, store, tmp_path, monkeypatch):
        """The audit trail is not destroyed: the disposition stays 'researched',
        only stale_note changes."""
        monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)
        plan_path = _write_plan(tmp_path / "plan.toml",
                                [(1, "img-one"), (2, "img-two")])
        sid = "stale-disp-audit"
        _setup_session_with_disposed_question(store, sid, str(plan_path))

        corrected = _write_plan(tmp_path / "corrected-audit.toml",
                                [(1, "img-one-EDITED"), (2, "img-two")])
        cli.cmd_declare(ns(session=sid, symptom="img changed", diagnosis="img moved"),
                        store=store)
        cli.cmd_critique(ns(session=sid, similarities="same approach",
                            differences="img updated",
                            failure_address="not_applicable"), store=store)
        cli.cmd_replan(ns(session=sid, plan=str(corrected),
                          normalization_waiver="one-off", coverage_waiver=None),
                       store=store)

        state = store.load(sid)
        q = next(q for q in
                 questions_from_dicts(state.plugins["premise"].get("questions", []))
                 if q.id == "Q1")
        assert q.disposition == "researched"  # preserved
        assert q.stale_note == STALE_DISPOSITION_NOTE  # annotated
        assert q.answer  # original answer field still present
