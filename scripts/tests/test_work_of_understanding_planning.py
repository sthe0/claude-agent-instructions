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

from agentctl.state import Difficulty
from lib import difficulty_phase, understanding_effort
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


def test_phases_are_understanding_planning_stage():
    assert difficulty_phase.PHASES == ("understanding", "planning", "stage")


def test_difficulty_met_in_round_trip():
    record = Difficulty(met_in="planning")
    loaded = Difficulty.from_dict(dataclasses.asdict(record))
    assert loaded.met_in == "planning"


@pytest.mark.parametrize("phase", difficulty_phase.PHASES)
def test_every_phase_survives_a_load(phase):
    assert Difficulty.from_dict(dataclasses.asdict(Difficulty(met_in=phase))).met_in == phase


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


def test_block_parse_render_round_trip():
    block = {"estimate": {"minutes": 12.0, "usd": 1.5, "author": "agent", "confirmer": "user"},
             "actuals": [{"minutes": 20.0, "usd": 2.0, "session": "s1", "date": DATE}]}
    rendered = understanding_effort.render(block)
    assert understanding_effort.parse(rendered) == block
    assert understanding_effort.problems(rendered) == []


def test_add_actual_appends_and_sets_estimate():
    text = "# leaf\n\n## Cost\n1 USD\n"
    text = understanding_effort.set_estimate(text, 10, 1, "agent", "user")
    text = understanding_effort.add_actual(text, 14, 1.2, "s1", DATE)
    text = understanding_effort.add_actual(text, 9, 0.8, "s2", DATE)
    assert understanding_effort.estimate_of(text) == {
        "minutes": 10, "usd": 1, "author": "agent", "confirmer": "user"}
    assert [row["session"] for row in understanding_effort.parse(text)["actuals"]] == ["s1", "s2"]
    assert text.count(understanding_effort.SECTION) == 1
    assert understanding_effort.problems(text) == []


def test_set_estimate_replaces_in_place_and_keeps_later_sections():
    text = understanding_effort.set_estimate("# leaf\n\n## Cost\n1 USD\n", 10, 1, "agent", "user")
    text += "\n## Self-critique of the agent system\nlater\n"
    text = understanding_effort.set_estimate(text, 20, 2, "agent", "user")
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
    "## Understanding effort\n\n```toml\n[[actuals]]\nminutes = 1\nusd = 1\nsession = \"\"\ndate = \"d\"\n```\n",
    "## Understanding effort\n\n```toml\nnot = = toml\n```\n",
    "## Understanding effort\n\n```toml\n```\n",
])
def test_malformed_block_refused(body):
    assert understanding_effort.problems("# leaf\n\n" + body)


def test_malformed_block_refused_on_a_difficulty_leaf(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    leaf.write_text(leaf.read_text(encoding="utf-8")
                    + "\n## Understanding effort\n\n```toml\n[estimate]\nminutes = 5\n```\n",
                    encoding="utf-8")
    assert "Understanding effort" in _errors(leaf) or "estimate" in _errors(leaf)


def test_understanding_effort_command_records_estimate_then_actual(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    base = ["understanding-effort", "--leaf", str(leaf), "--minutes", "10", "--usd", "1", "--date", DATE]
    assert RE.main([*base, "--kind", "estimate", "--author", "agent", "--confirmer", "user"]) == 0
    assert RE.main([*base, "--kind", "actual", "--actual-session", "s1"]) == 0
    block = understanding_effort.parse(leaf.read_text(encoding="utf-8"))
    assert block["estimate"]["author"] == "agent"
    assert block["actuals"][0]["session"] == "s1"
    assert _errors(leaf) is None


def test_understanding_effort_estimate_without_author_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    before = leaf.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        RE.main(["understanding-effort", "--leaf", str(leaf), "--kind", "estimate",
                 "--minutes", "10", "--usd", "1"])
    assert "--author" in str(exc.value)
    assert leaf.read_text(encoding="utf-8") == before


def test_understanding_effort_actual_without_session_refused(tmp_path):
    leaf = _new(tmp_path, "alpha", "the order was read twice")
    before = leaf.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        RE.main(["understanding-effort", "--leaf", str(leaf), "--kind", "actual",
                 "--minutes", "10", "--usd", "1"])
    assert "--actual-session" in str(exc.value)
    assert leaf.read_text(encoding="utf-8") == before


def test_regularity_record_with_block_passes():
    leaf = (
        "---\nname: reg\ndescription: an order of this type is read slowly\ntype: reference\n"
        "schema: regularity/v1\ncreated: 2026-11-23\nlast_verified: 2026-11-23\n---\n\n"
        "# reg\n\n## Regularity\nOrders of this type take long to understand.\n\n"
        "## Generality\nOrders that change a gate.\n\n"
        "## Refutation\nAn order of this type read quickly.\n\n"
        "## Induced from\n- [[some-leaf]]\n"
    )
    with_block = understanding_effort.add_actual(
        understanding_effort.set_estimate(leaf, 30, 3, "agent", "user"), 41, 4.2, "s1", DATE)
    assert understanding_effort.estimate_of(with_block)["minutes"] == 30
    assert VLS.check_content(with_block, "memory-global/leaves/regularities/reg.md") is None
