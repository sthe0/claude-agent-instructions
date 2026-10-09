"""The background debt cycle driver, run end to end against a fake world.

Everything the driver reaches outside itself goes through three seams -- the command runner
(`gh`, `git`, the test suite, the org-neutral checker), the specialist spawner and the triage
judge -- and all three are replaced here, so a test spawns no process and touches no network,
systemd or real agent home. The mandate store and the cost log live in tmp dirs.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl import cost, mandate as rules, mandate_store as store
from agentctl.dispatch import RunResult
from mandate_cycle import driver, notifiers
from session_scope import registry as scopes

ID = "core-debt"
OWNER = "owner"
DRIVER_SOURCE = Path(driver.__file__).read_text(encoding="utf-8")


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)


def raw_issue(number, labels=("backlog",), author=OWNER, day=1, title=None, body="", **extra):
    return {
        "number": number, "labels": [{"name": n} for n in labels], "author": {"login": author},
        "createdAt": f"2026-09-{day:02d}T00:00:00Z", "title": title or f"issue {number}", "body": body, **extra,
    }


def junit(passed, failed=()):
    suite = ET.Element("testsuite")
    for name in passed:
        ET.SubElement(suite, "testcase", classname="scripts.tests.test_a", name=name)
    for name in failed:
        case = ET.SubElement(suite, "testcase", classname="scripts.tests.test_a", name=name)
        ET.SubElement(case, "failure", message="boom")
    return ET.tostring(ET.ElementTree(suite).getroot(), encoding="unicode")


def ok(stdout=""):
    return RunResult(0, stdout)


class World:
    """The repository, the forge and the test suite as the driver sees them."""

    def __init__(self, temp_root: Path):
        self.temp_root = temp_root
        self.issues: "list[dict]" = []
        self.open_prs: "list[dict]" = []
        self.pr_heads: "list[dict]" = []
        self.remote_heads = ""
        self.blocks: "dict[str, str]" = {}
        self.local_branches = ""
        self.name_status = "M\tscripts/foo.py\n"
        self.suites = {
            "baseline.xml": (["t1", "t2"], []),
            "item.xml": (["t1", "t2", "t3"], []),
            "rerun.xml": (["t1", "t2"], []),
        }
        self.neutral = lambda text: 0
        self.gate_rc: "dict[str, int]" = {}
        self.pr_url = "https://example.test/owner/repo/pull/7"
        self.calls: "list[list[str]]" = []
        self.on_edit = None
        self.on_suite = None
        self.head = "a" * 40
        self.status = ""
        # Files a fresh worktree holds: the junit class `scripts.tests.test_a` maps to the first.
        self.worktree_files = ["scripts/tests/test_a.py"]

    def run(self, argv, cwd=None, timeout=None):
        self.calls.append(list(argv))
        a = list(argv)
        if a[:3] == ["gh", "repo", "view"]:
            return ok(json.dumps({"nameWithOwner": f"{OWNER}/repo"}))
        if a[:3] == ["gh", "issue", "list"]:
            return ok(json.dumps(self.issues))
        if a[:3] == ["gh", "pr", "list"]:
            return ok(json.dumps(self.pr_heads if "all" in a else self.open_prs))
        if a[:3] == ["gh", "label", "list"]:
            return ok("[]")
        if a[:3] == ["gh", "pr", "create"]:
            return ok(self.pr_url + "\n")
        if a[:3] == ["gh", "issue", "edit"]:
            if self.on_edit:
                self.on_edit(a)
            number, label = int(a[3]), a[a.index("--add-label") + 1]
            for issue in self.issues:
                if issue["number"] == number:
                    issue["labels"].append({"name": label})
            return ok()
        if a[:3] == ["git", "worktree", "list"]:
            return ok("\n".join(self.blocks.values()))
        if a[:3] == ["git", "worktree", "add"]:
            root = Path(a[-2])
            for relative in self.worktree_files:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("", encoding="utf-8")
            return ok()
        if a[:3] == ["git", "worktree", "remove"]:
            self.blocks.pop(a[-1], None)
            return ok()
        if a[:2] == ["git", "for-each-ref"]:
            return ok(self.local_branches)
        if a[:2] == ["git", "ls-remote"]:
            return ok(self.remote_heads)
        if a[:2] == ["git", "status"]:
            return ok(self.status)
        if a[:3] == ["git", "rev-parse", "HEAD"]:
            return ok(self.head + "\n")
        if a[:2] == ["git", "diff"]:
            return ok(self.name_status if "--name-status" in a else "+++ b/scripts/foo.py\n+a new line\n")
        if a[:2] == ["git", "log"]:
            return ok("a commit message\n")
        if a[:1] == ["bash"] and a[1].endswith("cap-run.sh"):
            target = next(x for x in a if x.startswith("--junitxml="))[len("--junitxml="):]
            passed, failed = self.suites[Path(target).name]
            Path(target).write_text(junit(passed, failed), encoding="utf-8")
            if self.on_suite:
                self.on_suite(Path(target).name)
            return ok()
        if a[0] == sys.executable and a[1].endswith("check-org-neutral.py"):
            return RunResult(self.neutral(Path(a[2]).read_text(encoding="utf-8")))
        if a[0] == sys.executable and a[1] in driver.GATE_SCRIPTS:
            return RunResult(self.gate_rc.get(a[1], 0))
        return ok()

    def calls_with(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class Spawner:
    def __init__(self, scopes_dir):
        self.scopes_dir = scopes_dir
        self.calls: "list[dict]" = []
        self.live_scopes: "list[list[str]]" = []
        self.developer = lambda argv, cwd: driver.SpawnResult(0, "COMPLETED:\nFixed the issue.\n")
        self.reviewer = lambda argv, cwd: driver.SpawnResult(0, "No blocking findings.\nVERDICT: accept\n")

    def __call__(self, argv, cwd, seconds):
        kind = argv[argv.index("--kind") + 1]
        self.calls.append({"argv": list(argv), "cwd": cwd, "seconds": seconds, "kind": kind})
        self.live_scopes.append([r.session_id for r in scopes.load_all(self.scopes_dir)])
        return (self.developer if kind == "developer" else self.reviewer)(argv, cwd)

    def of(self, kind):
        return [c for c in self.calls if c["kind"] == kind]


class Judge:
    def __init__(self):
        self.prompts: "list[str]" = []
        self.usd = 0.4
        self.verdict = "auto-ok"
        self.text = None

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self.text is not None:
            return self.text, self.usd
        numbers = [int(n) for n in re.findall(r'<issue number="(\d+)">', prompt)]
        reply = [{"issue": n, "verdict": self.verdict, "reason": "small and clear"} for n in numbers]
        return json.dumps(reply), self.usd


class Env:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_MANDATE_DIR", str(tmp_path / "mandates"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")
    # A breaker trip must never reach a real systemctl, whatever a mutant makes the driver call.
    monkeypatch.setattr(store, "subprocess_runner", lambda argv, *a, **k: RunResult(0))
    e = Env()
    e.tmp = tmp_path
    e.clock = Clock()
    e.temp_root = tmp_path / "worktrees"
    e.scopes_dir = tmp_path / "scopes"
    e.plugins = tmp_path / "mandate-plugins"
    e.world = World(e.temp_root)
    e.spawner = Spawner(e.scopes_dir)
    e.judge = Judge()
    e.capsys = capsys

    def grant(**limits):
        mandate = rules.new_mandate(ID, "alice", e.clock(), rules.MandateLimits(**limits))
        store.save_mandate(mandate)
        return mandate

    def cfg(**extra):
        return driver.Config(
            repo=tmp_path / "repo", temp_root=e.temp_root, scopes_dir=e.scopes_dir,
            plugin_root=e.plugins, **extra,
        )

    def seams():
        return driver.Seams(run=e.world.run, spawn=e.spawner, judge=e.judge, clock=e.clock)

    def main(*argv, **extra):
        rc = driver.main(list(argv), seams=seams(), cfg=cfg(**extra))
        return rc, e.capsys.readouterr().out

    def cycle(**extra):
        return main("run", **extra)[0]

    def add_cost(usd, plan_path, ts=None):
        ts = rules.format_ts(ts or e.clock())
        with (tmp_path / "costs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": ts, "cost_usd": usd, "plan_path": plan_path}) + "\n")

    def plan_of(argv):
        return argv[argv.index("--plan") + 1]

    def plugin(body="NAME = 'chat'\ndef send(text):\n    return True\n"):
        (e.plugins / "notifiers").mkdir(parents=True, exist_ok=True)
        (e.plugins / "notifiers" / "chat.py").write_text(textwrap.dedent(body), encoding="utf-8")

    e.grant, e.cfg, e.main, e.cycle, e.add_cost, e.plan_of, e.plugin = grant, cfg, main, cycle, add_cost, plan_of, plugin
    return e


def eligible(env, number=1, **kwargs):
    env.world.issues.append(raw_issue(number, labels=("backlog", "auto-ok"), **kwargs))


def item_rows():
    return store.read_item_rows(ID)


def only_cycle_id():
    ids = list(store.read_cycle_records(ID))
    assert len(ids) == 1
    return ids[0]


def events(name=None):
    rows = store.read_events(ID)
    return [r for r in rows if name is None or r["event"] == name]


def mandate_files():
    root = store.mandate_dir(ID)
    return sorted((str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file())


# --- dry run ----------------------------------------------------------------------------

def test_dry_run_lists_what_would_happen_and_writes_nothing(env):
    env.grant(max_items=2)
    eligible(env, 1, day=2)
    eligible(env, 2, day=1)
    env.world.issues.append(raw_issue(3, labels=("backlog",)))
    env.world.issues.append(raw_issue(4, labels=("backlog",), author="stranger"))
    before = mandate_files()
    rc, out = env.main("run", "--dry-run", "--json")
    report = json.loads(out)
    assert rc == 0
    assert report["gate"] == {"may_start": True, "reasons": []}
    assert report["mandate"]["synthetic"] is False
    assert report["would_take"] == [2, 1]
    assert report["would_triage"] == [3]
    by_issue = {c["issue"]: c for c in report["candidates"]}
    assert by_issue[1]["eligible"] and by_issue[1]["auto_ok_source"] == "user"
    assert by_issue[3]["eligible"] is False and by_issue[3]["reason"] == "not-auto-ok"
    assert by_issue[4]["eligible"] is False
    assert 4 not in report["would_take"] + report["would_triage"]
    assert mandate_files() == before
    assert env.spawner.calls == [] and env.judge.prompts == []
    assert not env.scopes_dir.exists() or list(env.scopes_dir.iterdir()) == []


def test_dry_run_issues_only_read_commands(env):
    env.grant()
    eligible(env, 1)
    env.main("run", "--dry-run", "--json")
    assert {tuple(c[:3]) for c in env.world.calls} <= {
        ("gh", "repo", "view"), ("gh", "issue", "list"), ("gh", "pr", "list"),
    }


def test_dry_run_without_a_mandate_is_synthetic_and_would_take_nothing(env):
    eligible(env, 1)
    rc, out = env.main("run", "--dry-run", "--json")
    report = json.loads(out)
    assert rc == 0
    assert report["mandate"]["synthetic"] is True
    assert report["gate"] == {"may_start": False, "reasons": ["no-mandate"]}
    assert report["would_take"] == []
    assert [c["issue"] for c in report["candidates"]] == [1]
    assert not store.mandates_root().exists() or store.list_mandate_ids() == []


# --- gate, lock, limit events -----------------------------------------------------------

def test_a_refused_cycle_logs_limit_and_delivers_no_digest(env):
    env.grant()
    mandate = store.load_mandate(ID)
    store.save_mandate(rules.stopped(mandate))
    eligible(env, 1)
    assert env.cycle() == 0
    refused = events("limit")
    assert len(refused) == 1 and refused[0]["detail"]["reasons"] == ["paused"]
    record = list(store.read_cycle_records(ID).values())[0]
    assert record["status"] == "refused" and "digest" not in record
    assert not (store.mandate_dir(ID) / "digests").exists()
    assert env.spawner.calls == [] and env.judge.prompts == []
    assert env.world.calls == []


def test_a_cycle_over_the_daily_budget_is_refused_and_says_why(env):
    env.grant(daily_usd=5.0)
    env.add_cost(5.0, str(store.mandate_dir(ID) / "cycles" / "old" / "items" / "9" / "brief.md"))
    assert env.cycle() == 0
    assert events("limit")[0]["detail"]["reasons"] == ["daily-budget"]


def test_a_second_cycle_while_one_holds_the_lock_exits_busy_and_records_nothing(env):
    env.grant()
    eligible(env, 1)
    with store.cycle_lock(ID):
        assert env.cycle() == driver.EXIT_BUSY
    assert events() == []
    assert store.read_cycle_rows(ID) == []
    assert env.spawner.calls == [] and env.world.calls == []


# --- the fix path -----------------------------------------------------------------------

def test_an_eligible_issue_becomes_a_pull_request_through_every_gate(env):
    env.grant()
    eligible(env, 7, title="Fix the thing", body="Details from the owner.")
    seen_scopes = []
    original = env.spawner.developer

    def developer(argv, cwd):
        seen_scopes.append([(r.session_id, r.pid) for r in scopes.load_all(env.scopes_dir)])
        return original(argv, cwd)

    env.spawner.developer = developer
    assert env.cycle() == 0

    rows = item_rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row["issue"], row["outcome"], row["tests"], row["review"]) == (7, "pr-opened", "pass", "accept")
    assert row["pr_url"] == env.world.pr_url
    assert row["branch"].startswith("mandate/7-")

    pushes = env.world.calls_with("git", "push")
    assert len(pushes) == 1 and pushes[0][2:] == ["origin", f"{env.world.head}:refs/heads/{row['branch']}"]
    created = env.world.calls_with("gh", "pr", "create")[0]
    assert created[created.index("--base") + 1] == "main"
    assert created[created.index("--head") + 1] == row["branch"]
    assert "--draft" not in created

    comments = env.world.calls_with("gh", "issue", "comment", "7")
    assert comments and env.world.pr_url in Path(comments[-1][-1]).read_text(encoding="utf-8")

    assert [c["kind"] for c in env.spawner.calls] == ["developer", "code-reviewer"]
    for call in env.spawner.calls:
        assert "--add-dir" not in call["argv"]
        assert call["argv"][call["argv"].index("--workdir") + 1] == call["cwd"]
        assert call["cwd"].startswith(str(env.temp_root))
        assert Path(env.plan_of(call["argv"])).is_relative_to(store.mandate_dir(ID))
        assert 0 < call["seconds"] <= store.load_mandate(ID).item_minutes_cap * 60
    assert seen_scopes == [[(f"mandate-{row['cycle_id']}-7", os.getpid())]]
    assert list(env.scopes_dir.iterdir()) == []
    assert env.world.calls_with("git", "worktree", "remove")[-1][-1] == str(
        env.temp_root / f"{row['cycle_id']}-7"
    )
    assert env.world.calls_with("git", "branch", "-D", row["branch"])

    record = store.read_cycle_records(ID)[row["cycle_id"]]
    assert record["status"] == "ok" and record["taken"] == [7]
    assert record["digest"]["notifier"] == "file" and record["digest"]["ok"] is True
    logged = store.read_command_rows(ID, row["cycle_id"])
    assert logged and all({"argv", "cwd", "exit", "ts"} <= set(r) for r in logged)
    assert [e["event"] for e in events()][0] == "cycle-start"
    assert events()[-1]["event"] == "cycle-end"


MOVED = "b" * 40


def test_a_commit_made_while_the_suite_runs_is_never_pushed(env):
    env.grant()
    eligible(env, 1)
    env.world.on_suite = lambda name: setattr(env.world, "head", MOVED) if name == "item.xml" else None
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "state-tampered")
    assert env.world.calls_with("git", "push") == [] and env.world.calls_with("gh", "pr", "create") == []
    assert env.spawner.of("code-reviewer") == []
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "state-tampered"


def test_a_commit_made_during_the_review_is_never_pushed(env):
    env.grant()
    eligible(env, 1)

    def reviewer(argv, cwd):
        env.world.head = MOVED
        return driver.SpawnResult(0, "No blocking findings.\nVERDICT: accept\n")

    env.spawner.reviewer = reviewer
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "state-tampered")
    assert env.world.calls_with("git", "push") == [] and env.world.calls_with("gh", "pr", "create") == []
    assert store.load_mandate(ID).breaker_open


def test_a_file_changed_during_the_review_is_never_pushed(env):
    env.grant()
    eligible(env, 1)

    def reviewer(argv, cwd):
        env.world.status = " M scripts/foo.py"
        return driver.SpawnResult(0, "No blocking findings.\nVERDICT: accept\n")

    env.spawner.reviewer = reviewer
    env.cycle()
    assert item_rows()[0]["failure_kind"] == "state-tampered"
    assert env.world.calls_with("git", "push") == []


def test_work_the_developer_left_uncommitted_is_committed_before_it_is_checked(env):
    env.grant()
    eligible(env, 1)
    env.world.status = " M scripts/foo.py"
    env.cycle()
    assert env.world.calls_with("git", "add", "-A") and env.world.calls_with("git", "commit")
    assert item_rows()[0]["outcome"] == "pr-opened"


# --- the records every outcome leaves ---------------------------------------------------

R6_FIELDS = {"cycle_id", "issue", "branch", "pr_url", "outcome", "tests", "review", "gates", "cost_usd", "duration_s"}
GATE_NAMES = {"constitution", "org_neutral", "lint"}


def only_row(**expected):
    rows = item_rows()
    assert len(rows) == 1
    row = rows[0]
    assert R6_FIELDS <= set(row), R6_FIELDS - set(row)
    assert set(row["gates"]) == GATE_NAMES
    assert isinstance(row["cost_usd"], float) and isinstance(row["duration_s"], float)
    for key, value in expected.items():
        assert row[key] == value, key
    return row


def test_a_pull_request_row_carries_every_field_and_its_measured_cost_and_duration(env):
    env.grant()
    eligible(env, 1)

    def developer(argv, cwd):
        env.add_cost(1.5, env.plan_of(argv))
        env.clock.advance(seconds=90)
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    row = only_row(
        issue=1, outcome="pr-opened", tests="pass", review="accept", pr_url=env.world.pr_url,
        gates={"constitution": "pass", "org_neutral": "pass", "lint": "pass"}, cost_usd=1.5, duration_s=90.0,
    )
    assert row["branch"].startswith("mandate/1-")


def test_a_failed_row_says_which_gates_ran_and_has_no_pull_request(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1"], ["t2"])
    env.cycle()
    row = only_row(
        issue=1, outcome="failed", failure_kind="tests", tests="fail", review="not-run", pr_url=None,
        gates={"constitution": "pass", "org_neutral": "pass", "lint": "pass"},
    )
    assert row["branch"].startswith("mandate/1-")


def test_an_overrun_row_stops_at_the_spawn_that_overran(env):
    env.grant(item_usd_cap=2.0)
    eligible(env, 1)

    def developer(argv, cwd):
        env.add_cost(3.0, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    row = only_row(
        issue=1, outcome="overrun", tests="not-run", review="not-run", pr_url=None, cost_usd=3.0,
        gates={"constitution": "not-run", "org_neutral": "not-run", "lint": "not-run"},
    )
    assert row["overrun_reasons"] == ["item-usd-cap"]


def test_a_declined_row_has_a_branch_and_no_pull_request(env):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "ESCALATE:\nNeeds a decision.\n")
    env.cycle()
    row = only_row(
        issue=1, outcome="declined", marker="ESCALATE", tests="not-run", review="not-run", pr_url=None,
        gates={"constitution": "not-run", "org_neutral": "not-run", "lint": "not-run"},
    )
    assert row["branch"].startswith("mandate/1-")


def test_a_limit_row_names_the_reasons_and_no_branch(env):
    env.grant(max_items=2, daily_usd=5.0)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)

    def developer(argv, cwd):
        env.add_cost(6.0, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    row = item_rows()[1]
    assert R6_FIELDS <= set(row)
    assert (row["issue"], row["outcome"], row["reasons"]) == (2, "limit", ["daily-budget"])
    assert (row["branch"], row["pr_url"], row["tests"], row["review"]) == (None, None, "not-run", "not-run")
    assert (row["cost_usd"], row["duration_s"]) == (0.0, 0.0)
    assert set(row["gates"]) == GATE_NAMES


def test_a_constitution_row_marks_only_that_gate(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/agentctl/mandate.py\n"
    env.cycle()
    only_row(
        failure_kind="constitution", tests="not-run",
        gates={"constitution": "fail", "org_neutral": "not-run", "lint": "not-run"},
    )


# --- wall-clock and budget limits mid-cycle ---------------------------------------------

def test_the_cycle_wall_clock_cap_stops_the_next_item_with_a_limit_event(env):
    env.grant(max_items=2, item_minutes_cap=100.0, cycle_minutes_cap=30.0)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)

    def developer(argv, cwd):
        env.clock.advance(minutes=31)
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [(1, "pr-opened"), (2, "limit")]
    assert events("limit")[-1]["detail"] == {"reasons": ["cycle-minutes-cap"], "phase": "item", "issue": 2}
    assert len(env.spawner.of("developer")) == 1
    assert not store.load_mandate(ID).breaker_open


def test_the_weekly_budget_stops_the_next_item_with_a_limit_event(env):
    env.grant(max_items=2, daily_usd=50.0, weekly_usd=5.0)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)

    def developer(argv, cwd):
        env.add_cost(6.0, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [(1, "pr-opened"), (2, "limit")]
    assert events("limit")[-1]["detail"]["reasons"] == ["weekly-budget"]
    assert len(env.spawner.of("developer")) == 1
    assert not store.load_mandate(ID).breaker_open


def test_a_spawn_killed_at_the_cycle_bound_is_charged_as_a_cycle_minutes_overrun(env):
    env.grant(item_usd_cap=4.0, item_minutes_cap=100.0, cycle_minutes_cap=30.0)
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(-9, "", "", killed=True)
    env.cycle()
    row = item_rows()[0]
    assert row["outcome"] == "overrun" and row["overrun_reasons"] == ["cycle-minutes-cap"]
    assert not store.load_mandate(ID).breaker_open
    assert env.spawner.calls[0]["seconds"] == pytest.approx(30 * 60, abs=5)


def test_a_spawn_runs_no_longer_than_what_is_left_of_the_item_cap(env):
    env.grant(item_minutes_cap=10.0, cycle_minutes_cap=100.0)
    eligible(env, 1)
    env.cycle()
    assert env.spawner.calls[0]["seconds"] == pytest.approx(10 * 60, abs=5)


# --- a scope record that stays live, and a log of every external command ----------------

def test_the_scope_record_is_heartbeated_while_an_item_works(env):
    env.grant()
    eligible(env, 1)
    seen = []

    def developer(argv, cwd):
        first = scopes.load_all(env.scopes_dir)[0]
        last = first.heartbeat_ts
        deadline = time.monotonic() + 10
        while last <= first.heartbeat_ts and time.monotonic() < deadline:
            time.sleep(0.02)
            last = scopes.load_all(env.scopes_dir)[0].heartbeat_ts
        seen.append((first.pid, first.heartbeat_ts, last))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle(heartbeat_s=0.05)
    pid, first, last = seen[0]
    assert pid == os.getpid() and last > first
    assert list(env.scopes_dir.iterdir()) == []


def test_every_external_command_is_logged_with_its_argv_and_exit_code(env):
    env.grant()
    eligible(env, 1)
    env.world.gate_rc = {"scripts/verify-all.py": 1}
    env.cycle()
    cycle_id = item_rows()[0]["cycle_id"]
    logged = store.read_command_rows(ID, cycle_id)
    expected = env.world.calls + [c["argv"] for c in env.spawner.calls]
    assert sorted(r["argv"] for r in logged) == sorted(expected)
    gate_exits = {r["exit"] for r in logged if r["argv"][1:2] == ["scripts/verify-all.py"]}
    assert gate_exits == {1}
    assert {r["exit"] for r in logged if r["argv"][1:2] != ["scripts/verify-all.py"]} == {0}
    assert [r["argv"][0] for r in logged].count("git") >= 5
    assert any(r["argv"][:3] == ["git", "push", "origin"] for r in logged)


def test_a_cycle_that_reruns_a_failing_test_runs_only_the_module_that_holds_it(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], [])
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"
    reruns = [c for c in env.world.calls if any(x.endswith("rerun.xml") for x in c)]
    assert len(reruns) == 1 and "scripts/tests/test_a.py" in reruns[0]
    assert "scripts/tests" not in reruns[0]


def test_a_failing_test_that_maps_to_no_module_is_not_rerun_and_fails_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.worktree_files = []
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], [])
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"], row["tests"]) == ("failed", "tests", "fail")
    assert not any(x.endswith("rerun.xml") for c in env.world.calls for x in c)


def test_a_cycle_fixes_before_it_triages(env):
    env.grant()
    eligible(env, 1)
    env.world.issues.append(raw_issue(2, labels=("backlog",)))
    order = []
    env.spawner.developer = lambda argv, cwd: (order.append("fix"), driver.SpawnResult(0, "COMPLETED:\nok\n"))[1]
    original_judge = env.judge
    env.judge = lambda prompt: (order.append("triage"), original_judge(prompt))[1]
    env.cycle()
    assert order == ["fix", "triage"]


def test_the_brief_holds_only_the_owner_authored_issue_text(env):
    env.grant()
    eligible(
        env, 1, title="Owner title", body="Owner body with <<this step>> pasted in.",
        comments=[{"author": {"login": "stranger"}, "body": "THIRD-PARTY-INSTRUCTION"}],
    )
    env.cycle()
    brief = Path(env.plan_of(env.spawner.of("developer")[0]["argv"])).read_text(encoding="utf-8")
    assert "Owner title" in brief and "Owner body" in brief
    assert "THIRD-PARTY-INSTRUCTION" not in brief
    assert brief.count("<<this step>>") == 1


def test_a_third_party_issue_never_reaches_a_spawn_or_the_judge(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog", "auto-ok"), author="stranger"))
    env.cycle()
    assert env.spawner.calls == [] and env.judge.prompts == []


# --- tests relative to the baseline -----------------------------------------------------

def test_a_test_failing_on_the_baseline_does_not_count_against_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["baseline.xml"] = (["t1", "t2"], ["t_broken"])
    env.world.suites["item.xml"] = (["t1", "t2"], ["t_broken"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], ["t_broken"])
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"
    assert item_rows()[0]["tests"] == "pass"


def test_a_baseline_passing_test_that_fails_on_the_item_fails_it_and_is_not_pushed(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1"], ["t2"])
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"], row["tests"]) == ("failed", "tests", "fail")
    assert env.world.calls_with("git", "push") == []
    assert env.world.calls_with("gh", "pr", "create") == []
    assert store.load_mandate(ID).breaker_open


def test_a_failure_that_passes_on_its_single_rerun_does_not_fail_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t2"], [])
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"


def test_a_red_baseline_takes_nothing_still_triages_and_opens_no_breaker(env):
    env.grant()
    eligible(env, 1)
    env.world.issues.append(raw_issue(2, labels=("backlog",)))
    env.world.suites["baseline.xml"] = ([], ["t1"])
    assert env.cycle() == 0
    assert env.spawner.calls == [] and item_rows() == []
    assert events("baseline-red")
    assert len(env.judge.prompts) == 1
    assert not store.load_mandate(ID).breaker_open
    assert list(store.read_cycle_records(ID).values())[0]["status"] == "baseline-red"


def test_a_gate_script_failing_on_the_baseline_does_not_count_against_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.gate_rc = {"scripts/verify-all.py": 1}
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"


def test_a_gate_script_the_item_breaks_fails_it(env):
    env.grant()
    eligible(env, 1)
    original = env.world.run

    def run(argv, cwd=None, timeout=None):
        if argv[:2] == [sys.executable, "scripts/lint-prose-length.py"] and not str(cwd).endswith("-baseline"):
            env.world.calls.append(list(argv))
            return RunResult(1)
        return original(argv, cwd, timeout)

    env.world.run = run
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "tests")
    assert row["gates"]["lint"] == "fail" and row["gate_exit_codes"]["scripts/lint-prose-length.py"] == 1


# --- what a diff may not touch, what may not be published -------------------------------

def test_a_diff_that_touches_the_constitution_is_never_pushed_and_trips_the_breaker(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/foo.py\nM\tscripts/agentctl/mandate.py\n"
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "constitution")
    assert env.world.calls_with("git", "push") == []
    assert env.spawner.of("code-reviewer") == []
    assert store.load_mandate(ID).breaker_open


def test_published_text_that_fails_the_org_neutral_check_is_not_pushed(env):
    env.grant()
    eligible(env, 1)
    env.world.neutral = lambda text: 1 if "a new line" in text else 0
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "org-neutral")
    assert env.world.calls_with("git", "push") == []


def test_a_review_without_an_accept_verdict_is_not_pushed(env):
    env.grant()
    eligible(env, 1)
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(0, "Blocking: x.\nVERDICT: reject\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"], row["review"]) == ("failed", "review-reject", "reject")
    assert env.world.calls_with("git", "push") == []


def test_a_review_that_never_states_a_verdict_is_a_rejection(env):
    env.grant()
    eligible(env, 1)
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(0, "Looks good to me.\n")
    env.cycle()
    assert item_rows()[0]["failure_kind"] == "review-reject"


def test_a_diff_with_no_changed_file_is_declined(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = ""
    env.cycle()
    row = item_rows()[0]
    assert row["outcome"] == "declined"
    assert not store.load_mandate(ID).breaker_open


# --- developer return markers -----------------------------------------------------------

@pytest.mark.parametrize("marker", ["INCOMPLETE", "ESCALATE", "CLARIFY", "REPLAN"])
def test_a_developer_that_cannot_proceed_declines_the_item_without_the_breaker(env, marker):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, f"{marker}:\nNeeds a decision.\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["marker"]) == ("declined", marker)
    assert not store.load_mandate(ID).breaker_open
    labels = [r for r in store.read_label_rows(ID) if r["by"] == "cycle"]
    assert [(r["issue"], r["label"]) for r in labels] == [(1, "auto-no")]
    assert env.world.calls_with("gh", "issue", "edit", "1", "--add-label", "auto-no")
    assert env.world.calls_with("git", "push") == []


def test_a_permission_request_fails_the_item_with_the_permission_kind(env):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "PERMISSION-REQUEST:\nAction: push\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "permission")
    assert store.load_mandate(ID).breaker_open


def test_a_specialist_that_returns_no_marker_fails_the_item(env):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(1, "", "traceback")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "spawn-error")


# --- overrun, breaker, limits -----------------------------------------------------------

def test_an_overrun_does_not_open_the_breaker_and_the_cycle_continues(env):
    env.grant(max_items=2, item_usd_cap=2.0)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    n = {"count": 0}

    def developer(argv, cwd):
        n["count"] += 1
        env.add_cost(3.0 if n["count"] == 1 else 0.5, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [(1, "overrun"), (2, "pr-opened")]
    assert not store.load_mandate(ID).breaker_open
    assert item_rows()[0]["overrun_reasons"] == ["item-usd-cap"]
    assert env.world.calls_with("git", "push") != [] and len(env.world.calls_with("git", "push")) == 1


def test_a_killed_spawn_with_no_cost_row_is_charged_the_whole_item_cap(env):
    env.grant(item_usd_cap=4.0)
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(-9, "", "", killed=True)
    env.cycle()
    charges = events("overrun-charge")
    assert [c["detail"]["cost_usd"] for c in charges] == [4.0]
    row = item_rows()[0]
    assert row["outcome"] == "overrun" and "item-minutes-cap" in row["overrun_reasons"]
    assert not store.load_mandate(ID).breaker_open
    spend = store.spend_now(ID, env.clock())
    assert (spend.day_usd, spend.week_usd) == (pytest.approx(4.0), pytest.approx(4.0))


def test_a_killed_spawn_that_left_a_cost_row_is_not_charged_twice(env):
    env.grant(item_usd_cap=4.0)
    eligible(env, 1)

    def developer(argv, cwd):
        env.add_cost(1.5, env.plan_of(argv))
        return driver.SpawnResult(-9, "", "", killed=True)

    env.spawner.developer = developer
    env.cycle()
    assert events("overrun-charge") == []
    spend = store.spend_now(ID, env.clock())
    assert (spend.day_usd, spend.week_usd) == (pytest.approx(1.5), pytest.approx(1.5))


def test_a_failed_item_opens_the_breaker_and_no_further_item_is_taken(env):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "PERMISSION-REQUEST:\nAction: x\n")
    env.cycle()
    assert [r["issue"] for r in item_rows()] == [1]
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and "#1" in mandate.breaker_reason
    assert [e["detail"] for e in events("breaker-open")] == [{"reason": mandate.breaker_reason}]
    assert list(store.read_cycle_records(ID).values())[0]["status"] == "breaker"


def test_a_spawn_that_touches_the_mandate_state_fails_the_item_and_opens_the_breaker(env):
    env.grant()
    eligible(env, 1)

    def developer(argv, cwd):
        store.append_event(ID, "forged-grant", {"by": "spawn"})
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "state-tampered")
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "state-tampered"
    assert env.world.calls_with("git", "push") == []


def test_the_gate_is_re_evaluated_before_each_item(env):
    env.grant(max_items=2, daily_usd=5.0)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)

    def developer(argv, cwd):
        env.add_cost(6.0, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [(1, "pr-opened"), (2, "limit")]
    assert len(env.spawner.of("developer")) == 1
    limits = events("limit")
    assert limits[-1]["detail"]["reasons"] == ["daily-budget"]
    assert env.judge.prompts == []
    assert not store.load_mandate(ID).breaker_open


def test_a_cycle_obeys_max_items(env):
    env.grant(max_items=1)
    for n in (1, 2, 3):
        eligible(env, n, day=n)
    env.cycle()
    assert [r["issue"] for r in item_rows()] == [1]


def test_items_are_taken_in_board_rank_order_when_a_board_exists(env):
    env.grant(max_items=2)
    for n in (1, 2, 3):
        eligible(env, n, day=n)
    board = env.tmp / "board.json"
    board.write_text(json.dumps({"items": {
        "owner/repo#3": {"rank": 0}, "owner/repo#1": {"rank": 1}, "owner/repo#2": {"rank": 2},
    }}), encoding="utf-8")
    env.cycle(board_path=board)
    assert [r["issue"] for r in item_rows()] == [3, 1]


# --- triage -----------------------------------------------------------------------------

def test_triage_logs_the_label_row_before_the_label_is_applied(env):
    env.grant()
    env.world.issues.append(raw_issue(5, labels=("backlog",)))
    seen = []
    env.world.on_edit = lambda argv: seen.append(
        [(r["issue"], r["label"], r["by"]) for r in store.read_label_rows(ID)]
    )
    env.cycle()
    assert seen == [[(5, "auto-ok", "cycle")]]
    comment = Path(env.world.calls_with("gh", "issue", "comment", "5")[0][-1]).read_text(encoding="utf-8")
    assert "auto-ok" in comment and "no-auto" in comment and "veto" in comment.lower()


def test_triage_goes_through_the_judge_in_batches_of_ten_oldest_first(env):
    env.grant()
    for n in range(1, 26):
        env.world.issues.append(raw_issue(n, labels=("backlog",), day=26 - n))
    env.cycle()
    batches = [[int(x) for x in re.findall(r'<issue number="(\d+)">', p)] for p in env.judge.prompts]
    assert [len(b) for b in batches] == [10, 10, 5]
    assert [sorted(b) for b in batches] == [
        list(range(16, 26)), list(range(6, 16)), list(range(1, 6)),
    ]
    assert len(events("judge-cost")) == 3
    assert {r["issue"] for r in store.read_label_rows(ID)} == set(range(1, 26))


def test_triage_stops_when_a_judge_call_exhausts_the_budget(env):
    env.grant(daily_usd=1.0)
    for n in range(1, 16):
        env.world.issues.append(raw_issue(n, labels=("backlog",), day=n))
    env.judge.usd = 1.5
    env.cycle()
    assert len(env.judge.prompts) == 1
    assert events("limit")[-1]["detail"]["phase"] == "triage"


def test_the_judge_never_sees_a_third_party_or_out_of_domain_issue(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog",)))
    env.world.issues.append(raw_issue(2, labels=("backlog",), author="stranger"))
    env.world.issues.append(raw_issue(3, labels=("enhancement",)))
    env.world.issues.append(raw_issue(4, labels=("backlog", "no-auto")))
    env.cycle()
    assert len(env.judge.prompts) == 1
    assert re.findall(r'<issue number="(\d+)">', env.judge.prompts[0]) == ["1"]


def test_an_unparsable_judge_reply_labels_nothing(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog",)))
    env.judge.text = "I cannot decide."
    env.cycle()
    assert store.read_label_rows(ID) == []
    assert env.world.calls_with("gh", "issue", "comment") == []
    assert [e["detail"]["reason"] for e in events("triage-skipped")] == ["no-verdict"]


def test_a_judge_that_raises_is_charged_a_flat_amount_and_labels_nothing(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog",)))

    def judge(prompt):
        raise RuntimeError("unavailable")

    env.judge = judge
    assert env.cycle() == 0
    assert [e["detail"]["cost_usd"] for e in events("judge-cost")] == [driver.JUDGE_FLAT_CHARGE_USD]
    assert store.read_label_rows(ID) == []
    judged = [r for r in store.read_command_rows(ID, only_cycle_id()) if r["argv"][0] == "judge"]
    assert [(r["argv"], r["exit"]) for r in judged] == [(["judge", "triage", "#1"], 1)]


def test_a_judge_call_is_logged_with_the_issues_it_judged(env):
    env.grant()
    env.world.issues.append(raw_issue(5, labels=("backlog",)))
    env.world.issues.append(raw_issue(6, labels=("backlog",), day=2))
    env.cycle()
    judged = [r for r in store.read_command_rows(ID, only_cycle_id()) if r["argv"][0] == "judge"]
    assert [(r["argv"], r["exit"]) for r in judged] == [(["judge", "triage", "#5", "#6"], 0)]


@pytest.mark.parametrize("code", [1, 2])
def test_a_triage_comment_is_not_posted_unless_the_org_neutral_check_is_clean(env, code):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog",)))
    env.world.neutral = lambda text: code
    env.cycle()
    assert env.world.calls_with("gh", "issue", "comment") == []
    assert env.world.calls_with("gh", "issue", "edit") == []
    assert store.read_label_rows(ID) == []
    assert len(events("triage-skipped")) == 1


# --- digest delivery and eligibility ----------------------------------------------------

def triaged_world(env):
    env.grant()
    env.world.issues.append(raw_issue(5, labels=("backlog",)))
    env.cycle()
    assert any(l["name"] == "auto-ok" for l in env.world.issues[0]["labels"])


def test_a_file_digest_never_makes_a_cycle_set_label_eligible(env):
    triaged_world(env)
    env.clock.advance(hours=72)
    env.cycle()
    assert env.spawner.calls == []
    report = json.loads(env.main("run", "--dry-run", "--json")[1])
    assert report["candidates"][0]["reason"] == "digest-not-delivered"
    assert report["would_take"] == []


def test_a_cycle_set_label_becomes_eligible_only_after_a_delivered_digest_and_the_veto_window(env):
    env.plugin()
    triaged_world(env)
    digest = list(store.read_digests(ID).values())[0]
    assert (digest["notifier"], digest["ok"]) == ("chat", True)

    env.clock.advance(hours=1)
    report = json.loads(env.main("run", "--dry-run", "--json")[1])
    assert report["candidates"][0]["reason"] == "veto-window"
    assert env.cycle() == 0
    assert env.spawner.calls == []

    env.clock.advance(hours=25)
    report = json.loads(env.main("run", "--dry-run", "--json")[1])
    assert report["would_take"] == [5]
    env.cycle()
    assert [c["kind"] for c in env.spawner.calls] == ["developer", "code-reviewer"]


def test_a_plugin_that_fails_to_deliver_does_not_make_labels_eligible(env):
    env.plugin("NAME = 'chat'\ndef send(text):\n    return False\n")
    triaged_world(env)
    digest = list(store.read_digests(ID).values())[0]
    assert (digest["notifier"], digest["ok"]) == ("chat", False)
    env.clock.advance(hours=72)
    report = json.loads(env.main("run", "--dry-run", "--json")[1])
    assert report["would_take"] == []


def test_the_digest_reports_items_triage_and_the_veto_instruction(env):
    env.grant()
    eligible(env, 1)
    env.world.issues.append(raw_issue(2, labels=("backlog",)))
    env.cycle()
    text = next((store.mandate_dir(ID) / "digests").glob("*.md")).read_text(encoding="utf-8")
    assert "#1: pr-opened" in text and env.world.pr_url in text
    assert "#2: auto-ok" in text
    assert "no-auto" in text and "non-file notifier" in text


def digest_text():
    return next((store.mandate_dir(ID) / "digests").glob("*.md")).read_text(encoding="utf-8")


def digest_section(text, title):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"## {title}"))
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            body.append(line)
    return body


def test_the_digest_carries_every_item_the_owner_needs_to_audit_a_cycle(env):
    env.grant(max_items=3, item_usd_cap=2.0, weekly_usd=3.0)
    for n in (1, 2, 3):
        eligible(env, n, day=n)
    env.world.open_prs = [{
        "number": 9, "title": "t", "body": "", "headRefName": "mandate/9-20260101",
        "url": "https://example.test/owner/repo/pull/9",
    }]
    env.world.remote_heads = "aaa\trefs/heads/mandate/8-20260101\n"
    calls = {"n": 0}

    def developer(argv, cwd):
        calls["n"] += 1
        env.add_cost(0.5 if calls["n"] == 1 else 3.0, env.plan_of(argv))
        return driver.SpawnResult(0, "COMPLETED:\nok\n")

    env.spawner.developer = developer
    env.cycle()
    text = digest_text()
    mandate = store.load_mandate(ID)

    assert f"Mandate expires {mandate.expires_at}." in text
    assert "Kill switch: `agentctl mandate-stop`." in text
    items = digest_section(text, "Items")
    assert items[0].startswith("- #1: pr-opened") and env.world.pr_url in items[0]
    assert items[1].startswith("- #2: overrun")
    assert items[2].startswith("- #3: limit")
    assert digest_section(text, "Overruns") == ["- #2: item-usd-cap"]
    assert digest_section(text, "Limits hit") == ["- item (#3): weekly-budget"]
    assert digest_section(text, "Open mandate pull requests") == [
        f"- {env.world.pr_url}", "- https://example.test/owner/repo/pull/9",
    ]
    assert digest_section(text, "Remote `mandate/*` branches") == ["- mandate/8-20260101"]
    assert digest_section(text, "Labels applied this cycle") == ["- none"]
    assert digest_section(text, "Triage comments skipped") == ["- none"]
    assert "Breaker: closed." in text


def test_the_digest_lists_the_labels_applied_and_the_comments_the_neutral_check_held_back(env):
    env.grant()
    env.world.issues.append(raw_issue(3, labels=("backlog",), day=3))
    env.world.issues.append(raw_issue(4, labels=("backlog",), day=4))
    env.judge.text = json.dumps([
        {"issue": 3, "verdict": "auto-ok", "reason": "fine"},
        {"issue": 4, "verdict": "auto-ok", "reason": "LEAKY"},
    ])
    env.world.neutral = lambda text: 1 if "LEAKY" in text else 0
    env.cycle()
    text = digest_text()
    assert digest_section(text, "Labels applied this cycle") == ["- #3: `auto-ok` -- fine"]
    assert digest_section(text, "Triage comments skipped") == ["- #4: org-neutral-hit"]
    assert digest_section(text, "Triage (1)") == ["- #3: auto-ok -- fine"]
    assert "`no-auto`" in text


def test_the_digest_states_the_open_breaker_and_how_to_close_it(env):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "PERMISSION-REQUEST:\nAction: x\n")
    env.cycle()
    text = digest_text()
    assert "**Breaker open**: failed:permission on #1." in text
    assert "`agentctl mandate-resume`" in text
    assert "- #1: failed (permission)" in text


def test_a_user_set_label_is_eligible_without_any_digest(env):
    env.grant()
    eligible(env, 1)
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"


# --- never an issue create, never a user-authority transition ---------------------------

def test_no_gh_issue_create_is_invoked_across_triage_decline_and_pull_request(env):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    env.world.issues.append(raw_issue(3, labels=("backlog",)))
    n = {"count": 0}

    def developer(argv, cwd):
        n["count"] += 1
        text = "COMPLETED:\nok\n" if n["count"] == 1 else "INCOMPLETE:\nneeds a decision\n"
        return driver.SpawnResult(0, text)

    env.spawner.developer = developer
    env.cycle()
    assert {r["outcome"] for r in item_rows()} == {"pr-opened", "declined"}
    assert env.world.calls_with("gh", "issue", "comment") and env.world.calls_with("gh", "pr", "create")
    for call in env.world.calls:
        assert call[:3] != ["gh", "issue", "create"]
        assert "create" not in call[:3] or call[:3] in (["gh", "pr", "create"], ["gh", "label", "create"])


def string_constants(source):
    return [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


CYCLE_SOURCES = {
    "driver": DRIVER_SOURCE,
    "notifiers": Path(notifiers.__file__).read_text(encoding="utf-8"),
    "cli": (Path(driver.__file__).resolve().parents[1] / "mandate-cycle.py").read_text(encoding="utf-8"),
}


@pytest.mark.parametrize("name", sorted(CYCLE_SOURCES))
def test_no_cycle_source_names_issue_create(name):
    source = CYCLE_SOURCES[name]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)):
            words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            assert not ("issue" in words and "create" in words), ast.dump(node)[:120]
    assert "gh issue create" not in source
    assert "file-difficulty" not in source


FORBIDDEN_NAMES = {
    "new_mandate", "extended", "resumed", "resume_cycle", "save_mandate", "stop_cycle", "stopped",
    "cmd_mandate_grant", "cmd_mandate_extend", "cmd_mandate_resume", "cmd_mandate_stop",
}


@pytest.mark.parametrize("name", sorted(CYCLE_SOURCES))
def test_no_cycle_source_references_a_user_authority_transition(name):
    source = CYCLE_SOURCES[name]
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names & FORBIDDEN_NAMES), names & FORBIDDEN_NAMES
    for text in string_constants(source):
        assert not re.fullmatch(r"mandate-(grant|extend|resume|stop)", text), text


def test_push_refspec_pushes_the_given_commit_to_a_mandate_branch_only():
    assert driver.push_refspec("mandate/12-20261009", "c" * 40) == f"{'c' * 40}:refs/heads/mandate/12-20261009"
    for branch in ("main", "master", "release-1", "mandate", "feature/x", ""):
        with pytest.raises(driver.DriverError):
            driver.push_refspec(branch, "c" * 40)
    for sha in ("HEAD", "main", "", "c" * 39, "C" * 40, "c" * 40 + "\n"):
        with pytest.raises(driver.DriverError):
            driver.push_refspec("mandate/12-20261009", sha)


def test_real_spawn_returns_when_a_killed_process_will_not_close_its_pipes(monkeypatch):
    class Stuck:
        pid, returncode = 1, None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("stuck", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Stuck())
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    result = driver.real_spawn(["stuck"], ".", 1)
    assert (result.returncode, result.killed, result.stdout, result.stderr) == (-9, True, "", "")


# --- reaping ----------------------------------------------------------------------------

def test_a_cycle_reaps_what_a_killed_one_left_and_reports_orphan_remote_branches(env):
    env.grant()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    old, live = env.temp_root / "old", env.temp_root / "live"
    scopes.save(env.scopes_dir, scopes.ScopeRecord(
        session_id="mandate-old-1", heartbeat_ts=1.0, cwd=str(old), pid=dead.pid))
    scopes.save(env.scopes_dir, scopes.ScopeRecord(
        session_id="mandate-live-1", heartbeat_ts=1.0, cwd=str(live), pid=os.getpid()))
    scopes.save(env.scopes_dir, scopes.ScopeRecord(
        session_id="someone-else", heartbeat_ts=1.0, cwd="/x", pid=dead.pid))
    repo = env.tmp / "repo"
    env.world.blocks = {
        str(repo): f"worktree {repo}\nHEAD a\nbranch refs/heads/main\n",
        str(old): f"worktree {old}\nHEAD a\nbranch refs/heads/mandate/5-20260101\n",
        str(live): f"worktree {live}\nHEAD a\nbranch refs/heads/mandate/6-20260101\n",
    }
    env.world.local_branches = "mandate/5-20260101\nmandate/6-20260101\nmandate/9-20260101\n"
    env.world.remote_heads = (
        "aaa\trefs/heads/mandate/3-20260101\naaa\trefs/heads/mandate/4-20260101\n"
    )
    env.world.pr_heads = [{"headRefName": "mandate/4-20260101"}]

    assert env.cycle() == 0

    removed = [c[-1] for c in env.world.calls_with("git", "worktree", "remove")]
    assert removed == [str(old)]
    deleted = sorted(c[-1] for c in env.world.calls_with("git", "branch", "-D"))
    assert deleted == ["mandate/5-20260101", "mandate/9-20260101"]
    assert {r.session_id for r in scopes.load_all(env.scopes_dir)} == {"mandate-live-1", "someone-else"}
    detail = events("reaped")[0]["detail"]
    assert detail["orphan_remote_branches"] == ["mandate/3-20260101"]
    assert env.world.calls_with("git", "push") == []
    text = next((store.mandate_dir(ID) / "digests").glob("*.md")).read_text(encoding="utf-8")
    assert "mandate/3-20260101" in text


# --- readers of the driver's own helpers ------------------------------------------------

def test_parse_verdicts_accepts_only_well_formed_entries_for_the_batch():
    text = 'Sure:\n[{"issue": 1, "verdict": "auto-ok", "reason": "r"}, {"issue": 2, "verdict": "maybe"},' \
           ' {"issue": 99, "verdict": "auto-no"}, {"issue": true, "verdict": "auto-ok"}, "x"]'
    assert driver.parse_verdicts(text, {1, 2}) == {1: ("auto-ok", "r")}
    assert driver.parse_verdicts("no json here", {1}) == {}


def test_read_junit_counts_failures_errors_and_skips_as_not_passed(tmp_path):
    path = tmp_path / "r.xml"
    path.write_text(
        '<testsuite><testcase classname="a.b" name="ok"/>'
        '<testcase classname="a.b" name="bad"><failure/></testcase>'
        '<testcase classname="a.b" name="err"><error/></testcase>'
        '<testcase classname="a.b" name="skip"><skipped/></testcase></testsuite>',
        encoding="utf-8",
    )
    result = driver.read_junit(path)
    assert result.passed == {"a.b::ok"} and len(result.seen) == 4 and result.ran
    assert driver.read_junit(tmp_path / "missing.xml").ran is False


# --- the timer installer ----------------------------------------------------------------

INSTALLER = Path(__file__).resolve().parents[1] / "install-mandate-timer.sh"


@pytest.fixture
def box(tmp_path):
    fakes = tmp_path / "fakebin"
    fakes.mkdir()
    log = tmp_path / "systemctl.log"
    fake = fakes / "systemctl"
    fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {"fakes": fakes, "log": log, "home": home, "units": tmp_path / "units"}


def install(box, *args, extra_env=None):
    env = {
        "PATH": f"{box['fakes']}:/usr/bin:/bin", "HOME": str(box["home"]),
        "SYSTEMD_USER_DIR": str(box["units"]), "SYSTEMCTL_BIN": str(box["fakes"] / "systemctl"),
    }
    env.update(extra_env or {})
    return subprocess.run(["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=60)


def systemctl_calls(box):
    return box["log"].read_text(encoding="utf-8").splitlines() if box["log"].exists() else []


def test_the_installer_writes_a_oneshot_service_and_a_nightly_timer_and_enables_it(box):
    result = install(box)
    assert result.returncode == 0, result.stderr
    service = (box["units"] / "agent-debt-cycle.service").read_text(encoding="utf-8")
    timer = (box["units"] / "agent-debt-cycle.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "WorkingDirectory=%h/claude-agent-instructions" in service
    assert "EnvironmentFile=%h/.config/agent-debt-cycle/env" in service
    assert "ExecStart=%h/claude-agent-instructions/scripts/mandate-cycle.py run" in service
    assert "append:%h/.local/log/agent-debt-cycle.log" in service
    assert "OnCalendar=*-*-* 01:00:00" in timer and "Persistent=false" in timer
    assert systemctl_calls(box) == [
        "--user daemon-reload", "--user enable --now agent-debt-cycle.timer",
    ]


def test_the_env_file_carries_path_and_config_roots_but_never_a_token(box):
    result = install(box, extra_env={
        "CLAUDE_AGENT_HOME": "/srv/agent", "GH_CONFIG_DIR": "/srv/gh",
        "GH_TOKEN": "ghp_secret", "GITHUB_TOKEN": "ghp_secret2", "ANTHROPIC_API_KEY": "sk-secret",
    })
    assert result.returncode == 0, result.stderr
    env_file = box["home"] / ".config" / "agent-debt-cycle" / "env"
    text = env_file.read_text(encoding="utf-8")
    assert text.startswith("PATH=")
    assert "CLAUDE_AGENT_HOME=/srv/agent\n" in text and "GH_CONFIG_DIR=/srv/gh\n" in text
    assert "CLAUDE_CONFIG_DIR" not in text
    assert "secret" not in text and "TOKEN" not in text and "API_KEY" not in text
    assert env_file.stat().st_mode & 0o077 == 0


def test_write_env_writes_only_the_env_file(box):
    result = install(box, "--write-env")
    assert result.returncode == 0, result.stderr
    assert (box["home"] / ".config" / "agent-debt-cycle" / "env").is_file()
    assert not box["units"].exists()
    assert systemctl_calls(box) == []


def test_uninstall_disables_the_timer_and_removes_both_units(box):
    assert install(box).returncode == 0
    box["log"].unlink()
    result = install(box, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert list(box["units"].iterdir()) == []
    assert systemctl_calls(box) == ["--user disable --now agent-debt-cycle.timer", "--user daemon-reload"]
    assert (box["home"] / ".config" / "agent-debt-cycle" / "env").is_file()


def test_the_installer_rejects_an_unknown_argument(box):
    assert install(box, "--frobnicate").returncode == 2
