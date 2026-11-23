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
import re
from pathlib import Path

import pytest

from agentctl import cli_common, cli_difficulty
from agentctl.state import Declaration, Difficulty, Node, SessionState
from lib import difficulty_phase, markdown_sections, understanding_effort
from order_agreement_mutants import mutant, script_module  # noqa: F401  (autouse fixture)

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


def test_the_leaf_command_and_the_effort_block_find_a_section_with_one_helper():
    text = "# t\n\n## Understanding effort (draft)\nx\n\n## Cost\nc\n"
    assert RE.section_span(text, "Understanding effort") == understanding_effort.section_span(text)
    assert understanding_effort.section_span(text) == (text.index("## Understanding"), text.index("## Cost"))


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
