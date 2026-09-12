"""Fixture-driven tests for lib/writer_pass.py.

Drives every fixture transcript under fixtures/published-text/: the witnessed
vs unwitnessed pair, the ordering/two-witness inversion guard, the per-body
discrimination fixture, the containment-trap fixture, and the two shape/unit
assertions the plan's Stage 4 Procedure names directly (witness-shape
recognition, normalization tolerance, UNREADABLE/NO_WITNESS_IN_WINDOW).
"""
from __future__ import annotations

import pytest

from lib import writer_pass as wp


@pytest.fixture
def published_text_dir(fixtures_dir):
    return fixtures_dir / "published-text"


@pytest.fixture
def reader_facing(published_text_dir):
    return (published_text_dir / "reader-facing.md").read_text(encoding="utf-8")


def test_witnessed_fixture_binds_non_none(published_text_dir, reader_facing):
    result = wp.bind(reader_facing, published_text_dir / "transcript-witnessed.jsonl")
    assert result.strength in (wp.WRITER_OUTPUT, wp.POST_WITNESS), result.strength
    assert result.witness is not None


def test_unwitnessed_fixture_binds_none(published_text_dir, reader_facing):
    result = wp.bind(reader_facing, published_text_dir / "transcript-unwitnessed.jsonl")
    assert result.strength == wp.NONE_STRENGTH


def test_one_witness_backs_all_three_bodies(published_text_dir):
    siblings = {
        "/tmp/de467/sibling2.md": "Second sibling comment: attachments are up to date on the linked sub-ticket.\n",
        "/tmp/de467/sibling3.md": "Third sibling comment: no further action needed on the parent ticket.\n",
    }
    for content in siblings.values():
        result = wp.bind(content, published_text_dir / "transcript-witnessed.jsonl")
        assert result.strength == wp.POST_WITNESS, content


def test_witnessed_fixture_carries_exactly_one_witness(published_text_dir):
    ws = wp.witnesses(published_text_dir / "transcript-witnessed.jsonl", tail_bytes=wp._SCAN_TAIL_BYTES)
    assert len(ws) == 1, ws


def test_two_witness_fixture_carries_at_least_two(published_text_dir):
    ws = wp.witnesses(published_text_dir / "transcript-two-witness.jsonl", tail_bytes=wp._SCAN_TAIL_BYTES)
    assert len(ws) >= 2, ws


def test_two_witness_existential_predicate_not_latest_witness(published_text_dir, reader_facing):
    """A body composed between W1 and W2 must still bind: it discriminates an
    EXISTS-a-preceding-witness implementation (correct) from a
    compare-against-the-latest-witness one (the inversion this fixture
    exists to catch -- see the module docstring)."""
    result = wp.bind(reader_facing, published_text_dir / "transcript-two-witness.jsonl")
    assert result.strength != wp.NONE_STRENGTH, (
        "a latest-witness predicate is in force: a body a witness genuinely preceded was denied"
    )


def test_discrimination_fixture_separates_polished_from_unpolished(published_text_dir):
    polished = (published_text_dir / "polished.md").read_text(encoding="utf-8")
    unpolished = (published_text_dir / "unpolished.md").read_text(encoding="utf-8")
    transcript = published_text_dir / "transcript-discrimination.jsonl"
    bound_polished = wp.bind(polished, transcript)
    bound_unpolished = wp.bind(unpolished, transcript)
    assert bound_polished.strength != wp.NONE_STRENGTH, bound_polished
    assert bound_unpolished.strength == wp.NONE_STRENGTH, bound_unpolished


def test_ordering_a_body_composed_before_the_only_witness_denies(published_text_dir):
    """Direct analogue of the recorded failure (TICKET-467 occurrence 4:
    publish at 10:08:03Z, witness at 10:10:52Z) -- the unpolished half of the
    discrimination fixture is exactly this case."""
    unpolished = (published_text_dir / "unpolished.md").read_text(encoding="utf-8")
    result = wp.bind(unpolished, published_text_dir / "transcript-discrimination.jsonl")
    assert result.strength == wp.NONE_STRENGTH


def test_containment_trap_full_body_embedded_not_equal_denies(published_text_dir, reader_facing):
    """POST_WITNESS is equality-only: a Write that merely CONTAINS the body
    (a heading/footer wrapped around it) must not bind."""
    result = wp.bind(reader_facing, published_text_dir / "transcript-containment-trap.jsonl")
    assert result.strength == wp.NONE_STRENGTH, result.strength


def test_containment_trap_short_body_embedded_not_equal_denies(published_text_dir):
    """The containment floor gates WRITER_OUTPUT containment specifically: a
    short body merely embedded in a tool_result must not bind."""
    short_body = (published_text_dir / "short-body.md").read_text(encoding="utf-8")
    assert len(wp.normalize(short_body)) < wp._MIN_CONTAINMENT_CHARS
    result = wp.bind(short_body, published_text_dir / "transcript-containment-trap.jsonl")
    assert result.strength == wp.NONE_STRENGTH, result.strength


def test_short_body_binds_writer_output_via_exact_equality(published_text_dir):
    """The same short body that is denied via containment (previous test)
    still binds WRITER_OUTPUT when a witness's tool_result equals it exactly
    -- the floor gates containment, not equality."""
    short_body = (published_text_dir / "short-body.md").read_text(encoding="utf-8")
    result = wp.bind(short_body, published_text_dir / "transcript-writer-output-equality.jsonl")
    assert result.strength == wp.WRITER_OUTPUT, result.strength


def test_spawned_writer_fixture_body_arrives_verbatim_binds_writer_output(published_text_dir):
    short_body = (published_text_dir / "short-body.md").read_text(encoding="utf-8")
    result = wp.bind(short_body, published_text_dir / "transcript-writer-output-equality.jsonl")
    assert result.strength == wp.WRITER_OUTPUT
    assert result.witness is not None
    assert result.witness.shape == "bash"


def test_body_differing_only_in_trailing_whitespace_still_binds(published_text_dir, reader_facing):
    result = wp.bind(reader_facing + "\n   ", published_text_dir / "transcript-witnessed.jsonl")
    assert result.strength != wp.NONE_STRENGTH


def test_missing_transcript_path_is_unreadable(published_text_dir, reader_facing):
    result = wp.bind(reader_facing, published_text_dir / "does-not-exist.jsonl")
    assert result.strength == wp.UNREADABLE


def test_witness_beyond_tail_bound_yields_no_witness_in_window(published_text_dir, reader_facing):
    result_full = wp.bind(reader_facing, published_text_dir / "transcript-witnessed.jsonl")
    assert result_full.strength != wp.NONE_STRENGTH
    tiny_bound = 10  # smaller than even the first line of the fixture -- forces truncation
    wp_scan = wp._scan(published_text_dir / "transcript-witnessed.jsonl", tiny_bound)
    assert wp_scan.witnesses == []
    assert wp_scan.truncated is True


def test_full_untruncated_read_with_no_witness_is_plain_none_not_window(published_text_dir, reader_facing):
    """A transcript small enough to be read WHOLE that carries no witness at
    all is plain NONE, not NO_WITNESS_IN_WINDOW -- the window outcome is
    reserved for a genuinely truncated read (see the module docstring)."""
    scan = wp._scan(published_text_dir / "transcript-unwitnessed.jsonl", wp._SCAN_TAIL_BYTES)
    assert scan.truncated is False
    result = wp.bind(reader_facing, published_text_dir / "transcript-unwitnessed.jsonl")
    assert result.strength == wp.NONE_STRENGTH


def test_witness_shapes_are_each_recognized():
    skill = {"type": "tool_use", "id": "a", "name": "Skill", "input": {"skill": "tech-writer"}}
    subagent_agent = {"type": "tool_use", "id": "b", "name": "Agent", "input": {"subagent_type": "tech-writer"}}
    subagent_task = {"type": "tool_use", "id": "c", "name": "Task", "input": {"subagent_type": "tech-writer"}}
    bash_kind = {
        "type": "tool_use",
        "id": "d",
        "name": "Bash",
        "input": {"command": "python3 scripts/spawn-specialist.py --kind tech-writer --task x"},
    }
    bash_path = {
        "type": "tool_use",
        "id": "e",
        "name": "Bash",
        "input": {"command": "cat ~/.claude/skills/tech-writer/SKILL.md"},
    }
    not_a_witness = {"type": "tool_use", "id": "f", "name": "Skill", "input": {"skill": "developer"}}
    assert wp._witness_shape(skill) == "skill"
    assert wp._witness_shape(subagent_agent) == "subagent"
    assert wp._witness_shape(subagent_task) == "subagent"
    assert wp._witness_shape(bash_kind) == "bash"
    assert wp._witness_shape(bash_path) == "bash"
    assert wp._witness_shape(not_a_witness) is None


def test_normalize_collapses_blank_runs_and_strips_edges():
    raw = "\n\nLine one.  \n\n\nLine two.\n\n"
    assert wp.normalize(raw) == "Line one.\n\nLine two."
