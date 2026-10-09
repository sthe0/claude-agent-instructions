"""The `mandate-*` verbs and the mandate store, driven through `cli.main`.

Every test points `$AGENTCTL_MANDATE_DIR` and the spawn-cost log at a tmp dir and replaces the
process runner, so nothing here touches the real agent home, systemd or a live process.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agentctl import cli, cost, mandate as rules, mandate_store as store
from agentctl.directive import DIRECTIVE_ESCALATE_TO_USER
from agentctl.dispatch import RunResult
from lib import widening_targets

ID = "core-debt"


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        return RunResult(0)

    @property
    def kill_calls(self):
        return [c for c in self.calls if c[0] == sys.executable]

    @property
    def systemctl_calls(self):
        return [c for c in self.calls if c[0] == "systemctl"]


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTCTL_MANDATE_DIR", str(tmp_path / "mandates"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    cost_log = tmp_path / "costs.jsonl"
    monkeypatch.setattr(cost, "COST_LOG", cost_log)
    runner = FakeRunner()
    monkeypatch.setattr(store, "subprocess_runner", runner)
    state = tmp_path / "state"

    def run(*argv):
        rc = cli.main(["--state-root", str(state), *argv])
        return rc, json.loads(capsys.readouterr().out)

    class Env:
        pass

    e = Env()
    e.run, e.runner, e.cost_log, e.tmp = run, runner, cost_log, tmp_path
    return e


def grant(env, *extra, by="alice"):
    return env.run("mandate-grant", "--by", by, *extra)


def write_cost(env, usd, hours_ago=1, plan=None):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = plan or str(store.mandate_dir(ID) / "plans" / "p.toml")
    with env.cost_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": ts, "cost_usd": usd, "plan_path": plan}) + "\n")


# --- grant ----------------------------------------------------------------------------

def test_grant_creates_the_record_and_logs_the_event(env):
    rc, out = grant(env)
    assert rc == 0 and out["ok"] is True
    saved = store.load_mandate(ID)
    assert saved.granted_by == "alice" and not saved.paused and not saved.breaker_open
    assert saved.limits == rules.MandateLimits()
    assert [e["event"] for e in store.read_events(ID)] == ["granted"]
    assert out["data"]["may_start"] is True


def test_grant_applies_limit_overrides(env):
    rc, out = grant(env, "--daily-usd", "5", "--weekly-usd", "20", "--max-items", "2", "--days", "3",
                    "--item-minutes-cap", "60", "--cycle-minutes-cap", "180", "--veto-window-hours", "12",
                    "--item-usd-cap", "4")
    assert rc == 0
    saved = store.load_mandate(ID)
    assert (saved.daily_usd, saved.weekly_usd, saved.max_items) == (5.0, 20.0, 2)
    assert (saved.item_minutes_cap, saved.cycle_minutes_cap) == (60.0, 180.0)
    assert (saved.veto_window_hours, saved.item_usd_cap) == (12.0, 4.0)
    span = rules.parse_ts(saved.expires_at) - rules.parse_ts(saved.granted_at)
    assert span == timedelta(days=3)


@pytest.mark.parametrize("by", ["agent", "AGENT", " Agent ", "", "   "])
def test_grant_refuses_the_coordinator_and_writes_nothing(env, by):
    rc, out = grant(env, by=by)
    assert rc == 1 and out["ok"] is False
    assert out["marker"] == DIRECTIVE_ESCALATE_TO_USER
    assert store.load_mandate(ID) is None
    assert store.read_events(ID) == []


def test_grant_refuses_an_id_that_already_exists(env):
    grant(env)
    rc, out = grant(env, "--daily-usd", "1")
    assert rc == 1 and "already exists" in out["detail"]
    assert store.load_mandate(ID).daily_usd == rules.MandateLimits().daily_usd


def test_grant_with_a_non_positive_limit_fails_and_writes_nothing(env):
    rc, out = grant(env, "--daily-usd", "0")
    assert rc == 1 and out["ok"] is False
    assert store.load_mandate(ID) is None


def test_a_second_mandate_id_is_independent(env):
    grant(env)
    rc, _ = env.run("mandate-grant", "--id", "other", "--by", "alice")
    assert rc == 0
    assert store.list_mandate_ids() == ["core-debt", "other"]


# --- extend / resume ------------------------------------------------------------------

def test_extend_pushes_the_expiry_and_logs_it(env):
    grant(env)
    before = rules.parse_ts(store.load_mandate(ID).expires_at)
    rc, out = env.run("mandate-extend", "--days", "7", "--by", "alice")
    assert rc == 0
    assert rules.parse_ts(store.load_mandate(ID).expires_at) - before == timedelta(days=7)
    assert store.read_events(ID)[-1]["event"] == "extended"


def test_extend_refuses_the_coordinator_and_an_unknown_mandate(env):
    grant(env)
    before = store.load_mandate(ID).expires_at
    rc, _ = env.run("mandate-extend", "--days", "7", "--by", "agent")
    assert rc == 1 and store.load_mandate(ID).expires_at == before
    rc, out = env.run("mandate-extend", "--id", "ghost", "--days", "7", "--by", "alice")
    assert rc == 1 and "no mandate" in out["detail"]


def test_extend_with_a_non_positive_term_fails(env):
    grant(env)
    rc, _ = env.run("mandate-extend", "--days", "-1", "--by", "alice")
    assert rc == 1


def test_resume_unpauses_closes_the_breaker_and_reenables_the_timer(env):
    grant(env)
    broken = rules.breaker_opened(rules.stopped(store.load_mandate(ID)), "tests failed")
    store.save_mandate(broken)
    rc, out = env.run("mandate-resume", "--by", "alice")
    assert rc == 0
    saved = store.load_mandate(ID)
    assert (saved.paused, saved.breaker_open, saved.breaker_reason) == (False, False, "")
    assert env.runner.systemctl_calls == [["systemctl", "--user", "enable", "--now", store.TIMER_UNIT]]
    assert out["data"]["may_start"] is True


def test_resume_refuses_the_coordinator_and_leaves_the_breaker_open(env):
    grant(env)
    store.save_mandate(rules.breaker_opened(store.load_mandate(ID), "x"))
    rc, _ = env.run("mandate-resume", "--by", "agent")
    assert rc == 1 and store.load_mandate(ID).breaker_open
    assert env.runner.calls == []


# --- stop -----------------------------------------------------------------------------

def test_stop_pauses_and_disables_the_timer_without_killing_an_idle_mandate(env):
    grant(env)
    rc, out = env.run("mandate-stop")
    assert rc == 0
    assert store.load_mandate(ID).paused
    assert env.runner.systemctl_calls == [["systemctl", "--user", "disable", "--now", store.TIMER_UNIT]]
    assert env.runner.kill_calls == []
    assert out["data"]["stop"]["cycle_running"] is False and out["data"]["stop"]["killed"] is None
    assert "no running cycle" in out["detail"]
    assert store.read_events(ID)[-1]["event"] == "stopped"


def test_stop_with_the_lock_file_missing_kills_nothing_and_says_so(env):
    grant(env)
    assert not store.path_of(ID, store.LOCK_FILE).exists()
    rc, out = env.run("mandate-stop")
    assert rc == 0 and env.runner.kill_calls == [] and "no running cycle" in out["detail"]


def test_stop_survives_a_corrupt_record_and_still_disables_the_timer_and_kills_the_cycle(env):
    grant(env)
    store.path_of(ID, store.MANDATE_FILE).write_text("{not json")
    with store.cycle_lock(ID):
        rc, out = env.run("mandate-stop")
    assert rc == 0
    assert env.runner.systemctl_calls == [["systemctl", "--user", "disable", "--now", store.TIMER_UNIT]]
    assert env.runner.kill_calls == [[sys.executable, str(store.KILL_TREE), str(os.getpid())]]
    stop = out["data"]["stop"]
    assert stop["paused"] is False and "pause_error" in stop
    assert out["data"]["record_unreadable"] is True
    assert store.read_events(ID)[-1]["event"] == "stopped"


def test_stop_reports_an_unknown_holder_pid_instead_of_killing(env, monkeypatch):
    grant(env)
    monkeypatch.setattr(store, "PID_READ_DELAY_S", 0)
    fd = os.open(store.path_of(ID, store.LOCK_FILE), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store.probe_cycle(ID) == (True, None)
        rc, out = env.run("mandate-stop")
    finally:
        os.close(fd)
    assert rc == 0 and env.runner.kill_calls == []
    stop = out["data"]["stop"]
    assert stop["cycle_running"] is True and stop["killed"] is None
    assert stop["pid"] is None and stop["reason"] == "holder pid unknown"


def test_stop_kills_the_process_tree_of_a_running_cycle(env):
    grant(env)
    with store.cycle_lock(ID):
        rc, out = env.run("mandate-stop")
    assert rc == 0
    assert env.runner.kill_calls == [[sys.executable, str(store.KILL_TREE), str(os.getpid())]]
    assert out["data"]["stop"]["killed"]["pid"] == os.getpid()


def test_stop_does_not_kill_for_a_stale_pid_left_in_an_unheld_lock_file(env):
    grant(env)
    lock = store.path_of(ID, store.LOCK_FILE)
    lock.write_text("424242")
    rc, _ = env.run("mandate-stop")
    assert rc == 0 and env.runner.kill_calls == []


def test_stop_is_open_to_the_coordinator_and_needs_no_by(env):
    grant(env)
    rc, _ = env.run("mandate-stop", "--id", ID)
    assert rc == 0


def test_stop_of_an_unknown_mandate_fails_without_touching_systemd(env):
    rc, out = env.run("mandate-stop", "--id", "ghost")
    assert rc == 1 and env.runner.calls == []


def test_a_stopped_mandate_refuses_to_start_a_cycle(env):
    grant(env)
    env.run("mandate-stop")
    rc, out = env.run("mandate-status")
    assert out["data"]["may_start"] is False and "paused" in out["data"]["refusals"]


# --- status ---------------------------------------------------------------------------

def test_status_reports_the_gate_and_the_spend_windows(env):
    grant(env, "--daily-usd", "10")
    write_cost(env, 4.0)
    write_cost(env, 3.0, plan="/elsewhere/p.toml")
    rc, out = env.run("mandate-status")
    assert rc == 0
    assert out["data"]["spend"]["day_usd"] == pytest.approx(4.0)
    assert out["data"]["may_start"] is True


def test_status_refuses_once_the_daily_budget_is_spent(env):
    grant(env, "--daily-usd", "10")
    write_cost(env, 10.0)
    _, out = env.run("mandate-status")
    assert out["data"]["may_start"] is False and out["data"]["refusals"] == ["daily-budget"]


def test_status_of_an_unknown_mandate_fails(env):
    rc, out = env.run("mandate-status", "--id", "ghost")
    assert rc == 1 and "no mandate" in out["detail"]


def test_status_sees_a_running_cycle(env):
    grant(env)
    with store.cycle_lock(ID):
        _, out = env.run("mandate-status")
    assert out["data"]["cycle_running"] is True and out["data"]["cycle_pid"] == os.getpid()


def test_mandate_verbs_survive_the_default_session_injection(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "harness-session")
    rc, _ = grant(env)
    assert rc == 0
    rc, _ = env.run("mandate-status")
    assert rc == 0


# --- user authority -------------------------------------------------------------------

def test_grant_extend_and_resume_are_user_authority_verbs():
    for verb in ("mandate-grant", "mandate-extend", "mandate-resume"):
        assert verb in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS, verb
        call = ["python3", "scripts/agentctl-cli.py", verb, "--by", "alice"]
        assert widening_targets.agentctl_user_authority_call(call) == verb


def test_stop_and_status_are_not_user_authority_verbs():
    for verb in ("mandate-stop", "mandate-status"):
        assert verb not in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS, verb
        assert widening_targets.agentctl_user_authority_call(["python3", "scripts/agentctl-cli.py", verb]) is None


def test_existing_user_authority_verbs_are_all_still_there():
    previous = {
        "start", "reset", "approve", "resolve-permission", "resolve", "reject", "accept", "risk-accept",
        "plan-review", "code-review", "stage-review", "confirm-delivery", "present-plan", "submit-plan",
        "dispatch", "record-result", "verify-final", "replan", "close", "fire-acknowledge", "block",
        "unblock", "drive", "push-subplan", "pop-subplan", "task-reset", "declare", "investigate",
        "critique", "normalize", "partition", "partition-units", "next-stage", "plugin-activate",
        "plugin-deactivate", "plugin-record",
    }
    assert previous <= widening_targets.AGENTCTL_USER_AUTHORITY_VERBS


# --- the store ------------------------------------------------------------------------

def test_the_cycle_lock_is_exclusive_records_the_pid_and_is_released(env):
    store.path_of(ID, "x").parent.mkdir(parents=True)
    with store.cycle_lock(ID):
        assert store.path_of(ID, store.LOCK_FILE).read_text() == str(os.getpid())
        assert store.probe_cycle(ID) == (True, os.getpid())
        with pytest.raises(store.CycleBusy):
            with store.cycle_lock(ID):
                pass
    assert store.probe_cycle(ID) == (False, None)
    with store.cycle_lock(ID):
        pass


def test_the_probe_sees_a_lock_held_by_another_process(env):
    store.path_of(ID, "x").parent.mkdir(parents=True)
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from agentctl import mandate_store as s; "
        "cm = s.cycle_lock(sys.argv[2]); cm.__enter__(); print('ready', flush=True); sys.stdin.read()"
    )
    scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    child = subprocess.Popen(
        [sys.executable, "-c", script, scripts_dir, ID], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, env=dict(os.environ),
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        assert store.probe_cycle(ID) == (True, child.pid)
        with pytest.raises(store.CycleBusy):
            with store.cycle_lock(ID):
                pass
    finally:
        child.stdin.close()
        child.wait(timeout=30)
    assert store.probe_cycle(ID) == (False, None)


def test_probe_of_a_mandate_that_never_ran_is_idle(env):
    assert store.probe_cycle("never-ran") == (False, None)


def test_atomic_write_leaves_no_temp_files_and_replaces_whole(env, tmp_path):
    target = tmp_path / "d" / "x.json"
    store.atomic_write_json(target, {"a": 1})
    store.atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]


def test_jsonl_reader_skips_garbage_lines(env, tmp_path):
    path = tmp_path / "e.jsonl"
    store.append_jsonl(path, {"n": 1})
    with path.open("a") as handle:
        handle.write("{not json\n[1,2]\n")
    store.append_jsonl(path, {"n": 2})
    assert store.read_jsonl(path) == [{"n": 1}, {"n": 2}]
    assert store.read_jsonl(tmp_path / "absent.jsonl") == []


T0 = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)


def test_label_rows_round_trip_with_author_and_reason(env):
    store.append_label_row(ID, 7, "auto-ok", "c1", reason="small, tests exist", now=T0)
    store.append_label_row(ID, 8, "no-auto", "", by="user", now=T0)
    assert store.read_label_rows(ID) == [
        {"ts": "2026-10-09T08:00:00Z", "issue": 7, "label": "auto-ok", "by": "cycle", "cycle_id": "c1",
         "reason": "small, tests exist"},
        {"ts": "2026-10-09T08:00:00Z", "issue": 8, "label": "no-auto", "by": "user", "cycle_id": "",
         "reason": ""},
    ]


@pytest.mark.parametrize("by", ["agent", "", "User", None])
def test_a_label_row_refuses_an_author_other_than_user_or_cycle(env, by):
    with pytest.raises(rules.MandateError):
        store.append_label_row(ID, 7, "auto-ok", "c1", by=by)
    assert store.read_label_rows(ID) == []


def test_item_rows_round_trip_and_enforce_the_enumerations(env):
    row = {"cycle_id": "c1", "issue": "12", "outcome": "pr-opened", "branch": "mandate/12-20261009",
           "pr_url": "https://example.test/pr/1", "cost_usd": 3.5, "duration_s": 600, "tests": "pass",
           "review": "accept", "gates": {"constitution": "ok", "org_neutral": "ok", "lint": "ok"}}
    stored = store.append_item_row(ID, row, now=T0)
    assert stored["ts"] == "2026-10-09T08:00:00Z" and stored["issue"] == 12
    store.append_item_row(ID, {"cycle_id": "c1", "issue": 13, "outcome": "limit"}, now=T0)
    rows = store.read_item_rows(ID)
    assert [r["issue"] for r in rows] == [12, 13] and rows[0]["pr_url"] == row["pr_url"]
    assert rows[0]["gates"] == row["gates"]
    for outcome in rules.OUTCOMES:
        store.append_item_row(ID, {"cycle_id": "c2", "issue": 1, "outcome": outcome}, now=T0)
    assert len(store.read_item_rows(ID)) == 2 + len(rules.OUTCOMES)


@pytest.mark.parametrize("bad", [
    {"cycle_id": "c1", "issue": 1, "outcome": "merged"},
    {"cycle_id": "c1", "issue": 1, "outcome": "failed", "tests": "green"},
    {"cycle_id": "c1", "issue": 1, "outcome": "failed", "review": "maybe"},
    {"cycle_id": "c1", "issue": 1},
    {"issue": 1, "outcome": "failed"},
    {"cycle_id": "../x", "issue": 1, "outcome": "failed"},
])
def test_an_item_row_that_breaks_the_shape_is_refused_and_nothing_is_written(env, bad):
    with pytest.raises(rules.MandateError):
        store.append_item_row(ID, bad)
    assert store.read_item_rows(ID) == []


def test_cycle_rows_round_trip_and_fold_by_cycle_id(env):
    store.append_cycle_row(ID, {"cycle_id": "c1", "started_at": "2026-10-09T01:00:00Z", "status": "done",
                                "taken": [12], "triaged": [20, 21], "spend_24h": 4.0, "spend_7d": 9.0}, now=T0)
    store.record_digest(ID, "c1", ok=True, notifier="telegram", delivered_at=T0)
    store.append_cycle_row(ID, {"cycle_id": "c2", "status": "limit"}, now=T0)
    rows = store.read_cycle_rows(ID)
    assert [r["cycle_id"] for r in rows] == ["c1", "c1", "c2"]
    records = store.read_cycle_records(ID)
    assert records["c1"]["status"] == "done" and records["c1"]["taken"] == [12]
    assert records["c1"]["digest"] == {"ok": True, "notifier": "telegram", "delivered_at": "2026-10-09T08:00:00Z"}
    assert "digest" not in records["c2"]


def test_the_digest_reader_returns_the_records_the_eligibility_rule_takes(env):
    store.append_cycle_row(ID, {
        "cycle_id": "c1", "digest": {"notifier": "telegram", "ok": True, "delivered_at": "2026-10-09T08:00:00Z"},
    })
    store.append_cycle_row(ID, {"cycle_id": "c2"})
    store.record_digest(ID, "c3", ok=False, notifier="file", delivered_at=T0)
    digests = store.read_digests(ID)
    assert digests == {
        "c1": {"notifier": "telegram", "ok": True, "delivered_at": "2026-10-09T08:00:00Z"},
        "c3": {"notifier": "file", "ok": False, "delivered_at": "2026-10-09T08:00:00Z"},
    }
    issue = {"number": 7, "labels": ["backlog", "auto-ok"], "author": "owner", "created_at": ""}
    store.append_label_row(ID, 7, "auto-ok", "c1", now=T0)
    verdict = rules.evaluate_candidate(
        issue, owner="owner", labels_cfg=rules.DEFAULT_LABELS, label_rows=store.read_label_rows(ID),
        digests=digests, pr_refs=set(), veto_window_hours=24, now=T0 + timedelta(hours=25),
    )
    assert verdict.eligible and verdict.reason == "cycle-set-veto-window-passed"


@pytest.mark.parametrize("digest", [
    {"notifier": "telegram", "ok": True},
    {"notifier": "telegram", "ok": "yes", "delivered_at": "2026-10-09T08:00:00Z"},
    {"notifier": "telegram", "ok": True, "delivered_at": "yesterday"},
    "delivered",
])
def test_a_cycle_row_with_a_malformed_digest_is_refused(env, digest):
    with pytest.raises(rules.MandateError):
        store.append_cycle_row(ID, {"cycle_id": "c1", "digest": digest})
    assert store.read_cycle_rows(ID) == []


def test_command_rows_round_trip_per_cycle(env):
    store.append_command_row(ID, "c1", {"argv": ["gh", "issue", "list"], "cwd": "/w", "exit": 0}, now=T0)
    store.append_command_row(ID, "c1", {"argv": ["git", "fetch"], "cwd": "/w", "exit": 1, "ts": "2026-10-09T09:00:00Z"})
    store.append_command_row(ID, "c2", {"argv": ["true"], "cwd": "/w", "exit": 0}, now=T0)
    assert store.read_command_rows(ID, "c1") == [
        {"ts": "2026-10-09T08:00:00Z", "argv": ["gh", "issue", "list"], "cwd": "/w", "exit": 0},
        {"ts": "2026-10-09T09:00:00Z", "argv": ["git", "fetch"], "cwd": "/w", "exit": 1},
    ]
    assert len(store.read_command_rows(ID, "c2")) == 1
    assert store.read_command_rows(ID, "never") == []
    assert (store.mandate_dir(ID) / "cycles" / "c1" / "commands.jsonl").is_file()


@pytest.mark.parametrize("bad", [
    {"argv": "git fetch", "cwd": "/w", "exit": 0},
    {"argv": ["git"], "cwd": "/w"},
])
def test_a_command_row_that_breaks_the_shape_is_refused(env, bad):
    with pytest.raises(rules.MandateError):
        store.append_command_row(ID, "c1", bad)
    assert store.read_command_rows(ID, "c1") == []


@pytest.mark.parametrize("cycle_id", ["../escape", "a/b", "", ".."])
def test_a_cycle_id_cannot_leave_the_mandate_directory(env, cycle_id):
    with pytest.raises(rules.MandateError):
        store.append_command_row(ID, cycle_id, {"argv": ["x"], "cwd": "/", "exit": 0})


def test_open_breaker_persists_the_record_and_logs_the_reason(env):
    grant(env)
    tripped = store.open_breaker(ID, "tests: 3 newly failing", now=T0)
    assert tripped.breaker_open and tripped.breaker_reason == "tests: 3 newly failing"
    saved = store.load_mandate(ID)
    assert saved.breaker_open and saved.breaker_reason == "tests: 3 newly failing"
    event = store.read_events(ID)[-1]
    assert event["event"] == "breaker-open" and event["detail"] == {"reason": "tests: 3 newly failing"}
    _, out = env.run("mandate-status")
    assert "breaker-open" in out["data"]["refusals"]


def test_open_breaker_of_an_unknown_mandate_raises(env):
    with pytest.raises(rules.MandateError):
        store.open_breaker("ghost", "x")


def test_events_record_who_acted(env):
    grant(env, by="alice")
    env.run("mandate-extend", "--days", "2", "--by", "alice")
    env.run("mandate-resume", "--by", "alice")
    assert [(e["event"], e["by"]) for e in store.read_events(ID)] == [
        ("granted", "alice"), ("extended", "alice"), ("resumed", "alice"),
    ]


def test_fingerprint_moves_with_a_one_byte_edit_of_each_fingerprinted_file(env):
    grant(env)
    store.append_label_row(ID, 1, "auto-ok", "c1", now=T0)
    assert store.FINGERPRINT_FILES == (store.MANDATE_FILE, store.EVENTS_FILE, store.LABELS_FILE)
    for name in store.FINGERPRINT_FILES:
        path = store.path_of(ID, name)
        original = path.read_bytes()
        before = store.state_fingerprint(ID)
        path.write_bytes(original[:-2] + bytes([original[-2] ^ 0x01]) + original[-1:])
        assert store.state_fingerprint(ID) != before, name
        path.write_bytes(original)
        assert store.state_fingerprint(ID) == before, name


def test_fingerprint_marks_a_missing_file_distinctly_from_an_empty_one(env):
    grant(env)
    absent = store.state_fingerprint(ID)
    store.path_of(ID, store.LABELS_FILE).write_bytes(b"")
    assert store.state_fingerprint(ID) != absent


def test_fingerprint_ignores_the_lock_and_the_cycle_logs(env):
    grant(env)
    first = store.state_fingerprint(ID)
    with store.cycle_lock(ID):
        assert store.state_fingerprint(ID) == first
    store.append_item_row(ID, {"cycle_id": "c1", "issue": 1, "outcome": "failed"})
    store.append_cycle_row(ID, {"cycle_id": "c1"})
    store.append_command_row(ID, "c1", {"argv": ["x"], "cwd": "/", "exit": 0})
    assert store.state_fingerprint(ID) == first
    store.append_label_row(ID, 1, "auto-ok", "c1")
    assert store.state_fingerprint(ID) != first


def test_spend_now_reads_the_cost_log_for_the_mandate_directory(env):
    grant(env)
    write_cost(env, 2.5)
    write_cost(env, 9.0, plan="/elsewhere/p.toml")
    store.append_event(ID, "judge-cost", {"cost_usd": 0.5})
    spend = store.spend_now(ID)
    assert spend.day_usd == pytest.approx(3.0) and spend.week_usd == pytest.approx(3.0)


def test_mandates_root_honours_the_override(env, tmp_path):
    assert store.mandates_root() == tmp_path / "mandates"


def test_loading_a_corrupt_record_raises_rather_than_granting_defaults(env):
    path = store.path_of(ID, store.MANDATE_FILE)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": ID}))
    with pytest.raises(rules.MandateError):
        store.load_mandate(ID)
