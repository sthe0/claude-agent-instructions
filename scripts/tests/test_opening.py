"""Tests for project_entry.opening — pure-function unit tests + CLI/subprocess
tests for the emit contract (exit 0 / exit 3 suppression / other-nonzero crash).

Mirrors scripts/tests/test_detect_backend.py (same pure-function-then-subprocess
shape).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from project_entry.opening import (
    Artifact,
    build_brief,
    probe_branches,
    probe_plan_files,
    probe_tracker_comments,
    resume_candidacy,
)

SCRIPTS = Path(__file__).resolve().parents[1]
OPENING = SCRIPTS / "project_entry" / "opening.py"
PROJECTS_PY = SCRIPTS / "project_entry" / "projects.py"


# ── Pure-function tests ──────────────────────────────────────────────────────


def test_probe_plan_files_matches_content_not_filename():
    """The slug-named-fixture case: a plan file's NAME never mentions the key —
    only its content does. This is the case the P1 mutation proof exercises."""
    files = {"/plans/some-unrelated-slug.toml": "[meta]\ntask_id = \"ABC-123\"\n"}
    found = probe_plan_files(
        "ABC-123",
        list_plan_files=lambda: list(files),
        read_file=lambda p: files.get(p),
    )
    assert len(found) == 1
    assert "some-unrelated-slug.toml" in found[0].detail


def test_probe_plan_files_case_insensitive_and_no_match():
    files = {"/plans/x.toml": "task_id = \"abc-123\"\n"}
    assert probe_plan_files("ABC-123", lambda: list(files), lambda p: files.get(p))
    assert not probe_plan_files("ZZZ-999", lambda: list(files), lambda p: files.get(p))


def test_probe_plan_files_skips_unreadable_file():
    found = probe_plan_files("K", lambda: ["/plans/gone.toml"], lambda p: None)
    assert found == []


def test_probe_tracker_comments_fires_on_login_match():
    text = "--- comment 1 by alice at 2024-01-02T03:04:05Z ---\nsome body\n"
    found = probe_tracker_comments("alice", ticket_ok=True, ticket_text=text)
    assert len(found) == 1


def test_probe_tracker_comments_abstains_without_identity():
    text = "--- comment 1 by alice at 2024-01-02T03:04:05Z ---\n"
    assert probe_tracker_comments(None, ticket_ok=True, ticket_text=text) == []
    assert probe_tracker_comments("", ticket_ok=True, ticket_text=text) == []


def test_probe_tracker_comments_abstains_when_ticket_unreadable():
    text = "--- comment 1 by alice at 2024-01-02T03:04:05Z ---\n"
    assert probe_tracker_comments("alice", ticket_ok=False, ticket_text=text) == []


def test_probe_tracker_comments_no_match_for_other_login():
    text = "--- comment 1 by bob at 2024-01-02T03:04:05Z ---\n"
    assert probe_tracker_comments("alice", ticket_ok=True, ticket_text=text) == []


def test_probe_branches_matches_prefix_with_commits_ahead():
    found = probe_branches(
        "ABC-123",
        list_branches=lambda: ["ABC-123-my-slug", "unrelated"],
        commits_ahead=lambda b: 2,
    )
    assert len(found) == 1
    assert "ABC-123-my-slug" in found[0].detail


def test_probe_branches_zero_ahead_does_not_fire():
    found = probe_branches(
        "ABC-123",
        list_branches=lambda: ["ABC-123-my-slug"],
        commits_ahead=lambda b: 0,
    )
    assert found == []


def test_probe_branches_non_prefix_does_not_fire():
    found = probe_branches(
        "ABC-123",
        list_branches=lambda: ["totally-different"],
        commits_ahead=lambda b: 5,
    )
    assert found == []


def test_resume_candidacy_ors_all_three_probes():
    found = resume_candidacy(
        "K",
        list_plan_files=lambda: [],
        read_file=lambda p: None,
        agent_login=None,
        ticket_ok=False,
        ticket_text="",
        list_branches=lambda: ["K-slug"],
        commits_ahead=lambda b: 1,
    )
    assert len(found) == 1  # only P3 fired


def test_build_brief_zero_artifacts_forces_opening():
    brief = build_brief(ticket_ok=True, ticket_text="hello", ticket_reason=None, artifacts=[])
    assert "mode: opening" in brief
    assert "artifacts: (none)" in brief


def test_build_brief_artifacts_force_resume_candidate():
    brief = build_brief(
        ticket_ok=True, ticket_text="hello", ticket_reason=None,
        artifacts=[Artifact("plan", "x.toml mentions K")],
    )
    assert "mode: resume-candidate" in brief


def test_build_brief_q7_ticket_unreadable_never_touches_mode():
    """Q7: an unreadable ticket affects only ticket:, never mode:."""
    brief = build_brief(
        ticket_ok=False, ticket_text="", ticket_reason="gh: rate limited",
        artifacts=[Artifact("plan", "x.toml mentions K")],
    )
    assert "ticket: unavailable (gh: rate limited)" in brief
    assert "mode: resume-candidate" in brief


# ── CLI/subprocess helpers ───────────────────────────────────────────────────


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def _make_git_stub(tmp: Path, branches: "list[str]", ahead_count: int = 1, garbage_for: str = "") -> Path:
    """A stub `git` that lists `branches`, has no remote (forces the origin/main
    / main rev-parse fallback in _real_merge_base_ref), and reports `ahead_count`
    commits for `rev-list --count`. When the range's branch side equals
    `garbage_for`, it prints a non-numeric count instead — this is the "raising
    git runner" injection for the internal-crash test."""
    branch_lines = "\n".join(branches)
    stub = tmp / "git-stub"
    _write_exec(
        stub,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            shift  # -C
            shift  # <dir>
            case "$1" in
              branch)
                printf '%s\\n' {shlex.quote(branch_lines)}
                ;;
              symbolic-ref)
                exit 1
                ;;
              rev-parse)
                if [[ "$4" == "main" ]]; then exit 0; else exit 1; fi
                ;;
              rev-list)
                range="$3"
                br="${{range#*..}}"
                if [[ -n {shlex.quote(garbage_for)} && "$br" == {shlex.quote(garbage_for)} ]]; then
                  printf 'not-a-number\\n'
                else
                  printf '%s\\n' "{ahead_count}"
                fi
                ;;
              *) exit 0 ;;
            esac
            """
        ),
    )
    return stub


def _run(args: "list[str]", env: dict) -> "subprocess.CompletedProcess":
    return subprocess.run(["python3", str(OPENING)] + args, env=env, capture_output=True, text=True)


@pytest.fixture
def base_env(tmp_path):
    git_stub = _make_git_stub(tmp_path, branches=[])
    env = dict(os.environ)
    env["GIT_BIN"] = str(git_stub)
    for k in ("CLAUDE_OPENING", "CLAUDE_AGENT_LOGIN"):
        env.pop(k, None)
    return env


# ── CLI: --title path ────────────────────────────────────────────────────────


def test_cli_title_path_always_opening(base_env, tmp_path):
    r = _run(["emit", "--dir", str(tmp_path), "--title", "New feature"], base_env)
    assert r.returncode == 0, r.stderr
    assert "mode: opening" in r.stdout
    assert "ticket: unavailable (new task (no ticket yet))" in r.stdout


# ── CLI: suppression ─────────────────────────────────────────────────────────


def test_cli_suppressed_by_env_exits_3_with_empty_stdout(base_env, tmp_path):
    base_env["CLAUDE_OPENING"] = "off"
    r = _run(["emit", "--dir", str(tmp_path), "--key", "ABC-123"], base_env)
    assert r.returncode == 3
    assert r.stdout == ""


# ── CLI: zero artifacts ──────────────────────────────────────────────────────


def test_cli_zero_artifacts_mode_opening(base_env, tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    r = _run(
        ["emit", "--dir", str(tmp_path), "--key", "ZZZ-999", "--plans-dir", str(plans_dir)],
        base_env,
    )
    assert r.returncode == 0, r.stderr
    assert "mode: opening" in r.stdout
    assert "artifacts: (none)" in r.stdout


# ── CLI: P1 production-shaped (slug-named) plan file ────────────────────────


def test_cli_plan_file_content_match_slug_named_fixture(base_env, tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_file = plans_dir / "some-unrelated-task-slug.toml"
    plan_file.write_text('[meta]\ntask_id = "ABC-123"\ntitle = "whatever"\n')
    r = _run(
        ["emit", "--dir", str(tmp_path), "--key", "ABC-123", "--plans-dir", str(plans_dir)],
        base_env,
    )
    assert r.returncode == 0, r.stderr
    assert "mode: resume-candidate" in r.stdout
    assert "some-unrelated-task-slug.toml" in r.stdout


# ── CLI: P3 production-shaped (KEY-slug) branch name ────────────────────────


def test_cli_branch_prefix_match_with_commits_ahead(tmp_path):
    git_stub = _make_git_stub(tmp_path, branches=["ABC-123-my-slug"], ahead_count=2)
    env = dict(os.environ)
    env["GIT_BIN"] = str(git_stub)
    env.pop("CLAUDE_OPENING", None)
    r = _run(["emit", "--dir", str(tmp_path), "--key", "ABC-123"], env)
    assert r.returncode == 0, r.stderr
    assert "mode: resume-candidate" in r.stdout
    assert "ABC-123-my-slug" in r.stdout


def test_cli_branch_prefix_match_zero_ahead_does_not_fire(tmp_path):
    git_stub = _make_git_stub(tmp_path, branches=["ABC-123-my-slug"], ahead_count=0)
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    env = dict(os.environ)
    env["GIT_BIN"] = str(git_stub)
    env.pop("CLAUDE_OPENING", None)
    r = _run(
        ["emit", "--dir", str(tmp_path), "--key", "ABC-123", "--plans-dir", str(plans_dir)],
        env,
    )
    assert r.returncode == 0, r.stderr
    assert "mode: opening" in r.stdout


# ── CLI: P2 gated on CLAUDE_AGENT_LOGIN ──────────────────────────────────────


def test_cli_agent_login_unset_p2_abstains(base_env, tmp_path):
    ticket_file = tmp_path / "ticket.txt"
    ticket_file.write_text("--- comment 1 by alice at 2024-01-02T03:04:05Z ---\nhi\n")
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    r = _run(
        [
            "emit", "--dir", str(tmp_path), "--key", "ABC-123",
            "--ticket-file", str(ticket_file), "--plans-dir", str(plans_dir),
        ],
        base_env,
    )
    assert r.returncode == 0, r.stderr
    assert "mode: opening" in r.stdout


def test_cli_agent_login_set_p2_fires(base_env, tmp_path):
    ticket_file = tmp_path / "ticket.txt"
    ticket_file.write_text("--- comment 1 by alice at 2024-01-02T03:04:05Z ---\nhi\n")
    base_env["CLAUDE_AGENT_LOGIN"] = "alice"
    r = _run(
        ["emit", "--dir", str(tmp_path), "--key", "ABC-123", "--ticket-file", str(ticket_file)],
        base_env,
    )
    assert r.returncode == 0, r.stderr
    assert "mode: resume-candidate" in r.stdout
    assert "tracker-comment" in r.stdout


# ── CLI: Q6/Q7 separation — unreadable ticket + existing plan file ──────────


def test_cli_unreadable_ticket_with_existing_plan_file_q6_q7_separation(base_env, tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / "some-slug.toml").write_text('task_id = "ABC-123"\n')
    r = _run(
        [
            "emit", "--dir", str(tmp_path), "--key", "ABC-123",
            "--plans-dir", str(plans_dir),
            "--ticket-unavailable", "gh: rate limited",
        ],
        base_env,
    )
    assert r.returncode == 0, r.stderr
    assert "ticket: unavailable (gh: rate limited)" in r.stdout
    assert "mode: resume-candidate" in r.stdout


# ── CLI: internal crash is never conflated with suppression ─────────────────


def test_cli_internal_crash_on_garbage_git_output_is_not_exit_3(tmp_path):
    git_stub = _make_git_stub(
        tmp_path, branches=["ABC-123-x"], garbage_for="ABC-123-x",
    )
    env = dict(os.environ)
    env["GIT_BIN"] = str(git_stub)
    env.pop("CLAUDE_OPENING", None)
    r = _run(["emit", "--dir", str(tmp_path), "--key", "ABC-123"], env)
    assert r.returncode != 0
    assert r.returncode != 3
    assert r.stdout == ""


# ── CLI: usage errors are never conflated with suppression either ──────────


def test_cli_usage_error_missing_dir_is_not_exit_3(base_env):
    r = _run(["emit", "--key", "ABC-123"], base_env)
    assert r.returncode != 0
    assert r.returncode != 3
    assert r.stdout == ""


# ── projects.py: opening_prompt_path override survives the `fields` boundary ─
#
# load_records' unknown-key pass-through is not enough on its own: the
# `fields` subcommand filters to _KNOWN_FIELDS and additionally requires
# isinstance(val, str). A field absent from that tuple is silently dropped
# at the CLI boundary rather than raising.


def test_projects_fields_echoes_opening_prompt_path_override(tmp_path):
    root = tmp_path / "registry"
    rec_dir = root / "team" / "web"
    rec_dir.mkdir(parents=True)
    (rec_dir / "agent-project.json").write_text(
        json.dumps({"opening_prompt_path": "/custom/opening-prompt.md"})
    )
    env = dict(os.environ)
    env["CLAUDE_PROJECT_ROOTS"] = str(root)
    r = subprocess.run(
        ["python3", str(PROJECTS_PY), "fields", "team/web"],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "opening_prompt_path=/custom/opening-prompt.md" in r.stdout
