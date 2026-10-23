"""Risk bets (phase A, stage 3): the derivation table, the bet-driven validator, the tag rule
that decides which concerns block, and the review depth a bound session owes before approval.

A bound session reads every stage's `[stage.bet]`; an unbound one never does.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from agentctl import gates, plan, review_briefs, risk
from agentctl.config import RISK_PLANNING_ENV
from risk_planning_scenario import Scenario, skeleton_text

RU, ST, DC = risk.REMOVE_UNCERTAINTY, risk.STAGING, risk.DISCRIMINATING_CONTROL
AL, BA = risk.AT_LAUNCH, risk.BEFORE_APPROVE
SK, SG, NB = risk.SKELETON, risk.STAGE, risk.NEIGHBOURS

QUIET = ((), AL, SK)
LIKELY = ((RU,), AL, SG)
SILENT_LIKELY = ((RU, DC), BA, SG)
SILENT_DAMAGING = ((DC,), BA, SG)
HEAVY = ((ST,), BA, NB)
HEAVY_LIKELY = ((RU, ST), BA, NB)
HEAVY_SILENT = ((ST, DC), BA, NB)
HEAVY_SILENT_LIKELY = ((RU, ST, DC), BA, NB)

# (damage, recovery, detection): the result for likelihood low, medium, high.
TABLE = {
    ("low", "cheap", "loud"): (QUIET, LIKELY, LIKELY),
    ("low", "cheap", "silent"): (QUIET, SILENT_LIKELY, SILENT_LIKELY),
    ("low", "costly", "loud"): (QUIET, LIKELY, LIKELY),
    ("low", "costly", "silent"): (QUIET, SILENT_LIKELY, SILENT_LIKELY),
    ("low", "irreversible", "loud"): (HEAVY, HEAVY_LIKELY, HEAVY_LIKELY),
    ("low", "irreversible", "silent"): (HEAVY, HEAVY_SILENT_LIKELY, HEAVY_SILENT_LIKELY),
    ("medium", "cheap", "loud"): (QUIET, LIKELY, LIKELY),
    ("medium", "cheap", "silent"): (SILENT_DAMAGING, SILENT_LIKELY, SILENT_LIKELY),
    ("medium", "costly", "loud"): (QUIET, LIKELY, LIKELY),
    ("medium", "costly", "silent"): (SILENT_DAMAGING, SILENT_LIKELY, SILENT_LIKELY),
    ("medium", "irreversible", "loud"): (HEAVY, HEAVY_LIKELY, HEAVY_LIKELY),
    ("medium", "irreversible", "silent"): (HEAVY_SILENT, HEAVY_SILENT_LIKELY, HEAVY_SILENT_LIKELY),
    ("high", "cheap", "loud"): (HEAVY, HEAVY_LIKELY, HEAVY_LIKELY),
    ("high", "cheap", "silent"): (HEAVY_SILENT, HEAVY_SILENT_LIKELY, HEAVY_SILENT_LIKELY),
    ("high", "costly", "loud"): (HEAVY, HEAVY_LIKELY, HEAVY_LIKELY),
    ("high", "costly", "silent"): (HEAVY_SILENT, HEAVY_SILENT_LIKELY, HEAVY_SILENT_LIKELY),
    ("high", "irreversible", "loud"): (HEAVY, HEAVY_LIKELY, HEAVY_LIKELY),
    ("high", "irreversible", "silent"): (HEAVY_SILENT, HEAVY_SILENT_LIKELY, HEAVY_SILENT_LIKELY),
}
LIKELIHOODS = ("low", "medium", "high")


def derived(elaboration, timing, depth):
    return risk.Derived(elaboration, timing, depth)


def test_table_all_54_combinations():
    assert len(TABLE) * len(LIKELIHOODS) == 54
    for (damage, recovery, detection), row in TABLE.items():
        for likelihood, expected in zip(LIKELIHOODS, row):
            got = risk.derive(likelihood, damage, detection, recovery)
            assert got == derived(*expected), (likelihood, damage, detection, recovery)


def bet(**fields) -> risk.Bet:
    base = dict(difficulty="d", likelihood="low", damage="low", detection="loud", recovery="cheap")
    return risk.parse_bet({**base, **fields})


def test_parse_reads_the_leading_word_and_keeps_the_explanation():
    parsed = bet(likelihood="Medium: new rules", recovery="irreversible once pushed")
    assert (parsed.likelihood, parsed.recovery) == ("medium", "irreversible")


@pytest.mark.parametrize("fields", [{"likelihood": "sometimes"}, {"detection": ""}, {"damage": None}])
def test_parse_refuses_a_level_outside_the_vocabulary(fields):
    with pytest.raises(risk.BetError):
        bet(**fields)


def test_levels_only_raise():
    quiet = risk.effective(bet())
    assert quiet == derived((), AL, SK)
    raised = risk.effective(bet(elaboration=["staging"], review="neighbours"))
    assert raised == derived((ST,), BA, NB)
    lowered = risk.effective(bet(likelihood="high", damage="high", detection="silent",
                                 elaboration=["remove_uncertainty"], review="skeleton"))
    assert lowered == derived((RU, ST, DC), BA, NB)
    assert risk.effective(bet(review="stage")).depth == SG
    assert risk.effective(bet(likelihood="medium", review="skeleton")).depth == SG


TWO_STAGES = skeleton_text(edits=(('phase = "skeleton"\n', ""),))


def with_bets(text: str, bets: dict[int, str], extras: dict[int, str] | None = None) -> str:
    """`text` with a `[stage.bet]` table (and optional extra stage keys) per stage index."""
    extras = extras or {}
    if 1 in extras:
        text = text.replace("depends_on = []\n", "depends_on = []\n" + extras[1], 1)
    if 2 in extras:
        text = text.replace("depends_on = [1]\n", "depends_on = [1]\n" + extras[2], 1)
    if 1 in bets:
        text = text.replace("\n[[stage]]\nindex = 2\n", f"\n[stage.bet]\n{bets[1]}\n\n[[stage]]\nindex = 2\n", 1)
    if 2 in bets:
        text = text.rstrip("\n") + f"\n\n[stage.bet]\n{bets[2]}\n"
    return text


def bet_toml(likelihood="low", damage="low", detection="loud", recovery="cheap", **extra) -> str:
    lines = [f'difficulty = "a risk"', f'likelihood = "{likelihood}"', f'damage = "{damage}"',
             f'detection = "{detection}"', f'recovery = "{recovery}"']
    lines += [f'{k} = "{v}"' for k, v in extra.items()]
    return "\n".join(lines)


QUIET_BET = bet_toml()
SILENT_BET = bet_toml(damage="medium", detection="silent")
WAIVER = 'negative_control_waiver = "no stub reaches this path"\n'
STAGE_TWO_REVIEWED = {1: QUIET_BET, 2: SILENT_BET}


def doc_of(tmp_path: Path, text: str) -> plan.PlanDoc:
    path = tmp_path / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return plan.load_plan(str(path), strict=False)


def test_only_called_fields_are_required(tmp_path):
    quiet = with_bets(TWO_STAGES, {1: QUIET_BET, 2: QUIET_BET})
    assert risk.bet_problems(doc_of(tmp_path, quiet)) == []
    likely_at_launch = with_bets(TWO_STAGES, {1: bet_toml(likelihood="medium"), 2: QUIET_BET})
    assert risk.bet_problems(doc_of(tmp_path, likely_at_launch)) == []
    silent_without_control = with_bets(TWO_STAGES, STAGE_TWO_REVIEWED)
    problems = risk.bet_problems(doc_of(tmp_path, silent_without_control))
    assert len(problems) == 1 and "stage 2" in problems[0] and "negative_control" in problems[0]
    silent_with_waiver = with_bets(TWO_STAGES, STAGE_TWO_REVIEWED, {2: WAIVER})
    assert risk.bet_problems(doc_of(tmp_path, silent_with_waiver)) == []


def test_a_malformed_bet_is_a_problem_not_a_crash(tmp_path):
    text = with_bets(TWO_STAGES, {1: QUIET_BET, 2: 'difficulty = "x"'})
    problems = risk.bet_problems(doc_of(tmp_path, text))
    assert len(problems) == 1 and problems[0].startswith("stage 2:")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


@pytest.fixture
def sc(tmp_path):
    return Scenario(tmp_path)


def sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stage_review(sc: Scenario, path: str, n: int, verdict: str = "pass", concerns=(),
                 sid: str = "sk1") -> dict:
    return sc.eng.run("plan_review", session=sid, verdict=verdict, reviewer="thinker", target=path,
                      plan_digest=sha(path), scope=f"stage:{n}", note="read it",
                      concern=list(concerns) or None)


def freeze(sc: Scenario) -> str:
    path = sc.write(skeleton_text(), "skeleton.toml")
    assert sc.submit(path)["marker"] == "PLAN-READY"
    assert sc.review(path)["ok"] is True
    assert sc.state().skeleton_freeze is not None
    return path


def submit_full(sc: Scenario, text: str, name: str = "full.toml") -> tuple[str, dict]:
    path = sc.write(text, name)
    return path, sc.submit(path)


def bound_plan(sc: Scenario, text: str | None = None) -> str:
    freeze(sc)
    path, d = submit_full(sc, text or with_bets(TWO_STAGES, STAGE_TWO_REVIEWED, {2: WAIVER}))
    assert d["marker"] == "PLAN-READY", d
    return path


def blockers(sc: Scenario, path: str) -> list[str]:
    return gates.plan_review_blockers(sc.state(), path)


def named_stages(found: list[str]) -> set[int]:
    return {int(m.group(1)) for b in found if (m := re.match(r"stage (\d+):", b))}


def test_bound_stage_without_bet_refused(sc):
    freeze(sc)
    _, d = submit_full(sc, with_bets(TWO_STAGES, {1: QUIET_BET}))
    assert d["ok"] is False and d["action"] == "fix_plan"
    assert any("stage 2" in p and "[stage.bet]" in p for p in d["data"]["problems"])


def test_bound_stage_missing_called_field_refused(sc):
    freeze(sc)
    _, d = submit_full(sc, with_bets(TWO_STAGES, STAGE_TWO_REVIEWED))
    assert d["ok"] is False and d["action"] == "fix_plan"
    assert any("stage 2" in p and "negative_control" in p for p in d["data"]["problems"])


def test_missing_freeze_blocks(sc):
    _, d = submit_full(sc, with_bets(TWO_STAGES, {1: QUIET_BET, 2: QUIET_BET}))
    assert d["marker"] == "PLAN-READY", d
    assert any("not frozen" in b for b in blockers(sc, sc.state().plan_path))


def test_bound_blockers_name_unreviewed_before_approve_stages(sc):
    path = bound_plan(sc, with_bets(TWO_STAGES, {1: SILENT_BET, 2: SILENT_BET}, {1: WAIVER, 2: WAIVER}))
    assert named_stages(blockers(sc, path)) == {1, 2}
    stage_review(sc, path, 1)
    assert named_stages(blockers(sc, path)) == {2}
    assert stage_review(sc, path, 2)["ok"] is True
    assert blockers(sc, path) == []


def test_an_at_launch_stage_owes_no_review(sc):
    path = bound_plan(sc, with_bets(TWO_STAGES, {1: QUIET_BET, 2: bet_toml(likelihood="medium")}))
    assert blockers(sc, path) == []


def test_shallower_review_does_not_satisfy_blocker(sc):
    deep = with_bets(TWO_STAGES, {1: QUIET_BET, 2: SILENT_BET + '\nreview = "neighbours"'}, {2: WAIVER})
    shallow = with_bets(TWO_STAGES, STAGE_TWO_REVIEWED, {2: WAIVER})
    for text, owed in ((deep, True), (shallow, False)):
        sid = "deep" if owed else "shallow"
        run = Scenario(sc.eng.tmp / sid)
        run.eng.tmp.mkdir()
        freeze_path = run.write(skeleton_text(), "skeleton.toml")
        run.submit(freeze_path, sid)
        run.review(freeze_path, sid=sid)
        path = run.write(text, "full.toml")
        assert run.submit(path, sid)["marker"] == "PLAN-READY"
        stage_review(run, path, 2, sid=sid)
        assert gates.plan_review_blockers(run.state(sid), path) == []
        state = run.state(sid)
        state.plan_stage_reviews["stage:2"].reviewed_interface_keys = {}
        run.eng.store.save(state)
        found = gates.plan_review_blockers(run.state(sid), path)
        assert (2 in named_stages(found)) is owed, found


TAGGED_STAGE_1 = "blocking: {tag} stage:{n} the bet is understated for what it can hide"
SILENT_PLAN = with_bets(TWO_STAGES, {1: SILENT_BET, 2: QUIET_BET}, {1: WAIVER})


def tagged_revise(sc: Scenario, tag: str) -> list[str]:
    path = bound_plan(sc, SILENT_PLAN)
    assert named_stages(blockers(sc, path)) == {1}
    stage_review(sc, path, 1, "revise", [TAGGED_STAGE_1.format(tag=tag, n=1)])
    return blockers(sc, path)


def test_bet_concern_blocks(sc):
    found = tagged_revise(sc, "[bet:1]")
    assert found, "a [bet:<n>] concern must block"


def test_axis_r_concern_blocks(sc):
    found = tagged_revise(sc, "axis:R")
    assert found, "an axis:R concern must block"


def test_bound_untagged_blocking_concern_is_note(sc):
    assert tagged_revise(sc, "") == []
    assert sc.state().plan_stage_reviews["stage:1"].verdict == "pass"


def test_bound_other_axis_concern_is_note(sc):
    assert tagged_revise(sc, "axis:V") == []


def test_bound_route_never_pairwise(sc):
    path = bound_plan(sc)
    assert gates.pairwise_review_route(sc.state(), plan.load_plan(path)) is False


def test_concern_blocks_reads_only_the_tags():
    assert risk.concern_blocks("axis:R stage:1 x") and risk.concern_blocks("[bet:3] stage:3 x")
    assert not risk.concern_blocks("stage:1 axis: R x")
    assert not risk.concern_blocks("axis:V stage:1 x") and not risk.concern_blocks("plain")
    assert risk.bet_tag("a [bet:12] b") == 12 and risk.bet_tag("none") is None


def test_bound_brief_carries_tag_instruction_and_neighbours(tmp_path):
    doc = doc_of(tmp_path, with_bets(TWO_STAGES, {1: QUIET_BET, 2: QUIET_BET}))
    text = review_briefs.bound_addendum(doc, True, "", risk.NEIGHBOURS, 2)
    assert risk.TAG_INSTRUCTION in text and "[bet:<n>]" in text and "axis:R" in text
    assert "Neighbours of stage 2" in text and "Stage 1: Scaffold module" in text
    shallow = review_briefs.bound_addendum(doc, True, "", risk.STAGE, 2)
    assert risk.TAG_INSTRUCTION in shallow and "Neighbours" not in shallow


def test_unbound_brief_has_no_tag_instruction(tmp_path):
    doc = doc_of(tmp_path, TWO_STAGES)
    assert review_briefs.bound_addendum(doc, False, "", risk.NEIGHBOURS, 2) == ""
    assert review_briefs.bound_addendum(doc, None, "", risk.NEIGHBOURS, 2) == ""
    assert risk.tag_instruction(False) == ""
    skeleton = doc_of(tmp_path, skeleton_text())
    assert review_briefs.bound_addendum(skeleton, True, "skeleton", risk.NEIGHBOURS, 2) == ""
    assert risk.TAG_INSTRUCTION not in review_briefs.skeleton_brief(skeleton)


@pytest.fixture
def unbound(sc, monkeypatch):
    monkeypatch.setenv(RISK_PLANNING_ENV, "0")
    return sc


def unbound_blockers(sc: Scenario, concern: str) -> list[str]:
    path = sc.write(TWO_STAGES, "full.toml")
    assert sc.submit(path)["marker"] == "PLAN-READY"
    sc.review(path, "revise", concerns=(concern,))
    return blockers(sc, path)


def test_unbound_untagged_blocking_concern_still_blocks(unbound):
    assert unbound_blockers(unbound, "blocking: stage:1 the controls are weak")


@pytest.mark.parametrize("tag", ["axis:R", "axis:V", "[bet:1]"])
def test_unbound_tagged_concern_unchanged(unbound, tag):
    plain = "stage:1 the controls are weak"
    tagged = f"{tag} {plain}"
    assert gates._concern_part_token(tagged) == gates._concern_part_token(plain) == "stage:1"
    assert gates._concern_part_token("C1: " + tagged, after_marker=True) == "stage:1"
    assert plan.parse_concern("blocking: " + tagged).severity == plan.parse_concern("blocking: " + plain).severity
    assert unbound_blockers(unbound, "blocking: " + tagged)
