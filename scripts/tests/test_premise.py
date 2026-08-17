from agentctl import premise
from agentctl.premise import (
    Question,
    QuestionCandidate,
    parse_target,
    questions_from_dicts,
    questions_to_dicts,
    validate_question_candidates,
    validate_questions,
)


def _element_keys(whole_stage, **elements):
    """A per-stage `element -> key` map in the shape plan.stage_element_keys returns:
    the reserved whole-stage entry plus whichever element keys a case needs."""
    keys = {premise.WHOLE_STAGE_ELEMENT: whole_stage}
    keys.update(elements)
    return keys


def _researched(**overrides):
    base = dict(
        id="q1",
        target="plan.goal",
        question="is the thing true?",
        disposition="researched",
        own_research="checked the repo for prior art",
        answer="yes, per file X",
        source="scripts/agentctl/plan.py",
        derivation="X implies the answer because Y",
    )
    base.update(overrides)
    return Question(**base)


def test_target_must_parse():
    q = Question(id="q1", target="not-a-target", question="?", disposition="open")
    blockers = validate_questions([q], stage_keys={})
    assert any("target" in b for b in blockers)


def test_dangling_stage_target_blocks():
    q = _researched(target="stage:3.means", disposed_at_key="k1")
    blockers = validate_questions([q], stage_keys={1: _element_keys("a")})
    assert any("stage 3" in b and "dangling" in b for b in blockers)


def test_open_question_blocks():
    q = Question(id="q1", target="plan.goal", question="?", disposition="open")
    blockers = validate_questions([q], stage_keys={})
    assert any("open" in b for b in blockers)


def test_escalated_requires_own_research():
    q = Question(
        id="q1", target="plan.goal", question="?",
        disposition="escalated", own_research="", answer="the user said yes",
    )
    blockers = validate_questions([q], stage_keys={})
    assert any("own_research" in b for b in blockers)


def test_escalated_requires_answer():
    q = Question(
        id="q1", target="plan.goal", question="?",
        disposition="escalated", own_research="tried grep and web search", answer="",
    )
    blockers = validate_questions([q], stage_keys={})
    assert any("answer" in b for b in blockers)


def test_researched_requires_answer_source_derivation():
    q = Question(
        id="q1", target="plan.goal", question="?",
        disposition="researched", own_research="looked around",
        answer="", source="", derivation="",
    )
    blockers = validate_questions([q], stage_keys={})
    assert any("answer" in b for b in blockers)
    assert any("source" in b for b in blockers)
    assert any("derivation" in b for b in blockers)


def test_assumed_requires_own_research_basis_risk():
    q = Question(
        id="q1", target="plan.goal", question="?",
        disposition="assumed", own_research="", basis="", risk="",
    )
    blockers = validate_questions([q], stage_keys={})
    assert any("own_research" in b for b in blockers)
    assert any("basis" in b for b in blockers)
    assert any("risk" in b for b in blockers)


def test_retired_requires_reason():
    q = Question(id="q1", target="plan.goal", question="?", disposition="retired", reason="")
    blockers = validate_questions([q], stage_keys={})
    assert any("reason" in b for b in blockers)


def test_placeholder_fields_blocked():
    q = _researched(own_research="TBD")
    blockers = validate_questions([q], stage_keys={})
    assert any("placeholder" in b for b in blockers)


def test_derivation_may_not_echo_answer_or_source():
    q_echo_answer = _researched(derivation="Yes, per file X")
    blockers = validate_questions([q_echo_answer], stage_keys={})
    assert any("echoes its answer" in b for b in blockers)

    q_echo_source = _researched(derivation="scripts/agentctl/plan.py")
    blockers = validate_questions([q_echo_source], stage_keys={})
    assert any("echoes its source" in b for b in blockers)


def test_stage_bound_key_mismatch_blocks():
    q = _researched(target="stage:2.means", disposed_at_key="OLDKEY")
    blockers = validate_questions(
        [q], stage_keys={2: _element_keys("NEWKEY", means="NEWKEY-MEANS")})
    assert any("stage 2" in b and "changed" in b for b in blockers)


def test_unrelated_stage_edit_does_not_invalidate():
    q = _researched(target="stage:3.means", disposed_at_key="KEEP")
    # stage 3's means is unchanged; only stage 5 (an unrelated stage) changed.
    blockers = validate_questions([q], stage_keys={
        3: _element_keys("STAGE-3", means="KEEP"),
        5: _element_keys("CHANGED-NOW", means="CHANGED-NOW-MEANS"),
    })
    assert blockers == []


def test_sibling_element_edit_does_not_invalidate():
    """The narrowing this map shape exists for: stage 3's method was rewritten, so the
    whole-stage key moved, but a question answered against its MEANS still stands."""
    q = _researched(target="stage:3.means", disposed_at_key="MEANS-KEY")
    blockers = validate_questions([q], stage_keys={
        3: _element_keys("WHOLE-STAGE-MOVED", means="MEANS-KEY", method="METHOD-MOVED"),
    })
    assert blockers == []


def test_whole_stage_stamp_still_discharges():
    """A key stamped before the map carried per-element entries — no migration can
    rewrite it, so the whole-stage entry must keep discharging it."""
    q = _researched(target="stage:3.means", disposed_at_key="LEGACY-WHOLE-STAGE")
    blockers = validate_questions([q], stage_keys={
        3: _element_keys("LEGACY-WHOLE-STAGE", means="MEANS-KEY"),
    })
    assert blockers == []


def test_element_absent_from_the_map_blocks_rather_than_discharges():
    q = _researched(target="stage:3.means", disposed_at_key="MEANS-KEY")
    blockers = validate_questions([q], stage_keys={3: _element_keys("WHOLE")})
    assert any("stage 3" in b and "changed" in b for b in blockers)


def test_plan_goal_target_exempt_from_key_check():
    q = _researched(target="plan.goal", disposed_at_key="")
    blockers = validate_questions([q], stage_keys={1: _element_keys("some-key")})
    assert blockers == []


def test_empty_stage_keys_skips_binding_checks():
    q = _researched(target="stage:99.means", disposed_at_key="")
    blockers = validate_questions([q], stage_keys={})
    assert blockers == []


def test_empty_bag_does_not_block():
    assert validate_questions([], stage_keys={}) == []


def test_candidate_raised_blocks():
    cand = QuestionCandidate(id="c1", statement="did we consider Z?")
    blockers = validate_question_candidates([cand], [])
    assert any("undispositioned" in b for b in blockers)


def test_round_trip_tolerance():
    raw = [{"id": "q1", "target": "plan.goal", "question": "?"}]
    questions = questions_from_dicts(raw)
    assert questions[0].disposition == "open"
    assert questions[0].own_research == ""
    back = questions_to_dicts(questions)
    assert back[0]["id"] == "q1"
    assert back[0]["disposition"] == "open"


def test_parse_target_forms():
    assert parse_target("plan.goal") == ("goal", None, None)
    assert parse_target("plan.done_criterion") == ("done_criterion", None, None)
    assert parse_target("stage:4.material") == ("stage", 4, "material")
    assert parse_target("stage:4.not_an_element") is None
    assert parse_target("garbage") is None


def test_premise_module_is_pure():
    import ast
    import pathlib

    src = pathlib.Path(premise.__file__).read_text()
    tree = ast.parse(src)
    banned = {"os", "subprocess", "socket", "http", "urllib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned
