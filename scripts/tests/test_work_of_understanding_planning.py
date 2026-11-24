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
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import (
    cli_common, cli_difficulty, cli_final, cli_order, cli_replan, cli_session, delivery_channel, effort,
    effort_segments, gates, runtime_host, understanding_pricing)
from agentctl import cli_plan
from agentctl.config import Thresholds
from agentctl.state import Declaration, Difficulty, Node, SessionState
from agentctl.sub_plan import Estimate
from lib import difficulty_phase, markdown_sections, understanding_effort
from order_agreement_mutants import mutant, script_module  # noqa: F401  (autouse fixture)
from test_order_agreement import (  # noqa: F401  (home, ostore and armed_env are fixtures)
    SESSION, _agree, _classify, _confirm_delivery, _marker_of, _user_answers, _wire_answer_hook, armed_env, home,
    ostore)
from test_order_agreement import _start as _start_session
from test_order_phase import SIDES, _draft, _present, _review, _submit

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
    assert written == {"written": ["actual", "estimate"], "skipped": None, "retry": None}
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
    assert command.startswith("python3 ") and "record-experience.py understanding-effort" in command
    assert f"--leaf {leaf}" in command and "--kind estimate" in command and f"--session {SESSION}" in command
    assert command.split()[3:] == [str(part) for part in calls[1][3:]]


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
    assert done.data["understanding_actual"] == {"written": ["actual", "estimate"], "skipped": None, "retry": None}
    assert store.load("wb-1").effort_segments["understanding_actual"] == done.data["understanding_actual"]
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    (row,) = block["actuals"]
    assert (row["session"], row["usd"], row["minutes"]) == ("wb-1", 0.8, 6.0)
    assert (block["estimate"]["usd"], block["estimate"]["minutes"], block["estimate"]["author"]) == (0.8, 6.0, "wb-1")


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
