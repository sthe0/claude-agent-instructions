"""Tests for replay-permission-guard.py's two replay modes, the G1-CANDIDATE
heuristic, `--until` filtering (including the launch-time default), the
would-fire row shape (`source`/`day`/`commit`), `summary.md`'s report
sections, and the `--check-classified` coverage-gate flow. All corpus-mode
tests point `--corpus` at committed synthetic fixtures under
fixtures/permission_guard/ (never a live/`~/.claude` transcript root) so the
tool never runs against real machine data in this suite.

`--corpus` also means git-history mode never runs (`_run_replay` only calls
`_git_history_rows` when `args.corpus` is falsy) — git-history mode is
exercised separately, directly against a throwaway git repo built in a
tmp_path, never against this checkout's own real commit history.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPLAY_SCRIPT = SCRIPTS_DIR / "replay-permission-guard.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "permission_guard"

_SPEC = importlib.util.spec_from_file_location("replay_permission_guard", REPLAY_SCRIPT)
replay = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay)


def _read_rows(out_dir: Path) -> list[dict]:
    path = out_dir / "would-fires.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- transcript mode ---

def test_g4_fire_row_shape_from_single_fixture_file(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g4-fire.jsonl").write_text(
        (FIXTURES / "g4-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    rows = _read_rows(out_dir)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"id", "branch", "locator", "detail", "group", "source", "day"}
    assert row["branch"] == "G4"
    assert row["source"] == "transcript"
    assert row["day"] == "2026-01-01"
    assert row["group"] == f"G4:{row['detail']}"
    assert "g4-fire.jsonl" in row["locator"]
    assert "toolu_g4_1" in row["locator"]


def test_invalid_utf8_in_a_transcript_does_not_abort_the_replay(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g4-fire.jsonl").write_bytes(
        b'{"broken": "\xd1"}\n' + (FIXTURES / "g4-fire.jsonl").read_bytes(),
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    assert [r["branch"] for r in _read_rows(out_dir)] == ["G4"]


def test_no_fire_fixture_produces_zero_rows(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "no-fire.jsonl").write_text(
        (FIXTURES / "no-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    assert _read_rows(out_dir) == []


def test_edit_onto_live_settings_is_reported_as_g1_candidate_not_a_guard_fire(tmp_path):
    """`read_file=None` in transcript mode means G1-edit itself can never fire
    (there is no live disk to read the "before" text from) — the tool instead
    reports this shape as a G1-CANDIDATE human-review flag, and the guard's
    OWN decide_detailed (also invoked for Edit) allows it since none of
    G1-state/G3 apply either."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g1-candidate-edit.jsonl").write_text(
        (FIXTURES / "g1-candidate-edit.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    rows = _read_rows(out_dir)
    assert len(rows) == 1
    assert rows[0]["branch"] == "G1-CANDIDATE"
    assert rows[0]["source"] == "transcript"
    assert rows[0]["day"] == "2026-01-01"


def test_until_filters_out_events_after_the_cutoff(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "until-filter.jsonl").write_text(
        (FIXTURES / "until-filter.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main([
        "--out", str(out_dir), "--corpus", str(corpus), "--until", "2026-03-01T00:00:00Z",
    ])
    assert rc == 0
    rows = _read_rows(out_dir)
    assert len(rows) == 1
    assert "toolu_before" in rows[0]["locator"]


def test_omitting_until_uses_launch_time_not_unlimited(tmp_path, monkeypatch):
    """Omitting --until must NOT mean "no cutoff" — it means "now", pinned
    via a monkeypatched _launch_timestamp so the test is deterministic. An
    event after the pinned launch time is excluded, unlike the old
    "no cutoff" behavior this default replaces."""
    monkeypatch.setattr(replay, "_launch_timestamp", lambda: datetime(2026, 3, 1, tzinfo=timezone.utc))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "until-filter.jsonl").write_text(
        (FIXTURES / "until-filter.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    rows = _read_rows(out_dir)
    assert len(rows) == 1
    assert "toolu_before" in rows[0]["locator"]


def test_omitting_until_with_a_late_launch_time_includes_every_fixture_event(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "_launch_timestamp", lambda: datetime(2026, 12, 31, tzinfo=timezone.utc))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "until-filter.jsonl").write_text(
        (FIXTURES / "until-filter.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    assert len(_read_rows(out_dir)) == 2


def test_ids_identical_across_two_runs_over_the_same_corpus_and_until(tmp_path):
    """The id formula (branch|locator|detail) must not change with item 2's
    new source/day/commit fields — two runs over the same corpus and --until
    produce identical ids."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g4-fire.jsonl").write_text(
        (FIXTURES / "g4-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    replay.main(["--out", str(out1), "--corpus", str(corpus), "--until", "2026-12-31T00:00:00Z"])
    replay.main(["--out", str(out2), "--corpus", str(corpus), "--until", "2026-12-31T00:00:00Z"])
    assert [r["id"] for r in _read_rows(out1)] == [r["id"] for r in _read_rows(out2)]


def test_summary_md_reports_cutoff_and_branch_day_counts(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g4-fire.jsonl").write_text(
        (FIXTURES / "g4-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    replay.main(["--out", str(out_dir), "--corpus", str(corpus), "--until", "2026-12-31T00:00:00Z"])
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "cutoff: 2026-12-31" in summary
    assert "total would-fires: 1" in summary
    assert "## Counts by branch and session-day" in summary
    assert "| G4 | 2026-01-01 | 1 |" in summary
    assert "## Counts by branch and commit (G1-keys-calibration)" in summary
    assert "## Groups" in summary


def test_write_summary_reports_real_commit_counts_and_group_member_counts(tmp_path):
    rows = [
        {"id": "aaaa1111", "branch": "G1-keys-calibration", "locator": "l1", "detail": "d1",
         "group": "grp-a", "source": "git", "commit": "abc1234"},
        {"id": "bbbb2222", "branch": "G1-keys-calibration", "locator": "l2", "detail": "d1",
         "group": "grp-a", "source": "git", "commit": "abc1234"},
        {"id": "cccc3333", "branch": "G4", "locator": "l3", "detail": "d2",
         "group": "grp-b", "source": "transcript", "day": "2026-01-01"},
    ]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    replay._write_summary(out_dir, rows, 1893456000.0)
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "| G1-keys-calibration | abc1234 | 2 |" in summary
    assert "| grp-a | G1-keys-calibration | 2 |" in summary
    assert "| grp-b | G4 | 1 |" in summary


# --- git-history mode (isolated throwaway repo, never this checkout's own history) ---

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    import os
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        env={**os.environ, **env},
    )


def test_git_history_rows_fires_on_a_commit_adding_a_security_relevant_key(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    settings = repo / "settings.json"
    settings.write_text('{"permissions": {"allow": []}}', encoding="utf-8")
    _git(repo, "add", "settings.json")
    _git(repo, "commit", "-q", "-m", "base")
    settings.write_text('{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}', encoding="utf-8")
    _git(repo, "add", "settings.json")
    _git(repo, "commit", "-q", "-m", "widen")

    rows = replay._git_history_rows(repo, None)
    assert len(rows) == 1
    assert rows[0]["branch"] == "G1-keys-calibration"
    assert rows[0]["detail"] == "settings.json"
    assert rows[0]["source"] == "git"
    assert "commit" in rows[0]
    assert "day" not in rows[0]


def test_git_history_rows_skips_a_commit_with_no_security_relevant_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    settings = repo / "settings.json"
    settings.write_text('{"autoCompactWindow": 100000}', encoding="utf-8")
    _git(repo, "add", "settings.json")
    _git(repo, "commit", "-q", "-m", "base")
    settings.write_text('{"autoCompactWindow": 150000}', encoding="utf-8")
    _git(repo, "add", "settings.json")
    _git(repo, "commit", "-q", "-m", "tweak")

    rows = replay._git_history_rows(repo, None)
    assert rows == []


def test_replay_with_corpus_never_invokes_git_history_rows(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(replay, "_git_history_rows", lambda *a, **k: called.append(1) or [])
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "no-fire.jsonl").write_text(
        (FIXTURES / "no-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    replay.main(["--out", str(tmp_path / "out"), "--corpus", str(corpus)])
    assert called == []


# --- replay never touches the live guard's own log ---

def test_full_replay_run_leaves_the_guard_log_target_absent(tmp_path, monkeypatch):
    """A replay run must never write to the guard's own fire log — it calls
    decide_detailed directly, never main()/_log_fire, so the log path the
    live hook would write to (here, env-overridden into tmp_path) stays
    untouched by a replay run."""
    log_path = tmp_path / "would-be-guard-log.jsonl"
    monkeypatch.setenv("CLAUDE_PERMISSION_GUARD_LOG", str(log_path))
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "g4-fire.jsonl").write_text(
        (FIXTURES / "g4-fire.jsonl").read_text(encoding="utf-8"), encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = replay.main(["--out", str(out_dir), "--corpus", str(corpus)])
    assert rc == 0
    assert len(_read_rows(out_dir)) == 1
    assert not log_path.exists()


def test_replay_never_references_guard_main_or_log_fire_by_name():
    """Pins the module docstring's contract: the replay tool imports
    decide_detailed and nothing else guard-side — never guard.main, never
    guard._log_fire, and it never reads `os.environ` at all (so it can never
    read, let alone write, the guard's own log-path env var — the prose
    mentioning that env var's name, in the module's own docstring, is fine)."""
    source = REPLAY_SCRIPT.read_text(encoding="utf-8")
    assert "_guard.main" not in source
    assert "_guard._log_fire" not in source
    assert "import os" not in source
    assert "os.environ" not in source


# --- --check-classified ---

def _seed_would_fires(out_dir: Path, rows: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "would-fires.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _write_transcript_with_classification(path: Path, tsv_body: str, message_id: str = "msg1") -> None:
    text = f"key\tclass\treason\n{tsv_body}"
    entry = {
        "type": "assistant",
        "message": {"id": message_id, "role": "assistant", "content": [
            {"type": "text", "text": f"Classification:\n\n```classification\n{text}```\n"},
        ]},
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def test_check_classified_errors_when_would_fires_missing(tmp_path, capsys):
    rc = replay.main([
        "--out", str(tmp_path / "out"), "--check-classified",
        "--classification-from-transcript", str(tmp_path / "t.jsonl"),
    ])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_check_classified_errors_when_transcript_flag_missing(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [])
    rc = replay.main(["--out", str(out_dir), "--check-classified"])
    assert rc == 2
    assert "--classification-from-transcript" in capsys.readouterr().err


def test_check_classified_errors_when_no_fenced_block_found(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "no fence here"}]},
    }) + "\n", encoding="utf-8")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 2
    assert "fenced" in capsys.readouterr().err


def test_check_classified_joins_text_blocks_across_entries_sharing_one_message_id(tmp_path):
    """The harness can split one logical assistant turn across several JSONL
    entries; only the LAST message.id's text blocks — joined across every
    entry sharing that id, in transcript order — are searched for the fence."""
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "g1",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    entries = [
        {"type": "assistant", "message": {"id": "msg1", "role": "assistant", "content": [
            {"type": "text", "text": "Classification:\n\n```classification\naaaa1111\t"},
        ]}},
        {"type": "assistant", "message": {"id": "msg1", "role": "assistant", "content": [
            {"type": "text", "text": "true-positive\tpinned fire\n```\n"},
        ]}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0
    tsv = (out_dir / "classification.tsv").read_text(encoding="utf-8")
    assert "aaaa1111\ttrue-positive\tpinned fire" in tsv


def test_check_classified_group_row_covers_all_members(tmp_path):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "grp1",
         "source": "transcript", "day": "2026-01-01"},
        {"id": "bbbb2222", "branch": "G4", "locator": "l2", "detail": "d1", "group": "grp1",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(transcript, "grp1\tfalse-positive:ordinary\tgroup covers both\n")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0


def test_check_classified_member_row_overrides_its_group_row(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "grp1",
         "source": "transcript", "day": "2026-01-01"},
        {"id": "bbbb2222", "branch": "G4", "locator": "l2", "detail": "d1", "group": "grp1",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(
        transcript,
        "grp1\tfalse-positive:ordinary\tgroup default\naaaa1111\ttrue-positive\tspecific override\n",
    )
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "true-positive: 1" in out
    assert "false-positive:ordinary: 1" in out


def test_check_classified_rejects_a_row_with_an_invalid_class(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "g1",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(transcript, "aaaa1111\tintended\tstale verdict name\n")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "aaaa1111" in err
    assert "intended" in err


def test_check_classified_fails_on_uncovered_ids(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "g1",
         "source": "transcript", "day": "2026-01-01"},
        {"id": "bbbb2222", "branch": "G4", "locator": "l2", "detail": "d2", "group": "g2",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(transcript, "aaaa1111\ttrue-positive\tpinned fire\n")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 1
    assert "bbbb2222" in capsys.readouterr().err


def test_check_classified_passes_when_fully_covered_and_writes_classification_tsv(tmp_path):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "g1",
         "source": "transcript", "day": "2026-01-01"},
        {"id": "bbbb2222", "branch": "G1-bash", "locator": "l2", "detail": "d2", "group": "g2",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(
        transcript,
        "aaaa1111\ttrue-positive\tpinned fire\nbbbb2222\ttrue-positive\tpinned fire\n",
    )
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0
    tsv = (out_dir / "classification.tsv").read_text(encoding="utf-8")
    assert "aaaa1111\ttrue-positive\tpinned fire" in tsv
    assert "bbbb2222\ttrue-positive\tpinned fire" in tsv


def test_check_classified_prints_count_per_class(tmp_path, capsys):
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "aaaa1111", "branch": "G4", "locator": "l1", "detail": "d1", "group": "g1",
         "source": "transcript", "day": "2026-01-01"},
        {"id": "bbbb2222", "branch": "G1-bash", "locator": "l2", "detail": "d2", "group": "g2",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(
        transcript,
        "aaaa1111\ttrue-positive\tpinned fire\nbbbb2222\tfalse-positive:ordinary\tordinary work\n",
    )
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "true-positive: 1" in out
    assert "false-positive:ordinary: 1" in out


def test_check_classified_never_reruns_the_replay(tmp_path, monkeypatch):
    """`--check-classified` must read the already-written would-fires.jsonl
    verbatim, never regenerate it — the contract this test pins by seeding a
    row `_run_replay` could never itself produce (an unknown branch name) and
    confirming it still round-trips into classification.tsv."""
    monkeypatch.setattr(replay, "_run_replay", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    out_dir = tmp_path / "out"
    _seed_would_fires(out_dir, [
        {"id": "zzzz9999", "branch": "not-a-real-branch", "locator": "l", "detail": "d", "group": "g",
         "source": "transcript", "day": "2026-01-01"},
    ])
    transcript = tmp_path / "t.jsonl"
    _write_transcript_with_classification(transcript, "zzzz9999\tcandidate-benign\tout of scope\n")
    rc = replay.main([
        "--out", str(out_dir), "--check-classified",
        "--classification-from-transcript", str(transcript),
    ])
    assert rc == 0
