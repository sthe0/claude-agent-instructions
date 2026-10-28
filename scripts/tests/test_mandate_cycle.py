"""The background debt cycle driver, run end to end against a fake world.

Everything the driver reaches outside itself goes through three seams -- the command runner
(`gh`, `git`, the test suite, the org-neutral checker), the specialist spawner and the triage
judge -- and all three are replaced here, so a test spawns no process and touches no network,
systemd or real agent home. The mandate store and the cost log live in tmp dirs.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl import cost, inline_class, inline_git, mandate as rules, mandate_store as store, norm_handoff, state as state_module, task_accumulator
from agentctl.config import Thresholds, parse_config_md
from agentctl.dispatch import RunResult
from agentctl.store import FileStateStore
from lib import config_root
from mandate_cycle import driver, notifiers
from session_scope import registry as scopes
from slow_selection import RUN_SLOW_OPTION

ID = "core-debt"
OWNER = "owner"
BASE_SHA = "c" * 40
TREE_SHA = "d" * 40
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
    def split(name):
        """`module|name` puts the case in another junit class than the default one."""
        module, _, case = name.rpartition("|")
        return module or "scripts.tests.test_a", case

    for name in passed:
        ET.SubElement(suite, "testcase", classname=split(name)[0], name=split(name)[1])
    for name in failed:
        case = ET.SubElement(suite, "testcase", classname=split(name)[0], name=split(name)[1])
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
        self.numstat = "1\t0\tscripts/foo.py\0"
        self.generated: "set[str]" = set()
        self.attr_cwds: "list[str | None]" = []
        self.merge_base = "b" * 40
        self.failing: "list[str]" = []
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
        self.base = BASE_SHA
        self.tree = TREE_SHA
        self.trees: "dict[str, str]" = {}
        self.patch = "+++ b/scripts/foo.py\n+a new line\n"
        self.status = ""
        # Files a fresh worktree holds: the junit class `scripts.tests.test_a` maps to the first.
        self.worktree_files = ["scripts/tests/test_a.py"]
        self.engine_calls: "list[tuple[str, list[str]]]" = []
        self.engine_refusals: "dict[str, str]" = {}
        self.grants = [{"rule": "Bash(python3 -m pytest:*)", "provenance": "declared"}]
        self.on_engine = None

    def engine(self, verb, flags):
        """The engine CLI as the driver drives it: one JSON directive per verb."""
        self.engine_calls.append((verb, list(flags)))
        if self.on_engine:
            self.on_engine(verb, flags)
        if verb in self.engine_refusals:
            return RunResult(1, json.dumps({"ok": False, "data": {"blockers": [self.engine_refusals[verb]]}}))
        if verb == "stage-grants":
            return ok(json.dumps({"ok": True, "data": {"grants": self.grants}}))
        return ok(json.dumps({"ok": True}))

    def engine_verbs(self):
        return [verb for verb, _ in self.engine_calls]

    def run(self, argv, cwd=None, timeout=None, stdin=None):
        self.calls.append(list(argv))
        a = list(argv)
        if any(word in a for word in self.failing):
            return RunResult(1, stderr="fatal: boom")
        if a[0] == sys.executable and a[1].endswith("agentctl-cli.py"):
            return self.engine(a[2], a[3:])
        if a[0] == "env" and a[1].startswith("GIT_INDEX_FILE="):
            return ok(self.tree + "\n") if "write-tree" in a else ok()
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
        if a[:2] == ["git", "rev-parse"]:
            if a[2] == "HEAD":
                return ok(self.head + "\n")
            if a[2].endswith("^{tree}"):
                return ok(self.trees.get(a[2][:-len("^{tree}")], self.tree) + "\n")
            return ok(self.base + "\n")
        if a[:2] == ["git", "diff"]:
            if "--numstat" in a:
                return ok(self.numstat)
            if "--name-only" in a:
                names = [line.split("\t")[-1] for line in self.name_status.splitlines()]
                return ok("".join(f"{n}\n" for n in names))
            return ok(self.name_status if "--name-status" in a else self.patch)
        if a[:2] == ["git", "merge-base"]:
            return ok(self.merge_base + "\n")
        if a[:2] == ["git", "check-attr"]:
            self.attr_cwds.append(cwd)
            out = "".join(
                f"{p}\0linguist-generated\0{'true' if p in self.generated else 'unspecified'}\0"
                for p in (stdin or "").split("\0")[:-1]
            )
            return ok(out)
        if a[:2] == ["git", "log"]:
            return ok("a commit message\n")
        if a[:1] == ["bash"] and a[1].endswith("cap-run.sh"):
            target = next(x for x in a if x.startswith("--junitxml="))[len("--junitxml="):]
            passed, failed = self.suites[Path(target).name]
            # A test id may embed the path of the worktree the suite ran in, in either spelling.
            spellings = {"{worktree}": str(cwd), "{realpath}": os.path.realpath(cwd)}

            def spelled(names):
                out = []
                for name in names:
                    for placeholder, path in spellings.items():
                        name = name.replace(placeholder, path)
                    out.append(name)
                return out

            Path(target).write_text(junit(spelled(passed), spelled(failed)), encoding="utf-8")
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
        self.reviewer = lambda argv, cwd: driver.SpawnResult(0, "COMPLETED:\nNo blocking findings.\nVERDICT: accept\n")

    def __call__(self, argv, cwd, seconds):
        kind = argv[argv.index("--kind") + 1]
        self.calls.append({"argv": list(argv), "cwd": cwd, "seconds": seconds, "kind": kind})
        self.live_scopes.append([r.session_id for r in scopes.load_all(self.scopes_dir)])
        if kind == "developer" and "--session" in argv:
            parent = argv[argv.index("--session") + 1]
            norm_handoff.write_child_session(
                config_root.agentctl_state_dir(), parent, driver.STAGE_INDEX, session=f"child-{parent}", workdir=cwd,
            )
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
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "agent-home"))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
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
    e.check = None
    e.child = {"node": "RESOLUTION", "approval_basis": None, "fire_scale": None, "norm_reasons": []}
    e.child_state = lambda session: dict(e.child)
    e.transcripts_seen = []
    e.transcripts = lambda workdir, since: {}

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
        def transcripts(workdir, since):
            e.transcripts_seen.append((workdir, since))
            return e.transcripts(workdir, since)

        return driver.Seams(
            run=e.world.run, spawn=e.spawner, judge=e.judge, clock=e.clock,
            child_state=lambda session: e.child_state(session), transcripts=transcripts,
            check=None if e.check is None else (lambda inputs: e.check(inputs)),
        )

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


def paused_with_a_chat_notifier(env):
    env.clock.t = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    env.grant()
    store.save_mandate(rules.stopped(store.load_mandate(ID)))
    sent = env.tmp / "sent.txt"
    env.plugin(f"NAME = 'chat'\ndef send(text):\n    open({str(sent)!r}, 'a').write(text + '\\n=====\\n')\n    return True\n")
    return sent


def test_a_refused_start_sends_a_notice_through_the_digest_notifier(env):
    sent = paused_with_a_chat_notifier(env)
    assert env.cycle() == 0
    text = sent.read_text(encoding="utf-8")
    assert ID in text and "paused" in text
    assert "agentctl mandate-resume --by <you>" in text
    assert f"Blocked since {rules.format_ts(env.clock())}" in text
    [notice] = events("refused-notice")
    assert notice["detail"]["reasons"] == ["paused"]
    assert notice["detail"]["notifier"] == "chat" and notice["detail"]["ok"] is True
    assert notice["detail"]["blocked_since"] == rules.format_ts(env.clock())
    assert notice["detail"]["unblock"] == ["paused: agentctl mandate-resume --by <you>"]
    assert list(store.read_cycle_records(ID).values())[0]["notice"] == "sent"
    assert list((store.mandate_dir(ID) / "notices").glob("*.md"))


def test_a_second_refusal_the_same_day_is_suppressed(env):
    sent = paused_with_a_chat_notifier(env)
    env.cycle()
    env.clock.advance(minutes=30)
    assert env.cycle() == 0
    assert sent.read_text(encoding="utf-8").count("=====") == 1
    assert len(events("refused-notice")) == 1
    assert [r["notice"] for r in store.read_cycle_records(ID).values()] == ["sent", "suppressed"]
    env.clock.advance(days=1)
    env.cycle()
    assert sent.read_text(encoding="utf-8").count("=====") == 2


def test_a_crashing_refusal_notice_does_not_crash_the_run(env, monkeypatch):
    paused_with_a_chat_notifier(env)

    def explode(*args, **kwargs):
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(notifiers, "deliver", explode)
    assert env.cycle() == 0
    [error] = events("refused-notice-error")
    assert "notifier exploded" in error["detail"]["error"]
    assert list(store.read_cycle_records(ID).values())[0]["notice"] == "failed"


def test_a_refusal_notice_is_not_recorded_as_a_digest(env):
    paused_with_a_chat_notifier(env)
    env.cycle()
    assert store.read_digests(ID) == {}
    assert all("digest" not in row for row in store.read_cycle_rows(ID))
    assert not (store.mandate_dir(ID) / "digests").exists()


def test_the_digest_warns_of_an_expiry_within_three_days(env):
    env.grant()
    mandate = store.load_mandate(ID)
    env.cycle()
    assert "Mandate expires in" not in digest_text()
    for stale in (store.mandate_dir(ID) / "digests").glob("*.md"):
        stale.unlink()
    soon = rules.format_ts(env.clock() + timedelta(days=2, hours=1))
    store.save_mandate(dataclasses.replace(mandate, expires_at=soon))
    env.clock.advance(seconds=1)
    env.cycle()
    assert f"Mandate expires in 2 days ({soon}): run `agentctl mandate-extend --days <n> --by <you>`" in digest_text()


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
    env.world.trees[MOVED] = "e" * 40
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
        return driver.SpawnResult(0, "COMPLETED:\nNo blocking findings.\nVERDICT: accept\n")

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
        return driver.SpawnResult(0, "COMPLETED:\nNo blocking findings.\nVERDICT: accept\n")

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


def test_a_check_failed_row_says_which_gates_ran_and_has_no_pull_request(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1"], ["t2"])
    env.cycle()
    row = only_row(
        issue=1, outcome="declined", marker=driver.EXCEEDS_MARKER, stop_reason="rounds", tests="fail",
        review="not-run", pr_url=None, gates={"constitution": "pass", "org_neutral": "pass", "lint": "pass"},
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
        issue=1, outcome="declined", marker=driver.EXCEEDS_MARKER, tests="not-run", review="not-run", pr_url=None,
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
        gates={"constitution": "fail", "org_neutral": "pass", "lint": "not-run"},
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


def test_a_failing_test_that_maps_to_no_module_is_not_rerun_and_fails_the_check(env):
    env.grant()
    eligible(env, 1)
    env.world.worktree_files = []
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], [])
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"], row["tests"]) == ("declined", "rounds", "fail")
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
    assert "<<this step>>" not in brief and "<this step>" in brief


def test_a_third_party_issue_never_reaches_a_spawn_or_the_judge(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog", "auto-ok"), author="stranger"))
    env.cycle()
    assert env.spawner.calls == [] and env.judge.prompts == []


# --- tests relative to the baseline -----------------------------------------------------

def test_the_suite_runs_under_the_conftest_auto_parallel_with_no_xdist_opt_out(env):
    env.grant()
    eligible(env, 1)
    env.cycle()
    suites = [c for c in env.world.calls_with("bash") if c[1].endswith("cap-run.sh")]
    assert len(suites) >= 2, suites
    for argv in suites:
        opt_outs = [x for x in argv if x in ("no:xdist", "-n") or x.startswith("--dist")]
        assert opt_outs == [], argv


def test_the_baseline_and_every_item_run_the_whole_slow_set(env):
    """Slow selection runs every slow test on the baseline (its merge base is HEAD) but skips
    unguarded ones on an item branch; a skip is not a pass, so both runs must force the full set."""
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], [])
    env.cycle()
    suites = [c for c in env.world.calls_with("bash") if c[1].endswith("cap-run.sh")]
    runs = {name: [c for c in suites if any(x.endswith(name) for x in c)]
            for name in (driver.BASELINE_XML, "item.xml", "rerun.xml")}
    assert all(runs.values()), runs
    for argv in suites:
        assert RUN_SLOW_OPTION in argv, argv


def test_a_test_failing_on_the_baseline_does_not_count_against_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["baseline.xml"] = (["t1", "t2"], ["t_broken"])
    env.world.suites["item.xml"] = (["t1", "t2"], ["t_broken"])
    env.world.suites["rerun.xml"] = (["t1", "t2"], ["t_broken"])
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"
    assert item_rows()[0]["tests"] == "pass"


def remove_tests(env, baseline, item):
    """The item edits `scripts/tests/test_a.py` and its suite lacks tests the baseline passed."""
    env.world.name_status = "M\tscripts/tests/test_a.py\n"
    env.world.suites["baseline.xml"] = (baseline, [])
    env.world.suites["item.xml"] = (item, [])
    env.world.suites["rerun.xml"] = (item, [])


def test_a_test_removed_by_the_item_does_not_fail_the_gate_and_is_reported(env):
    env.grant()
    eligible(env, 1)
    remove_tests(env, ["t1", "t2"], ["t1"])
    env.cycle()
    row = item_rows()[0]
    assert (row["tests"], row["outcome"]) == ("pass", "pr-opened")
    assert row["excused_tests"] == ["scripts.tests.test_a::t2"] and row["excused_count"] == 1
    assert not any(x.endswith("rerun.xml") for c in env.world.calls for x in c)


def test_a_test_vanishing_from_an_untouched_module_still_fails_the_check(env):
    env.grant()
    eligible(env, 1)
    remove_tests(env, ["t1", "t2"], ["t1"])
    env.world.name_status = "M\tscripts/foo.py\n"
    env.cycle()
    row = item_rows()[0]
    assert (row["tests"], row["stop_reason"]) == ("fail", "rounds")
    assert row["excused_tests"] == [] and row["excused_count"] == 0


def test_an_excused_id_is_not_held_against_the_item_after_the_rerun(env):
    env.grant()
    eligible(env, 1)
    env.world.worktree_files = ["scripts/tests/test_a.py", "scripts/tests/test_z.py"]
    flaky = "scripts.tests.test_z|t9"
    env.world.name_status = "M\tscripts/tests/test_a.py\n"
    env.world.suites["baseline.xml"] = (["t1", "t2", flaky], [])
    env.world.suites["item.xml"] = (["t1"], [flaky])
    env.world.suites["rerun.xml"] = (["t1", flaky], [])
    env.cycle()
    row = item_rows()[0]
    assert (row["tests"], row["outcome"]) == ("pass", "pr-opened")
    assert row["excused_tests"] == ["scripts.tests.test_a::t2"]
    assert any(x.endswith("rerun.xml") for c in env.world.calls for x in c)


def test_a_collection_error_in_the_touched_module_fails_the_gate(tmp_path):
    module = tmp_path / "scripts" / "tests" / "test_broken.py"
    module.parent.mkdir(parents=True)
    module.write_text("import a_module_that_does_not_exist\n\n\ndef test_x():\n    pass\n", encoding="utf-8")
    xml_path = tmp_path / "item.xml"
    env_ = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", f"--rootdir={tmp_path}",
         f"--junitxml={xml_path}", str(module)],
        cwd=tmp_path, env=env_, capture_output=True, check=False,
    )
    item = driver.read_junit(xml_path)
    assert item.ran and not item.passed
    baseline_id = "scripts.tests.test_broken::test_x"
    failing, excused = rules.split_failures(
        {baseline_id}, {baseline_id}, item.passed, item.seen, rules.diff_path_groups(["M\tscripts/tests/test_broken.py"]),
    )
    assert (failing, excused) == ([baseline_id], [])


def test_the_digest_lists_the_excused_ids_capped(env):
    env.grant()
    eligible(env, 1)
    names = [f"u{n:02d}" for n in range(1, 13)]
    remove_tests(env, ["t1", *names], ["t1"])
    env.cycle()
    row = item_rows()[0]
    ids = [f"scripts.tests.test_a::{n}" for n in names]
    assert row["excused_tests"] == ids[:10] and row["excused_count"] == 12
    line = digest_section(digest_text(), "Items")[0]
    assert f"excused (removed or renamed by the item): 12: {', '.join(ids[:10])}, ... and 2 more" in line


PATH_ID = "test_c[Bash(python3 {worktree}/scripts/agentctl-cli.py:*)]"
PATH_ID_REAL = "test_c[Bash(python3 {realpath}/scripts/agentctl-cli.py:*)]"


def link_temp_root(env) -> None:
    """Reach the worktree root through a symlink, so a worktree has two spellings (the one the
    driver passes and its realpath), as /var/tmp -> /place/vartmp gives it on the host."""
    env.temp_root.mkdir(parents=True, exist_ok=True)
    link = env.tmp / "worktrees-link"
    link.symlink_to(env.temp_root, target_is_directory=True)
    env.temp_root = link


def test_a_test_id_that_embeds_the_worktree_path_is_the_same_test_on_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["baseline.xml"] = (["t1", PATH_ID], [])
    env.world.suites["item.xml"] = (["t1", PATH_ID_REAL], [])
    link_temp_root(env)
    env.cycle()
    row = item_rows()[0]
    assert (row["tests"], row["outcome"]) == ("pass", "pr-opened")
    assert not any(x.endswith("rerun.xml") for c in env.world.calls for x in c)


def test_a_path_embedding_test_that_passes_on_its_rerun_does_not_fail_the_item(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["baseline.xml"] = (["t1", PATH_ID], [])
    env.world.suites["item.xml"] = (["t1"], [PATH_ID])
    env.world.suites["rerun.xml"] = (["t1", PATH_ID], [])
    link_temp_root(env)
    env.cycle()
    row = item_rows()[0]
    assert (row["tests"], row["outcome"]) == ("pass", "pr-opened")
    assert any(x.endswith("rerun.xml") for c in env.world.calls for x in c)


def test_a_baseline_passing_test_that_fails_on_the_item_is_rejected_each_round_and_not_pushed(env):
    env.grant()
    eligible(env, 1)
    env.world.suites["item.xml"] = (["t1"], ["t2"])
    env.world.suites["rerun.xml"] = (["t1"], ["t2"])
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"], row["tests"]) == ("declined", "rounds", "fail")
    assert row["rounds"] == Thresholds().effort_replan_absolute()
    assert len(env.spawner.of("developer")) == row["rounds"] + 1
    assert env.world.calls_with("git", "push") == []
    assert env.world.calls_with("gh", "pr", "create") == []
    assert not store.load_mandate(ID).breaker_open


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


def test_a_gate_script_the_item_breaks_fails_the_check(env):
    env.grant()
    eligible(env, 1)
    original = env.world.run

    def run(argv, cwd=None, timeout=None, **kwargs):
        if argv[:2] == [sys.executable, "scripts/lint-prose-length.py"] and not str(cwd).endswith("-baseline"):
            env.world.calls.append(list(argv))
            return RunResult(1)
        return original(argv, cwd, timeout, **kwargs)

    env.world.run = run
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"]) == ("declined", "rounds")
    assert row["gates"]["lint"] == "fail" and row["gate_exit_codes"]["scripts/lint-prose-length.py"] == 1


# --- what a diff may not touch, what may not be published -------------------------------

def test_a_diff_that_touches_the_constitution_is_never_pushed_and_fails_only_that_item(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/foo.py\nM\tscripts/agentctl/mandate.py\n"
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "constitution")
    assert env.world.calls_with("git", "push") == []
    assert env.spawner.of("code-reviewer") == []
    assert not store.load_mandate(ID).breaker_open


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
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(0, "COMPLETED:\nBlocking: x.\nVERDICT: reject\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"], row["review"]) == ("declined", "rounds", "reject")
    assert env.world.calls_with("git", "push") == []


def test_a_review_that_never_states_a_verdict_is_a_rejection(env):
    env.grant()
    eligible(env, 1)
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(0, "COMPLETED:\nLooks good to me.\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["review"], row["stop_reason"]) == ("reject", "rounds")
    assert env.world.calls_with("git", "push") == []


def test_a_diff_with_no_changed_file_is_declined(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = ""
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"]) == ("declined", "rounds")
    assert not store.load_mandate(ID).breaker_open


# --- the inline size bound ---------------------------------------------------------------

BOUND = Thresholds().small_change_max_lines


def numstat(*entries):
    return "".join(f"{added}\t{deleted}\t{path}\0" for added, deleted, path in entries)


def over_bound_item(env, number=1, entries=None, name_status="M\tscripts/foo.py\n"):
    env.grant()
    eligible(env, number)
    env.world.name_status = name_status
    env.world.numstat = numstat(*(entries or [(BOUND + 1, 0, "scripts/foo.py")]))
    env.cycle()
    return only_row(
        issue=number, outcome="declined", marker=driver.EXCEEDS_MARKER, stop_reason="rounds", pr_url=None,
    )


def judge(env, basis=None, head=None):
    """`judge_item` over the fake world: the baseline in a scratch directory, the item in a fake tree."""
    baseline_dir, item_dir = env.tmp / "judge-baseline", env.tmp / "judge-item"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / driver.BASELINE_XML).write_text(junit(*env.world.suites["baseline.xml"]), encoding="utf-8")
    (baseline_dir / driver.BASELINE_GATES).write_text(json.dumps({s: 0 for s in driver.GATE_SCRIPTS}), encoding="utf-8")
    inputs = {"base_sha": env.world.base, "worktree": str(env.tmp / "wt"), "bound": BOUND, "repo": str(env.tmp / "repo")}
    return driver.judge_item(
        env.world.run, inputs, mandate=store.load_mandate(ID), item_dir=item_dir, baseline_dir=baseline_dir,
        suite=driver.DEFAULT_SUITE, timeout_s=60, basis=basis or (lambda: None), head=head,
    )


def test_a_small_production_change_with_many_test_and_generated_lines_opens_a_pull_request(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/foo.py\nM\tscripts/tests/test_foo.py\nM\tgen/out.py\n"
    env.world.numstat = numstat(
        (3, 0, "scripts/foo.py"), (BOUND * 5, 0, "scripts/tests/test_foo.py"), (BOUND * 5, 0, "gen/out.py"),
    )
    env.world.generated = {"gen/out.py"}
    env.cycle()
    row = item_rows()[0]
    assert row["outcome"] == "pr-opened"
    assert "inline" not in row
    assert env.world.calls_with("gh", "pr", "create")
    assert "Needs a plan" not in digest_text()


def test_a_change_of_exactly_the_bound_is_still_opened(env):
    env.grant()
    eligible(env, 1)
    env.world.numstat = numstat((BOUND - 2, 2, "scripts/foo.py"))
    env.cycle()
    assert item_rows()[0]["outcome"] == "pr-opened"


def test_a_change_over_the_bound_is_rejected_as_a_failed_check_and_never_pushed(env):
    row = over_bound_item(env, entries=[(BOUND, 1, "scripts/foo.py")])
    assert [a["kind"] for a in row["asks"]][:1] == ["over-bound-inline"]
    assert "inline" not in row
    assert env.world.calls_with("git", "push") == []
    assert env.world.calls_with("gh", "pr", "create") == []
    assert env.spawner.of("code-reviewer") == []
    assert not store.load_mandate(ID).breaker_open
    label = store.load_mandate(ID).labels["no"]
    assert env.world.calls_with("gh", "issue", "edit", "1", "--add-label", label)


def test_the_generated_set_is_read_in_the_drivers_own_checkout(env):
    over_bound_item(env)
    assert env.world.attr_cwds and set(env.world.attr_cwds) == {str(env.cfg().repo)}


def test_a_counted_binary_file_fails_the_check_even_when_its_lines_are_few(env):
    env.grant()
    env.world.numstat = numstat((5, 0, "scripts/foo.py"), ("-", "-", "scripts/blob.py"))
    verdict = judge(env)
    assert verdict.production_lines == 5 and verdict.over_bound
    assert [check for check, _ in verdict.failures] == ["bound"]


def test_the_digest_lists_an_over_bound_item_as_declined_by_the_mandate(env):
    over_bound_item(env)
    text = digest_text()
    assert digest_section(text, "Items") == ["- #1: declined (exceeds-mandate) -- $0.00"]
    assert "Needs a plan" not in text


def test_an_item_that_adds_an_attributes_file_is_stopped_by_the_constitution_before_measurement(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/foo.py\nA\tscripts/x/.gitattributes\n"
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "constitution")
    assert env.world.calls_with("git", "check-attr") == []
    assert env.world.calls_with("git", "diff", "--numstat") == []


def git(cwd, *argv):
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def real_item(tmp_path):
    """A trunk checkout and an item worktree whose one commit adds a 300-line `gen.py` and a
    `.gitattributes` marking it generated. Returns the Cycle over the trunk, the worktree and its tip."""
    repo, worktree = tmp_path / "trunk", tmp_path / "item"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "worktree", "add", "-q", "-b", "item", str(worktree), "origin/main")
    (worktree / ".gitattributes").write_text("gen.py linguist-generated=true\n", encoding="utf-8")
    (worktree / "gen.py").write_text("".join(f"y = {i}\n" for i in range(300)), encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "item")
    return repo, worktree, git(worktree, "rev-parse", "HEAD")


def measure(repo, worktree, tip, source="origin/main"):
    """What `judge_item` measures: the item's tree against trunk, the attributes read at trunk."""
    def runner(cwd, argv, stdin=""):
        return subprocess.run(argv, cwd=cwd, input=stdin or None, capture_output=True, text=True, check=True).stdout

    base = git(repo, "rev-parse", "origin/main")
    return inline_git.measure_range(worktree, base, tip, attrs_repo=repo, attrs_source=git(repo, "rev-parse", source), runner=runner)


def test_the_measurement_counts_a_file_the_items_own_attributes_mark_generated(env, tmp_path):
    repo, worktree, tip = real_item(tmp_path)
    measured = measure(repo, worktree, tip)
    assert "gen.py" in measured.counted_paths
    assert measured.production_lines == 300


def test_the_measurement_reads_the_trusted_checkouts_committed_attributes_not_its_working_tree(env, tmp_path):
    repo, worktree, tip = real_item(tmp_path)
    (repo / ".gitattributes").write_text("gen.py linguist-generated=true\n", encoding="utf-8")
    measured = measure(repo, worktree, tip)
    assert measured.production_lines == 300


def test_the_measurement_reads_the_attributes_at_trunk_not_at_the_checkouts_lagging_head(env, tmp_path):
    repo, worktree, tip = real_item(tmp_path)
    base = git(repo, "rev-parse", "origin/main")
    lagging_head = git(repo, "rev-parse", "HEAD")
    (repo / ".gitattributes").write_text("gen.py linguist-generated=true\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "trunk marks gen.py generated")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "-q", "--hard", lagging_head)

    def runner(cwd, argv, stdin=""):
        return subprocess.run(argv, cwd=cwd, input=stdin or None, capture_output=True, text=True, check=True).stdout

    measured = inline_git.measure_range(
        worktree, base, tip, attrs_repo=repo, attrs_source=git(repo, "rev-parse", "origin/main"), runner=runner,
    )
    assert "gen.py" not in measured.counted_paths
    assert measured.production_lines == 0


def test_measure_range_runs_the_change_in_repo_and_the_attributes_in_attrs_repo_at_the_source(tmp_path):
    calls = []

    def runner(repo, argv, stdin):
        calls.append((repo, argv[:3], stdin))
        return "3\t0\ta.py\0" if "diff" in argv else "a.py\0linguist-generated\0set\0"

    measured = inline_git.measure_range(
        tmp_path / "item", "base", "tip", attrs_repo=tmp_path / "trunk", attrs_source="HEAD", runner=runner,
    )
    assert measured.production_lines == 0
    assert [(c[0], c[1][1]) for c in calls] == [(tmp_path / "item", "diff"), (tmp_path / "trunk", "check-attr")]
    assert calls[1][1][2] == "--source=HEAD" and calls[1][2] == "a.py\0"


@pytest.mark.parametrize("failing, detail", [("--numstat", "git diff: fatal: boom"), ("check-attr", "git check-attr: fatal: boom")])
def test_a_failed_measurement_command_fails_the_check_closed_with_no_push_and_no_pull_request(env, failing, detail):
    env.grant()
    eligible(env, 1)
    env.world.failing.append(failing)
    assert ("bound", detail) in judge(env).failures
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["stop_reason"]) == ("declined", "rounds")
    assert row["pr_url"] is None
    assert env.world.calls_with("git", "push") == []
    assert env.world.calls_with("gh", "pr", "create") == []
    assert env.spawner.of("code-reviewer") == []


def test_the_real_runner_passes_stdin_through_and_returns_its_output():
    done = driver.real_run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper(), end='')"], stdin="abc")
    assert (done.returncode, done.stdout) == (0, "ABC")


def test_the_real_runner_runs_without_stdin():
    done = driver.real_run([sys.executable, "-c", "print('x', end='')"])
    assert (done.returncode, done.stdout) == (0, "x")


def test_the_real_runner_reports_a_timeout_with_or_without_stdin():
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    for stdin in (None, ""):
        done = driver.real_run(sleeper, timeout=0.2, stdin=stdin)
        assert done.returncode == -1
        assert "timed out after 0.2s" in done.stderr


def test_the_real_runner_reports_a_missing_program():
    done = driver.real_run(["/nonexistent/program"], stdin="x")
    assert done.returncode == 127
    assert "FileNotFoundError" in done.stderr


def test_the_norm_and_constraints_state_the_bound_and_the_check(env):
    env.grant()
    eligible(env, 1)
    env.cycle()
    plan = Path(env.plan_of(env.spawner.of("developer")[0]["argv"]))
    assert f"over {BOUND} production lines" in " ".join(plan.read_text(encoding="utf-8").split())
    argv = env.spawner.of("developer")[0]["argv"]
    constraints = Path(argv[argv.index("--constraints") + 1].removeprefix("@")).read_text(encoding="utf-8")
    assert "check-item --item-dir" in constraints


def test_the_triage_prompt_admits_an_in_repo_item_of_any_size_and_keeps_its_json_example(env):
    env.grant()
    env.world.issues.append(raw_issue(1, labels=("backlog",)))
    env.cycle()
    prompt = env.judge.prompts[0]
    assert "only inside a checkout of this repository" in prompt
    assert 'Size is not a reason for "auto-no"' in prompt
    assert '[{"issue": <number>, "verdict": "auto-ok" | "auto-no"' in prompt
    assert '<issue number="1">' in prompt
    assert inline_class.INLINE_DEFINITION not in prompt
    assert "small-change-max-lines" not in prompt and "needs-plan" not in prompt


# --- developer return markers -----------------------------------------------------------

@pytest.mark.parametrize(
    "marker, reason",
    [("INCOMPLETE", "incomplete"), ("REPLAN", "incomplete"), ("ESCALATE", "question-for-user"), ("CLARIFY", "question-for-user")],
)
def test_a_developer_that_cannot_proceed_stops_the_item_without_the_breaker(env, marker, reason):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, f"{marker}:\nNeeds a decision.\n")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["marker"], row["stop_reason"]) == ("declined", driver.EXCEEDS_MARKER, reason)
    assert not store.load_mandate(ID).breaker_open
    labels = [r for r in store.read_label_rows(ID) if r["by"] == "cycle"]
    assert [(r["issue"], r["label"]) for r in labels] == [(1, "auto-no")]
    assert env.world.calls_with("gh", "issue", "edit", "1", "--add-label", "auto-no")
    assert env.world.calls_with("git", "push") == []


def test_a_specialist_that_returns_no_marker_fails_the_item(env):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(1, "", "traceback")
    env.cycle()
    row = item_rows()[0]
    assert (row["outcome"], row["failure_kind"]) == ("failed", "spawn-error")


def test_a_spawn_error_still_opens_the_breaker(env):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(1, "", "traceback")
    env.cycle()
    assert [r["issue"] for r in item_rows()] == [1]
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "failed:spawn-error on #1"
    assert list(store.read_cycle_records(ID).values())[0]["status"] == "breaker"


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


def constitution_fails(env, issues):
    """The item check is red on the constitution for these issues and green for the rest."""
    env.check = lambda inputs: (
        driver.ItemVerdict(failures=[("constitution", "scripts/agentctl/mandate.py")])
        if inputs["issue"] in issues else driver.ItemVerdict()
    )


def test_an_item_local_failure_is_recorded_and_the_cycle_continues(env):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    constitution_fails(env, {1})
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [(1, "failed"), (2, "pr-opened")]
    assert not store.load_mandate(ID).breaker_open
    assert events("breaker-open") == []
    assert list(store.read_cycle_records(ID).values())[0]["status"] == "ok"


def test_three_consecutive_item_failures_open_the_breaker(env):
    env.grant(max_items=4)
    for n in (1, 2, 3, 4):
        eligible(env, n, day=n)
    constitution_fails(env, {1, 2, 3, 4})
    env.cycle()
    assert [r["issue"] for r in item_rows()] == [1, 2, 3]
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "3 consecutive item failures: #1, #2, #3"
    assert [e["detail"] for e in events("breaker-open")] == [{"reason": mandate.breaker_reason}]
    assert list(store.read_cycle_records(ID).values())[0]["status"] == "breaker"


def test_a_pull_request_between_failures_resets_the_streak(env):
    env.grant(max_items=4)
    for n in (1, 2, 3, 4):
        eligible(env, n, day=n)
    constitution_fails(env, {1, 3, 4})
    env.cycle()
    assert [(r["issue"], r["outcome"]) for r in item_rows()] == [
        (1, "failed"), (2, "pr-opened"), (3, "failed"), (4, "failed"),
    ]
    assert not store.load_mandate(ID).breaker_open


def test_failures_before_the_resume_do_not_count_toward_the_streak(env):
    env.grant()
    seams = driver.Seams(run=env.world.run, spawn=env.spawner, judge=env.judge, clock=env.clock)
    cycle = driver.Cycle(env.cfg(), seams, "c1")

    def fail(issue):
        env.clock.advance(seconds=1)
        cycle.record_item({"cycle_id": "c1", "issue": issue, "outcome": "failed", "failure_kind": "tests"})

    for issue in (1, 2, 3):
        fail(issue)
    assert len(events("breaker-open")) == 1
    env.clock.advance(seconds=1)
    store.save_mandate(rules.resumed(store.load_mandate(ID)))
    store.append_event(ID, "resumed", {}, by="alice", now=env.clock())
    fail(4)
    assert len(events("breaker-open")) == 1
    assert not store.load_mandate(ID).breaker_open


def test_the_cycle_does_not_retake_a_failed_issue(env):
    env.grant()
    eligible(env, 1)
    store.append_item_row(
        ID, {"cycle_id": "old", "issue": 1, "outcome": "failed", "failure_kind": "tests"}, now=env.clock(),
    )
    env.clock.advance(seconds=10)
    rc, out = env.main("run", "--dry-run", "--json")
    report = json.loads(out)
    assert report["would_take"] == []
    assert {c["issue"]: c["reason"] for c in report["candidates"]} == {1: "failed-before"}
    env.cycle()
    assert [r["cycle_id"] for r in item_rows()] == ["old"]
    assert env.spawner.of("developer") == []
    env.clock.advance(seconds=10)
    store.allow_retry(ID, 1, by="alice", now=env.clock())
    env.clock.advance(seconds=10)
    env.cycle()
    assert [r["outcome"] for r in item_rows()] == ["failed", "pr-opened"]


def test_a_declined_item_is_not_a_failed_issue_and_is_not_blocked_by_the_rule(env):
    env.grant()
    eligible(env, 1)
    store.append_item_row(ID, {"cycle_id": "old", "issue": 1, "outcome": "declined"}, now=env.clock())
    env.clock.advance(seconds=10)
    env.cycle()
    assert [r["outcome"] for r in item_rows()] == ["declined", "pr-opened"]


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
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(1, "", "traceback")
    env.cycle()
    text = digest_text()
    assert "**Breaker open**: failed:spawn-error on #1." in text
    assert "`agentctl mandate-resume`" in text
    assert "- #1: failed (spawn-error)" in text


def test_the_digest_lists_failed_issues_with_the_retry_command(env):
    env.grant()
    eligible(env, 1)
    env.world.name_status = "M\tscripts/foo.py\nM\tscripts/agentctl/mandate.py\n"
    env.cycle()
    text = digest_text()
    assert "- #1: failed (constitution)" in text
    assert "Not retried (failed before)" in text
    assert "- #1 (constitution) - run `agentctl mandate-retry --issue 1 --by <you>`" in text
    assert "Breaker open" not in text


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
    "allow_retry", "cmd_mandate_retry",
}


@pytest.mark.parametrize("name", sorted(CYCLE_SOURCES))
def test_no_cycle_source_references_a_user_authority_transition(name):
    source = CYCLE_SOURCES[name]
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (names & FORBIDDEN_NAMES), names & FORBIDDEN_NAMES
    for text in string_constants(source):
        assert not re.fullmatch(r"mandate-(grant|extend|resume|retry|stop)", text), text


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
    exec_start = [ln for ln in service.splitlines() if ln.startswith("ExecStart=")]
    interpreter, _, command = exec_start[0].removeprefix("ExecStart=").partition(" ")
    assert interpreter.startswith("/") and interpreter.endswith("/python3"), exec_start
    assert command == "%h/claude-agent-instructions/scripts/mandate-cycle.py run"
    assert "append:%h/.local/log/agent-debt-cycle.log" in service
    assert "OnCalendar=*-*-* 01:00:00" in timer and "Persistent=false" in timer
    assert systemctl_calls(box) == [
        "--user daemon-reload", "--user enable --now agent-debt-cycle.timer",
    ]


def test_the_unit_path_leads_with_python3_and_execstart_names_it_even_when_gh_comes_first(box, tmp_path):
    gh_dir, py_dir = tmp_path / "gh-first", tmp_path / "py-second"
    for d, name in ((gh_dir, "gh"), (py_dir, "python3")):
        d.mkdir()
        (d / name).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (d / name).chmod(0o755)
    result = install(box, extra_env={"PATH": f"{gh_dir}:{py_dir}:{box['fakes']}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    env_text = (box["home"] / ".config" / "agent-debt-cycle" / "env").read_text(encoding="utf-8")
    unit_path = env_text.splitlines()[0].removeprefix("PATH=").split(":")
    assert unit_path[:2] == [str(py_dir), str(gh_dir)]
    assert len(unit_path) == len(set(unit_path)), unit_path
    service = (box["units"] / "agent-debt-cycle.service").read_text(encoding="utf-8")
    assert f"ExecStart={py_dir}/python3 %h/claude-agent-instructions/scripts/mandate-cycle.py run\n" in service


def test_the_installer_refuses_to_pin_execstart_without_a_python3(box, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    for name in ("bash", "uname", "dirname", "mkdir", "chmod", "sed", "cat", "rm"):
        found = shutil.which(name)
        if found:
            (bare / name).symlink_to(found)
    result = install(box, "--write-env", extra_env={"PATH": f"{bare}:{box['fakes']}"})
    assert result.returncode == 1
    assert "python3 not found" in result.stderr
    assert not (box["home"] / ".config" / "agent-debt-cycle" / "env").exists()


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


# --- an item as a linked child of a mandate-approved parent session -----------------------

OPEN_VERBS = ["start", "classify", "plan", "submit-plan", "approve", "partition", "next-stage"]
DONE = "COMPLETED:\nFixed the issue.\n"
APPROVER = rules.mandate_approver(ID)
RATE_LIMIT = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "timestamp": "2026-10-27T00:00:00Z"}


def parent_of(number=1):
    return driver.parent_session_id(only_cycle_id(), number)


def item_dir_of(number=1):
    return store.cycle_dir(ID, only_cycle_id()) / "items" / str(number)


def engine_flags(env, verb):
    return [flags for called, flags in env.world.engine_calls if called == verb]


def flag(flags, name):
    return flags[flags.index(name) + 1]


def developer_says(env, *texts):
    queue = list(texts)

    def developer(argv, cwd):
        return driver.SpawnResult(0, queue.pop(0) if len(queue) > 1 else queue[0])

    env.spawner.developer = developer


def red_checks(env, red):
    """The item check is red `red` times per issue (a dict by issue, or one count for all), then green."""
    seen: "dict[int, int]" = {}

    def check(inputs):
        n = seen[inputs["issue"]] = seen.get(inputs["issue"], 0) + 1
        wanted = red.get(inputs["issue"], 0) if isinstance(red, dict) else red
        return driver.ItemVerdict(failures=[("tests", "t1")]) if n <= wanted else driver.ItemVerdict()

    env.check = check


def stopped(env, reason, outcome="declined", **extra):
    row = only_row(outcome=outcome, stop_reason=reason, pr_url=None, **extra)
    assert not store.load_mandate(ID).breaker_open
    assert events("breaker-open") == []
    assert env.world.calls_with("git", "push") == [] and env.world.calls_with("gh", "pr", "create") == []
    return row


def test_an_item_runs_as_a_linked_child_of_a_mandate_approved_parent_session(env):
    env.grant()
    eligible(env, 1)
    env.cycle()
    session, row = parent_of(), item_rows()[0]
    verbs = env.world.engine_calls[:len(OPEN_VERBS)]
    assert [verb for verb, _ in verbs] == OPEN_VERBS
    assert all(flags[:2] == ["--session", session] for _, flags in verbs)
    assert flag(engine_flags(env, "approve")[0], "--by") == APPROVER == f"mandate:{ID}"
    assert flag(engine_flags(env, "submit-plan")[0], "--plan") == str(item_dir_of() / "norm.toml")
    developer = env.spawner.of("developer")[0]["argv"]
    assert (flag(developer, "--session"), flag(developer, "--stage-index")) == (session, str(driver.STAGE_INDEX))
    assert "--session" not in env.spawner.of("code-reviewer")[0]["argv"]
    record = norm_handoff.read_child_session(config_root.agentctl_state_dir(), session, driver.STAGE_INDEX)
    assert row["parent_session"] == session and row["child_session"] == record["session"] == f"child-{session}"


def test_an_item_passing_on_the_first_round_opens_a_pr_and_leaves_the_parent_executing(env):
    env.grant()
    eligible(env, 1)
    env.cycle()
    row = only_row(outcome="pr-opened", rounds=0, asks=[])
    assert row["pr_url"] == env.world.pr_url
    assert env.world.engine_verbs() == OPEN_VERBS
    assert env.spawner.calls and len(env.spawner.of("developer")) == 1


def test_a_failed_check_item_is_rejected_by_the_mandate_and_the_child_resumed(env):
    env.grant()
    eligible(env, 1)
    red_checks(env, 1)
    env.cycle()
    row = only_row(outcome="pr-opened", rounds=1)
    assert row["asks"] == [{"kind": "check-failed", "class": "settled", "answer": "reject"}]
    [rejection] = engine_flags(env, "reject")
    assert (flag(rejection, "--session"), flag(rejection, "--by")) == (f"child-{parent_of()}", APPROVER)
    assert len(env.spawner.of("developer")) == 2
    assert all("--session" in c["argv"] for c in env.spawner.of("developer"))


def test_a_review_reject_is_rejected_by_the_mandate_and_reviewed_again(env):
    env.grant()
    eligible(env, 1)
    replies = ["COMPLETED:\nNot good.\nVERDICT: reject\n", "COMPLETED:\nFine now.\nVERDICT: accept\n"]
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(0, replies.pop(0))
    env.cycle()
    row = only_row(outcome="pr-opened", rounds=1, review="accept")
    assert row["asks"] == [{"kind": "review-reject", "class": "settled", "answer": "reject"}]
    [rejection] = engine_flags(env, "reject")
    assert (flag(rejection, "--session"), flag(rejection, "--by")) == (f"child-{parent_of()}", APPROVER)
    assert (len(env.spawner.of("developer")), len(env.spawner.of("code-reviewer"))) == (2, 2)


PERMISSION = "PERMISSION-REQUEST:\nRule: Bash(python3 -m pytest:*)\nResource: the suite\n"


def test_a_covered_permission_request_is_resumed_without_a_new_grant(env):
    env.grant()
    eligible(env, 1)
    developer_says(env, PERMISSION, DONE)
    env.cycle()
    row = only_row(outcome="pr-opened", rounds=1)
    assert row["asks"] == [{"kind": "permission-covered", "class": "settled", "answer": "resume"}]
    assert set(env.world.engine_verbs()) == {*OPEN_VERBS, "stage-grants"}
    assert len(env.spawner.of("developer")) == 2


def test_a_repeated_covered_denial_stops_the_item_as_exceeds_mandate(env):
    env.grant()
    eligible(env, 1)
    developer_says(env, PERMISSION)
    env.cycle()
    row = stopped(env, "materialization-defect", marker=driver.EXCEEDS_MARKER)
    assert [a["class"] for a in row["asks"]] == ["settled", "exceeds"]
    assert len(env.spawner.of("developer")) == 2


def test_an_uncovered_permission_request_stops_the_item_as_exceeds_mandate(env):
    env.grant()
    eligible(env, 1)
    developer_says(env, "PERMISSION-REQUEST:\nRule: Bash(rm -rf /tmp/x:*)\nResource: a directory\n")
    env.cycle()
    stopped(env, "permission-outside-mandate", marker=driver.EXCEEDS_MARKER)
    assert len(env.spawner.of("developer")) == 1


def test_a_plan_outside_the_norm_stop_carries_the_reason_codes(env):
    env.grant()
    eligible(env, 1)
    env.child["norm_reasons"] = ["scope", "cost"]
    developer_says(env, "REPLAN:\nThe plan needs more.\n")
    env.cycle()
    row = stopped(env, "plan-outside-mandate:scope,cost", marker=driver.EXCEEDS_MARKER)
    assert row["asks"][-1] == {"kind": "child-plan-outside-norm", "class": "exceeds", "answer": "plan-outside-mandate:scope,cost"}


def test_a_spend_fire_stops_the_item_as_overrun_without_the_breaker(env):
    env.grant()
    eligible(env, 1)
    env.child["fire_scale"] = "spend"
    developer_says(env, "INCOMPLETE:\nThe effort ran past its estimate.\n")
    env.cycle()
    stopped(env, "effort-spend", outcome="overrun", overrun_reasons=["effort-spend"])
    assert not rules.opens_breaker("overrun")


def test_a_question_for_the_user_stops_the_item_without_the_breaker_or_streak(env):
    env.grant(max_items=4)
    for n in (1, 2, 3, 4):
        eligible(env, n, day=n)
    developer_says(env, "ESCALATE:\nWhich of the two designs?\n")
    env.cycle()
    assert [(r["issue"], r["outcome"], r["stop_reason"]) for r in item_rows()] == [
        (n, "declined", "question-for-user") for n in (1, 2, 3, 4)
    ]
    assert not store.load_mandate(ID).breaker_open and events("breaker-open") == []


def test_the_rounds_limit_stops_the_item(env):
    env.grant()
    eligible(env, 1)
    limit = Thresholds().effort_replan_absolute()
    red_checks(env, 99)
    env.cycle()
    row = stopped(env, "rounds", marker=driver.EXCEEDS_MARKER, rounds=limit)
    assert [a["class"] for a in row["asks"]] == ["settled"] * limit + ["exceeds"]
    assert len(env.spawner.of("developer")) == limit + 1 and len(engine_flags(env, "reject")) == limit


def test_an_over_bound_diff_passes_only_under_a_parent_norm_approval(env):
    env.grant()
    env.world.numstat = numstat((BOUND + 1, 0, "scripts/foo.py"))
    refused = judge(env, basis=lambda: None)
    assert refused.over_bound and [check for check, _ in refused.failures] == ["bound"]
    assert judge(env, basis=lambda: "child_self").failures != []
    allowed = judge(env, basis=lambda: driver.CHILD_BASIS_OK)
    assert allowed.over_bound and allowed.failures == [] and allowed.green


def test_the_needs_plan_decline_is_removed(env):
    for name in ("decline_needs_plan", "measure_item", "needs_plan_reason"):
        assert not hasattr(driver, name) and not hasattr(driver.Cycle, name) and not hasattr(rules, name)
    assert "needs-plan" not in DRIVER_SOURCE and "needs_plan" not in DRIVER_SOURCE
    row = over_bound_item(env)
    assert row["marker"] == driver.EXCEEDS_MARKER != "needs-plan"
    assert row["asks"][0]["kind"] == "over-bound-inline" and row["asks"][0]["class"] == "settled"
    assert "inline" not in row and row["outcome"] == "declined"


def test_every_settled_answer_is_recorded_as_the_mandates_never_the_users(env):
    env.grant()
    eligible(env, 1)
    red_checks(env, 1)
    developer_says(env, PERMISSION, DONE)
    env.cycle()
    answers = events("mandate-answer")
    assert [(a["detail"]["kind"], a["detail"]["answer"]) for a in answers] == [
        ("permission-covered", "resume"), ("check-failed", "reject"),
    ]
    assert {a["by"] for a in answers} == {APPROVER}
    assert all(a["detail"]["issue"] == 1 for a in answers)
    assert [a["by"] for a in store.read_events(ID) if a["by"] != "cycle"] == [APPROVER, APPROVER]


def test_nested_spawn_spend_is_charged_to_the_item_and_the_mandate_windows(env):
    env.grant(item_usd_cap=5.0)
    eligible(env, 1)
    ledger = env.tmp / "costs.jsonl"

    def developer(argv, cwd):
        child = f"child-{flag(argv, '--session')}"
        nested = [
            {"plan_path": "/elsewhere/sub-plan.md", "spawner_session": child, "child_session_id": "nested-1", "cost_usd": 0.7},
            {"plan_path": "/elsewhere/deeper.md", "spawner_session": "nested-1", "child_session_id": "nested-2", "cost_usd": 0.4},
            {"plan_path": "/elsewhere/other.md", "spawner_session": "unrelated", "child_session_id": "x", "cost_usd": 9.0},
        ]
        with ledger.open("a", encoding="utf-8") as handle:
            for row in nested:
                handle.write(json.dumps({"ts": rules.format_ts(env.clock()), "transcript_path": None, **row}) + "\n")
        return driver.SpawnResult(0, DONE)

    env.spawner.developer = developer
    env.cycle()
    row = only_row(outcome="pr-opened")
    assert row["cost_usd"] == pytest.approx(1.1)
    charged = events("nested-spawn-cost")
    assert sorted(c["detail"]["cost_usd"] for c in charged) == [0.4, 0.7]
    spend = store.spend_now(ID, env.clock())
    assert (spend.day_usd, spend.week_usd) == (pytest.approx(1.1), pytest.approx(1.1))


def write_nested_row(env, argv, cost_usd, child_session_id):
    row = {
        "ts": rules.format_ts(env.clock()), "transcript_path": None, "plan_path": "/elsewhere/sub-plan.md",
        "spawner_session": f"child-{flag(argv, '--session')}", "child_session_id": child_session_id, "cost_usd": cost_usd,
    }
    with (env.tmp / "costs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_nested_spend_of_the_first_round_is_charged_before_the_check_and_is_no_tampering(env):
    env.grant(item_usd_cap=5.0)
    eligible(env, 1)
    red_checks(env, 1)
    calls = []

    def developer(argv, cwd):
        calls.append(argv)
        if len(calls) == 1:
            write_nested_row(env, argv, 0.7, "nested-1")
        return driver.SpawnResult(0, DONE)

    env.spawner.developer = developer
    env.cycle()
    row = only_row(outcome="pr-opened", rounds=1)
    assert row["cost_usd"] == pytest.approx(0.7)
    names = [e["event"] for e in events()]
    assert names.index("nested-spawn-cost") < names.index("mandate-answer")
    assert [e["detail"]["cost_usd"] for e in events("nested-spawn-cost")] == [0.7]
    assert events("breaker-open") == [] and not store.load_mandate(ID).breaker_open


def test_nested_spend_over_the_item_cap_ends_the_item_in_the_round_that_spent_it(env):
    env.grant(item_usd_cap=1.0)
    eligible(env, 1)
    red_checks(env, 1)
    env.spawner.developer = lambda argv, cwd: (write_nested_row(env, argv, 1.4, "nested-1"), driver.SpawnResult(0, DONE))[1]
    env.cycle()
    row = only_row(outcome="overrun")
    assert row["cost_usd"] == pytest.approx(1.4) and row["rounds"] == 0
    assert len(env.spawner.of("developer")) == 1 and env.world.calls_with("git", "push") == []


def limit_ledger_row(env, plan_path, session, child):
    row = {"ts": rules.format_ts(env.clock()), "plan_path": plan_path, "cost_usd": 0.1, "child_session_id": child}
    if session:
        row["spawner_session"] = session
    with (env.tmp / "costs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


@pytest.mark.parametrize("kind", ["developer", "resumed", "reviewer"])
def test_an_account_limit_on_a_spawn_stops_the_cycle_without_the_breaker(env, kind):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    env.world.issues.append(raw_issue(3, labels=("backlog",)))
    limited = driver.SpawnResult(1, "", "")
    spawned = {"n": 0}

    def hit(argv, cwd, child, session):
        limit_ledger_row(env, env.plan_of(argv), session, child)
        env.transcripts = lambda workdir, since: {child: [{"type": "assistant", "message": "ok"}, RATE_LIMIT]}
        return limited

    def developer(argv, cwd):
        spawned["n"] += 1
        if kind == "developer" or (kind == "resumed" and spawned["n"] == 2):
            return hit(argv, cwd, "dev-run", flag(argv, "--session"))
        return driver.SpawnResult(0, DONE)

    env.spawner.developer = developer
    env.spawner.reviewer = lambda argv, cwd: hit(argv, cwd, "review-run", None)
    if kind == "resumed":
        red_checks(env, 1)
    assert env.cycle() == 0
    row = only_row(outcome="limit", reasons=["account-limit"], stop_reason="account-limit", pr_url=None)
    assert [a["kind"] for a in row["asks"]][-1] == "account-limit"
    assert not store.load_mandate(ID).breaker_open
    assert [r["issue"] for r in item_rows()] == [1]
    assert list(store.read_cycle_records(ID).values())[0]["status"] == driver.STATUS_ACCOUNT_LIMIT
    assert env.judge.prompts == []
    assert [e["detail"]["reasons"] for e in events("limit")] == [["account-limit"]]
    last = env.spawner.calls[-1]
    assert last["kind"] == ("code-reviewer" if kind == "reviewer" else "developer")
    assert env.transcripts_seen[-1][0] == flag(last["argv"], "--workdir")
    assert not rules.opens_breaker("limit")


@pytest.mark.parametrize("records", [
    [],
    [{"type": "assistant", "message": "done"}],
    [{"type": "assistant", "isApiErrorMessage": True, "error": "server_error", "apiErrorStatus": 500}],
    [RATE_LIMIT, {"type": "assistant", "message": "later work"}],
])
def test_a_malformed_spawn_without_the_rate_limit_record_still_opens_the_breaker(env, records):
    env.grant()
    eligible(env, 1)
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "no marker here")
    env.transcripts = lambda workdir, since: {"dev-run": records}
    env.cycle()
    row = only_row(outcome="failed", failure_kind="spawn-error")
    assert row["asks"] == []
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "failed:spawn-error on #1"


@pytest.mark.parametrize("keep, name_status", [(True, "M\tscripts/foo.py\n"), (False, "")])
@pytest.mark.parametrize("says", ["ESCALATE:\nA question.\n", "INCOMPLETE:\nspend\n", "garbage"])
def test_every_stopped_item_with_a_diff_keeps_its_patch(env, says, keep, name_status):
    env.grant()
    eligible(env, 1)
    env.world.name_status = name_status
    env.child["fire_scale"] = "spend" if says.startswith("INCOMPLETE") else None
    developer_says(env, says)
    env.cycle()
    [row] = item_rows()
    assert row["outcome"] != "pr-opened"
    if not keep:
        assert row["salvage_patch"] is None and row["diff_files"] == []
        assert not (item_dir_of() / "salvage.patch").exists()
        return
    kept = item_dir_of() / "salvage.patch"
    assert row["salvage_patch"] == str(kept) and row["diff_files"] == ["scripts/foo.py"]
    assert kept.read_text(encoding="utf-8") == env.world.patch
    assert env.world.calls_with("git", "push") == []


def test_a_salvage_error_is_an_event_and_keeps_the_outcome(env):
    env.grant()
    eligible(env, 1)
    env.world.failing = ["--binary"]
    developer_says(env, "ESCALATE:\nA question.\n")
    env.cycle()
    row = only_row(outcome="declined", stop_reason="question-for-user")
    assert row["salvage_patch"] is None and row["diff_files"] == []
    [event] = events("salvage-failed")
    assert event["detail"]["issue"] == 1 and "boom" in event["detail"]["error"]
    assert not (item_dir_of() / "salvage.patch").exists()


def test_the_digest_names_rounds_answers_stops_and_kept_work(env):
    env.grant(max_items=3)
    for n in (1, 2, 3):
        eligible(env, n, day=n)
    red_checks(env, {1: 1})
    env.child["fire_scale"] = None

    def developer(argv, cwd):
        issue = int(flag(argv, "--session").rsplit("-", 1)[1])
        env.child["fire_scale"] = "spend" if issue == 3 else None
        return driver.SpawnResult(0, DONE if issue == 1 else "ESCALATE:\nA question.\n" if issue == 2 else "INCOMPLETE:\nspend\n")

    env.spawner.developer = developer
    env.cycle()
    text = digest_text()
    rows = {r["issue"]: r for r in item_rows()}
    assert digest_section(text, "Rounds and answers") == [
        "- #1: 1 round(s); answered by the mandate: check-failed->reject",
        "- #2: 0 round(s); answered by the mandate: none",
        "- #3: 0 round(s); answered by the mandate: none",
    ]
    assert digest_section(text, "Stops") == [
        "- #2: question-for-user (declined, not retried)",
        "- #3: effort-spend (overrun, retried by the ordinary rules)",
    ]
    assert digest_section(text, "Kept work") == [
        f"- #{n}: {rows[n]['salvage_patch']} (1 file(s))" for n in (2, 3)
    ]
    assert digest_section(text, "Items")[1] == "- #2: declined (exceeds-mandate) -- $0.00"


def test_a_parent_session_open_failure_is_a_spawn_error_and_opens_the_breaker(env):
    env.grant()
    eligible(env, 1)
    env.world.engine_refusals = {"submit-plan": "the norm is invalid"}
    env.cycle()
    row = only_row(outcome="failed", failure_kind="spawn-error")
    assert "engine submit-plan: the norm is invalid" in row["detail"]
    assert env.spawner.calls == []
    mandate = store.load_mandate(ID)
    assert mandate.breaker_open and mandate.breaker_reason == "failed:spawn-error on #1"
    assert env.world.engine_verbs() == OPEN_VERBS[:4]


def test_an_approve_refusal_after_the_gate_closed_ends_as_a_limit_without_the_breaker(env):
    env.grant()
    eligible(env, 1)
    env.world.engine_refusals = {"approve": "the mandate is paused"}
    env.world.on_engine = lambda verb, flags: (
        store.save_mandate(rules.stopped(store.load_mandate(ID))) if verb == "approve" else None
    )
    env.cycle()
    row = only_row(outcome="limit", reasons=["paused"], pr_url=None)
    assert row["gates"]["constitution"] == driver.NOT_RUN
    assert [(e["detail"]["phase"], e["detail"]["reasons"]) for e in events("limit")] == [("approve", ["paused"])]
    assert not store.load_mandate(ID).breaker_open
    assert env.spawner.calls == [] and env.judge.prompts == []


def test_an_approve_refusal_with_the_gate_still_open_is_a_spawn_error_and_opens_the_breaker(env):
    env.grant()
    eligible(env, 1)
    env.world.engine_refusals = {"approve": "the plan is outside the norm"}
    env.cycle()
    row = only_row(outcome="failed", failure_kind="spawn-error")
    assert "engine approve: the plan is outside the norm" in row["detail"]
    assert events("limit") == [] and env.spawner.calls == []
    assert store.load_mandate(ID).breaker_open


def _wall_clock(env):
    env.child["fire_scale"] = "wall-clock"
    return "ESCALATE:\nTook long.\n"


def _norm_mismatch(env):
    env.child["norm_mismatch"] = True
    return "REPLAN:\nThe stage's norm cannot be met as written.\n"


def _backstop(env):
    env.child["since_user_backstop"] = True
    return "ESCALATE:\nThe agent's budget of decisions is spent.\n"


def _wall_clock_with_backstop(env):
    env.child["since_user_backstop"] = True
    return _wall_clock(env)


def _spend_with_norm_mismatch(env):
    env.child["norm_mismatch"] = True
    env.child["fire_scale"] = "spend"
    return "REPLAN:\nOver budget and off the norm.\n"


def _loop(env):
    return "LOOP_DETECTED:\nThe same step again.\n"


def _incomplete(env):
    env.child["node"] = "EXECUTING"
    return DONE


@pytest.mark.parametrize("setup, outcome, reason", [
    (_wall_clock, "overrun", "effort-wall-clock"),
    (_wall_clock_with_backstop, "overrun", "effort-wall-clock"),
    (_spend_with_norm_mismatch, "overrun", "effort-spend"),
    (_norm_mismatch, "declined", "norm-mismatch"),
    (_backstop, "declined", "backstop"),
    (_loop, "declined", "loop-detected"),
    (_incomplete, "declined", "incomplete"),
])
def test_every_exceeds_kind_stops_the_item_without_the_breaker(env, setup, outcome, reason):
    env.grant(max_items=4)
    for n in (1, 2, 3, 4):
        eligible(env, n, day=n)
    developer_says(env, setup(env))
    env.cycle()
    assert [(r["issue"], r["outcome"], r["stop_reason"]) for r in item_rows()] == [(n, outcome, reason) for n in (1, 2, 3, 4)]
    assert not store.load_mandate(ID).breaker_open and events("breaker-open") == []
    assert env.world.calls_with("git", "push") == []
    if outcome == "declined":
        assert {r["marker"] for r in item_rows()} == {driver.EXCEEDS_MARKER}


def child_difficulty(*, escalated, target="parent"):
    return state_module.Difficulty(
        norm_targets=[{"target": target, "kind": "stage", "baseline": "d", "source": "critique"}],
        escalated_to={"parent_session": "p", "stage_index": 1, "norm_digest": "d"} if escalated else {},
    )


def save_child(**fields):
    state = state_module.SessionState(session_id="child-x", task_id="task-x", **fields)
    FileStateStore(config_root.agentctl_state_dir()).save(state)


@pytest.mark.parametrize("difficulty, stacked, expected", [
    (child_difficulty(escalated=True), False, True),
    (child_difficulty(escalated=True, target="file"), False, False),
    (child_difficulty(escalated=False), False, False),
    (None, True, True),
])
def test_a_childs_norm_mismatch_is_read_from_its_difficulty_records(env, difficulty, stacked, expected):
    live = {"difficulty_stack": [child_difficulty(escalated=True)], "difficulty": state_module.Difficulty()} if stacked else {"difficulty": difficulty}
    save_child(**live)
    assert driver.real_child_state("child-x")["norm_mismatch"] is expected


@pytest.mark.parametrize("axis, backstop", [
    ("replans_since_user_decision", Thresholds().effort_replan_backstop()),
    ("review_rounds_since_user_decision", Thresholds().review_round_backstop()),
])
def test_a_childs_since_user_backstop_is_read_from_the_task_accumulator(env, axis, backstop):
    save_child()
    assert driver.real_child_state("child-x")["since_user_backstop"] is False
    task_accumulator.add("task-x", axis, backstop - 1)
    assert driver.real_child_state("child-x")["since_user_backstop"] is False
    task_accumulator.add("task-x", axis, 1)
    assert driver.real_child_state("child-x")["since_user_backstop"] is True


@pytest.mark.parametrize("returncode, stdout, stderr", [(1, "", "boom"), (0, "no marker, no verdict", "")],
                         ids=["exit-code", "no-marker"])
def test_a_reviewer_that_crashed_fails_the_item_and_opens_the_breaker(env, returncode, stdout, stderr):
    env.grant()
    eligible(env, 1)
    env.spawner.reviewer = lambda argv, cwd: driver.SpawnResult(returncode, stdout, stderr)
    env.cycle()
    row = only_row(outcome="failed", failure_kind="spawn-error")
    assert row["review"] == "not-run"
    assert env.world.calls_with("git", "push") == []
    assert store.load_mandate(ID).breaker_open


# --- the one check an item is judged by ----------------------------------------------------

def _constitution(world):
    world.name_status = "M\tscripts/foo.py\nM\tscripts/agentctl/mandate.py\n"


def _org_neutral(world):
    world.neutral = lambda text: 1


def _suite(world):
    world.suites["item.xml"] = (["t1"], ["t2"])
    world.suites["rerun.xml"] = (["t1"], ["t2"])


def _gate(world):
    world.gate_rc = {driver.GATE_SCRIPTS[0]: 1}


def _bound(world):
    world.numstat = numstat((BOUND + 1, 0, "scripts/foo.py"))


@pytest.mark.parametrize("change, failed", [
    (lambda world: None, []),
    (_constitution, ["constitution"]),
    (_org_neutral, ["org-neutral"]),
    (_suite, ["tests"]),
    (_gate, ["tests"]),
    (_bound, ["bound"]),
])
def test_check_item_judges_constitution_org_neutral_suite_gates_and_bound(env, change, failed):
    env.grant()
    change(env.world)
    verdict = judge(env)
    assert [check for check, _ in verdict.failures] == failed
    assert verdict.green is (not failed)
    if change is _gate:
        assert verdict.regressed_gates == [driver.GATE_SCRIPTS[0]]
    if change is _suite:
        assert len(verdict.failing) == 1 and verdict.failing[0].endswith("t2")
    assert verdict.ran_suite is (failed != ["constitution"])


def test_check_item_is_red_on_an_unchanged_tree(env):
    env.grant()
    env.world.name_status = ""
    verdict = judge(env)
    assert verdict.failures == [("diff", "no change against the base")]
    assert not verdict.green and not verdict.ran_suite


def test_the_driver_judges_with_its_own_check_never_the_worktree_copy(env):
    env.grant()
    eligible(env, 1)
    _suite(env.world)

    def developer(argv, cwd):
        poisoned = Path(cwd) / "scripts" / "mandate-cycle.py"
        poisoned.parent.mkdir(parents=True, exist_ok=True)
        poisoned.write_text("raise SystemExit(0)\n", encoding="utf-8")
        return driver.SpawnResult(0, DONE)

    env.spawner.developer = developer
    env.cycle()
    row = stopped(env, "rounds", marker=driver.EXCEEDS_MARKER)
    assert row["tests"] == "fail"
    assert env.world.calls_with("bash") and not [c for c in env.world.calls if "mandate-cycle.py" in " ".join(c)]
    assert not [c for c in env.world.calls if "check-item" in c]


# --- the real transcript seam ------------------------------------------------------------------

def transcript_root(env, monkeypatch):
    root = env.tmp / "projects"
    root.mkdir()
    monkeypatch.setattr(config_root, "projects_roots", lambda: [root])
    env.transcripts = driver.real_transcripts
    return root


def write_transcript(root, workdir, session, records, age_s=0.0):
    directory = root / driver.workdir_dir_name(str(workdir))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


PLAIN = {"type": "assistant", "message": "worked", "timestamp": "2026-10-27T00:00:05Z"}


@pytest.mark.parametrize("fresh_limited", [True, False])
def test_only_a_transcript_new_since_the_spawn_is_read(env, monkeypatch, fresh_limited):
    env.grant()
    eligible(env, 1)
    root = transcript_root(env, monkeypatch)

    def developer(argv, cwd):
        write_transcript(root, cwd, "old-run", [RATE_LIMIT if not fresh_limited else PLAIN], age_s=7200)
        write_transcript(root, cwd, "new-run", [RATE_LIMIT if fresh_limited else PLAIN])
        return driver.SpawnResult(0, "no marker here")

    env.spawner.developer = developer
    env.cycle()
    if fresh_limited:
        only_row(outcome="limit", reasons=["account-limit"])
        assert not store.load_mandate(ID).breaker_open
    else:
        only_row(outcome="failed", failure_kind="spawn-error")
        assert store.load_mandate(ID).breaker_open


@pytest.mark.parametrize("parent_limited", [True, False])
def test_the_account_limit_transcript_is_the_parent_spawns_not_a_nested_one(env, monkeypatch, parent_limited):
    env.grant()
    eligible(env, 1)
    root = transcript_root(env, monkeypatch)

    def developer(argv, cwd):
        session = flag(argv, "--session")
        child = f"child-{session}"
        mine = {**(RATE_LIMIT if parent_limited else PLAIN), "timestamp": "2026-10-27T00:00:01Z"}
        nested = {**(PLAIN if parent_limited else RATE_LIMIT), "timestamp": "2026-10-27T00:00:09Z"}
        write_transcript(root, cwd, "dev-run", [mine])
        write_transcript(root, cwd, "nested-run", [nested])
        limit_ledger_row(env, env.plan_of(argv), session, "dev-run")
        limit_ledger_row(env, "/elsewhere/sub-plan.md", child, "nested-run")
        return driver.SpawnResult(1, "", "")

    env.spawner.developer = developer
    env.cycle()
    if parent_limited:
        only_row(outcome="limit", reasons=["account-limit"])
        assert not store.load_mandate(ID).breaker_open
    else:
        only_row(outcome="failed", failure_kind="spawn-error")
        assert store.load_mandate(ID).breaker_open


def test_the_transcript_dir_encoding_matches_spawn_specialist():
    path = Path(driver.__file__).resolve().parents[1] / "spawn-specialist.py"
    spec = importlib.util.spec_from_file_location("spawn_specialist_for_encoding", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for workdir in (
        "/tmp/a/b", "/home/u/work tree/item_1.x", "/tmp/~weird/a-b", "/tmp/ünï/čode", "C:\\work\\a.b", "", "plain",
    ):
        assert driver.workdir_dir_name(workdir) == module._project_dir_name(workdir)


def test_a_missing_transcript_dir_is_an_empty_listing_and_keeps_spawn_error(env, monkeypatch):
    env.grant()
    eligible(env, 1)
    root = transcript_root(env, monkeypatch)
    assert driver.real_transcripts(str(env.tmp / "nowhere"), 0.0) == {}
    monkeypatch.setattr(config_root, "projects_roots", lambda: [root, env.tmp / "no-such-root"])
    assert driver.real_transcripts(str(env.tmp / "nowhere"), 0.0) == {}
    env.spawner.developer = lambda argv, cwd: driver.SpawnResult(0, "no marker here")
    env.cycle()
    only_row(outcome="failed", failure_kind="spawn-error")
    assert store.load_mandate(ID).breaker_open


def test_a_reviewer_account_limit_on_the_real_seam_ends_the_item_as_limit(env, monkeypatch):
    env.grant(max_items=2)
    eligible(env, 1, day=1)
    eligible(env, 2, day=2)
    root = transcript_root(env, monkeypatch)

    def reviewer(argv, cwd):
        write_transcript(root, cwd, "review-run", [PLAIN, RATE_LIMIT])
        limit_ledger_row(env, env.plan_of(argv), None, "review-run")
        return driver.SpawnResult(1, "", "")

    env.spawner.reviewer = reviewer
    assert env.cycle() == 0
    row = only_row(outcome="limit", reasons=["account-limit"], stop_reason="account-limit", pr_url=None)
    assert row["review"] == driver.NOT_RUN
    assert env.world.calls_with("git", "push") == [] and env.judge.prompts == []
    assert not store.load_mandate(ID).breaker_open
    assert list(store.read_cycle_records(ID).values())[0]["status"] == driver.STATUS_ACCOUNT_LIMIT


# --- the commands the norm renders, run on a real worktree -------------------------------------

CHECK_RUNNER = '''\
"""Runs `mandate-cycle.py <argv>` in this process with the suite, org-neutral and gate runs
stubbed: argv[1] is the junit file the stubbed suite writes."""
import sys
from pathlib import Path

from agentctl.dispatch import RunResult
from mandate_cycle import driver

real = driver.real_run
suite = Path(sys.argv[1]).read_text(encoding="utf-8")


def run(argv, cwd=None, timeout=None, stdin=None):
    a = list(argv)
    if a[0] == "bash" and a[1].endswith("cap-run.sh"):
        target = next(x for x in a if x.startswith("--junitxml="))[len("--junitxml="):]
        Path(target).write_text(suite, encoding="utf-8")
        return RunResult(0)
    if a[0] == sys.executable and (a[1].endswith("check-org-neutral.py") or a[1] in driver.GATE_SCRIPTS):
        return RunResult(0)
    return real(argv, cwd=cwd, timeout=timeout, stdin=stdin)


driver.real_run = run
sys.exit(driver.main(sys.argv[2:]))
'''


def real_check_tree(env, *, edited: bool):
    """A trunk checkout, an item worktree on `origin/main` (with one uncommitted new file when
    `edited`) and the item directory holding the inputs and the cycle's baselines."""
    repo, worktree = env.tmp / "trunk", env.tmp / "item"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "worktree", "add", "-q", "-b", "item", str(worktree), "origin/main")
    if edited:
        (worktree / "foo.py").write_text("y = 1\nz = 2\n", encoding="utf-8")

    item_dir = store.cycle_dir(ID, "cyc") / "items" / "1"
    item_dir.mkdir(parents=True)
    inputs = {
        "issue": 1, "title": "t", "body": "b", "cycle_id": "cyc", "base_sha": base, "bound": BOUND,
        "worktree": str(worktree), "repo": str(repo), "item_dir": str(item_dir), "mandate_id": ID,
    }
    (item_dir / driver.INPUTS_FILE).write_text(json.dumps(inputs), encoding="utf-8")
    cycle_dir = store.cycle_dir(ID, "cyc")
    (cycle_dir / driver.BASELINE_XML).write_text(junit(["t1", "t2"]), encoding="utf-8")
    (cycle_dir / driver.BASELINE_GATES).write_text(json.dumps({s: 0 for s in driver.GATE_SCRIPTS}), encoding="utf-8")
    runner = env.tmp / "check_runner.py"
    runner.write_text(CHECK_RUNNER, encoding="utf-8")
    (env.tmp / "suite.xml").write_text(junit(["t1", "t2", "t3"]), encoding="utf-8")
    return repo, worktree, base, item_dir


def run_check_item(env, worktree, argv):
    """`mandate-cycle.py <argv>` as its own process started in the worktree."""
    done = subprocess.run(
        [sys.executable, str(env.tmp / "check_runner.py"), str(env.tmp / "suite.xml"), *argv],
        capture_output=True, text=True, cwd=str(worktree),
        env={**os.environ, "PYTHONPATH": str(Path(driver.__file__).parent.parent)},
    )
    return done.returncode, done.stdout


def test_the_rendered_check_item_commands_judge_a_real_worktree(env):
    env.grant()
    repo, worktree, base, item_dir = real_check_tree(env, edited=True)
    norm = rules.item_norm_toml(
        mandate=store.load_mandate(ID), issue=1, title="t", body="b", cycle_id="cyc", base_sha=base,
        bound=BOUND, worktree=str(worktree), repo=str(repo), item_dir=str(item_dir),
    )
    stage = tomllib.loads(norm)["stage"][0]
    verify, control = stage["verify_command"], stage["negative_control"]
    assert control == f"{verify} --head {base}" and stage["expected_negative_exit"] == 1
    assert tomllib.loads(norm)["final_check"][0]["command"] == verify

    def command(text):
        words = shlex.split(text)
        assert words[:2] == ["python3", str(repo / "scripts" / "mandate-cycle.py")]
        return run_check_item(env, worktree, words[2:])

    def repository_state():
        return git(worktree, "rev-parse", "HEAD"), git(worktree, "diff", "--cached")

    before = repository_state()
    assert command(verify) == (0, "check-item: green\n")
    assert repository_state() == before
    rc, out = command(control)
    assert rc == 1 and out.startswith("check-item: red") and "no change against the base" in out
    assert repository_state() == before


def test_check_item_is_red_on_an_unchanged_real_worktree(env):
    env.grant()
    _, worktree, base, item_dir = real_check_tree(env, edited=False)
    rc, out = run_check_item(env, worktree, ["check-item", "--item-dir", str(item_dir)])
    assert rc == 1 and out.startswith("check-item: red") and "no change against the base" in out, out
    assert git(worktree, "rev-parse", "HEAD") == base and git(worktree, "diff", "--cached") == ""
