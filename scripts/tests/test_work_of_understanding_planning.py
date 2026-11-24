"""Understanding and planning as work: the phase a difficulty is met in, and the effort of understanding.

A difficulty carries the phase of the work it was met in (understanding, planning, a stage), on the
engine's record and on the memory leaf that records it. A leaf of a type of order may also carry the
estimate of understanding an order of that type and the actuals met. These cases pin the one source
of the phase names, the record round trip, the three leaf commands (`new`, `extend`, `search`), the
leaf verifier's refusals, and the effort block, including a regularity leaf that carries the block.

The mutant `met_in_dropped` (order_agreement_mutants.py) loses the phase at every seam; the cases
that go red under it are the ones driving the record, the leaf frontmatter and the search.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import (
    cli, cli_common, cli_difficulty, cli_final, cli_order, cli_replan, cli_session, cost, delivery_channel, effort,
    effort_segments, gates, runtime_host, sub_plan, understanding_pricing)
from agentctl import cli_plan
from agentctl.config import Thresholds
from agentctl.dispatch import RunResult
from agentctl.state import EFFORT_FIRE_FINISHED_WORK, Declaration, Difficulty, Node, SessionState
from agentctl.state import estimate_correction_marker
from agentctl.sub_plan import Estimate
from conftest import patch_cli_name
from lib import difficulty_phase, markdown_sections, understanding_effort
from order_agreement_mutants import mutant, script_module  # noqa: F401  (autouse fixture)
from test_effort_fire_diagnosing import Eng as PlanningEng
from test_effort_fire_diagnosing import StageEng, _RealStageEng, _stamp_raised_fire
from test_effort_per_stage_judging import executing_overspent, seed_replans, stage_arming
from test_effort_planning_segment import OVERSPENT_USD
from test_stage_estimates import _user_choice
from test_order_agreement import (  # noqa: F401  (home, ostore and armed_env are fixtures)
    SESSION, _agree, _classify, _confirm_delivery, _marker_of, _plan, _user_answers, _wire_answer_hook, armed_env,
    home, ostore)
from test_order_agreement import _start as _start_session
from agentctl.plan import ORDER_COST_MARKER, load_order_draft, understanding_cost_line
from agentctl.render import order_cost_block_lines
from test_order_phase import (
    OLD_RENDERING, SIDES, _bind_question, _draft, _present, _rendering_naming, _review, _submit, ns)

RE = script_module("record-experience.py")
VEL = script_module("verify-experience-leaf.py")
VLS = script_module("verify-leaf-structure.py")

DATE = "2026-11-23"
GOOD_COST = "1.00 USD"


@pytest.fixture(autouse=True)
def _isolated_ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-log.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))


def _experience_dir(project: Path) -> Path:
    return project / ".claude" / "agent-memory" / "experience"


def _new(project: Path, slug: str, difficulty: str, *met_in: str) -> Path:
    argv = ["new", "--scope", "project", "--project-dir", str(project), "--date", DATE,
            "--slug", slug, "--title", slug, "--description", difficulty,
            "--confirmed-by", "user", "--difficulty", difficulty, "--order", "o",
            "--criterion", "c", "--context-where", "W", "--plan", "P", "--cost", GOOD_COST]
    for phase in met_in:
        argv += ["--met-in", phase]
    assert RE.main(argv) == 0
    return _experience_dir(project) / f"{DATE}-{slug}.md"


def _search(project: Path, keywords: str, capsys, *met_in: str) -> str:
    capsys.readouterr()
    argv = ["search", "--scope", "project", "--project-dir", str(project)]
    for phase in met_in:
        argv += ["--met-in", phase]
    assert RE.main([*argv, keywords]) == 0
    return capsys.readouterr().out


def _met_in_line(leaf: Path) -> str | None:
    found = re.search(r"^met_in:.*$", leaf.read_text(encoding="utf-8"), re.MULTILINE)
    return found.group(0) if found else None


def _errors(leaf: Path) -> str | None:
    return VEL.check_content(leaf.read_text(encoding="utf-8"))


def test_difficulty_met_in_round_trip():
    record = Difficulty(met_in="planning")
    loaded = Difficulty.from_dict(dataclasses.asdict(record))
    assert loaded.met_in == "planning"


@pytest.mark.parametrize("phase", difficulty_phase.PHASES)
def test_every_phase_survives_a_load(phase):
    assert Difficulty.from_dict(dataclasses.asdict(Difficulty(met_in=phase))).met_in == phase


def _diagnosing() -> SessionState:
    return SessionState(session_id="s", task_id="t", node=Node.DIAGNOSING.value)


def test_open_difficulty_records_the_phase_through_save_and_load():
    state = _diagnosing()
    cli_common._open_difficulty(state, met_in="planning")
    assert state.difficulty.met_in == "planning"
    assert SessionState.from_json(state.to_json()).difficulty.met_in == "planning"


def test_open_difficulty_without_a_phase_records_none():
    state = _diagnosing()
    cli_common._open_difficulty(state)
    assert state.difficulty.met_in is None


def test_open_difficulty_refuses_an_unknown_phase_and_opens_nothing():
    state = _diagnosing()
    with pytest.raises(ValueError, match="understanding, planning, stage"):
        cli_common._open_difficulty(state, met_in="brainstorming")
    assert state.difficulty is None


def test_a_release_supersede_keeps_the_phase_of_the_replaced_record():
    state = _diagnosing()
    cli_common._open_difficulty(state, declaration=Declaration("e", "a", "m"), met_in="understanding")
    cli_difficulty._supersede_for_release(state)
    assert state.difficulty.declaration is None
    assert state.difficulty.met_in == "understanding"


def test_difficulty_unknown_met_in_loads_as_none():
    raw = dataclasses.asdict(Difficulty())
    raw["met_in"] = "brainstorming"
    assert Difficulty.from_dict(raw).met_in is None


def test_old_difficulty_without_met_in_loads():
    raw = dataclasses.asdict(Difficulty(origin="stage"))
    del raw["met_in"]
    assert Difficulty.from_dict(raw).met_in is None


def test_new_writes_met_in(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice", "understanding", "planning")
    assert _met_in_line(leaf) == "met_in: [understanding, planning]"
    assert _errors(leaf) is None


def test_new_without_met_in_writes_no_line(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    assert _met_in_line(leaf) is None


def test_extend_adds_phase(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice", "understanding")
    assert RE.main(["extend", "--leaf", str(leaf), "--date", DATE, "--context-label", "second",
                    "--context-where", "W", "--plan", "P", "--met-in", "stage"]) == 0
    assert _met_in_line(leaf) == "met_in: [understanding, stage]"
    assert _errors(leaf) is None


def test_extend_adds_a_phase_to_an_untagged_leaf(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    assert RE.main(["extend", "--leaf", str(leaf), "--date", DATE, "--context-label", "second",
                    "--context-where", "W", "--plan", "P", "--met-in", "planning"]) == 0
    assert _met_in_line(leaf) == "met_in: [planning]"


def test_ticket_leaf_writes_met_in(tmp_path):
    assert RE.main(["ticket", "--scope", "project", "--project-dir", str(tmp_path), "--date", DATE,
                    "--slug", "thin", "--title", "thin", "--description", "a thin pointer",
                    "--confirmed-by", "user", "--difficulty", "d", "--order", "o", "--criterion", "c",
                    "--context-where", "W", "--plan", "P", "--ticket", "ABC-1",
                    "--met-in", "planning"]) == 0
    leaf = _experience_dir(tmp_path) / f"{DATE}-thin.md"
    assert _met_in_line(leaf) == "met_in: [planning]"
    assert _errors(leaf) is None


def test_search_names_the_tiers_that_ignore_met_in(capsys):
    capsys.readouterr()
    assert RE.main(["search", "--tier", "regularities", "--met-in", "planning", "overrun"]) == 0
    assert "--met-in narrows the experience tier only" in capsys.readouterr().err


def test_search_filters_by_phase(tmp_path, capsys):
    _new(tmp_path, "alpha", "overrun of the estimate in understanding", "understanding")
    _new(tmp_path, "beta", "overrun of the estimate in planning", "planning")
    out = _search(tmp_path, "overrun estimate", capsys, "understanding")
    assert "alpha" in out
    assert "beta" not in out


def test_search_without_phase_lists_every_leaf(tmp_path, capsys):
    _new(tmp_path, "alpha", "overrun of the estimate in understanding", "understanding")
    _new(tmp_path, "beta", "overrun of the estimate in planning", "planning")
    out = _search(tmp_path, "overrun estimate", capsys)
    assert "alpha" in out
    assert "beta" in out


def test_search_ranks_named_phase_first(tmp_path, capsys):
    _new(tmp_path, "plain", "overrun overrun overrun of the estimate estimate estimate")
    _new(tmp_path, "tagged", "overrun of the estimate", "understanding")
    out = _search(tmp_path, "overrun estimate", capsys, "understanding")
    assert out.index("tagged") < out.index("plain")


def test_old_leaf_without_met_in_found_and_passes(tmp_path, capsys):
    leaf = _new(tmp_path, "alpha", "overrun of the estimate in understanding")
    assert _met_in_line(leaf) is None
    assert _errors(leaf) is None
    out = _search(tmp_path, "overrun estimate", capsys, "understanding")
    assert "alpha" in out


def test_invalid_met_in_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    text = leaf.read_text(encoding="utf-8").replace("last_verified:", "met_in: [brainstorming]\nlast_verified:", 1)
    leaf.write_text(text, encoding="utf-8")
    assert "brainstorming" in _errors(leaf)


def test_invalid_met_in_refused_names_a_valid_phase(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    text = leaf.read_text(encoding="utf-8").replace("last_verified:", "met_in: [brainstorming]\nlast_verified:", 1)
    leaf.write_text(text, encoding="utf-8")
    message = _errors(leaf)
    assert all(phase in message for phase in difficulty_phase.PHASES)


def test_invalid_met_in_refused_on_a_ticket_leaf(tmp_path):
    text = ("---\nname: t\ndescription: d\ntype: reference\nschema: difficulty/v1\n"
            "resolution_confirmed_by_user: \"ok\"\nticket: ABC-1\nmet_in: [nowhere]\n"
            "generalized_into: []\ncreated: 2026-11-23\nlast_verified: 2026-11-23\n---\n\n# t\nABC-1\n")
    assert "nowhere" in VEL.check_content(text)


def test_met_in_helpers_treat_an_untagged_value_as_matching_every_phase():
    assert difficulty_phase.matches(None, "planning")
    assert difficulty_phase.matches("[stage]", None)
    assert not difficulty_phase.matches("[stage]", "planning")
    assert difficulty_phase.matches("[stage, planning]", "planning")


def test_section_span_ends_at_the_next_heading_and_matches_a_whole_word():
    text = "# t\n\n## Costs\nx\n\n## Cost\nbody\n\n## Plan\nc\n"
    start, end = markdown_sections.section_span(text, "Cost")
    assert text[start:end] == "## Cost\nbody\n\n"
    assert markdown_sections.section_span(text, "Absent") is None
    assert markdown_sections.section_span(text, "Plan") == (text.index("## Plan"), len(text))
    suffixed = "# t\n\n## Cost notes\nx\n"
    assert markdown_sections.section_span(suffixed, "Cost") is not None
    assert markdown_sections.section_span(suffixed, "Cost", exact=True) is None


def test_the_leaf_command_and_the_effort_block_find_a_section_with_one_helper():
    text = "# t\n\n## Understanding effort\nx\n\n## Cost\nc\n"
    assert RE.section_span(text, "Understanding effort") == understanding_effort.section_span(text)
    assert understanding_effort.section_span(text) == (text.index("## Understanding"), text.index("## Cost"))


def test_the_effort_block_is_found_by_its_exact_heading_only():
    text = "# t\n\n## Understanding effort notes\nx\n\n## Cost\nc\n"
    assert understanding_effort.section_span(text) is None
    assert understanding_effort.estimate_of(text) is None
    assert markdown_sections.section_span(text, "Understanding effort") is not None
    assert markdown_sections.section_span(text, "Understanding effort", exact=True) is None
    assert markdown_sections.section_span("## Cost  \nb\n", "Cost", exact=True) == (0, len("## Cost  \nb\n"))


def test_a_notes_section_does_not_hide_the_block_after_it():
    block = understanding_effort.render({"estimate": {"minutes": 3.0, "author": "agent"}})
    text = "# t\n\n## Understanding effort notes\nprose\n\n" + block
    assert understanding_effort.estimate_of(text) == {"minutes": 3.0, "author": "agent"}
    rewritten = understanding_effort.set_estimate(text, author="agent", minutes=4.0)
    assert "## Understanding effort notes\nprose" in rewritten
    assert understanding_effort.estimate_of(rewritten)["minutes"] == 4.0


def test_block_parse_render_round_trip():
    block = {"estimate": {"minutes": 12.0, "usd": 1.5, "author": "agent", "confirmer": "user"},
             "actuals": [{"minutes": 20.0, "usd": 2.0, "session": "s1", "date": DATE}]}
    rendered = understanding_effort.render(block)
    assert understanding_effort.parse(rendered) == block
    assert understanding_effort.problems(rendered) == []


def _estimate(text: str, minutes=10, usd=1, **kw) -> str:
    return understanding_effort.set_estimate(text, author="agent", minutes=minutes, usd=usd, **kw)


def _actual(text: str, minutes=14, usd=1.2, session="s1") -> str:
    return understanding_effort.add_actual(text, session=session, date=DATE, minutes=minutes, usd=usd)


def test_add_actual_appends_and_keeps_estimate():
    text = _estimate("# leaf\n\n## Cost\n1 USD\n", confirmer="user")
    text = _actual(text, 14, 1.2, "s1")
    text = _actual(text, 9, 0.8, "s2")
    assert understanding_effort.estimate_of(text) == {
        "minutes": 10, "usd": 1, "author": "agent", "confirmer": "user"}
    assert [row["session"] for row in understanding_effort.parse(text)["actuals"]] == ["s1", "s2"]
    assert text.count(understanding_effort.SECTION) == 1
    assert understanding_effort.problems(text) == []


@pytest.mark.parametrize("scale", [{"minutes": 12}, {"usd": 1.5}])
def test_estimate_and_actual_on_one_scale_only_render_parse_and_pass(scale):
    text = understanding_effort.set_estimate("# leaf\n", author="agent", confirmer="user", **scale)
    text = understanding_effort.add_actual(text, session="s1", date=DATE, **scale)
    block = understanding_effort.parse(text)
    assert set(block["estimate"]) == {*scale, "author", "confirmer"}
    assert set(block["actuals"][0]) == {*scale, "session", "date"}
    assert understanding_effort.problems(text) == []


def test_estimate_without_confirmer_is_recorded_as_unconfirmed():
    text = _estimate("# leaf\n")
    assert "confirmer" not in understanding_effort.estimate_of(text)
    assert understanding_effort.problems(text) == []


def test_confirming_an_estimate_later_replaces_it_in_place():
    text = _actual(_estimate("# leaf\n"))
    text = _estimate(text, minutes=12, confirmer="user")
    assert understanding_effort.estimate_of(text)["confirmer"] == "user"
    assert len(understanding_effort.parse(text)["actuals"]) == 1


def test_set_estimate_replaces_in_place_and_keeps_later_sections():
    text = _estimate("# leaf\n\n## Cost\n1 USD\n", confirmer="user")
    text += "\n## Self-critique of the agent system\nlater\n"
    text = _estimate(text, minutes=20, usd=2, confirmer="user")
    assert understanding_effort.estimate_of(text)["minutes"] == 20
    assert text.count(understanding_effort.SECTION) == 1
    assert "## Self-critique of the agent system\nlater" in text


def test_estimate_of_a_leaf_without_the_block_is_none():
    assert understanding_effort.estimate_of("# leaf\n\n## Cost\n1 USD\n") is None
    assert understanding_effort.problems("# leaf\n") == []


@pytest.mark.parametrize("body", [
    "## Understanding effort\n\nno fence here\n",
    "## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\n```\n",
    "## Understanding effort\n\n```toml\n[estimate]\nminutes = -1\nusd = 1\nauthor = \"a\"\nconfirmer = \"b\"\n```\n",
    "## Understanding effort\n\n```toml\n[estimate]\nminutes = nan\nauthor = \"a\"\n```\n",
    "## Understanding effort\n\n```toml\n[estimate]\nusd = inf\nauthor = \"a\"\n```\n",
    "## Understanding effort\n\n```toml\n[estimate]\nauthor = \"a\"\nconfirmer = \"b\"\n```\n",
    "## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\nauthor = \"a\"\nconfirmer = \"\"\n```\n",
    "## Understanding effort\n\n```toml\n[[actuals]]\nminutes = 1\nusd = 1\nsession = \"\"\ndate = \"d\"\n```\n",
    "## Understanding effort\n\n```toml\n[[actuals]]\nsession = \"s\"\ndate = \"d\"\n```\n",
    "## Understanding effort\n\n```toml\nnot = = toml\n```\n",
    "## Understanding effort\n\n```toml\n```\n",
])
def test_malformed_block_refused(body):
    assert understanding_effort.problems("# leaf\n\n" + body)


def test_a_missing_scale_is_named():
    out = understanding_effort.problems(
        "## Understanding effort\n\n```toml\n[estimate]\nauthor = \"a\"\n```\n")
    assert out == ["estimate holds neither minutes nor usd"]


def test_malformed_block_refused_on_a_difficulty_leaf(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    leaf.write_text(leaf.read_text(encoding="utf-8")
                    + "\n## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\n```\n",
                    encoding="utf-8")
    assert "estimate lacks author" in _errors(leaf)


def _effort(leaf: Path, *argv: str) -> int:
    return RE.main(["understanding-effort", "--leaf", str(leaf), "--date", DATE, *argv])


def test_understanding_effort_command_records_estimate_then_actual(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    scale = ["--minutes", "10", "--usd", "1"]
    assert _effort(leaf, *scale, "--kind", "estimate", "--author", "agent", "--confirmer", "user") == 0
    assert _effort(leaf, *scale, "--kind", "actual", "--actual-session", "s1") == 0
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert block["estimate"]["author"] == "agent"
    assert block["actuals"][0]["session"] == "s1"
    assert _errors(leaf) is None


def test_understanding_effort_command_records_one_scale_and_an_unconfirmed_estimate(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    assert _effort(leaf, "--minutes", "10", "--kind", "estimate", "--author", "agent") == 0
    assert _effort(leaf, "--usd", "0.5", "--kind", "actual", "--actual-session", "s1") == 0
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert block["estimate"] == {"minutes": 10.0, "author": "agent"}
    assert block["actuals"] == [{"usd": 0.5, "session": "s1", "date": DATE}]
    assert _errors(leaf) is None


def _refused(leaf: Path, needle: str, *argv: str) -> None:
    before = leaf.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _effort(leaf, *argv)
    assert needle in str(exc.value)
    assert leaf.read_text(encoding="utf-8") == before


def test_understanding_effort_command_without_either_scale_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    _refused(leaf, "--minutes", "--kind", "estimate", "--author", "agent")
    assert _effort(leaf, "--minutes", "10", "--kind", "estimate", "--author", "agent") == 0
    assert _errors(leaf) is None


@pytest.mark.parametrize("figure", [["--minutes", "-3"], ["--usd", "nan"], ["--minutes", "inf"]])
def test_understanding_effort_command_refuses_a_figure_that_is_not_a_finite_non_negative_number(
        tmp_path, figure):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    _refused(leaf, "refusing to write", "--kind", "estimate", "--author", "agent", *figure)


def test_understanding_effort_command_refuses_a_leaf_whose_block_is_malformed_already(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    clean = leaf.read_text(encoding="utf-8")
    leaf.write_text(clean + "\n## Understanding effort\n\n```toml\nnot = = toml\n```\n", encoding="utf-8")
    actual = ["--kind", "actual", "--actual-session", "s1", "--minutes", "5"]
    _refused(leaf, "repair it by hand", *actual)
    leaf.write_text(clean, encoding="utf-8")
    assert _effort(leaf, *actual) == 0
    assert _errors(leaf) is None


def test_understanding_effort_estimate_without_author_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    _refused(leaf, "--author", "--kind", "estimate", "--minutes", "10", "--usd", "1")
    assert _effort(leaf, "--kind", "estimate", "--minutes", "10", "--usd", "1", "--author", "agent") == 0
    assert _errors(leaf) is None


def test_understanding_effort_actual_without_session_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    _refused(leaf, "--actual-session", "--kind", "actual", "--minutes", "10", "--usd", "1")
    assert _effort(leaf, "--kind", "actual", "--minutes", "10", "--usd", "1", "--actual-session", "s1") == 0
    assert _errors(leaf) is None


def test_regularity_record_with_block_passes():
    leaf = (
        "---\nname: reg\ndescription: an order of this type is read slowly\ntype: reference\n"
        "schema: regularity/v1\ncreated: 2026-11-23\nlast_verified: 2026-11-23\n---\n\n"
        "# reg\n\n## Regularity\nOrders of this type take long to understand.\n\n"
        "## Generality\nOrders that change a gate.\n\n"
        "## Refutation\nAn order of this type read quickly.\n\n"
        "## Induced from\n- [[some-leaf]]\n"
    )
    with_block = _actual(_estimate(leaf, 30, 3, confirmer="user"), 41, 4.2)
    assert understanding_effort.estimate_of(with_block)["minutes"] == 30
    assert VLS.check_content(with_block, "memory-global/leaves/regularities/reg.md") is None


@pytest.mark.parametrize("body, key", [
    ("[estimate]\nminutes = 5\nauthor = \"a\"\nnote = \"x\"\n", "note"),
    ("[[actuals]]\nminutes = 5\nsession = \"s\"\ndate = \"d\"\nauthor = \"a\"\n", "author"),
    ("[estimate]\nminutes = 5\nauthor = \"a\"\n[extra]\nk = 1\n", "extra"),
])
def test_a_key_outside_the_block_schema_is_refused_by_name(body, key):
    out = understanding_effort.problems("## Understanding effort\n\n```toml\n" + body + "```\n")
    assert any(key in line and "does not take" in line for line in out)


def test_the_leaf_verifier_refuses_a_block_that_render_would_silently_trim(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    leaf.write_text(leaf.read_text(encoding="utf-8")
                    + "\n## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\nauthor = \"a\"\nnote = \"x\"\n```\n",
                    encoding="utf-8")
    assert "note" in _errors(leaf)


def test_an_empty_confirmer_is_no_confirmer(tmp_path):
    text = understanding_effort.set_estimate("# leaf\n", author="agent", minutes=5, confirmer="")
    assert "confirmer" not in understanding_effort.estimate_of(text)
    assert understanding_effort.problems(text) == []
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    assert _effort(leaf, "--kind", "estimate", "--minutes", "5", "--author", "agent", "--confirmer", "") == 0
    assert "confirmer" not in understanding_effort.estimate_of(leaf.read_text(encoding="utf-8"))
    assert _errors(leaf) is None


def test_an_unknown_phase_is_refused_before_the_depth_cap_is_looked_at(monkeypatch):
    monkeypatch.setattr(cli_common, "_difficulty_level_cap", lambda: 0)
    state = _diagnosing()
    with pytest.raises(ValueError, match="understanding, planning, stage"):
        cli_common._open_difficulty(state, met_in="brainstorming")
    assert cli_common._open_difficulty(state, met_in="planning") is None
    assert state.difficulty is None


@pytest.mark.parametrize("kind, stray", [
    ("actual", ["--author", "agent"]),
    ("actual", ["--confirmer", "user"]),
    ("estimate", ["--actual-session", "s1"]),
])
def test_a_flag_of_the_other_kind_is_refused_and_names_the_way_on(tmp_path, kind, stray):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    own = ["--actual-session", "s1"] if kind == "actual" else ["--author", "agent"]
    _refused(leaf, "belongs to the other kind", "--kind", kind, "--minutes", "5", *own, *stray)
    assert _effort(leaf, "--kind", kind, "--minutes", "5", *own) == 0
    assert _errors(leaf) is None


def test_confirming_an_estimate_with_one_scale_keeps_the_other_and_drops_the_old_confirmer():
    text = understanding_effort.set_estimate("# leaf\n", author="agent", minutes=10, usd=1.5, confirmer="user")
    text = understanding_effort.set_estimate(text, author="agent", minutes=12)
    assert understanding_effort.estimate_of(text) == {"minutes": 12, "usd": 1.5, "author": "agent"}
    text = understanding_effort.set_estimate(text, author="agent", usd=2.0, confirmer="user")
    assert understanding_effort.estimate_of(text) == {
        "minutes": 12, "usd": 2.0, "author": "agent", "confirmer": "user"}


# --- the effort of understanding an order ---------------------------------------------

TYPE_REF = "a-child-process-reads-its-parents-ambient-root"
USD, MINUTES = effort_segments.SCALE_USD, effort_segments.SCALE_MINUTES
EXEMPT_ENVIRONMENTS = {
    "host": lambda mp: mp.setattr(runtime_host, "detect_host", lambda: None),
    "hook": lambda mp: _wire_answer_hook(mp, wired=False),
    "spawned": lambda mp: mp.setenv("AGENT_RECURSION_DEPTH", "1"),
    "channel": lambda mp: mp.setattr(delivery_channel, "load_command", lambda: "deliver --somewhere"),
}


def _default() -> dict:
    return effort.understanding_default(Thresholds())


def _overrun(budget_usd: float) -> float:
    """The smallest whole-dollar spend that fires against `budget_usd`: past the multiple and the floor."""
    margin = effort_segments.fire_margin(Thresholds())
    return float(int(max(margin.multiple * budget_usd, budget_usd + margin.floor[USD])) + 1)


def _type_leaf(tmp_path, monkeypatch, text: str = "# a type of order\n", ref: str = "a-type") -> Path:
    root = tmp_path / "leaves"
    (root / "regularities").mkdir(parents=True, exist_ok=True)
    leaf = root / "regularities" / f"{ref}.md"
    leaf.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli_order, "LEAVES_ROOT", root)
    return leaf


def _order_draft(tmp_path, ref: str = "a-type", **kw) -> Path:
    return _draft(tmp_path, sides=SIDES.replace(TYPE_REF, ref), **kw)


def _with_estimate(minutes=None, usd=None, author="the typing agent", confirmer=None) -> str:
    return understanding_effort.set_estimate("# a type of order\n", author=author, minutes=minutes, usd=usd,
                                             confirmer=confirmer)


def _understanding(store):
    state = store.load(SESSION)
    return next((a for a in reversed(state.effort_segments.get("armings") or ())
                 if a["segment"] == effort_segments.UNDERSTANDING), None)


def _classified(store, tmp_path, monkeypatch, text: str = "# a type of order\n"):
    """A classified, armed session, the type record holding `text`, and an order draft naming it."""
    leaf = _type_leaf(tmp_path, monkeypatch, text)
    _start_session(store)
    assert _classify(store).ok
    return leaf, _order_draft(tmp_path)


def _book(store, usd: float = 0.0, minutes: float = 0.0) -> None:
    state = store.load(SESSION)
    effort_segments.book(state.effort_segments, time.time(), usd, minutes, "test")
    store.save(state)


def _agreed_order(store, tmp_path, path: Path) -> None:
    assert _review(store).ok
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1
    done = _agree(store)
    assert done.ok, done.detail


def _line(store, segment=effort_segments.UNDERSTANDING) -> dict:
    disclosure = cli_common._effort_disclosure(store.load(SESSION))
    return next(line for line in disclosure["lines"] if line["segment"] == segment)


def test_understanding_segment_carries_budget_source_author(ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch)
    arming = _understanding(ostore)
    assert arming is not None
    assert arming["budget"] == _default()
    assert (arming["budget_source"], arming["author"]) == ("configured-default", "configured default")
    assert [a["segment"] for a in ostore.load(SESSION).effort_segments["armings"]][:2] == [
        effort_segments.PLANNING, effort_segments.UNDERSTANDING]

    leaf.write_text(_with_estimate(minutes=40, usd=3.5), encoding="utf-8")
    assert _submit(ostore, path).ok
    arming = _understanding(ostore)
    assert arming["budget"] == {USD: 3.5, MINUTES: 40.0}
    assert (arming["budget_source"], arming["author"]) == ("type-record", "the typing agent")
    assert arming["pricing"]["type"]["ref"] == "a-type"
    assert arming["pricing"]["scale_sources"] == {USD: "type-record", MINUTES: "type-record"}
    line = _line(ostore)
    assert (line["author"], line["budget_source"], line["kind"]) == ("the typing agent", "type-record", "understanding")
    assert line["budget"] == {USD: 3.5, MINUTES: 40.0}


def test_a_scale_the_record_does_not_carry_keeps_the_default(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(usd=2.5))
    assert _submit(ostore, path).ok
    arming = _understanding(ostore)
    assert arming["budget"] == {USD: 2.5, MINUTES: _default()[MINUTES]}
    assert arming["pricing"]["scale_sources"] == {USD: "type-record", MINUTES: "configured-default"}


def test_default_budget_named_as_such(ostore, armed_env, tmp_path, monkeypatch):
    _classified(ostore, tmp_path, monkeypatch)
    line = _line(ostore)
    assert (line["budget_source"], line["author"]) == ("configured-default", "configured default")
    assert line["budget"] == _default()
    assert "configured default prices it" in line["pricing"]["reason"]
    assert line["work_item"]["estimate"] == {
        "budget": _default(), "author": "configured default", "source": "configured-default"}


@pytest.mark.parametrize("text, why", [
    ("# a type of order\n", "carries no understanding estimate"),
    ("# a type of order\n\n## Understanding effort\n\n```toml\n[estimate]\nminutes = 0\nauthor = \"a\"\n```\n",
     "carries no positive figure"),
    ("# a type of order\n\n## Understanding effort\n\n```toml\n[estimate]\nauthor = \"a\"\n```\n",
     "holds neither minutes nor usd"),
    ("# a type of order\n\n## Understanding effort\n\n```toml\n[estimate]\nminutes = \n```\n", "is malformed"),
    ("# a type of order\n\n## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\nauthor = \"a\"\n"
     "stray = 1\n```\n", "is malformed"),
    ("# a type of order\n\n## Understanding effort\n\nno fence here\n", "is malformed"),
])
def test_order_submit_prices_at_the_default_when_the_record_cannot_price_it(
        ostore, armed_env, tmp_path, monkeypatch, text, why):
    _, path = _classified(ostore, tmp_path, monkeypatch, text)
    submitted = _submit(ostore, path)
    assert submitted.ok, submitted.detail
    arming = _understanding(ostore)
    assert arming["budget"] == _default() and arming["budget_source"] == "configured-default"
    assert arming["author"] == "configured default"
    assert why in arming["pricing"]["reason"] and "configured default prices it" in arming["pricing"]["reason"]


def test_an_unreadable_record_prices_at_the_default(tmp_path):
    priced = understanding_pricing.price(_default(), {"kind": "regularity", "ref": "gone"}, tmp_path / "gone.md")
    assert priced.budget == _default() and "cannot be read" in priced.reason
    assert priced.type["ref"] == "gone"


def test_an_entry_naming_no_record_prices_at_the_default():
    for entry, path in ((None, None), ({"kind": "regularity", "ref": "x"}, None)):
        priced = understanding_pricing.price(_default(), entry, path)
        assert priced.budget == _default() and priced.type is None and "names no record" in priced.reason


def test_understanding_estimate_beside_order(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.5, minutes=2)
    disclosure = cli_common._effort_disclosure(ostore.load(SESSION))
    assert [line["segment"] for line in disclosure["lines"]][:2] == [
        effort_segments.UNDERSTANDING, effort_segments.PLANNING]
    item = _line(ostore)["work_item"]
    assert item["estimate"]["budget"] == {USD: 3.0, MINUTES: 30.0}
    assert item["actual"][USD] == pytest.approx(0.5) and item["actual"][MINUTES] >= 2


def test_work_items_carry_customer_product_check(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    customer = cli_common._order_draft_customer_id(ostore.load(SESSION))
    assert customer
    for segment, product, check in (
            (effort_segments.UNDERSTANDING, "agreed order", "order-agree recorded"),
            (effort_segments.PLANNING, "approved plan", "approve recorded")):
        item = _line(ostore, segment)["work_item"]
        assert (item["customer"], item["product"], item["acceptance_check"]) == (customer, product, check)
        assert set(item["estimate"]) == {"budget", "author", "source"} and "actual" in item
    stage = effort_segments.STAGE_PREFIX + "s1"
    opened = effort_segments.open_arming({}, stage, {USD: 1.0, MINUTES: 1.0}, "agent-declared", 0.0, "t")
    assert "work_item" not in cli_common._effort_line(stage, [opened])


def _present_as_written(store, tmp_path, text):
    """`present-plan --kind order` on `text` exactly as written; unlike `_present` it adds nothing to
    the cost section, and it leaves the binding-conditions question to the caller."""
    rendering = tmp_path / "as-written.txt"
    rendering.write_text(text, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session=SESSION, kind="order", rendering_file=str(rendering), emit_skeleton=False, plan=None),
        store=store)


def _written_without_the_estimate(path: Path) -> str:
    return _rendering_naming(load_order_draft(path)[0])


def test_presentation_gate_requires_understanding_estimate(ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    submitted = _submit(ostore, path)
    assert submitted.ok
    assert _review(ostore).ok
    _bind_question(ostore)
    line = understanding_cost_line(30.0, 3.0, "type-record", "the typing agent")
    assert gates.understanding_estimate_line(ostore.load(SESSION)) == line
    written = _written_without_the_estimate(path)
    assert line not in written and ORDER_COST_MARKER in written

    refused = _present_as_written(ostore, tmp_path, written)
    assert not refused.ok
    assert any(line in problem and "understanding estimate" in problem for problem in refused.data["problems"])
    assert ostore.load(SESSION).plan_presentations == []

    block = submitted.data["order_cost_block"]
    assert block == order_cost_block_lines(load_order_draft(path)[0].meta.order, line)
    registered = _present_as_written(
        ostore, tmp_path, written.split(ORDER_COST_MARKER)[0] + "\n".join(block) + "\n")
    assert registered.ok, registered.detail
    state = ostore.load(SESSION)
    assert gates.order_presentation_stale_reason(state, state.plan_presentations[-1]) is None

    leaf.write_text(_with_estimate(minutes=45, usd=4.0), encoding="utf-8")
    assert _submit(ostore, path).ok
    state = ostore.load(SESSION)
    stale = gates.order_presentation_stale_reason(state, state.plan_presentations[-1])
    assert stale is not None and gates.understanding_estimate_line(state) in stale


def test_the_understanding_estimate_in_the_orders_text_is_refused(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    assert _review(ostore).ok
    _bind_question(ostore)
    line = gates.understanding_estimate_line(ostore.load(SESSION))
    written = _written_without_the_estimate(path)
    inside = written.replace(f"\n{ORDER_COST_MARKER}", f"{line}\n\n{ORDER_COST_MARKER}", 1)
    refused = _present_as_written(ostore, tmp_path, inside)
    assert not refused.ok
    assert any("inside the order's text" in problem for problem in refused.data["problems"])


def test_a_session_with_no_understanding_estimate_or_an_old_form_order_is_not_asked_for_one(
        ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    assert _review(ostore).ok
    _bind_question(ostore)
    state = ostore.load(SESSION)
    state.effort_segments["armings"] = [
        a for a in state.effort_segments["armings"] if a["segment"] != effort_segments.UNDERSTANDING]
    ostore.save(state)
    assert gates.understanding_estimate_line(ostore.load(SESSION)) is None
    assert _present_as_written(ostore, tmp_path, _written_without_the_estimate(path)).ok

    old = _draft(tmp_path, omit=("customer_side",), name="old.toml")
    doc = load_order_draft(old)[0]
    assert gates.order_rendering_problems(ostore.load(SESSION), doc, OLD_RENDERING) == []
    assert not any("understanding estimate" in problem
                   for problem in gates.order_rendering_problems(ostore.load(SESSION), doc, OLD_RENDERING))


def test_the_cost_block_names_the_estimate_only_when_there_is_one(tmp_path):
    order = load_order_draft(_order_draft(tmp_path))[0].meta.order
    without = order_cost_block_lines(order)
    with_line = order_cost_block_lines(order, "- the estimate line")
    assert without[0] == ORDER_COST_MARKER and "- the estimate line" not in without
    assert with_line == [with_line[0], with_line[1], "- the estimate line", *without[2:]]


def test_author_values_named():
    author = effort_segments.arming_author
    assert author({"segment": "understanding", "budget_source": "configured-default",
                   "author": "configured default"}) == "configured default"
    assert author({"segment": "planning", "budget_source": "fallback"}) == "configured default"
    assert author({"segment": "stage:s1", "budget_source": "agent-declared"}) == "tier price"
    assert author({"segment": "planning", "budget_source": "agreed-estimate"}) == "agreed estimate"
    assert understanding_pricing.price(_default(), None, None).author == "configured default"


def test_the_type_records_author_is_the_authors_figure(tmp_path):
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=9, author="session-7", confirmer="the customer"), encoding="utf-8")
    priced = understanding_pricing.price(_default(), {"kind": "regularity", "ref": "t"}, leaf)
    assert (priced.author, priced.confirmer, priced.source) == ("session-7", "the customer", "type-record")


def test_the_confirmer_mapping_goes_both_ways():
    unconfirmed = understanding_pricing.to_estimate({"minutes": 5, "usd": 1.0, "author": "a"})
    assert unconfirmed.confirmer == "" and not unconfirmed.agreed
    assert "confirmer" not in understanding_pricing.to_row(unconfirmed)
    confirmed = understanding_pricing.to_estimate({"minutes": 5, "author": "a", "confirmer": "u"})
    assert confirmed.agreed and understanding_pricing.to_row(confirmed)["confirmer"] == "u"
    assert understanding_pricing.to_row(Estimate(minutes=5, usd=None, author="a")) == {"minutes": 5, "author": "a"}
    from_actual = understanding_pricing.estimate_from_actual({USD: 0.0, MINUTES: 7.0}, "s9")
    assert (from_actual.author, from_actual.confirmer, from_actual.usd) == ("s9", "", None)


def test_resubmission_with_a_changed_type_entry_reprices_and_keeps_the_actual(
        ostore, armed_env, tmp_path, monkeypatch):
    _type_leaf(tmp_path, monkeypatch, _with_estimate(usd=3.0), ref="first")
    _type_leaf(tmp_path, monkeypatch, _with_estimate(usd=9.0), ref="second")
    _start_session(ostore)
    assert _classify(ostore).ok
    assert _submit(ostore, _order_draft(tmp_path, ref="first")).ok
    assert _understanding(ostore)["budget"][USD] == 3.0
    _book(ostore, usd=0.7)
    state = ostore.load(SESSION)
    state.effort_segments["armings"][-1]["fire"] = {"scales": ["usd"], "ack": None, "ts": 1.0}
    ostore.save(state)

    assert _submit(ostore, _order_draft(tmp_path, ref="second", name="two.toml")).ok
    arming = _understanding(ostore)
    assert arming["budget"][USD] == 9.0 and arming["pricing"]["type"]["ref"] == "second"
    assert effort_segments.arming_actual(arming)[USD] == pytest.approx(0.7)
    assert arming["fire"] == {"scales": ["usd"], "ack": None, "ts": 1.0}


def test_a_revision_after_agreement_changes_neither_the_pricing_type_nor_the_actual(
        ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(usd=3.0))
    _type_leaf(tmp_path, monkeypatch, _with_estimate(usd=9.0), ref="other")
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.4)
    _agreed_order(ostore, tmp_path, path)
    before = json.dumps(_understanding(ostore), sort_keys=True)
    assert _submit(ostore, _order_draft(tmp_path, ref="other", name="rev.toml")).ok
    assert json.dumps(_understanding(ostore), sort_keys=True) == before
    assert _understanding(ostore)["closed_at"] is not None


def test_the_understanding_arming_closes_at_agreement_and_planning_resumes(
        ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    _agreed_order(ostore, tmp_path, path)
    ledger = ostore.load(SESSION).effort_segments
    assert effort_segments.open_arming_of(ledger, effort_segments.UNDERSTANDING) is None
    assert effort_segments.current_arming(ledger)["segment"] == effort_segments.PLANNING


def test_spend_fire_after_repricing(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=_default()[MINUTES] * 4))
    _book(ostore, usd=_overrun(_default()[USD]))
    submitted = _submit(ostore, path)
    assert submitted.ok, submitted.detail
    arming = _understanding(ostore)
    assert arming["budget"] == {USD: _default()[USD], MINUTES: _default()[MINUTES] * 4}
    assert arming["pricing"]["scale_sources"] == {USD: "configured-default", MINUTES: "type-record"}
    state = ostore.load(SESSION)
    assert [fire["scale"] for fire in state.effort_fires] == [effort.SCALE_SPEND]
    assert arming["fire"]["scales"] == [USD]
    assert effort_segments.arming_actual(arming)[MINUTES] < arming["budget"][MINUTES]


def test_a_spend_only_record_reprices_the_spend_scale_and_a_fire_follows_the_new_budget(
        ostore, armed_env, tmp_path, monkeypatch):
    record_usd = _default()[USD] * 4 + 1
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(usd=record_usd))
    _book(ostore, usd=_overrun(_default()[USD]))
    assert _submit(ostore, path).ok
    assert ostore.load(SESSION).effort_fires == [] and _understanding(ostore)["fire"] is None

    _book(ostore, usd=_overrun(record_usd))
    assert _submit(ostore, path).ok
    assert [fire["scale"] for fire in ostore.load(SESSION).effort_fires] == [effort.SCALE_SPEND]
    assert _understanding(ostore)["fire"]["scales"] == [USD]


def test_an_order_submit_that_raises_a_fire_says_so_in_its_directive(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    quiet = _submit(ostore, path)
    assert quiet.ok and "effort_fire" not in quiet.data and "effort-divergence" not in quiet.detail
    _book(ostore, usd=_overrun(_default()[USD]))
    fired = _submit(ostore, path)
    assert fired.ok, fired.detail
    assert "raised an effort-divergence fire on understanding the order" in fired.detail
    assert fired.data["effort_fire"]["scales"] == [USD]
    assert fired.data["effort_fire"]["arming_id"] == _understanding(ostore)["arming_id"]


def test_understanding_overrun_yields_fire_record(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    _book(ostore, usd=_overrun(_default()[USD]))
    submitted = _submit(ostore, path)
    assert submitted.ok, submitted.detail
    state = ostore.load(SESSION)
    assert [fire["scale"] for fire in state.effort_fires] == [effort.SCALE_SPEND]
    assert state.effort_fires[0].get("ack") is None
    arming = _understanding(ostore)
    assert arming["fire"]["scales"] == [USD] and arming["budget_source"] == "configured-default"


def test_understanding_is_judged_at_submit_review_and_agreement_only():
    assert effort_segments.UNDERSTANDING_JUDGING_ACTS == (
        effort_segments.ACT_ORDER_SUBMIT, effort_segments.ACT_ORDER_REVIEW, effort_segments.ACT_ORDER_AGREE)
    assert effort_segments.judging_acts_of(effort_segments.UNDERSTANDING) == \
        effort_segments.UNDERSTANDING_JUDGING_ACTS
    assert not effort_segments.replans_judged_at(effort_segments.UNDERSTANDING, effort_segments.ACT_ORDER_SUBMIT)


def test_an_order_review_is_judged_as_the_act_of_understanding(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    _book(ostore, usd=_overrun(_default()[USD]))
    assert _review(ostore).ok
    assert [fire["scale"] for fire in ostore.load(SESSION).effort_fires] == [effort.SCALE_SPEND]


def test_an_order_review_while_planning_is_current_is_judged_as_a_plan_review(
        ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    state = ostore.load(SESSION)
    understanding = effort_segments.open_arming_of(state.effort_segments, effort_segments.UNDERSTANDING)
    effort_segments.close_arming(state.effort_segments, understanding["arming_id"],
                                 effort_segments.ACT_ORDER_AGREE, time.time(), 5.0)
    ostore.save(state)
    _book(ostore, usd=1000.0)
    assert _review(ostore).ok
    assert [fire["scale"] for fire in ostore.load(SESSION).effort_fires] == [effort.SCALE_SPEND]


def test_actual_goes_to_pricing_type(ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    written = cli_final._write_understanding_actual(ostore.load(SESSION), None)
    assert written == {"written": ["actual", "estimate"], "skipped": None, "retry": None, "already_there": False,
                       "replaced_confirmed": None}
    text = leaf.read_text(encoding="utf-8")
    block = understanding_effort.parse(text)
    (row,) = block["actuals"]
    assert row["session"] == SESSION and row["usd"] == pytest.approx(0.6) and row["minutes"] >= 4
    assert block["estimate"]["author"] == SESSION and block["estimate"]["usd"] == pytest.approx(0.6)
    assert understanding_effort.problems(text) == []


def test_an_estimate_taken_from_an_actual_is_authored_by_the_session_and_confirmed_by_nobody(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    assert cli_final._write_understanding_actual(ostore.load(SESSION), None)["written"] == ["actual", "estimate"]
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert block["estimate"]["author"] == SESSION and "confirmer" not in block["estimate"]
    assert understanding_pricing.to_estimate(block["estimate"]).confirmer == ""
    assert len(block["actuals"]) == 1


def test_a_failed_write_back_is_reported_not_raised(ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch)
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6)
    _agreed_order(ostore, tmp_path, path)

    def refusing(argv):
        return SimpleNamespace(returncode=2, stdout="", stderr="the leaf is refused")

    def raising(argv):
        raise OSError("no disk")

    state = ostore.load(SESSION)
    refused = cli_final._write_understanding_actual(state, refusing)
    assert "the actual was not written" in refused["skipped"]
    assert state.effort_segments["understanding_actual"] == refused
    assert "the write failed" in cli_final._write_understanding_actual(state, raising)["skipped"]
    leaf.unlink()
    assert "cannot be read" in cli_final._write_understanding_actual(state, None)["skipped"]


def _closed(**kw) -> dict:
    base = {"segment": "understanding", "carried": {USD: 0.0, MINUTES: 0.0}, "booked": {USD: 0.0, MINUTES: 0.0},
            "closed_at": 1.0}
    return {**base, **kw}


def test_write_back_names_why_nothing_was_written(tmp_path, monkeypatch):
    assert "names no record" in understanding_pricing.write_back(
        [_closed(pricing={"type": None})], session="s")["skipped"]
    leaf = tmp_path / "t.md"
    leaf.write_text("# t\n", encoding="utf-8")
    typed = _closed(pricing={"type": {"kind": "regularity", "ref": "t", "path": str(leaf)}})
    assert "nothing was measured" in understanding_pricing.write_back([typed], session="s")["skipped"]
    typed["booked"] = {USD: 1.0, MINUTES: 0.0}
    monkeypatch.setenv("CLAUDE_DRY_RUN", "1")
    assert "dry run" in understanding_pricing.write_back([typed], session="s")["skipped"]
    assert leaf.read_text(encoding="utf-8") == "# t\n"


def test_a_write_that_did_not_land_is_printed_as_the_command_that_repeats_it(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    calls = []

    def second_write_refused(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=2 if len(calls) == 2 else 0, stdout="", stderr="busy")

    outcome = cli_final._write_understanding_actual(ostore.load(SESSION), second_write_refused)
    assert outcome["written"] == ["actual"] and "the estimate from the actual was not written" in outcome["skipped"]
    command = outcome["retry"]
    actual_command, estimate_command = command.split(" && ")
    for part in (actual_command, estimate_command):
        assert part.startswith("python3 ") and "record-experience.py understanding-effort" in part
        assert f"--leaf {leaf}" in part and f"--session {SESSION}" in part
    assert "--kind actual" in actual_command and "--kind estimate" in estimate_command
    assert "--replace" in estimate_command
    assert estimate_command.split()[3:] == [str(part) for part in calls[1][3:]]


def _run_printed(command: str) -> None:
    """Run the printed repeat command the way a shell would: each `&&` part through the verb."""
    import shlex
    for part in command.split(" && "):
        argv = shlex.split(part)
        assert RE.main(argv[argv.index("understanding-effort"):]) == 0


def _first_call_lands_second_refused(calls):
    def runner(argv):
        calls.append(argv)
        if len(calls) == 2:
            return SimpleNamespace(returncode=2, stdout="", stderr="busy")
        return understanding_pricing.subprocess_runner(argv)
    return runner


def test_the_printed_repeat_after_a_failed_actual_repairs_the_actual_and_the_estimate(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0, confirmer="user"))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    before = leaf.read_text(encoding="utf-8")

    outcome = cli_final._write_understanding_actual(
        ostore.load(SESSION), lambda argv: SimpleNamespace(returncode=2, stdout="", stderr="busy"))
    assert outcome["written"] == [] and "the actual was not written" in outcome["skipped"]
    assert leaf.read_text(encoding="utf-8") == before
    _run_printed(outcome["retry"])
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert row["session"] == SESSION and row["usd"] == pytest.approx(0.6)
    assert block["estimate"]["author"] == SESSION and block["estimate"]["usd"] == pytest.approx(0.6)
    assert "confirmer" not in block["estimate"]


def test_the_printed_repeat_after_a_partial_success_leaves_one_actual_row(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    calls = []

    outcome = understanding_pricing.write_back(
        [a for a in ostore.load(SESSION).effort_segments["armings"] if a["segment"] == effort_segments.UNDERSTANDING],
        session=SESSION, runner=_first_call_lands_second_refused(calls))
    assert outcome["written"] == ["actual"] and outcome["retry"]
    assert len(understanding_effort.parse(leaf.read_text(encoding="utf-8"))["actuals"]) == 1
    _run_printed(outcome["retry"])
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert row["session"] == SESSION
    assert block["estimate"]["author"] == SESSION and block["estimate"]["usd"] == pytest.approx(0.6)


def test_the_verb_adds_no_second_actual_row_for_the_same_session_and_figures(tmp_path, capsys):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    actual = ["--kind", "actual", "--actual-session", "s1", "--minutes", "5"]
    assert _effort(leaf, *actual) == 0
    after_first = leaf.read_text(encoding="utf-8")
    capsys.readouterr()
    assert _effort(leaf, *actual) == 0
    assert "already recorded" in capsys.readouterr().out
    assert leaf.read_text(encoding="utf-8") == after_first
    assert _effort(leaf, "--kind", "actual", "--actual-session", "s2", "--minutes", "5") == 0
    assert [row["session"] for row in understanding_effort.parse(leaf.read_text(encoding="utf-8"))["actuals"]] == [
        "s1", "s2"]


def test_the_verb_adds_a_row_for_a_session_already_recorded_whose_figures_differ(tmp_path):
    leaf = _new(tmp_path, "alpha", "one session resolved two tasks of the type")
    assert _effort(leaf, "--kind", "actual", "--actual-session", "s1", "--minutes", "12") == 0
    assert _effort(leaf, "--kind", "actual", "--actual-session", "s1", "--minutes", "40", "--usd", "1.5") == 0
    assert _effort(leaf, "--kind", "actual", "--actual-session", "s1", "--minutes", "40") == 0
    rows = understanding_effort.parse(leaf.read_text(encoding="utf-8"))["actuals"]
    assert [(row["session"], row["minutes"], row.get("usd")) for row in rows] == [
        ("s1", 12.0, None), ("s1", 40.0, 1.5), ("s1", 40.0, None)]


def test_two_tasks_of_one_session_resolved_on_one_type_leave_two_actual_rows(tmp_path):
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=30, usd=3.0), encoding="utf-8")
    pricing = {"type": {"kind": "regularity", "ref": "t", "path": str(leaf)}}
    first_task = _closed(pricing=pricing, booked={USD: 0.5, MINUTES: 12.0})
    second_task = _closed(pricing=pricing, booked={USD: 1.5, MINUTES: 40.0})

    first = understanding_pricing.write_back([first_task], session="s")
    second = understanding_pricing.write_back([second_task], session="s")
    assert first["already_there"] is False and second["already_there"] is False
    assert second["written"] == ["actual", "estimate"]
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert [(row["session"], row["minutes"], row["usd"]) for row in block["actuals"]] == [
        ("s", 12.0, 0.5), ("s", 40.0, 1.5)]
    assert (block["estimate"]["minutes"], block["estimate"]["usd"]) == (40.0, 1.5)

    again = understanding_pricing.write_back([second_task], session="s")
    assert again["already_there"] is True
    assert len(understanding_effort.parse(leaf.read_text(encoding="utf-8"))["actuals"]) == 2


def test_the_write_back_repeated_after_the_record_was_written_leaves_one_actual_row_and_says_so(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    state = ostore.load(SESSION)
    first = cli_final._write_understanding_actual(state, None)
    assert first["written"] == ["actual", "estimate"] and first["already_there"] is False
    # the state was not saved after the first resolve, so the repeat starts from the same ledger
    repeated = cli_final._write_understanding_actual(ostore.load(SESSION), None)
    assert repeated["written"] == ["actual", "estimate"] and repeated["already_there"] is True
    assert repeated["skipped"] is None
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert [row["session"] for row in block["actuals"]] == [SESSION]
    assert block["estimate"]["usd"] == pytest.approx(0.6)


def test_a_confirmed_estimate_is_replaced_by_the_actual_without_crediting_what_was_not_measured(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch,
                             _with_estimate(minutes=30, usd=3.0, author="the typing agent", confirmer="the customer"))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.0, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    outcome = cli_final._write_understanding_actual(ostore.load(SESSION), None)
    assert outcome["written"] == ["actual", "estimate"]
    assert outcome["replaced_confirmed"] == {"confirmer": "the customer", "minutes": 30.0, "usd": 3.0}
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert block["estimate"]["author"] == SESSION
    assert "confirmer" not in block["estimate"] and "usd" not in block["estimate"]
    assert block["estimate"]["minutes"] >= 4
    assert understanding_effort.problems(leaf.read_text(encoding="utf-8")) == []


def test_an_estimate_replaced_keeps_only_the_scales_given_and_a_kept_one_otherwise(tmp_path):
    text = understanding_effort.set_estimate("# leaf\n", author="agent", minutes=10, usd=1.5, confirmer="user")
    replaced = understanding_effort.set_estimate(text, author="s", minutes=4, keep_held=False)
    assert understanding_effort.estimate_of(replaced) == {"minutes": 4, "author": "s"}
    assert understanding_effort.estimate_of(
        understanding_effort.set_estimate(text, author="s", minutes=4))["usd"] == 1.5
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    assert _effort(leaf, "--kind", "estimate", "--minutes", "10", "--usd", "2", "--author", "a") == 0
    assert _effort(leaf, "--kind", "estimate", "--minutes", "4", "--author", "s", "--replace") == 0
    assert understanding_effort.estimate_of(leaf.read_text(encoding="utf-8")) == {"minutes": 4, "author": "s"}
    _refused(leaf, "belongs to the other kind", "--kind", "actual", "--minutes", "5", "--actual-session", "s1",
             "--replace")


def test_a_session_whose_last_arming_names_no_record_writes_nothing_and_says_so(tmp_path):
    leaf = tmp_path / "t.md"
    leaf.write_text("# t\n", encoding="utf-8")
    typed = _closed(pricing={"type": {"kind": "regularity", "ref": "t", "path": str(leaf)}},
                    booked={USD: 1.0, MINUTES: 1.0})
    untyped = _closed(pricing={"type": None}, booked={USD: 0.5, MINUTES: 1.0})
    outcome = understanding_pricing.write_back([typed, untyped], session="s")
    assert outcome["written"] == [] and "names no record" in outcome["skipped"]
    assert leaf.read_text(encoding="utf-8") == "# t\n"
    assert "names no record" in understanding_pricing.write_back([], session="s")["skipped"]




def test_resolve_prints_the_command_that_repeats_a_write_that_did_not_land(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    from test_quality_row_task_type import _drive_to_resolved
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=30, usd=3.0), encoding="utf-8")
    monkeypatch.setattr(cli_plan, "_ensure_understanding_arming", _closed_understanding_at_classify(leaf))
    monkeypatch.setattr(understanding_pricing, "subprocess_runner",
                        lambda argv: SimpleNamespace(returncode=2, stdout="", stderr="no route"))

    done = _drive_to_resolved(store, "wb-3", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert done.ok, done.detail
    retry = done.data["understanding_actual"]["retry"]
    assert f"repeat it with: {retry}" in done.detail and "--kind actual" in retry
    assert "run it before the next resolve of this type: it replaces the type record's estimate" in done.detail
    stored = store.load("wb-3").effort_segments["understanding_actual"]
    assert stored == done.data["understanding_actual"]


def test_a_session_resolved_with_its_order_never_agreed_writes_no_actual_and_says_so(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    before = leaf.read_text(encoding="utf-8")
    state = ostore.load(SESSION)
    assert _understanding(ostore)["closed_at"] is None

    outcome = cli_final._write_understanding_actual(state, None)
    assert outcome["written"] == [] and "still open" in outcome["skipped"] and outcome["retry"] is None
    assert state.effort_segments["understanding_actual"] == outcome
    assert leaf.read_text(encoding="utf-8") == before


def test_a_reset_into_a_small_change_discards_the_open_arming_with_the_old_state(
        ostore, armed_env, tmp_path, monkeypatch):
    leaf, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.6, minutes=4)
    before = leaf.read_text(encoding="utf-8")
    reset = cli_session.cmd_reset(SimpleNamespace(
        session=SESSION, task="small-one", goal="g", done_criterion="dc", criterion_type="measurable",
        recursion_depth=0, force=True), store=ostore)
    assert reset.ok, reset.detail
    assert _classify(ostore, changed_lines=5, files=1, wall_clock_min=3, architectural=False).ok
    assert _understanding(ostore) is None
    assert cli_final._write_understanding_actual(ostore.load(SESSION), None) is None
    assert leaf.read_text(encoding="utf-8") == before


def test_the_next_order_of_a_type_is_priced_from_the_latest_actual_a_resolved_session_wrote(
        ostore, armed_env, tmp_path, monkeypatch):
    project = tmp_path / "project"
    record = _new(project, "reading-an-order", "an order of this type was read")
    monkeypatch.setattr(cli_order, "LEAVES_ROOT", project / ".claude" / "agent-memory")
    ref = record.stem
    sides = SIDES.replace(TYPE_REF, ref).replace('kind = "regularity"', 'kind = "experience"')
    path = _draft(tmp_path, sides=sides)

    _start_session(ostore)
    assert _classify(ostore).ok
    assert _submit(ostore, path).ok
    assert _understanding(ostore)["budget"] == _default()
    _book(ostore, usd=0.6, minutes=4)
    _agreed_order(ostore, tmp_path, path)
    assert cli_final._write_understanding_actual(ostore.load(SESSION), None)["written"] == ["actual", "estimate"]
    block = understanding_effort.parse(record.read_text(encoding="utf-8"))
    (first_actual,) = block["actuals"]
    assert block["estimate"]["usd"] == pytest.approx(0.6) and block["estimate"]["author"] == SESSION

    _start_session(ostore, session="second-session")
    assert _classify(ostore, session="second-session").ok
    assert _submit(ostore, path, session="second-session").ok
    arming = next(a for a in ostore.load("second-session").effort_segments["armings"]
                  if a["segment"] == effort_segments.UNDERSTANDING)
    assert arming["budget"] == {USD: pytest.approx(0.6), MINUTES: pytest.approx(first_actual["minutes"])}
    assert (arming["budget_source"], arming["author"]) == ("type-record", SESSION)
    assert arming["pricing"]["type"]["path"] == str(record)

    _book_for(ostore, "second-session", usd=0.2, minutes=1)
    arming = next(a for a in ostore.load("second-session").effort_segments["armings"]
                  if a["segment"] == effort_segments.UNDERSTANDING)
    state = ostore.load("second-session")
    effort_segments.close_arming(state.effort_segments, arming["arming_id"], effort_segments.ACT_ORDER_AGREE,
                                 time.time(), 5.0)
    assert understanding_pricing.write_back(
        [a for a in state.effort_segments["armings"] if a["segment"] == effort_segments.UNDERSTANDING],
        session="second-session")["written"] == ["actual", "estimate"]
    block = understanding_effort.parse(record.read_text(encoding="utf-8"))
    assert len(block["actuals"]) == 2
    assert block["estimate"]["author"] == "second-session" and block["estimate"]["usd"] == pytest.approx(0.2)


def _book_for(store, session: str, usd: float = 0.0, minutes: float = 0.0) -> None:
    state = store.load(session)
    effort_segments.book(state.effort_segments, time.time(), usd, minutes, "test")
    store.save(state)


def test_a_renewed_understanding_arming_does_not_lose_its_actual_at_write_back(tmp_path, monkeypatch):
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=30, usd=3.0), encoding="utf-8")
    type_ref = {"kind": "regularity", "ref": "t", "path": str(leaf)}
    before = _closed(pricing={"type": type_ref}, booked={USD: 0.6, MINUTES: 4.0})
    after = _closed(pricing={"type": type_ref}, booked={USD: 0.3, MINUTES: 2.0})
    outcome = understanding_pricing.write_back([before, after], session="s9")
    assert outcome["written"] == ["actual", "estimate"]
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert (row["usd"], row["minutes"]) == (pytest.approx(0.9), pytest.approx(6.0))
    assert block["estimate"]["usd"] == pytest.approx(0.9)


def test_an_arming_still_open_is_not_written_as_a_final_actual(tmp_path):
    leaf = tmp_path / "t.md"
    leaf.write_text("# t\n", encoding="utf-8")
    open_one = _closed(pricing={"type": {"kind": "regularity", "ref": "t", "path": str(leaf)}},
                       booked={USD: 1.0, MINUTES: 1.0}, closed_at=None)
    outcome = understanding_pricing.write_back([open_one], session="s")
    assert outcome["written"] == [] and "still open" in outcome["skipped"] and outcome["retry"] is None
    assert leaf.read_text(encoding="utf-8") == "# t\n"


def test_a_session_with_no_understanding_arming_writes_nothing(ostore, armed_env):
    _start_session(ostore)
    assert cli_final._write_understanding_actual(ostore.load(SESSION), None) is None


def test_old_state_files_without_a_segment_load_and_price_nothing(ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch)
    state = ostore.load(SESSION)
    state.effort_segments["armings"] = [a for a in state.effort_segments["armings"]
                                        if a["segment"] != effort_segments.UNDERSTANDING]
    ostore.save(state)
    assert _submit(ostore, path).ok
    assert _understanding(ostore) is None
    assert cli_common._effort_disclosure(ostore.load(SESSION)) is not None


@pytest.mark.parametrize("flags", [
    {"chat": True},
    {"changed_lines": 5, "files": 1, "wall_clock_min": 3, "architectural": False},
], ids=["chat", "small-change"])
def test_chat_and_small_change_open_no_understanding_arming(ostore, armed_env, flags):
    _start_session(ostore)
    assert _classify(ostore, **flags).ok
    assert _understanding(ostore) is None


@pytest.mark.parametrize("environment", [*EXEMPT_ENVIRONMENTS, "linked"])
def test_a_session_that_never_agrees_an_order_opens_no_understanding_arming(
        ostore, armed_env, monkeypatch, environment):
    if environment == "linked":
        _start_session(ostore)
        state = ostore.load(SESSION)
        state.parent_link = {"parent_session": "p", "stage_index": 1, "depth": 1, "norm_digest": "x"}
        ostore.save(state)
    else:
        EXEMPT_ENVIRONMENTS[environment](monkeypatch)
        _start_session(ostore)
    assert _classify(ostore).ok
    assert ostore.load(SESSION).order_agreement_armed is False
    assert _understanding(ostore) is None


def test_a_second_classify_opens_no_second_understanding_arming(ostore, armed_env):
    _start_session(ostore)
    assert _classify(ostore).ok
    state = ostore.load(SESSION)
    cli_common._ensure_understanding_arming(state, time.time())
    assert sum(a["segment"] == effort_segments.UNDERSTANDING for a in state.effort_segments["armings"]) == 1


def test_understanding_is_not_carried_across_a_session_link(ostore, armed_env):
    _start_session(ostore)
    assert _classify(ostore).ok
    payload = cli_common._effort_identity_payload(ostore.load(SESSION))
    assert effort_segments.UNDERSTANDING not in json.dumps(payload, default=str)


def _closed_understanding_at_classify(leaf: Path):
    def opener(state, now):
        arming = effort_segments.open_arming(
            state.effort_segments, effort_segments.UNDERSTANDING, _default(), "type-record", now, "test")
        arming["pricing"] = {"type": {"kind": "regularity", "ref": "t", "path": str(leaf)}}
        arming["booked"] = {USD: 0.8, MINUTES: 6.0}
        arming["closed_at"] = now
    return opener


def test_resolve_writes_the_understanding_actual_to_the_pricing_type(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    from test_quality_row_task_type import _drive_to_resolved
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=30, usd=3.0), encoding="utf-8")
    monkeypatch.setattr(cli_plan, "_ensure_understanding_arming", _closed_understanding_at_classify(leaf))

    done = _drive_to_resolved(store, "wb-1", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert done.ok, done.detail
    assert done.data["understanding_actual"] == {
        "written": ["actual", "estimate"], "skipped": None, "retry": None, "already_there": False,
        "replaced_confirmed": None}
    assert store.load("wb-1").effort_segments["understanding_actual"] == done.data["understanding_actual"]
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert (row["session"], row["usd"], row["minutes"]) == ("wb-1", 0.8, 6.0)
    assert (block["estimate"]["usd"], block["estimate"]["minutes"], block["estimate"]["author"]) == (0.8, 6.0, "wb-1")


def test_resolve_says_a_confirmed_estimate_was_replaced_and_whose_confirmation_it_carried(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    from test_quality_row_task_type import _drive_to_resolved
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    leaf = tmp_path / "t.md"
    leaf.write_text(_with_estimate(minutes=30, usd=3.0, confirmer="the customer"), encoding="utf-8")
    monkeypatch.setattr(cli_plan, "_ensure_understanding_arming", _closed_understanding_at_classify(leaf))

    done = _drive_to_resolved(store, "wb-5", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert done.ok, done.detail
    replaced = done.data["understanding_actual"]["replaced_confirmed"]
    assert replaced == {"confirmer": "the customer", "minutes": 30.0, "usd": 3.0}
    assert "confirmed by the customer" in done.detail and "was replaced by this session's actual" in done.detail
    assert store.load("wb-5").effort_segments["understanding_actual"]["replaced_confirmed"] == replaced


def test_resolve_says_the_actual_was_on_the_record_already_and_adds_no_second_row(
        store, venued_fixtures_dir, tmp_path, monkeypatch):
    from test_quality_row_task_type import _drive_to_resolved
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    leaf = tmp_path / "t.md"
    leaf.write_text(understanding_effort.add_actual(_with_estimate(minutes=30, usd=3.0), session="wb-6", date=DATE,
                                                    usd=0.8, minutes=6.0), encoding="utf-8")
    monkeypatch.setattr(cli_plan, "_ensure_understanding_arming", _closed_understanding_at_classify(leaf))

    done = _drive_to_resolved(store, "wb-6", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert done.ok, done.detail
    assert done.data["understanding_actual"]["already_there"] is True
    assert "on the type record already; no second row was added" in done.detail
    assert [row["session"] for row in understanding_effort.parse(leaf.read_text(encoding="utf-8"))["actuals"]] == [
        "wb-6"]


def test_resolve_of_a_session_without_an_understanding_arming_writes_nothing(
        store, venued_fixtures_dir, monkeypatch):
    from test_quality_row_task_type import _drive_to_resolved
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    done = _drive_to_resolved(store, "wb-2", str(venued_fixtures_dir / "plan_two_stage.toml"))
    assert done.ok and "understanding_actual" not in done.data


def _fired_understanding(store, tmp_path, monkeypatch, record: str, usd: float = 3.0):
    """A classified session whose order, typed by a record, has overrun the understanding budget."""
    _, path = _classified(store, tmp_path, monkeypatch, record)
    _book(store, usd=_overrun(usd))
    submitted = _submit(store, path)
    assert submitted.ok, submitted.detail
    assert _understanding(store)["fire"] is not None
    return path


@pytest.mark.parametrize("decision", ["continue", "revise"])
def test_a_user_acknowledging_an_understanding_fire_renews_the_arming(
        ostore, armed_env, tmp_path, monkeypatch, decision):
    _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    first = _understanding(ostore)
    acknowledged = cli_replan.cmd_fire_acknowledge(
        SimpleNamespace(session=SESSION, by="user", decision=decision, note="go on"), store=ostore)
    assert acknowledged.ok, acknowledged.detail
    state = ostore.load(SESSION)
    renewed = effort_segments.open_arming_of(state.effort_segments, effort_segments.UNDERSTANDING)
    assert renewed is not None and renewed["arming_id"] != first["arming_id"]
    assert renewed["budget"] == {USD: 3.0, MINUTES: 30.0}
    assert (renewed["budget_source"], renewed["author"]) == ("type-record", "the typing agent")
    assert renewed["pricing"] == first["pricing"]
    assert renewed["fire"] is None
    assert effort_segments.arming_actual(renewed) == {USD: 0.0, MINUTES: 0.0}
    assert state.effort_fires[-1]["ack"]["decision"] == decision


def _difficulty_cycle(store, factor: str = "an order read without its type's record") -> None:
    """Declare, investigate, critique and normalize the open difficulty record (the cycle a fire opens)."""
    assert cli_difficulty.cmd_declare(
        SimpleNamespace(session=SESSION, expected="the order agreed within its estimate",
                        actual="the understanding ran past its estimate", mismatch="the type record was not read"),
        store=store).ok
    assert cli_difficulty.cmd_investigate(
        SimpleNamespace(session=SESSION, localized_expectation="the record names the requirements",
                        localized_actual="the requirements were found by asking", hypotheses=["h one", "h two"]),
        store=store).ok
    assert cli_difficulty.cmd_critique(
        SimpleNamespace(session=SESSION, functional_ground="the type record is the order's functional place",
                        replanning_task="read the record before the draft", failure_address="resource",
                        cause_category="implementation_defect", machinery_induced="false", mechanism="none",
                        functional_place="verification",
                        differences_to_remove=["the record is read before the draft is written"]), store=store).ok
    assert cli_difficulty.cmd_normalize(
        SimpleNamespace(session=SESSION, factor=factor, level="note"), store=store).ok


def _checks_pass(argv, cwd=None, *, timeout=None):
    """Every check passes and every negative control, the command `false`, fails as it should."""
    return RunResult(1 if argv[-1].strip().endswith("false") else 0, "", "")


def _agent_acknowledges(store, decision: str = "revise"):
    return cli_replan.cmd_fire_acknowledge(
        SimpleNamespace(session=SESSION, by="agent", decision=decision, note="the cycle is closed"), store=store)


def _planned_and_one_stage_passed(store, tmp_path) -> None:
    """From an agreed order: a plan submitted, reviewed and approved, and its one stage passed."""
    from conftest import STAGE_OBSERVATIONS
    plan = _plan(tmp_path)
    assert cli.cmd_plan(SimpleNamespace(session=SESSION), store=store).ok
    submitted = cli.cmd_submit_plan(SimpleNamespace(session=SESSION, plan=plan), store=store)
    assert submitted.ok, (submitted.detail, submitted.data)
    cli.cmd_order_raise(SimpleNamespace(session=SESSION, id="O1", element="the release is one command"),
                        store=store)
    cli.cmd_order_dispose(SimpleNamespace(session=SESSION, id="O1", as_="covered", stage=1, reason="",
                                          plan=plan), store=store)
    reviewed = cli.cmd_plan_review(SimpleNamespace(
        session=SESSION, verdict="pass", reviewer="thinker", concerns=None, note="", target=None,
        plan_digest=hashlib.sha256(Path(plan).read_bytes()).hexdigest(), scope=None), store=store)
    assert reviewed.ok, reviewed.detail
    approved = cli.cmd_approve(SimpleNamespace(session=SESSION, by="user"), store=store)
    assert approved.ok, (approved.detail, approved.data.get("blockers"))
    cli.cmd_partition(SimpleNamespace(session=SESSION, m1=False, m2=False, m3=False, m4=False,
                                      m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(SimpleNamespace(session=SESSION), store=store)
    passed = cli.cmd_record_result(SimpleNamespace(
        session=SESSION, status="passed", actual="ok", control="reviewed: ok", observation=STAGE_OBSERVATIONS[0],
        cost_log=None), store=store, runner=_checks_pass)
    after = store.load(SESSION)
    assert passed.ok, (after.stage(1).outcome.status, after.stage(1).outcome.actual,
                       [(f["segment"], f["scale"]) for f in after.effort_fires], passed.detail[:80],
                       after.stage(1).outcome.actual.replace("\n", " ")[:400])


def _verify_final(store):
    verified = cli.cmd_verify_final(SimpleNamespace(session=SESSION), store=store, runner=_checks_pass)
    assert verified.ok, (verified.detail[:200], verified.data.get("blockers"))
    return verified


def _resolved(store, monkeypatch):
    for phase, note in (("searched", ""), ("skipped", "test fixture, nothing to record")):
        cli.cmd_plugin_record(SimpleNamespace(session=SESSION, plugin="experience", phase=phase, note=note),
                              store=store)
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)
    done = cli.cmd_resolve(SimpleNamespace(session=SESSION, by="user", quality=5, quality_by="user-confirmed",
                                           quality_note=None, cost_log=None), store=store)
    assert done.ok, (done.detail, done.data)
    return done


def test_the_whole_understanding_effort_across_a_renewal_is_what_resolve_hands_to_the_record(
        ostore, home, armed_env, tmp_path, monkeypatch):
    """Fire, a user's acknowledgment and renewal, more work, a second fire and its difficulty cycle, the
    agent's acknowledgment, the order agreed, a plan approved, a stage passed, `verify-final` and the
    `resolve` verb: the figures on the type record are the effort of every understanding arming."""
    path = _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0, confirmer="user"))
    leaf = Path(_understanding(ostore)["pricing"]["type"]["path"])
    assert ostore.load(SESSION).node == "DIAGNOSING"
    assert cli_replan.cmd_fire_acknowledge(
        SimpleNamespace(session=SESSION, by="user", decision="continue", note="go on"), store=ostore).ok
    assert ostore.load(SESSION).node == "DIAGNOSING", "a user's acknowledgment clears the fire, not the difficulty"
    _difficulty_cycle(ostore)
    assert _submit(ostore, path).ok
    assert ostore.load(SESSION).node == "ROUTED"
    _book(ostore, usd=_overrun(3.0))
    second = _submit(ostore, path)
    assert second.ok and second.data["route"]["route"] == "agent_planning_difficulty_cycle"
    state = ostore.load(SESSION)
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "understanding"
    _difficulty_cycle(ostore)
    acknowledged = _agent_acknowledges(ostore)
    assert acknowledged.ok, acknowledged.detail
    returned = _submit(ostore, path)
    assert returned.ok, returned.detail
    assert ostore.load(SESSION).node == "ROUTED"
    _agreed_order(ostore, tmp_path, path)

    armings = [a for a in ostore.load(SESSION).effort_segments["armings"]
               if a["segment"] == effort_segments.UNDERSTANDING]
    assert len(armings) >= 3 and all(a["closed_at"] is not None for a in armings)
    per_arming = [effort_segments.arming_actual(a) for a in armings]
    whole = {scale: sum(figures[scale] for figures in per_arming) for scale in (USD, MINUTES)}
    assert whole[USD] >= 2 * _overrun(3.0)

    _planned_and_one_stage_passed(ostore, tmp_path)
    _verify_final(ostore)
    done = _resolved(ostore, monkeypatch)

    written = done.data["understanding_actual"]
    assert written["written"] == ["actual", "estimate"] and written["skipped"] is None
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert row["usd"] == pytest.approx(whole[USD]) and row.get("minutes", 0.0) == pytest.approx(whole[MINUTES])
    assert written["replaced_confirmed"] == {"confirmer": "user", "minutes": 30.0, "usd": 3.0}


def _phase_lines_with_the_total(shown: dict) -> None:
    lines = shown["lines"]
    assert [line["segment"] for line in lines][:2] == [effort_segments.UNDERSTANDING, effort_segments.PLANNING]
    understanding, planning = lines[0], lines[1]
    assert understanding["author"] == "the typing agent" and understanding["budget"] == {USD: 3.0, MINUTES: 30.0}
    assert understanding["actual"][USD] == pytest.approx(0.7)
    assert planning["author"] and planning["budget"] is not None
    assert planning["actual"][USD] == pytest.approx(0.4)
    assert shown["total"]["actual"][USD] == pytest.approx(sum(line["actual"][USD] for line in lines))
    assert shown["total"]["actual"][USD] >= 1.1
    assert shown["total"]["budget"][USD] == pytest.approx(
        sum(line["budget"][USD] for line in lines if line["budget"] is not None))
    assert shown["total"]["budget"][USD] >= understanding["budget"][USD] + planning["budget"][USD]


def test_closing_total_includes_phases(ostore, home, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(ostore, path).ok
    _book(ostore, usd=0.7, minutes=3)
    _agreed_order(ostore, tmp_path, path)
    _book(ostore, usd=0.4, minutes=2)
    _planned_and_one_stage_passed(ostore, tmp_path)
    _phase_lines_with_the_total(_verify_final(ostore).data["effort"])
    _phase_lines_with_the_total(_resolved(ostore, monkeypatch).data["effort"])


@pytest.mark.parametrize("flags", [
    {"chat": True},
    {"changed_lines": 5, "files": 1, "wall_clock_min": 3, "architectural": False},
], ids=["chat", "small-change"])
def test_a_small_task_has_no_phase_segments_and_its_planning_line_keeps_its_shape(ostore, armed_env, flags):
    _start_session(ostore)
    assert _classify(ostore, **flags).ok
    _book(ostore, usd=0.2, minutes=1)
    lines = cli_common._effort_disclosure(ostore.load(SESSION))["lines"]
    assert [line["segment"] for line in lines] == [effort_segments.PLANNING]
    assert not {"author", "work_item", "pricing"} & set(lines[0])
    assert lines[0]["actual"][USD] == pytest.approx(0.2)


def _presented_and_overrun(store, tmp_path, monkeypatch):
    """An order presented and answered but not agreed, whose understanding ran past its budget after
    the last act that judges it: the agreement is the one that meets the overrun."""
    _, path = _classified(store, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    assert _submit(store, path).ok
    assert _review(store).ok
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    assert _user_answers(_marker_of(path)) == 1
    _book(store, usd=_overrun(3.0))
    assert gates.open_fire_record(store.load(SESSION)) is None
    return path


def test_the_agreement_does_not_discharge_an_open_understanding_fire(
        ostore, armed_env, tmp_path, monkeypatch):
    """The agreement closes the segment at its multiple, which would clear the fire unseen. It is
    refused instead, the fire stays open, and the way on is the difficulty cycle and the agent's
    acknowledgment: every understanding fire is the agent's to acknowledge."""
    _presented_and_overrun(ostore, tmp_path, monkeypatch)
    refused = _agree(ostore)
    state = ostore.load(SESSION)
    assert refused.ok is False and refused.action == "planning_difficulty"
    assert gates.open_fire_record(state) is not None
    assert state.order_agreement is None
    steps = refused.data["agent_route"]["steps"]
    assert steps[0] == "declare" and "fire-acknowledge --decision revise --by agent" in steps


def test_the_agent_may_not_acknowledge_an_understanding_fire_before_the_difficulty_record_is_complete(
        ostore, armed_env, tmp_path, monkeypatch):
    """The agreement met the fire and the session is in the difficulty cycle: the agent's
    acknowledgment is refused while the record is incomplete, the fire stays open, and the way on is
    the cycle, the acknowledgment and the return by `order-submit`."""
    path = _presented_and_overrun(ostore, tmp_path, monkeypatch)
    assert _agree(ostore).ok is False
    assert ostore.load(SESSION).node == "DIAGNOSING"
    early = _agent_acknowledges(ostore)
    assert early.ok is False
    assert gates.open_fire_record(ostore.load(SESSION)) is not None
    _difficulty_cycle(ostore)
    acknowledged = _agent_acknowledges(ostore)
    assert acknowledged.ok, acknowledged.detail
    assert acknowledged.action == "order-submit"
    returned = _submit(ostore, path)
    assert returned.ok, returned.detail
    assert ostore.load(SESSION).node == "ROUTED"


def test_order_agree_in_an_understanding_difficulty_cycle_is_refused_naming_order_submit_and_the_session_goes_on(
        ostore, armed_env, tmp_path, monkeypatch):
    """The reviewer's four steps: a fire at `order-submit`; `order-agree` (refused, naming
    `order-submit`, nothing changed); the difficulty cycle and the agent's acknowledgment; the revised
    draft's `order-submit` back to ROUTED, the agreement, and a plan submitted."""
    path = _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    before = ostore.load(SESSION)
    assert before.node == "DIAGNOSING" and before.difficulty.met_in == "understanding"

    refused = _agree(ostore)
    after = ostore.load(SESSION)
    assert refused.ok is False and "order-submit" in refused.detail, refused.detail
    assert (after.node, after.order_agreement, after.effort_fires) == (
        before.node, before.order_agreement, before.effort_fires)
    assert gates.open_fire_record(after) is not None

    _difficulty_cycle(ostore)
    acknowledged = _agent_acknowledges(ostore)
    assert acknowledged.ok and acknowledged.action == "order-submit", acknowledged.detail
    still = _agree(ostore)
    assert still.ok is False and "order-submit" in still.detail
    assert ostore.load(SESSION).node == "DIAGNOSING"

    assert _submit(ostore, path).ok
    assert ostore.load(SESSION).node == "ROUTED"
    _agreed_order(ostore, tmp_path, path)
    plan = _plan(tmp_path)
    assert cli.cmd_plan(SimpleNamespace(session=SESSION), store=ostore).ok
    submitted = cli.cmd_submit_plan(SimpleNamespace(session=SESSION, plan=plan), store=ostore)
    assert submitted.ok, (submitted.detail, submitted.data)
    assert ostore.load(SESSION).node == "PLAN_READY"


def test_the_understanding_cycle_guard_meets_only_the_cycle_an_understanding_fire_opened():
    """Every other state is exempt: a session not in DIAGNOSING, and a difficulty met in another
    phase or in none."""
    def state(node, met_in):
        built = SessionState(session_id="s", task_id="t", node=node)
        if met_in != "no difficulty":
            built.difficulty = Difficulty(met_in=met_in)
        return built

    for node, met_in in ((Node.ROUTED.value, "no difficulty"), (Node.DIAGNOSING.value, "planning"),
                         (Node.DIAGNOSING.value, "stage"), (Node.DIAGNOSING.value, None),
                         (Node.DIAGNOSING.value, "no difficulty"), (Node.PLANNING.value, "understanding")):
        assert cli_common._understanding_cycle_refusal(state(node, met_in), "record the agreement") is None
    inside = cli_common._understanding_cycle_refusal(
        state(Node.DIAGNOSING.value, "understanding"), "record the agreement")
    assert inside is not None and inside.startswith("cannot record the agreement:") and "order-submit" in inside


def test_an_agents_revise_of_an_understanding_fire_keeps_the_renewed_armings_budget_source_pricing_and_author(
        ostore, armed_env, tmp_path, monkeypatch):
    _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    first = _understanding(ostore)
    _difficulty_cycle(ostore)
    acknowledged = _agent_acknowledges(ostore)
    assert acknowledged.ok, acknowledged.detail
    renewed = _understanding(ostore)
    assert renewed["arming_id"] != first["arming_id"] and renewed["fire"] is None
    assert renewed["budget"] == first["budget"] == {USD: 3.0, MINUTES: 30.0}
    assert (renewed["budget_source"], renewed["author"], renewed["pricing"]) == (
        first["budget_source"], first["author"], first["pricing"])


def test_an_agent_continue_on_an_understanding_fire_is_refused_and_renews_nothing(
        ostore, armed_env, tmp_path, monkeypatch):
    """The agent acknowledges an understanding fire only with revise; a continue is refused, the
    arming and its fire stay as they were, and the revise is the way on."""
    _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    first = _understanding(ostore)
    _difficulty_cycle(ostore)
    refused = _agent_acknowledges(ostore, "continue")
    assert refused.ok is False and "revise" in refused.detail, refused.detail
    again = _understanding(ostore)
    assert again["arming_id"] == first["arming_id"] and not again["fire"].get("ack")
    assert _agent_acknowledges(ostore, "revise").ok


def test_seeded_difficulty_carries_met_in(ostore, armed_env, tmp_path, monkeypatch):
    """The difficulty an understanding fire opens is seeded with the phase it was met in, and the
    fire names that difficulty."""
    _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    state = ostore.load(SESSION)
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "understanding"
    assert gates.open_fire_record(state)["difficulty_uid"] == state.difficulty.uid


def test_understanding_difficulty_closes_to_order_draft(ostore, armed_env, tmp_path, monkeypatch):
    """The cycle, the agent's acknowledgment and the revised draft's `order-submit` close the
    difficulty (the record is filed, not open) and return the session to ROUTED by the machine's
    edge `understanding_return`."""
    path = _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    uid = ostore.load(SESSION).difficulty.uid
    _difficulty_cycle(ostore)
    assert _agent_acknowledges(ostore).ok
    assert ostore.load(SESSION).node == "DIAGNOSING", "the acknowledgment does not leave the cycle"
    assert _submit(ostore, path).ok
    state = ostore.load(SESSION)
    assert state.node == "ROUTED" and state.difficulty is None
    assert gates.open_fire_record(state) is None
    assert [filed["uid"] for filed in state.closed_difficulties] == [uid]
    assert state.closed_difficulties[0]["met_in"] == "understanding"


def test_the_order_submit_directive_names_the_verb_and_the_authority_of_a_fire(
        ostore, armed_env, tmp_path, monkeypatch):
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    _book(ostore, usd=_overrun(3.0))
    submitted = _submit(ostore, path)
    assert submitted.ok
    assert ("this submission raised an effort-divergence fire on understanding the order" in submitted.detail
            and "Every understanding fire is the agent's" in submitted.detail
            and "`fire-acknowledge --by agent --decision revise`" in submitted.detail
            and "order-submit (the revised order draft)" in submitted.detail), submitted.detail
    assert submitted.data["route"]["acknowledged_by"] == effort_segments.AUTHORITY_AGENT
    assert submitted.data["effort_fire"]["acknowledged_by"] == effort_segments.AUTHORITY_AGENT


# --- a fire on planning or a stage enters DIAGNOSING, and its line says who acknowledges it ----------------
# The planning and stage harnesses are those of test_effort_fire_diagnosing.py, whose fixtures the
# understanding cases above do not use: `fire_env` is what they run under, not autouse here.


@pytest.fixture
def fire_env(monkeypatch, tmp_path):
    """The environment of the planning and stage harnesses: no replan authorization and their own
    cost log."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")


def _unarmed(monkeypatch) -> None:
    """The harnesses classify their sessions with the answer hook unwired, as the suite at large
    does, so no order agreement is armed; this module opts out of that default."""
    monkeypatch.setattr(gates, "answer_hook_registered", lambda: False)


@pytest.fixture
def planning_eng(fire_env, tmp_path, monkeypatch):
    _unarmed(monkeypatch)
    return PlanningEng(tmp_path)


@pytest.fixture
def stage_eng(fire_env, tmp_path, monkeypatch):
    _unarmed(monkeypatch)
    return StageEng(tmp_path)


@pytest.fixture
def real_stage(fire_env, tmp_path, monkeypatch):
    """The stage harness under an agent home, so `estimate-correct` runs for real."""
    _unarmed(monkeypatch)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return _RealStageEng(tmp_path)


def _own_dir(tmp_path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _correction_marker(refused: dict) -> str:
    figure = refused["data"]["figure"]
    return estimate_correction_marker(1, figure["minutes"], figure["usd"])


def test_planning_difficulty_before_first_plan_closes_by_submit_plan(planning_eng):
    planning_eng.planning()
    planning_eng.book(usd=OVERSPENT_USD)
    plan = planning_eng.write()
    planning_eng.run("submit_plan", session="s", plan=plan)
    blocked = planning_eng.run("submit_plan", session="s", plan=plan)
    assert blocked["ok"] is False and planning_eng.state().node == "DIAGNOSING"
    planning_eng.cycle()
    still = planning_eng.run("submit_plan", session="s", plan=plan)
    assert still["ok"] is False, "the fire is the user's, so the difficulty alone does not close it"
    assert planning_eng.ack(decision="continue")["ok"]
    returned = planning_eng.run("submit_plan", session="s", plan=plan)
    assert returned["ok"], returned
    assert planning_eng.state().node == "PLAN_READY"


@pytest.mark.parametrize("booked, scale", [("usd", effort.SCALE_SPEND), ("minutes", effort.SCALE_WALL_CLOCK)])
def test_every_scale_opens_difficulty_on_all_three(
        ostore, armed_env, fire_env, tmp_path, monkeypatch, booked, scale):
    """Understanding, planning and a stage each take the session into DIAGNOSING on either scale,
    with the difficulty met in that phase and the fire naming the scale."""
    _, path = _classified(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    _book(ostore, **{booked: 100000.0})
    assert _submit(ostore, path).ok
    _unarmed(monkeypatch)
    planning = PlanningEng(_own_dir(tmp_path, "planning"))
    planning.planning()
    planning.book(**{booked: 100000.0})
    planning.run("submit_plan", session="s", plan=planning.write())
    stage = StageEng(_own_dir(tmp_path, "stage"))
    executing_overspent(stage, **{booked: 100000.0})
    _stamp_raised_fire(stage)
    stage.passed("s")
    for phase, state in (("understanding", ostore.load(SESSION)), ("planning", planning.state()),
                         ("stage", stage.state("s"))):
        assert state.node == "DIAGNOSING" and state.difficulty.met_in == phase, phase
        assert gates.open_fire_record(state)["scale"] == scale, phase


def test_finished_work_fire_enters_diagnosing(stage_eng):
    """The finished-work fire of the stage-3 plan: no path raises a fire with the finished-work
    disposition now, and the reachable form is the fire the prompt-boundary stamp raised on a stage
    whose passing result is the next act. That result meets the fire, is refused, and takes the session
    into DIAGNOSING with the fire's ordinary disposition instead of leaving the node unchanged."""
    executing_overspent(stage_eng, usd=30.0)
    _stamp_raised_fire(stage_eng)
    refused = stage_eng.passed("s")
    state = stage_eng.state("s")
    assert refused["ok"] is False
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "stage"
    assert state.stage(1).outcome.status != "passed"
    assert gates.open_fire_record(state).get("disposition") != EFFORT_FIRE_FINISHED_WORK


def test_old_session_stage_fire_enters_diagnosing(stage_eng):
    """A session whose stage arming and fire are in the old form (no tier, user budget, author or
    pricing on the arming, no line on the fire) meets the same edge into DIAGNOSING; its fire is read
    by scale, and a spend fire is the user's."""
    executing_overspent(stage_eng, usd=30.0)
    state = stage_eng.state("s")
    arming = stage_arming(state, 1)
    for key in ("user_budget", "author", "pricing", "declared_tier"):
        arming.pop(key, None)
    stage_eng.store.save(state)
    _stamp_raised_fire(stage_eng)
    state = stage_eng.state("s")
    fire = gates.open_fire_record(state)
    fire.pop("line", None)
    stage_eng.store.save(state)
    assert effort_segments.line_acknowledged_by(fire) == effort_segments.AUTHORITY_USER
    stage_eng.passed("s")
    state = stage_eng.state("s")
    assert state.node == "DIAGNOSING" and state.difficulty.met_in == "stage"
    assert gates.open_fire_record(state)["difficulty_uid"] == state.difficulty.uid


def test_old_form_stage_fire_ack_revise_renews_arming(stage_eng):
    """An old-form stage has no figure to correct: the user's revise writes none, acknowledges the
    fire and renews the arming from zero."""
    executing_overspent(stage_eng, usd=30.0)
    state = stage_eng.state("s")
    assert state.stage(1).place is None
    stage_eng.failed("s")
    first = stage_arming(stage_eng.state("s"), 1)
    done = stage_eng.ack(by="user", decision="revise")
    assert done["ok"], done
    state = stage_eng.state("s")
    renewed = stage_arming(state, 1)
    assert renewed["arming_id"] != first["arming_id"] and renewed["fire"] is None
    assert gates.open_fire_record(state) is None
    assert sub_plan.held_figure(state, 1) is None


def test_fire_acknowledge_revise_writes_correction(real_stage):
    """A user's revise without the marked ledger answer is refused with the verb that is the way on;
    with the answer, the one writer (`estimate-correct`) writes the user's correction, moves the user's
    line and the open arming, and the fire is acknowledged."""
    executing_overspent(real_stage, usd=30.0)
    real_stage.agreed_stage()
    real_stage.failed("s")
    refused = real_stage.ack(by="user", decision="revise")
    marker = _correction_marker(refused)
    assert refused["ok"] is False and marker in refused["detail"]
    figure = refused["data"]["figure"]
    assert (f"fire-acknowledge --session s --decision revise --by user "
            f"--minutes {figure['minutes']} --usd {figure['usd']}") in refused["detail"]
    assert real_stage.corrections() == [] and gates.open_fire_record(real_stage.state("s")) is not None
    _user_choice(marker, session="s")
    done = real_stage.ack(by="user", decision="revise")
    assert done["ok"], done
    [correction] = real_stage.corrections()
    assert (correction["by"], correction["authority"], correction["agreer"]) == ("user", "user", "s")
    state = real_stage.state("s")
    assert gates.open_fire_record(state) is None
    assert sub_plan.user_figure(state, 1)["usd"] == correction["to"]["usd"] == 30.0
    assert stage_arming(state, 1)["budget"][effort_segments.SCALE_USD] == 30.0


def test_agent_revise_writes_no_correction(real_stage):
    """A fire at the user line is the user's: the agent's revise is refused, writes no correction
    and holds no figure, and the user's `continue` is the way on."""
    executing_overspent(real_stage, usd=30.0)
    real_stage.agreed_stage()
    real_stage.failed("s")
    refused = real_stage.ack(by="agent", decision="revise")
    assert refused["ok"] is False and "fire is at the user line" in refused["detail"]
    assert real_stage.corrections() == [] and sub_plan.held_figure(real_stage.state("s"), 1) is None
    assert real_stage.ack(by="user", decision="continue")["ok"]


def _real_stage_in(tmp_path, monkeypatch, name):
    """A stage harness of its own whose estimates are written by the real `estimate-correct`,
    its agent home beside its state."""
    base = _own_dir(tmp_path, name)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(base / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return _RealStageEng(base)


def test_executor_line_fire_agent_acknowledged(ostore, armed_env, fire_env, tmp_path, monkeypatch):
    """The acknowledgment is the user's when the fire is at the user line, past the user-approved
    estimate (R10), and the agent's when it is at the difficulty line only, past the last agreed
    estimate, and then only as a revise. Walked on understanding, planning, an old-form stage and a
    new-form stage, each by the verbs the engine accepts: the route, the fire record's
    `acknowledged_by`, the refusal of the other actor, the correction's authority, and no fire
    again after the next judging act."""
    agent, user = effort_segments.AUTHORITY_AGENT, effort_segments.AUTHORITY_USER

    # understanding: the fire is at the difficulty line, so only the agent's revise closes it
    _fired_understanding(ostore, tmp_path, monkeypatch, _with_estimate(minutes=30, usd=3.0))
    fire = gates.open_fire_record(ostore.load(SESSION))
    assert effort_segments.line_acknowledged_by(fire) == agent
    _difficulty_cycle(ostore)
    assert _agent_acknowledges(ostore, "continue").ok is False
    assert gates.open_fire_record(ostore.load(SESSION)) is not None
    assert _agent_acknowledges(ostore, "revise").ok
    assert gates.open_fire_record(ostore.load(SESSION)) is None

    _unarmed(monkeypatch)
    patch_cli_name(monkeypatch, "_ledgered_order_key", lambda state: "an-order")

    # planning: past the user's line, so the agent's revise is refused and the user's continue closes it
    planning = PlanningEng(_own_dir(tmp_path, "planning"))
    planning.planning()
    planning.book(usd=OVERSPENT_USD)
    planning.run("submit_plan", session="s", plan=planning.write())
    fire = gates.open_fire_record(planning.state())
    assert effort_segments.line_acknowledged_by(fire) == user
    planning.cycle()
    refused = planning.ack(by="agent", decision="revise")
    assert refused["ok"] is False and "fire is at the user line" in refused["detail"]
    assert gates.open_fire_record(planning.state()) is not None
    assert planning.ack(by="user", decision="continue")["ok"]
    assert gates.open_fire_record(planning.state()) is None

    # an old-form stage: its fire carries no line and is read by its scale, the user's
    old = StageEng(_own_dir(tmp_path, "old"))
    executing_overspent(old, usd=30.0)
    old.failed("s")
    state = old.state("s")
    fire = gates.open_fire_record(state)
    fire.pop("line", None)
    old.store.save(state)
    assert effort_segments.line_acknowledged_by(fire) == user
    assert cli_common._fire_acknowledger(state, fire) == user
    assert old.ack(by="agent", decision="revise")["ok"] is False
    assert gates.open_fire_record(old.state("s")) is not None
    assert old.ack(by="user", decision="revise")["ok"]
    assert gates.open_fire_record(old.state("s")) is None and sub_plan.held_figure(old.state("s"), 1) is None
    fires = len(old.state("s").effort_fires)
    old.run("dispatch", session="s")
    assert len(old.state("s").effort_fires) == fires, "a judging act after the user's revise raises no fire"

    # the user's revise on a planning fire renews the planning arming: the next judging act raises nothing
    revised = PlanningEng(_own_dir(tmp_path, "planning-revise"))
    revised.planning()
    revised.book(usd=OVERSPENT_USD)
    revised.run("submit_plan", session="s", plan=revised.write())
    assert effort_segments.line_acknowledged_by(gates.open_fire_record(revised.state())) == user
    revised.cycle()
    assert revised.ack(by="user", decision="revise")["ok"]
    renewed = effort_segments.open_arming_of(revised.state().effort_segments, effort_segments.PLANNING)
    assert renewed is not None and renewed["fire"] is None and not any(effort_segments.arming_actual(renewed).values()), \
        "the user's revise renews the planning arming: a fresh one from zero, no fire on it"
    fires = len(revised.state().effort_fires)
    assert gates.open_fire_record(revised.state()) is None
    revised.run("submit_plan", session="s", plan=revised.write())
    assert len(revised.state().effort_fires) == fires and gates.open_fire_record(revised.state()) is None, \
        "a judging act after the user's revise raises no fire"

    # a new-form stage at the difficulty line, inside the margin: the agent's correction, the user's line unmoved
    inside = _real_stage_in(tmp_path, monkeypatch, "inside")
    executing_overspent(inside, usd=10.0)
    inside.agreed_stage(usd=1.0, approved_usd=3.0)
    failed = inside.failed("s")
    fire = gates.open_fire_record(inside.state("s"))
    assert effort_segments.line_acknowledged_by(fire) == agent
    assert failed["data"]["agent_route"]["acknowledged_by"] == agent
    refusal = inside.run("dispatch", session="s")
    assert refusal["data"]["effort_fire"]["acknowledged_by"] == agent
    assert refusal["data"]["agent_route"]["acknowledged_by"] == agent
    inside.cycle()
    assert inside.ack(by="agent", decision="continue")["ok"] is False
    assert inside.ack(by="agent", decision="revise")["ok"]
    state = inside.state("s")
    [correction] = inside.corrections()
    assert (correction["by"], correction["authority"]) == ("agent", "agent")
    assert sub_plan.user_figure(state, 1)["usd"] == 3.0, "an agent's correction never moves the user's line"
    assert stage_arming(state, 1)["budget"][effort_segments.SCALE_USD] == 10.0
    assert gates.open_fire_record(state) is None
    inside.run("dispatch", session="s")
    after = inside.state("s")
    assert len(after.effort_fires) == len(state.effort_fires) and gates.open_fire_record(after) is None, \
        "the next judging act raises no fire at the repriced figure"

    # the two lines differ and one act crosses both: the fire is at the user line, so the agent's revise
    # is refused and nothing is written; the user's continue closes it
    both = _real_stage_in(tmp_path, monkeypatch, "both")
    executing_overspent(both, usd=30.0)
    both.agreed_stage(usd=1.0, approved_usd=3.0)
    both.failed("s")
    fire = gates.open_fire_record(both.state("s"))
    assert effort_segments.line_acknowledged_by(fire) == user
    both.cycle()
    refused = both.ack(by="agent", decision="revise")
    assert refused["ok"] is False and "fire is at the user line" in refused["detail"]
    assert both.corrections() == [] and gates.open_fire_record(both.state("s")) is not None
    assert both.ack(by="user", decision="continue")["ok"]
    assert gates.open_fire_record(both.state("s")) is None

    # the same fire with a figure past the margin: held for the user, nothing written, the fire open;
    # the user's marked answer and correction, then the agent's re-run closes it with the user's authority
    past = _real_stage_in(tmp_path, monkeypatch, "past")
    executing_overspent(past, usd=10.0)
    past.agreed_stage(usd=1.0, approved_usd=3.0)
    past.failed("s")
    past.cycle()
    held = past.ack(by="agent", decision="revise", usd=40)
    marker = _correction_marker(held)
    assert held["ok"] is False and held["marker"] == "ESCALATE_TO_USER" and marker in held["detail"]
    assert sub_plan.held_figure(past.state("s"), 1)["usd"] == 40.0
    assert past.corrections() == [] and gates.open_fire_record(past.state("s")) is not None
    _user_choice(marker, session="s")
    assert past.correct(minutes=held["data"]["figure"]["minutes"], usd=40, basis="the user's call", by="user")["ok"]
    assert past.ack(by="agent", decision="revise", usd=40)["ok"]
    [only] = past.corrections()
    assert (only["by"], only["authority"]) == ("user", "user")
    assert sub_plan.user_figure(past.state("s"), 1)["usd"] == 40.0
    assert stage_arming(past.state("s"), 1)["budget"][effort_segments.SCALE_USD] == 40.0, \
        "the re-run carries the user's figure"
    assert gates.open_fire_record(past.state("s")) is None
    fires = len(past.state("s").effort_fires)
    past.run("dispatch", session="s")
    assert len(past.state("s").effort_fires) == fires, "a re-run after the user's own figure raises no fire"

    # a fire at the user line after an upward correction: past the user's new line, the user's again;
    # the user's own revise raised the line and does not fire again at the next judging act
    upward = _real_stage_in(tmp_path, monkeypatch, "upward")
    executing_overspent(upward, usd=30.0)
    upward.agreed_stage()
    upward.failed("s")
    assert effort_segments.line_acknowledged_by(gates.open_fire_record(upward.state("s"))) == user
    refused = upward.ack(by="user", decision="revise")
    _user_choice(_correction_marker(refused), session="s")
    assert upward.ack(by="user", decision="revise")["ok"]
    state = upward.state("s")
    assert sub_plan.user_figure(state, 1)["usd"] == 30.0 and gates.open_fire_record(state) is None
    upward.run("dispatch", session="s")
    assert len(upward.state("s").effort_fires) == len(state.effort_fires), "the user's revise does not fire again"
    upward.overspend("s", usd=500.0)
    upward.run("dispatch", session="s")
    fire = gates.open_fire_record(upward.state("s"))
    assert fire is not None and effort_segments.line_acknowledged_by(fire) == user
    refused = upward.ack(by="agent", decision="revise")
    assert refused["ok"] is False and "fire is at the user line" in refused["detail"]
    assert upward.corrections()[-1]["authority"] == "user" and len(upward.corrections()) == 1
    assert upward.ack(by="user", decision="continue")["ok"]


def test_a_difficulty_line_fire_of_a_session_with_no_ledgered_order_is_the_users_in_route_record_and_verb(
        stage_eng, monkeypatch):
    """The same fire with no user-approved version of the order to continue within (the ledger holds
    none: the stage harness's approval is stubbed away): the route, the fire record's `acknowledged_by`
    and the verb the engine accepts agree that it is the user's, though the fire's own line says
    agent (here the replans scale)."""
    stage_eng.executing("s")
    seed_replans(stage_eng, "s", Thresholds().effort_replan_absolute())
    patch_cli_name(monkeypatch, "_ledgered_order_key", lambda state: None)
    failed = stage_eng.failed("s")
    state = stage_eng.state("s")
    fire = gates.open_fire_record(state)
    assert effort_segments.line_acknowledged_by(fire) == effort_segments.AUTHORITY_AGENT
    assert cli_common._fire_acknowledger(state, fire) == effort_segments.AUTHORITY_USER
    assert "agent_route" not in failed["data"]
    assert failed["data"]["effort_divergence"]["scale"] == effort.SCALE_REPLANS
    refusal = stage_eng.run("dispatch", session="s")
    assert refusal["data"]["effort_fire"]["acknowledged_by"] == effort_segments.AUTHORITY_USER
    assert refusal["data"]["route"]["acknowledged_by"] == effort_segments.AUTHORITY_USER
    stage_eng.cycle()
    refused = stage_eng.ack(by="agent", decision="revise")
    assert refused["ok"] is False and "no user-approved version" in refused["detail"]
    assert gates.open_fire_record(stage_eng.state("s")) is not None
    assert stage_eng.ack(by="user", decision="continue")["ok"]
    assert gates.open_fire_record(stage_eng.state("s")) is None
