from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANCHOR = "engine state, session transcript (literal markers), VCS, plan fields"
PLANNER = ROOT / "skills/specializations/planner"
THINKER = ROOT / "skills/specializations/thinker/SKILL.md"


def test_anchor_in_planner_policy():
    text = (PLANNER / "policy.md").read_text()
    assert "## Decidability of escapes, warnings and judges" in text
    assert ANCHOR in text


def test_anchor_in_thinker_skill():
    assert ANCHOR in THINKER.read_text()


def test_planner_skill_points_to_section():
    text = (PLANNER / "SKILL.md").read_text()
    assert "#decidability-of-escapes-warnings-and-judges" in text
