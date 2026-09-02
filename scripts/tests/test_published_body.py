"""Fixture-driven tests for lib/published_body.py.

Drives every case in fixtures/published-text/commands.json plus the specific
assertions the plan's Stage 3 Procedure names: shared-reader byte-identity,
missing-target UNRESOLVED (never empty TEXT), seam-parity, seam-required
regression, artifact_syntax_hint non-gating, and record_advisory fail-open.
"""
from __future__ import annotations

import json

import pytest

from lib import config_root, published_body


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))




@pytest.fixture
def published_text_dir(fixtures_dir):
    return fixtures_dir / "published-text"


@pytest.fixture
def repo_root(published_text_dir):
    """Fixture commands embed paths relative to the repo root (as an actual
    Bash tool call's cwd would be), not relative to the fixtures directory
    itself -- so resolution must run with cwd=repo_root."""
    return published_text_dir.parent.parent.parent.parent


@pytest.fixture
def seam(published_text_dir):
    return _load_json(published_text_dir / "seam.json")


@pytest.fixture
def commands(published_text_dir):
    return _load_json(published_text_dir / "commands.json")


def _resolve(case, cwd, seam):
    tool_input = {"command": case["command"]}
    used_seam = seam if case.get("seam") == "fixture" else None
    return published_body.resolve("Bash", tool_input, cwd=str(cwd), seam=used_seam)


def test_every_fixture_case_resolves_as_declared(commands, seam, repo_root):
    for case in commands:
        res = _resolve(case, repo_root, seam)
        assert res.kind == case["expect_kind"], case["label"]
        if "expect_shape" in case:
            assert res.shape == case["expect_shape"], case["label"]


def test_all_four_kinds_are_covered(commands):
    kinds = {c["expect_kind"] for c in commands}
    assert kinds == {"TEXT", "ATTACHMENT", "UNRESOLVED", "NOT_A_PUBLICATION"}


def test_at_least_six_distinct_shapes_are_covered(commands):
    shapes = {c["expect_shape"] for c in commands if "expect_shape" in c}
    assert shapes == {1, 2, 3, 4, 5, 6}


def test_seam_parity_gh_pr_create_body_file(commands, seam, repo_root):
    case = next(c for c in commands if c["label"] == "core-gh-pr-create-body-file (new Core shape)")
    assert case["expect_kind"] == "TEXT"
    res_no_seam = _resolve(case, repo_root, seam=None)
    assert res_no_seam.kind == "TEXT"


def test_seam_required_verb_is_not_a_publication_without_seam(commands, repo_root):
    case = next(c for c in commands if c.get("seam") == "fixture" and c["expect_kind"] != "NOT_A_PUBLICATION")
    res = _resolve(case, repo_root, seam=None)
    assert res.kind == "NOT_A_PUBLICATION"


def test_attachment_via_seam_is_present(commands):
    assert any(c["expect_kind"] == "ATTACHMENT" and c.get("seam") == "fixture" for c in commands)


def test_gh_pr_create_body_flag_case_is_present(commands):
    assert any(
        "gh pr create" in c["command"] and "--body" in c["command"] for c in commands
    )


def test_inline_path_read_both_forms_are_present(commands):
    cat_form = [c for c in commands if c["label"].startswith("shape6-inline-path-read-cat-form")]
    lt_form = [c for c in commands if c["label"].startswith("shape6-inline-path-read-lt-form")]
    assert cat_form and lt_form
    assert all(c["expect_shape"] == 6 for c in cat_form + lt_form)


def test_missing_target_case_is_present_with_valid_shape(commands):
    missing = [c for c in commands if c["expect_kind"] == "UNRESOLVED" and "expect_shape" in c]
    assert missing
    assert all(c["expect_shape"] in (1, 3, 4, 6) for c in missing)


def test_non_publication_bash_command_resolves_not_a_publication():
    res = published_body.resolve("Bash", {"command": "git status --short"}, cwd=".")
    assert res.kind == published_body.NOT_A_PUBLICATION


def test_attachment_verb_resolves_with_path(seam, repo_root):
    command = (
        "bash .claude/skills/tracker/scripts/tracker-cli.sh attachment-upload "
        "PROJ-467 scripts/tests/fixtures/published-text/artifact-dump.md"
    )
    res = published_body.resolve(
        "Bash", {"command": command}, cwd=str(repo_root), seam=seam
    )
    assert res.kind == published_body.ATTACHMENT
    assert res.path is not None
    assert res.path.endswith("artifact-dump.md")


def test_shape6_both_forms_are_byte_identical_to_shape1(repo_root, seam):
    cat_form = published_body.resolve(
        "Bash",
        {
            "command": (
                "gh issue comment 125 --repo sthe0/claude-agent-instructions --body "
                '"$(cat scripts/tests/fixtures/published-text/reader-facing.md)"'
            )
        },
        cwd=str(repo_root),
    )
    lt_form = published_body.resolve(
        "Bash",
        {
            "command": (
                'gh pr comment 42 --repo sthe0/claude-agent-instructions --body '
                '"$(< scripts/tests/fixtures/published-text/reader-facing.md)"'
            )
        },
        cwd=str(repo_root),
    )
    shape1 = published_body.resolve(
        "Bash",
        {
            "command": (
                "bash .claude/skills/tracker/scripts/tracker-cli.sh comment PROJ-467 "
                "--text @scripts/tests/fixtures/published-text/reader-facing.md"
            )
        },
        cwd=str(repo_root),
        seam=seam,
    )
    assert cat_form.kind == lt_form.kind == shape1.kind == published_body.TEXT
    assert cat_form.body == lt_form.body == shape1.body
    assert cat_form.body


def test_missing_target_resolves_unresolved_never_empty_text(repo_root):
    res = published_body.resolve(
        "Bash",
        {
            "command": (
                "gh issue comment 999 --repo sthe0/claude-agent-instructions "
                "--body-file scripts/tests/fixtures/published-text/does-not-exist.md"
            )
        },
        cwd=str(repo_root),
    )
    assert res.kind == published_body.UNRESOLVED
    assert res.body is None


def test_artifact_syntax_hint_flags_dump_but_not_reader_facing(published_text_dir):
    dump = (published_text_dir / "artifact-dump.md").read_text(encoding="utf-8")
    prose = (published_text_dir / "reader-facing.md").read_text(encoding="utf-8")
    assert published_body.artifact_syntax_hint(dump) != ""
    assert published_body.artifact_syntax_hint(prose) == ""


def test_artifact_syntax_hint_never_gates_a_decision(published_text_dir, seam, repo_root):
    dump = (published_text_dir / "artifact-dump.md").read_text(encoding="utf-8")
    assert published_body.artifact_syntax_hint(dump) != ""
    # shape 1 (file-valued flag), reading the dump fixture itself -- proves a
    # body that trips the hint still resolves normally: the hint never feeds
    # into resolve()'s own kind/shape decision.
    command = (
        "gh issue comment 125 --repo sthe0/claude-agent-instructions "
        "--body-file scripts/tests/fixtures/published-text/artifact-dump.md"
    )
    res = published_body.resolve("Bash", {"command": command}, cwd=str(repo_root), seam=seam)
    assert res.kind == published_body.TEXT
    assert res.body == dump


def test_record_advisory_writes_a_parseable_line(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path))
    published_body.record_advisory(published_body.UNRESOLVED, 4, "some $VAR reference")
    sink = tmp_path / "state" / "published-text-gate" / published_body.ADVISORY_SINK_NAME
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == published_body.UNRESOLVED
    assert record["shape"] == 4
    assert "command" not in record
    assert len(record["command_sha256"]) == 64
    assert "timestamp" in record


def test_record_advisory_swallows_write_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    unwritable = tmp_path / "not-a-real-dir-parent-is-a-file"
    unwritable.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(unwritable / "nested"))
    published_body.record_advisory(published_body.TEXT, 2, "irrelevant")


def test_is_publication_matches_resolve_kind(published_text_dir, seam):
    assert published_body.is_publication("Bash", {"command": "git status --short"}) is False
    command = (
        "bash .claude/skills/tracker/scripts/tracker-cli.sh comment PROJ-467 "
        '--text "hello"'
    )
    assert published_body.is_publication("Bash", {"command": command}, seam=seam) is True
