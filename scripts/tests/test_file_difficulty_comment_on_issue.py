"""file-difficulty.py --comment-on-issue N: comment on issue N only when the dedup judges N the
same difficulty; otherwise exit 4 with a stderr line, filing nothing and commenting nowhere.

Offline: in-process fake channel, verdicts from a seeded cache (key formula inlined from
semantic_join) or a stubbed subprocess runner. Collects on a tree without the stage-2 code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

import difficulty_channel as dc

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("file_difficulty_coi", _SCRIPTS / "file-difficulty.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
main = _mod.main

FIXED_TS = "2026-06-27T00:00:00+00:00"
VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"
BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
GROUND = "agent skips the plan approval gate on small edits"
REWORDED = "small edits bypass the approval gate of the plan"
EVIDENCE = "evidence line one\nevidence line two\n"


@pytest.fixture(autouse=True)
def _non_author(monkeypatch):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "absent-verdicts.json"))
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "no-plugins"))
    monkeypatch.setenv("CLAUDE_TERM_RULESET_DIR", str(tmp_path / "no-rulesets"))


class FakeChannel(dc.DifficultyChannel):
    def __init__(self, open_records=(), list_raises=False):
        self.open_records = list(open_records)
        self.list_raises = list_raises
        self.submitted = []
        self.comments = []

    def submit(self, record):
        self.submitted.append(record)
        return "https://tracker.example/new/1"

    def pull(self, since=None):
        return list(self.open_records)

    def list_open(self):
        if self.list_raises:
            raise RuntimeError("listing failed")
        return list(self.open_records)

    def add_comment(self, ref, body):
        self.comments.append((ref, body))


_counter = iter(range(10_000))


def register(ch):
    name = f"fake-coi-{next(_counter)}"
    dc.register_channel(name, lambda **kw: ch)
    return name


def open_record(number, text=REWORDED):
    return dc.DifficultyRecord(
        ts=FIXED_TS, layer="core", target="CLAUDE.md", functional_ground=text,
        severity=dc.Severity.MEDIUM, reporter="someone", evidence="",
        ref=f"#{number}", title=text,
    )


def candidate_text(rec):
    return f"{rec.title or rec.functional_ground}\n\n{(rec.functional_ground + chr(10) + rec.evidence)[:2000]}"


def _salt():
    from agentctl import advisor

    seen = []

    def runner(argv, *, timeout=None, stdin=""):
        seen.append(stdin)
        return advisor.RunResult(0, stdout="YES", stderr="")

    advisor.judge_same_difficulty("fixed ground one", "fixed ground two", runner)
    return "same-difficulty@" + hashlib.sha256(seen[0].encode("utf-8")).hexdigest() + ":confirmed-yes"


def seed_verdicts(monkeypatch, tmp_path, verdicts):
    salt = _salt()

    def key(*texts):
        joined = "\x00".join([salt] + sorted(" ".join(t.split()) for t in texts))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps({key(GROUND, candidate_text(r)): v for r, v in verdicts.items()}),
                    encoding="utf-8")
    monkeypatch.setenv(VERDICTS_ENV, str(path))


def run(*argv):
    try:
        return main(list(argv), _ts=FIXED_TS)
    except SystemExit as exc:
        return exc.code


def file_args(channel, *extra):
    return ["--target", "CLAUDE.md", "--ground", GROUND, "--channel", channel,
            "--cost", "$1/week", "--evidence", EVIDENCE, *extra]


def refusal_line(err, issue, outcome_prefix):
    lines = [l for l in err.splitlines() if l.startswith(f"comment-on-issue: no comment on {issue}: ")]
    assert len(lines) == 1, err
    assert f"dedup {outcome_prefix}" in lines[0]
    assert lines[0].endswith("nothing filed or commented")
    return lines[0]


def test_comment_on_issue_comments_when_judged_match_is_that_issue(monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    assert rc == 0
    assert ch.comments == [("#7", EVIDENCE)]
    assert ch.submitted == []
    assert capsys.readouterr().out.strip().splitlines()[-1] == "#7"

    _assert_github_posts_one_comment_to_issue_7(monkeypatch, tmp_path)


def _assert_github_posts_one_comment_to_issue_7(monkeypatch, tmp_path):
    from difficulty_channel.adapters import github

    issue = {"number": 7, "title": f"[core] {REWORDED}", "body": "", "labels": [],
             "user": {"login": "someone"}, "created_at": FIXED_TS,
             "html_url": "https://github.com/sthe0/claude-agent-instructions/issues/7"}
    requests = []

    def fake_http(method, url, headers, body):
        requests.append((method, url, body))
        return [issue] if method == "GET" and "page=1" in url else []

    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")
    monkeypatch.setattr(github, "_default_http", fake_http)
    seed_verdicts(monkeypatch, tmp_path, {github._issue_to_record(issue): True})

    rc = run(*file_args("github", "--comment-on-issue", "7"))

    posts = [(url, json.loads(body)) for method, url, body in requests if method == "POST"]
    assert rc == 0
    assert posts == [(
        "https://api.github.com/repos/sthe0/claude-agent-instructions/issues/7/comments",
        {"body": EVIDENCE},
    )]


def test_comment_on_issue_refuses_on_no_match(monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: False})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    refusal_line(capsys.readouterr().err, "7", "no-match")
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []


def test_comment_on_issue_refuses_on_different_match(monkeypatch, tmp_path, capsys):
    rec = open_record(8)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    line = refusal_line(capsys.readouterr().err, "7", "match")
    assert "ref=#8" in line
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []


def test_comment_on_issue_refuses_on_no_candidates(capsys):
    ch = FakeChannel([])

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    refusal_line(capsys.readouterr().err, "7", "no-candidates")
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []


def test_comment_on_issue_refuses_on_listing_failure(capsys):
    ch = FakeChannel([open_record(7)], list_raises=True)

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    refusal_line(capsys.readouterr().err, "7", "search-failed")
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []


def test_comment_on_issue_refuses_when_judge_unavailable(monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    ch = FakeChannel([open_record(7)])

    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    refusal_line(capsys.readouterr().err, "7", "judge-unavailable")
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []


def test_comment_on_issue_refuses_when_judge_budget_exhausted(monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    monkeypatch.setenv(BUDGET_ENV, "0.5")
    ch = FakeChannel([open_record(7)])

    started = time.monotonic()
    rc = run(*file_args(register(ch), "--comment-on-issue", "7"))

    refusal_line(capsys.readouterr().err, "7", "judge-unavailable")
    assert time.monotonic() - started < 2
    assert rc == 4
    assert ch.comments == [] and ch.submitted == []
