"""quality-regression-investigate.py: range resolution (explicit + ledger-window
modes), salience path filtering, the three tag heuristics, and ranking.

Follows the fixture-repo pattern of test_policy_scorecard_quality.py: a
throwaway git repo under tmp_path, commits dated via GIT_AUTHOR_DATE/
GIT_COMMITTER_DATE so history is deterministic instead of wall-clock-derived.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS_DIR / "quality-regression-investigate.py"

_GIT_AVAILABLE = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
pytestmark = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not available")


def _load_module():
    spec = importlib.util.spec_from_file_location("quality_regression_investigate_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def qri(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    return mod


# ------------------------------------------------------------- repo fixture

def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                     capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    return path


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, iso_ts: str, *, rename: tuple[str, str] | None = None) -> str:
    if rename:
        src, dst = repo / rename[0], repo / rename[1]
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    env = dict(os.environ, GIT_AUTHOR_DATE=iso_ts, GIT_COMMITTER_DATE=iso_ts)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True,
                   capture_output=True, env=env)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


# --------------------------------------------------------- 1. path filtering

def test_commit_shas_only_returns_commits_touching_salience_paths(tmp_path, qri):
    repo = _repo(tmp_path / "r1")
    _write(repo, {"CLAUDE.md": "x"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"README.md": "irrelevant change"})
    _commit(repo, "unrelated readme edit", "2026-01-02T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "x\ny"})
    bad = _commit(repo, "claude.md edit", "2026-01-03T00:00:00+00:00")

    shas = qri._commit_shas(repo, good, bad)

    assert len(shas) == 1
    subject = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s", shas[0]],
                             check=True, capture_output=True, text=True).stdout.strip()
    assert subject == "claude.md edit"


def test_commit_shas_empty_when_no_relevant_commits(tmp_path, qri):
    repo = _repo(tmp_path / "r2")
    _write(repo, {"README.md": "a"})
    good = _commit(repo, "c1", "2026-01-01T00:00:00+00:00")
    _write(repo, {"README.md": "b"})
    bad = _commit(repo, "c2", "2026-01-02T00:00:00+00:00")

    assert qri._commit_shas(repo, good, bad) == []


# ------------------------------------------------------------- 2. tag: prose-removed

def test_prose_removed_tag_on_net_deletion(tmp_path, qri):
    repo = _repo(tmp_path / "r3")
    _write(repo, {"CLAUDE.md": "line1\nline2\nline3\nline4\nline5\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "line1\n"})
    bad = _commit(repo, "shrink claude.md", "2026-01-02T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)

    assert len(commits) == 1
    assert "prose-removed" in commits[0]["tags"]
    assert commits[0]["net_prose_deletion"] == 4


def test_no_prose_removed_tag_on_net_addition(tmp_path, qri):
    repo = _repo(tmp_path / "r4")
    _write(repo, {"CLAUDE.md": "line1\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "line1\nline2\nline3\n"})
    bad = _commit(repo, "grow claude.md", "2026-01-02T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)

    assert "prose-removed" not in commits[0]["tags"]
    assert commits[0]["net_prose_deletion"] < 0


# --------------------------------------------------------------- 3. tag: rule-moved

def test_rule_moved_tag_on_rename(tmp_path, qri):
    repo = _repo(tmp_path / "r5")
    _write(repo, {"skills/foo/SKILL.md": "content"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    bad = _commit(repo, "move skill file", "2026-01-02T00:00:00+00:00",
                  rename=("skills/foo/SKILL.md", "skills/bar/SKILL.md"))

    commits = qri.investigate(repo, good, bad)

    assert len(commits) == 1
    assert "rule-moved" in commits[0]["tags"]


# --------------------------------------------------------------- 4. tag: mechanized

def test_mechanized_tag_on_hook_glob_path(tmp_path, qri):
    repo = _repo(tmp_path / "r6")
    _write(repo, {"scripts/hook-existing.py": "pass\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"scripts/hook-new.py": "pass\n"})
    bad = _commit(repo, "add new hook", "2026-01-02T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)

    assert len(commits) == 1
    assert "mechanized" in commits[0]["tags"]
    assert "prose-removed" not in commits[0]["tags"]


def test_mechanized_tag_on_agentctl_path(tmp_path, qri):
    repo = _repo(tmp_path / "r7")
    _write(repo, {"scripts/agentctl/state.py": "x = 1\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"scripts/agentctl/state.py": "x = 1\ny = 2\n"})
    bad = _commit(repo, "extend agentctl", "2026-01-02T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)

    assert "mechanized" in commits[0]["tags"]


# ------------------------------------------------------------------- 5. ranking

def test_ranking_largest_net_prose_deletion_first(tmp_path, qri):
    repo = _repo(tmp_path / "r8")
    _write(repo, {"CLAUDE.md": "a\nb\nc\nd\ne\n", "config.md": "1\n2\n3\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "a\n"})  # -4 net
    _commit(repo, "small shrink", "2026-01-02T00:00:00+00:00")
    _write(repo, {"config.md": "1\n"})  # -2 net
    bad = _commit(repo, "bigger shrink of config", "2026-01-03T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)
    subjects = [c["subject"] for c in commits]

    assert subjects[0] == "small shrink"  # net -4, ranked before net -2
    assert subjects[1] == "bigger shrink of config"


# ---------------------------------------------------------- 6. multiple tags

def test_commit_can_carry_multiple_tags(tmp_path, qri):
    repo = _repo(tmp_path / "r9")
    _write(repo, {"CLAUDE.md": "a\nb\nc\n", "scripts/hook-x.py": "pass\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "a\n", "scripts/hook-x.py": "pass\nmore\n"})
    bad = _commit(repo, "shrink prose and touch a hook", "2026-01-02T00:00:00+00:00")

    commits = qri.investigate(repo, good, bad)

    assert set(commits[0]["tags"]) == {"prose-removed", "mechanized"}


# --------------------------------------------------------- 7. window resolution

def _quality_row(ts: str, instructions_head: str | None) -> dict:
    return {"ts": ts, "task_id": "t", "session": "s", "quality": 3,
            "quality_by": "user", "resolved_by": "user",
            "instructions_head": instructions_head}


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_resolve_window_range_picks_earliest_good_and_latest_bad(qri):
    # good_days=10, bad_days=5, now=06-20 -> bad window [06-15, 06-20],
    # good window [06-05, 06-15).
    now = dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc)
    rows = [
        _quality_row("2026-06-06T00:00:00+00:00", "good-early"),
        _quality_row("2026-06-10T00:00:00+00:00", "good-late"),
        _quality_row("2026-06-16T00:00:00+00:00", "bad-early"),
        _quality_row("2026-06-19T00:00:00+00:00", "bad-late"),
    ]
    _write_ledger(qri.TASK_QUALITY_LEDGER, rows)

    good, bad = qri.resolve_window_range(good_days=10, bad_days=5, now=now)

    assert good == "good-early"
    assert bad == "bad-late"


def test_resolve_window_range_ledger_absent_raises_clear_error(qri):
    with pytest.raises(qri.InvestigationError, match="ledger not found"):
        qri.resolve_window_range(good_days=10, bad_days=5,
                                 now=dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc))


def test_resolve_window_range_no_rows_in_good_window_raises(qri):
    now = dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc)
    _write_ledger(qri.TASK_QUALITY_LEDGER, [
        _quality_row("2026-06-19T00:00:00+00:00", "bad-only"),
    ])

    with pytest.raises(qri.InvestigationError, match="'good' window"):
        qri.resolve_window_range(good_days=10, bad_days=5, now=now)


def test_resolve_window_range_no_rows_in_bad_window_raises(qri):
    now = dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc)
    _write_ledger(qri.TASK_QUALITY_LEDGER, [
        _quality_row("2026-06-08T00:00:00+00:00", "good-only"),
    ])

    with pytest.raises(qri.InvestigationError, match="'bad' window"):
        qri.resolve_window_range(good_days=10, bad_days=5, now=now)


def test_resolve_window_range_rows_missing_head_are_ignored(qri):
    now = dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc)
    _write_ledger(qri.TASK_QUALITY_LEDGER, [
        _quality_row("2026-06-01T00:00:00+00:00", None),
        _quality_row("2026-06-19T00:00:00+00:00", "bad-only"),
    ])

    with pytest.raises(qri.InvestigationError, match="'good' window"):
        qri.resolve_window_range(good_days=10, bad_days=5, now=now)


# ------------------------------------------------------------------- 8. CLI

def test_main_explicit_good_bad_exits_zero(tmp_path, qri, capsys):
    repo = _repo(tmp_path / "r10")
    _write(repo, {"CLAUDE.md": "a\n"})
    good = _commit(repo, "baseline", "2026-01-01T00:00:00+00:00")
    _write(repo, {"CLAUDE.md": "a\nb\n"})
    bad = _commit(repo, "grow", "2026-01-02T00:00:00+00:00")

    rc = qri.main(["--repo", str(repo), "--good", good, "--bad", bad])

    assert rc == 0
    out = capsys.readouterr().out
    assert "grow" in out
    assert qri.RUNBOOK_LEAF in out


def test_main_requires_both_good_and_bad(qri):
    with pytest.raises(SystemExit):
        qri.main(["--good", "abc"])


def test_main_rejects_mixing_explicit_and_window_modes(qri):
    with pytest.raises(SystemExit):
        qri.main(["--good", "abc", "--bad", "def", "--good-days", "1", "--bad-days", "1"])


def test_main_ledger_absent_error_exits_nonzero(tmp_path, qri, capsys):
    rc = qri.main(["--good-days", "10", "--bad-days", "5"])

    assert rc == 1
    assert "ledger not found" in capsys.readouterr().err
