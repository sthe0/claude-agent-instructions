"""policy-scorecard.py Stage 2: user-signal counters, task-quality aggregation
(joined to the session ledger), degradation flags, and instruction-commit
range rendering when a quality flag fires.

Complements the pre-existing scorecard tests (session metrics, spawn
counting, existing flags) by exercising only the additive Stage 2 surface:
`user_signals`, `instructions_head`, `load_quality_ledger`/`_aggregate_quality`,
the two new `_flags` conditions, and `_commit_range_lines`/its wiring into
`scorecard()`.
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
SCRIPT = SCRIPTS_DIR / "policy-scorecard.py"

_GIT_AVAILABLE = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
needs_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason="git not available")


def _load_module():
    spec = importlib.util.spec_from_file_location("policy_scorecard_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ps(monkeypatch, tmp_path):
    """A freshly-loaded module instance with every real-machine path
    redirected into tmp_path, so no test reads or mutates this machine's
    actual ledgers/gate-log/instructions repo."""
    mod = _load_module()
    monkeypatch.setattr(mod, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(mod, "TASK_QUALITY_LEDGER", tmp_path / "task-quality.jsonl")
    monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(mod, "GATE_LOGS", (tmp_path / "no-gate-log.jsonl",))
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path / "no-instrepo")
    return mod


# --------------------------------------------------------------- git fixture

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a, **kw: subprocess.run(["git", "-C", str(path), *a], check=True,
                                          capture_output=True, **kw)
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    return path


def _commit(repo: Path, message: str, iso_ts: str) -> str:
    """Write `message` into CLAUDE.md and commit it dated `iso_ts` (so
    `_instructions_head_at`/`_commit_range_lines` see deterministic history
    instead of the test's wall-clock)."""
    (repo / "CLAUDE.md").write_text(message, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    env = dict(os.environ, GIT_AUTHOR_DATE=iso_ts, GIT_COMMITTER_DATE=iso_ts)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True,
                   capture_output=True, env=env)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


# --------------------------------------------------------- transcript fixture

def _write_transcript(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _user_text(ts: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _assistant_tool_use(ts: str, tool_use_id: str, name: str, input_: dict) -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-sonnet-5", "content": [
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": input_}
            ]}}


def _tool_result(ts: str, tool_use_id: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts,
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
            ]}}


# ------------------------------------------------------- session-row fixture

def _session_row(session_id: str, last_ts: str, *, corrections=0, questions=0,
                  freetext=0, interrupts=0, quality_rating=None) -> dict:
    return {
        "session_id": session_id,
        "project": "proj",
        "date": last_ts[:10],
        "first_ts": last_ts,
        "last_ts": last_ts,
        "instructions_head": None,
        "mtime": 0.0,
        "model_tokens": {k: {"in": 0, "out": 0, "cache_read": 0, "cache_create": 0}
                        for k in ("opus", "sonnet", "haiku")},
        "cost_usd": 0.01,
        "cache_read_usd": 0.0,
        "main_read_bash": 0,
        "agent_spawns": {"total": 0, "opus": 0, "sonnet": 0, "haiku": 0,
                         "no_explicit_model": 0, "inherit_opus": 0},
        "missed_delegation_clusters": 0,
        "attention": {"askq": 0, "prompts": 1, "interrupts": interrupts, "corrections": corrections},
        "user_signals": {"n_user_corrections": corrections, "n_user_questions": questions,
                         "n_freetext_askuser_answers": freetext, "n_interrupts": interrupts},
        "effectiveness": {"resolution_confirmed": 0, "replans": 0, "overcome_difficulty": 0,
                          "subagent_failures": 0, "rework_edits": 0},
        "quality_rating": quality_rating,
        "quality_note": None,
    }


def _quality_row(session: str, ts: str, quality: float, instructions_head: str | None,
                 task_id: str = "t1") -> dict:
    return {
        "ts": ts, "task_id": task_id, "session": session, "quality": quality,
        "quality_by": "user", "quality_note": None, "resolved_by": "user",
        "instructions_head": instructions_head,
        "n_stages": 1, "n_failed_stage_results": 0, "n_replans": 0,
        "n_difficulty_records": 0, "spawn_count": 0, "total_cost_usd": 0.0,
    }


def _iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + delta).isoformat()


# ----------------------------------------------------- 1. per-counter tests

def test_scan_session_counts_all_four_user_signals(tmp_path, ps):
    main_file = tmp_path / "projects" / "proj" / "sess-1.jsonl"
    _write_transcript(main_file, [
        _user_text("2026-06-05T10:00:00Z", "Actually, that's wrong, let's redo it."),
        _user_text("2026-06-05T10:01:00Z", "Why does this happen in prod?"),
        _assistant_tool_use("2026-06-05T10:02:00Z", "tu-1", "AskUserQuestion", {
            "questions": [{"question": "Apply the fix?",
                          "options": [{"label": "Yes"}, {"label": "No"}]}]
        }),
        _tool_result("2026-06-05T10:03:00Z", "tu-1",
                     '"Apply the fix?"="Let\'s try something different instead"'),
        _user_text("2026-06-05T10:04:00Z", "[Request interrupted by user]"),
    ])

    row = ps._scan_session(main_file)

    assert row["user_signals"] == {
        "n_user_corrections": 1,
        "n_user_questions": 1,
        "n_freetext_askuser_answers": 1,
        "n_interrupts": 1,
    }
    # existing counters must still be computed the same way (additive-only invariant)
    assert row["attention"] == {"askq": 1, "prompts": 2, "interrupts": 1, "corrections": 1}


def test_scan_session_askuserquestion_matching_option_not_freetext(tmp_path, ps):
    main_file = tmp_path / "projects" / "proj" / "sess-2.jsonl"
    _write_transcript(main_file, [
        _user_text("2026-06-05T10:00:00Z", "please continue"),
        _assistant_tool_use("2026-06-05T10:01:00Z", "tu-1", "AskUserQuestion", {
            "questions": [{"question": "Apply the fix?",
                          "options": [{"label": "Yes"}, {"label": "No"}]}]
        }),
        _tool_result("2026-06-05T10:02:00Z", "tu-1", '"Apply the fix?"="Yes"'),
    ])

    row = ps._scan_session(main_file)

    assert row["user_signals"]["n_freetext_askuser_answers"] == 0


def test_scan_session_zero_signals_when_no_events(tmp_path, ps):
    main_file = tmp_path / "projects" / "proj" / "sess-plain.jsonl"
    _write_transcript(main_file, [
        _user_text("2026-06-05T00:00:00Z", "please add a test"),
    ])

    row = ps._scan_session(main_file)

    assert row["user_signals"] == {
        "n_user_corrections": 0, "n_user_questions": 0,
        "n_freetext_askuser_answers": 0, "n_interrupts": 0,
    }


# --------------------------------------------------- 2. instructions_head

@needs_git
def test_instructions_head_at_picks_last_commit_before_ts(monkeypatch, tmp_path, ps):
    repo = _git_repo(tmp_path / "instrepo")
    c1 = _commit(repo, "c1", "2026-06-01T00:00:00+00:00")
    c2 = _commit(repo, "c2", "2026-06-10T00:00:00+00:00")
    monkeypatch.setattr(ps, "REPO_ROOT", repo)

    assert ps._instructions_head_at(dt.datetime.fromisoformat("2026-06-05T00:00:00+00:00")) == c1
    assert ps._instructions_head_at(dt.datetime.fromisoformat("2026-06-15T00:00:00+00:00")) == c2
    assert ps._instructions_head_at(dt.datetime.fromisoformat("2026-05-01T00:00:00+00:00")) is None


@needs_git
def test_scan_session_stamps_instructions_head(monkeypatch, tmp_path, ps):
    repo = _git_repo(tmp_path / "instrepo")
    c1 = _commit(repo, "c1", "2026-06-01T00:00:00+00:00")
    _commit(repo, "c2", "2026-06-10T00:00:00+00:00")
    monkeypatch.setattr(ps, "REPO_ROOT", repo)

    main_file = tmp_path / "projects" / "proj" / "sess-3.jsonl"
    _write_transcript(main_file, [_user_text("2026-06-05T00:00:00Z", "hello")])

    row = ps._scan_session(main_file)

    assert row["instructions_head"] == c1


def test_instructions_head_at_none_when_not_a_repo(ps):
    assert ps._instructions_head_at(dt.datetime.now(dt.timezone.utc)) is None


# ------------------------------------------------------- 3. quality ledger

def test_load_quality_ledger_missing_file_is_empty_list(ps):
    assert ps.load_quality_ledger() == []


def test_load_quality_ledger_reads_rows_tolerating_bad_lines(ps):
    ps.TASK_QUALITY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    ps.TASK_QUALITY_LEDGER.write_text(
        json.dumps(_quality_row("s1", "2026-06-05T00:00:00Z", 4, None)) + "\n"
        "not json\n"
        "\n",
        encoding="utf-8",
    )

    rows = ps.load_quality_ledger()

    assert len(rows) == 1
    assert rows[0]["session"] == "s1"


def test_aggregate_quality_joins_session_rows_and_averages_signals(ps):
    session_rows = {
        "s1": _session_row("s1", "2026-06-05T00:00:00Z", corrections=2, questions=1, freetext=1),
        "s2": _session_row("s2", "2026-06-06T00:00:00Z", corrections=0, questions=0, freetext=0),
    }
    task_rows = [
        _quality_row("s1", "2026-06-05T01:00:00Z", 4, "abc"),
        _quality_row("s2", "2026-06-06T01:00:00Z", 2, "def"),
    ]

    agg = ps._aggregate_quality(task_rows, session_rows)

    assert agg["n_tasks"] == 2
    assert agg["n_rated"] == 2
    assert agg["avg_rating"] == 3.0
    assert agg["n_joined"] == 2
    assert agg["avg_corrections"] == 1.0
    assert agg["avg_questions"] == 0.5
    assert agg["avg_freetext"] == 0.5
    assert agg["avg_interrupts"] == 0.0
    assert agg["correction_rate"] == 1.5  # avg_corrections + avg_freetext
    assert agg["last_instructions_head"] == "def"  # most recent by ts


def test_aggregate_quality_unjoined_task_row_skipped_gracefully(ps):
    agg = ps._aggregate_quality(
        [_quality_row("missing-session", "2026-06-05T00:00:00Z", 5, "abc")], {})

    assert agg["n_tasks"] == 1
    assert agg["avg_rating"] == 5.0
    assert agg["n_joined"] == 0
    assert agg["avg_corrections"] is None
    assert agg["correction_rate"] is None


def test_aggregate_quality_empty_window(ps):
    agg = ps._aggregate_quality([], {})
    assert agg == {
        "n_tasks": 0, "avg_rating": None, "n_rated": 0, "n_joined": 0,
        "avg_corrections": None, "avg_questions": None, "avg_freetext": None,
        "avg_interrupts": None, "correction_rate": None, "last_instructions_head": None,
    }


# --------------------------------------------------------- 4. flag thresholds

def _neutral_agg(**overrides) -> dict:
    base = {
        "sessions": 10, "spawns_total": 0, "inherit_opus_rate": 0.0, "inherit_opus": 0,
        "clusters_per_session": 0.0, "clusters": 0, "cost_per_session": 1.0,
        "resolution_rate": 0.9, "avg_quality": None, "n_rated": 0,
    }
    base.update(overrides)
    return base


def test_flags_none_quality_args_backward_compatible(ps):
    assert ps._flags(_neutral_agg(), _neutral_agg()) == []


def test_flags_task_quality_below_absolute_threshold(ps):
    cur_q = {"avg_rating": 3.0, "n_rated": 4, "correction_rate": None}
    prev_q = {"avg_rating": 3.2, "n_rated": 4, "correction_rate": None}

    flags = ps._flags(_neutral_agg(), _neutral_agg(), cur_q, prev_q)

    assert any(f.startswith("task quality avg") and "< 3.5" in f for f in flags)


def test_flags_task_quality_down_vs_previous_window(ps):
    cur_q = {"avg_rating": 4.0, "n_rated": 4, "correction_rate": None}
    prev_q = {"avg_rating": 4.6, "n_rated": 4, "correction_rate": None}

    flags = ps._flags(_neutral_agg(), _neutral_agg(), cur_q, prev_q)

    assert any(f.startswith("task quality avg") and "down 4.6" in f for f in flags)


def test_flags_task_quality_healthy_no_flag(ps):
    cur_q = {"avg_rating": 4.5, "n_rated": 4, "correction_rate": None}
    prev_q = {"avg_rating": 4.4, "n_rated": 4, "correction_rate": None}

    flags = ps._flags(_neutral_agg(), _neutral_agg(), cur_q, prev_q)

    assert not any(f.startswith("task quality avg") for f in flags)


def test_flags_correction_rate_up_over_1_5x(ps):
    cur_q = {"avg_rating": None, "n_rated": 0, "correction_rate": 2.0}
    prev_q = {"avg_rating": None, "n_rated": 0, "correction_rate": 1.0}

    flags = ps._flags(_neutral_agg(), _neutral_agg(), cur_q, prev_q)

    assert any("user-correction/free-text-answer rate" in f for f in flags)


def test_flags_correction_rate_below_threshold_no_flag(ps):
    cur_q = {"avg_rating": None, "n_rated": 0, "correction_rate": 1.2}
    prev_q = {"avg_rating": None, "n_rated": 0, "correction_rate": 1.0}

    flags = ps._flags(_neutral_agg(), _neutral_agg(), cur_q, prev_q)

    assert not any("user-correction/free-text-answer rate" in f for f in flags)


# --------------------------------------------------- 5. commit-range rendering

@needs_git
def test_commit_range_lines_between_two_commits(monkeypatch, tmp_path, ps):
    repo = _git_repo(tmp_path / "instrepo")
    c1 = _commit(repo, "first", "2026-01-01T00:00:00+00:00")
    c2 = _commit(repo, "second change", "2026-01-02T00:00:00+00:00")
    monkeypatch.setattr(ps, "REPO_ROOT", repo)

    lines = ps._commit_range_lines(c1, c2)

    assert len(lines) == 1
    assert "second change" in lines[0]


def test_commit_range_lines_empty_when_ref_missing_or_equal(ps):
    assert ps._commit_range_lines(None, "x") == []
    assert ps._commit_range_lines("x", None) == []
    assert ps._commit_range_lines("x", "x") == []


@needs_git
def test_scorecard_renders_commit_range_on_quality_flag(monkeypatch, tmp_path, ps):
    repo = _git_repo(tmp_path / "instrepo")
    good = _commit(repo, "good baseline", "2026-01-01T00:00:00+00:00")
    bad = _commit(repo, "regression-causing change", "2026-01-05T00:00:00+00:00")
    monkeypatch.setattr(ps, "REPO_ROOT", repo)

    cur_ts = _iso(dt.timedelta(days=-2))
    prev_ts = _iso(dt.timedelta(days=-9))
    rows = {
        "s-cur": _session_row("s-cur", cur_ts),
        "s-prev": _session_row("s-prev", prev_ts),
    }
    ps.TASK_QUALITY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with ps.TASK_QUALITY_LEDGER.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_quality_row("s-cur", cur_ts, 2, bad)) + "\n")
        f.write(json.dumps(_quality_row("s-prev", prev_ts, 4.5, good)) + "\n")

    out = ps.scorecard(rows, days=7, project=None)

    assert "## Task quality" in out
    assert "task quality avg" in out
    assert f"`{good[:12]}..{bad[:12]}`" in out
    assert "regression-causing change" in out
    assert "scripts/quality-regression-investigate.py" in out


def test_scorecard_task_quality_section_absent_ledger_degrades_gracefully(ps):
    out = ps.scorecard({}, days=7, project=None)

    assert "## Task quality" in out
    assert "no task-quality rows found" in out
    assert "- none past threshold this window." in out
