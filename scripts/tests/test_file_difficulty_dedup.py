"""file-difficulty.py dedup: a filing first checks the channel's open records for the same
difficulty (lexical nomination, model judge) and by default comments on a judged match
instead of filing a duplicate.

Every test is offline: channels are in-process fakes or the github adapter behind a fake http
installed on ``difficulty_channel.adapters.github._default_http``; judge verdicts come from a
seeded verdict cache (key formula inlined from semantic_join) or a stubbed subprocess runner.
Nothing here imports stage-2 code at module level, so the module collects on a tree without it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import difficulty_channel as dc
from difficulty_channel.adapters import github

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("file_difficulty_dedup", _SCRIPTS / "file-difficulty.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
main = _mod.main

FIXED_TS = "2026-06-27T00:00:00+00:00"
VERDICTS_ENV = "CLAUDE_SAME_DIFFICULTY_VERDICTS"
BUDGET_ENV = "CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S"
REPO = "sthe0/claude-agent-instructions"
ISSUES_URL = f"https://api.github.com/repos/{REPO}/issues"

GROUND = "agent skips the plan approval gate on small edits"
REWORDED = "small edits bypass the approval gate of the plan"
EVIDENCE = "résumé of the failure\nsecond line <!-- improvement-scan-batch2 -->\n"
DENY_ZORBLEX = "[[deny]]\npattern = 'zorblex'\nlabel = \"codename\"\n"

DEDUP_LINE = re.compile(
    r"^dedup: (?P<outcome>[a-z-]+) listed=(?P<listed>\d+) nominated=(?P<nominated>\d+) "
    r"judged=(?P<judged>\d+) cached=(?P<cached>\d+) unjudged=(?P<unjudged>\d+)(?: ref=(?P<ref>\S+))?$"
)


@pytest.fixture(autouse=True)
def _non_author(monkeypatch):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: False)


@pytest.fixture(autouse=True)
def _own_verdict_cache(monkeypatch, tmp_path):
    monkeypatch.setenv(VERDICTS_ENV, str(tmp_path / "absent-verdicts.json"))


@pytest.fixture
def no_plugin_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(tmp_path / "no-plugins"))


@pytest.fixture
def no_term_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_TERM_RULESET_DIR", str(tmp_path / "no-rulesets"))


@pytest.fixture
def zorblex_rules(monkeypatch, tmp_path):
    d = tmp_path / "rulesets"
    d.mkdir()
    (d / "synthetic.toml").write_text(DENY_ZORBLEX, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_TERM_RULESET_DIR", str(d))


@pytest.fixture
def github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token")


@pytest.fixture
def org_channel(monkeypatch, tmp_path):
    root = tmp_path / "difficulty-plugins"
    (root / "adapters").mkdir(parents=True)
    (root / "adapters" / "orgchan.py").write_text(
        "QUEUE = 'REPORTQ'\nBACKLOG_QUEUE = 'BACKLOGQ'\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_DIFFICULTY_PLUGIN_DIR", str(root))
    return "orgchan"


class FakeChannel(dc.DifficultyChannel):
    def __init__(self, open_records=(), list_raises=False, comment_raises=False):
        self.open_records = list(open_records)
        self.list_raises = list_raises
        self.comment_raises = comment_raises
        self.list_calls = 0
        self.submitted = []
        self.comments = []

    def submit(self, record):
        self.submitted.append(record)
        return "https://tracker.example/new/1"

    def pull(self, since=None):
        return list(self.open_records)

    def list_open(self):
        self.list_calls += 1
        if self.list_raises:
            raise RuntimeError("listing failed")
        return list(self.open_records)

    def add_comment(self, ref, body):
        if self.comment_raises:
            raise RuntimeError("comment refused")
        self.comments.append((ref, body))


_chan_counter = iter(range(10_000))


def register(ch, **factory_kw):
    """Register ``ch`` under a fresh in-process channel name and return the name."""
    name = f"fake-dedup-{next(_chan_counter)}"
    dc.register_channel(name, lambda **kw: ch)
    return name


def open_record(number, text=REWORDED, evidence=""):
    return dc.DifficultyRecord(
        ts=FIXED_TS, layer="core", target="CLAUDE.md", functional_ground=text,
        severity=dc.Severity.MEDIUM, reporter="someone", evidence=evidence,
        ref=f"#{number}", title=text,
    )


def candidate_text(rec):
    """The text the dedup shows the judge for an open record (D2: title, blank line, body)."""
    return f"{rec.title or rec.functional_ground}\n\n{(rec.functional_ground + chr(10) + rec.evidence)[:2000]}"


def _salt():
    from agentctl import advisor

    seen = []

    def runner(argv, *, timeout=None, stdin=""):
        seen.append(stdin)
        return advisor.RunResult(0, stdout="YES", stderr="")

    advisor.judge_same_difficulty("fixed ground one", "fixed ground two", runner)
    return "same-difficulty@" + hashlib.sha256(seen[0].encode("utf-8")).hexdigest() + ":confirmed-yes"


def verdict_key(*texts):
    joined = "\x00".join([_salt()] + sorted(" ".join(t.split()) for t in texts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def seed_verdicts(monkeypatch, tmp_path, verdicts):
    """``verdicts``: {open record: bool} for the ground GROUND. Returns the cache file."""
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps({
        verdict_key(GROUND, candidate_text(rec)): value for rec, value in verdicts.items()
    }), encoding="utf-8")
    monkeypatch.setenv(VERDICTS_ENV, str(path))
    return path


def run(*argv):
    try:
        return main(list(argv), _ts=FIXED_TS)
    except SystemExit as exc:
        return exc.code


def file_args(channel, *extra, ground=GROUND, evidence=EVIDENCE):
    args = ["--target", "CLAUDE.md", "--ground", ground, "--channel", channel,
            "--cost", "$1/week"]
    if evidence is not None:
        args += ["--evidence", evidence]
    return args + list(extra)


def dedup_lines(out):
    return [m for m in (DEDUP_LINE.match(line) for line in out.splitlines()) if m]


def last_line(out):
    return out.strip().splitlines()[-1]


def make_issue(number, title, pull_request=False):
    issue = {"number": number, "title": f"[core] {title}", "body": "", "labels": [],
             "user": {"login": "someone"}, "created_at": FIXED_TS,
             "html_url": f"https://github.com/{REPO}/issues/{number}"}
    if pull_request:
        issue["pull_request"] = {"url": "x"}
    return issue


class FakeGitHub:
    """Fake http for the github adapter: serves open issues by page, records every request,
    refuses a label-filtered listing, answers the issue POST with issue 4242."""

    def __init__(self, pages=()):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, body))
        if method == "GET":
            if "labels=" in url:
                raise AssertionError(f"label-filtered listing requested: {url}")
            page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            return self.pages[page - 1] if page <= len(self.pages) else []
        if url == ISSUES_URL:
            return {"number": 4242, "html_url": f"https://github.com/{REPO}/issues/4242"}
        return {}

    def posts(self):
        return [(url, json.loads(body)) for method, url, body in self.requests if method == "POST"]


def install_github(monkeypatch, pages=()):
    fake = FakeGitHub(pages)
    monkeypatch.setattr(github, "_default_http", fake)
    return fake


# ── default match: comment on the existing record ────────────────────────────

def test_match_comments_on_existing_issue_by_default(
        no_plugin_dir, no_term_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch)))

    out = capsys.readouterr().out
    assert ch.submitted == []
    assert ch.comments == [("#7", EVIDENCE)]
    assert last_line(out) == "#7"
    assert rc == 0


def test_no_comment_on_match_refuses_and_names_existing_issue(
        no_plugin_dir, no_term_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), "--no-comment-on-match"))

    out = capsys.readouterr().out
    assert rc == 3
    assert ch.submitted == []
    assert ch.comments == []
    assert last_line(out) == "#7"


def test_match_comment_failure_files_nothing(
        no_plugin_dir, no_term_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec], comment_raises=True)

    rc = run(*file_args(register(ch)))

    out = capsys.readouterr().out
    assert ch.submitted == []
    failed = [m for m in dedup_lines(out) if m["outcome"] == "match-comment-failed"]
    assert failed and failed[0]["ref"] == "#7"
    assert last_line(out) == "#7"
    assert rc == 1


def test_match_comment_body_passes_term_gate(
        no_plugin_dir, zorblex_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), evidence="the codename zorblex leaks here"))

    captured = capsys.readouterr()
    lines = dedup_lines(captured.out)
    assert lines and lines[0]["outcome"] == "match"
    assert "zorblex" in captured.err
    assert rc == 1
    assert ch.comments == []
    assert ch.submitted == []


def test_match_comment_github_runs_org_neutral(
        no_plugin_dir, zorblex_rules, github_token, monkeypatch, tmp_path, capsys):
    rec = _github_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    fake = install_github(monkeypatch, [[make_issue(7, REWORDED)]])
    seen = []

    def spy(text):
        seen.append(text)
        return 1, "spy refused"

    monkeypatch.setattr(_mod, "_org_neutral_check", spy, raising=False)

    rc = run(*file_args("github"))

    assert seen == [EVIDENCE]
    assert [url for url, _ in fake.posts()] == []
    assert rc != 0


def test_dedup_runs_before_term_gate(no_plugin_dir, zorblex_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch), "--no-comment-on-match", evidence="zorblex in the evidence"))

    assert rc == 3
    assert ch.comments == []
    assert ch.submitted == []


# ── the other outcomes file as before ────────────────────────────────────────

def test_no_match_files_as_before(no_plugin_dir, no_term_rules, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: False})
    ch = FakeChannel([rec])

    rc = run(*file_args(register(ch)))

    out = capsys.readouterr().out
    assert len(ch.submitted) == 1
    assert ch.comments == []
    assert last_line(out) == "https://tracker.example/new/1"
    assert rc == 0


def test_unjudged_files_with_outcome_printed(
        no_plugin_dir, no_term_rules, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    ch = FakeChannel([open_record(7)])

    rc = run(*file_args(register(ch)))

    lines = dedup_lines(capsys.readouterr().out)
    assert lines and lines[0]["outcome"] == "judge-unavailable"
    assert len(ch.submitted) == 1
    assert rc == 0


def test_existing_invocation_needs_no_new_flag(
        no_plugin_dir, no_term_rules, capsys):
    ch = FakeChannel([])

    rc = run("--target", "CLAUDE.md", "--ground", GROUND, "--channel", register(ch),
             "--cost-not-estimable", "unmeasured")

    assert rc == 0
    assert len(ch.submitted) == 1


def test_listing_failure_still_files(no_plugin_dir, no_term_rules, capsys):
    ch = FakeChannel([open_record(7)], list_raises=True)

    rc = run(*file_args(register(ch)))

    lines = dedup_lines(capsys.readouterr().out)
    assert lines and lines[0]["outcome"] == "search-failed"
    assert len(ch.submitted) == 1
    assert rc == 0


# ── --dry-run ────────────────────────────────────────────────────────────────

def test_dry_run_prints_dedup_and_changes_nothing(no_plugin_dir, capsys):
    ch = FakeChannel([open_record(n, text=f"plinth{n} quartz{n}") for n in (1, 2, 3)])

    rc = run(*file_args(register(ch), "--dry-run"))

    lines = dedup_lines(capsys.readouterr().out)
    assert lines and lines[0]["outcome"] in {"no-match", "no-candidates"}
    assert lines[0]["listed"] == "3"
    assert ch.submitted == []
    assert ch.comments == []
    assert rc == 0


def test_dry_run_never_publishes_on_match(no_plugin_dir, monkeypatch, tmp_path, capsys):
    rec = open_record(7)
    seed_verdicts(monkeypatch, tmp_path, {rec: True})
    ch = FakeChannel([rec])
    name = register(ch)

    for extra in ((), ("--no-comment-on-match",), ("--comment-on-issue", "7")):
        rc = run(*file_args(name, "--dry-run", *extra))
        lines = dedup_lines(capsys.readouterr().out)
        assert lines, extra
        assert (lines[-1]["outcome"], lines[-1]["ref"]) == ("match", "#7"), extra
        assert ch.submitted == [] and ch.comments == [], extra
        assert rc == 0, extra


def test_dry_run_match_prints_github_issue_ref(
        no_plugin_dir, github_token, monkeypatch, tmp_path, capsys):
    pr = make_issue(5, REWORDED, pull_request=True)
    target = make_issue(7, REWORDED)
    page1 = [make_issue(100 + n, f"plinth{n} quartz{n}") for n in range(100)]
    page2 = [pr, target, make_issue(8, "plinth quartz unrelated")]
    seed_verdicts(monkeypatch, tmp_path, {_github_record(7): True, _github_record(5): True})
    fake = install_github(monkeypatch, [page1, page2])

    rc = run(*file_args("github", "--dry-run"))

    out = capsys.readouterr().out
    assert fake.posts() == []
    assert not [url for _, url, _ in fake.requests if "labels=" in url]
    lines = dedup_lines(out)
    assert lines
    assert lines[-1]["outcome"] == "match"
    assert lines[-1]["listed"] == "102"
    assert re.match(rf"^(?:{REPO})?#7$", lines[-1]["ref"] or "")
    assert rc == 0


def _github_record(number):
    rec = github._issue_to_record(make_issue(number, REWORDED))
    return rec


# ── github adapter ───────────────────────────────────────────────────────────

def test_github_body_ends_with_verbatim_evidence_and_handle_names_issue(
        no_plugin_dir, no_term_rules, github_token, monkeypatch, capsys):
    fake = install_github(monkeypatch, [])

    rc = run(*file_args("github"))

    out = capsys.readouterr().out
    posts = fake.posts()
    assert rc == 0
    assert len(posts) == 1 and posts[0][0] == ISSUES_URL
    assert posts[0][1]["body"].endswith("\n**Evidence:**\n" + EVIDENCE)
    assert "4242" in last_line(out)


def test_filing_preview_equals_github_post_bytes(
        no_plugin_dir, no_term_rules, github_token, monkeypatch, tmp_path, capsys):
    fake = install_github(monkeypatch, [])
    ground = "é" + "long ground about the approval gate " * 10
    evidence = "evidence é ending in the marker <!-- improvement-scan-batch2 -->\n"
    preview = tmp_path / "full.md"

    rc_preview = run(*file_args("github", "--dry-run", "--filing-preview", str(preview),
                                ground=ground, evidence=evidence))
    assert rc_preview == 0
    assert fake.posts() == []
    rc_real = run(*file_args("github", ground=ground, evidence=evidence))

    posts = fake.posts()
    assert rc_real == 0
    assert len(posts) == 1 and posts[0][0] == ISSUES_URL
    assert len(ground) > 256
    expected = (posts[0][1]["title"] + "\n\n" + posts[0][1]["body"]).encode("utf-8")
    assert preview.read_bytes() == expected


def test_github_list_open_issues_paginates_and_drops_prs(github_token):
    list_open_issues = getattr(github, "list_open_issues", None)
    assert list_open_issues is not None
    page1 = [make_issue(n, f"plinth{n}") for n in range(1, 101)]
    page2 = [make_issue(101, "plinth101", pull_request=True), make_issue(102, "plinth102"),
             make_issue(103, "plinth103")]
    fake = FakeGitHub([page1, page2])

    records = list_open_issues(http=fake, token="t")

    assert len(records) == 102
    assert [url for _, url, _ in fake.requests].count(fake.requests[0][1]) == 1
    assert len(fake.requests) == 2
    assert not [url for _, url, _ in fake.requests if "labels=" in url]
    assert records[-1].ref == f"{REPO}#103"


def _offline_error():
    try:
        github._default_http("GET", f"{github.API_BASE}/user", {}, None)
    except Exception as exc:  # noqa: BLE001 - the assertion below names what was raised
        return exc
    return None


def test_github_default_http_refuses_when_offline(monkeypatch):
    monkeypatch.setenv("CLAUDE_DIFFICULTY_CHANNEL_OFFLINE", "1")
    reached = []

    def socket_stub(*args, **kwargs):
        reached.append(args)
        raise OSError("socket reached")

    monkeypatch.setattr(socket, "socket", socket_stub)
    monkeypatch.setattr(socket, "create_connection", socket_stub)

    err = _offline_error()

    assert type(err).__name__ == "ChannelOfflineError"
    assert reached == []


def test_conftest_offline_kill_switch_is_autouse():
    assert os.environ.get("CLAUDE_DIFFICULTY_CHANNEL_OFFLINE") == "1"
    assert type(_offline_error()).__name__ == "ChannelOfflineError"


def test_github_adapter_keeps_stage13_internals(monkeypatch):
    assert github.API_BASE == "https://api.github.com"
    assert list(inspect.signature(github._default_http).parameters) == [
        "method", "url", "headers", "body"]
    assert github._gh_headers("t")["Authorization"] == "token t"
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert github._read_token() == "from-env"

    reached = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"login": "x"}'

    def urlopen_stub(req, *args, **kwargs):
        reached.append(req)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_stub)
    monkeypatch.delenv("CLAUDE_DIFFICULTY_CHANNEL_OFFLINE", raising=False)
    url = github.API_BASE + "/user"
    assert github._default_http("GET", url, github._gh_headers("t"), None) == {"login": "x"}
    assert len(reached) == 1

    monkeypatch.setenv("CLAUDE_DIFFICULTY_CHANNEL_OFFLINE", "1")
    with pytest.raises(Exception) as excinfo:
        github._default_http("GET", url, github._gh_headers("t"), None)
    assert type(excinfo.value).__name__ == "ChannelOfflineError"
    assert len(reached) == 1


# ── ordering: refusals that stop the filing anyway spend no listing ──────────

def test_cost_check_precedes_dedup(no_plugin_dir, capsys):
    ch = FakeChannel([open_record(7)])

    rc = run("--target", "CLAUDE.md", "--ground", GROUND, "--channel", register(ch))

    assert rc == 2
    assert "exactly one of --cost or --cost-not-estimable" in capsys.readouterr().err
    assert ch.list_calls == 0


def test_fix_first_guard_precedes_dedup(org_channel, monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    ch = FakeChannel([open_record(7)])
    dc.register_channel(org_channel, lambda **kw: ch)

    for extra in (("--dry-run",), ()):
        rc = run("--target", "CLAUDE.md", "--ground", GROUND, "--channel", org_channel,
                 "--cost", "$1/week", *extra)
        assert rc == 2, extra
        assert "propose the fix directly (fix-first)" in capsys.readouterr().err
    assert ch.list_calls == 0


def test_author_refusal_precedes_dedup(org_channel, monkeypatch, capsys):
    monkeypatch.setattr(_mod.authority, "is_author", lambda: True)
    ch = FakeChannel([open_record(7)])
    dc.register_channel(org_channel, lambda **kw: ch)

    rc = run("--target", "CLAUDE.md", "--ground", GROUND, "--channel", org_channel,
             "--queue", "PROJ", "--cost", "$1/week")

    assert rc == 2
    assert "this machine has Core push rights" in capsys.readouterr().err
    assert ch.list_calls == 0


# ── the filer's judge budget ─────────────────────────────────────────────────

def test_filer_budget_env_bounds_judging(no_plugin_dir, no_term_rules, monkeypatch, capsys):
    from agentctl import advisor

    monkeypatch.setenv(BUDGET_ENV, "0.5")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "1")
    handed = []

    def slow_runner(argv, *, timeout=None, stdin=""):
        handed.append(timeout)
        time.sleep(min(5, timeout))
        if timeout < 5:
            raise subprocess.TimeoutExpired(argv, timeout)
        return advisor.RunResult(0, stdout="YES", stderr="")

    monkeypatch.setattr(advisor, "subprocess_runner", slow_runner)
    ch = FakeChannel([open_record(7)])

    started = time.monotonic()
    rc = run(*file_args(register(ch)))
    elapsed = time.monotonic() - started

    lines = dedup_lines(capsys.readouterr().out)
    assert lines and lines[0]["outcome"] == "judge-unavailable"
    assert len(ch.submitted) == 1
    assert rc == 0
    assert all(t <= 0.5 for t in handed)
    assert elapsed < 2


def _filer_env():
    seen = {}

    def run_stub(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    return seen, run_stub


def test_default_filer_bounds_judge_budget_below_its_timeout(monkeypatch):
    import self_diagnose_store as sds

    seen, run_stub = _filer_env()
    monkeypatch.setattr(subprocess, "run", run_stub)

    sds._default_filer({"path": "/core/x.md", "kind": "k", "detail": "d"})

    budget = (seen.get("env") or {}).get(BUDGET_ENV)
    assert budget is not None
    assert 0 < float(budget) < seen["timeout"]


# ── route_advisory: a match is covered by the matched ref ────────────────────

def _route_with_filer_exit(monkeypatch, tmp_path, returncode, stdout):
    import self_diagnose_store as sds

    class Result:
        pass

    result = Result()
    result.returncode, result.stdout, result.stderr = returncode, stdout, ""
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: result)
    root = tmp_path / "core"
    leaf = root / "memory-global" / "leaves" / "x.md"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("leaf\n", encoding="utf-8")
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    store = tmp_path / "findings.jsonl"
    rows = sds.upsert_findings(
        [{"kind": "near-duplicate", "path": str(leaf), "detail": "d"}], store, now)
    sds.route_advisory(rows, "backlog", sds._default_filer, core_root=root, path=store)
    return rows[0].get("filed_ref")


MATCH_STDOUT = ("dedup: match listed=3 nominated=1 judged=1 cached=0 unjudged=0 ref=#7\n"
                "matched: reworded ground\n#7\n")


def test_route_advisory_records_comment_match_as_covered(monkeypatch, tmp_path):
    assert _route_with_filer_exit(monkeypatch, tmp_path, 0, MATCH_STDOUT) == "#7"


def test_route_advisory_records_refused_match_as_covered(monkeypatch, tmp_path):
    assert _route_with_filer_exit(monkeypatch, tmp_path, 3, MATCH_STDOUT) == "#7"
