"""The `mandate-*` verbs and the mandate store, driven through `cli.main`.

Every test points `$AGENTCTL_MANDATE_DIR` and the spawn-cost log at a tmp dir and replaces the
process runner, so nothing here touches the real agent home, systemd or a live process.
"""
from __future__ import annotations

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
    assert store.read_events(ID)[-1]["event"] == "stopped"


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


def test_digest_and_label_records_round_trip(env):
    delivered = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    store.record_digest(ID, "c1", ok=True, notifier="telegram", delivered_at=delivered)
    assert store.read_digests(ID) == {"c1": {"ok": True, "notifier": "telegram", "delivered_at": "2026-10-09T08:00:00Z"}}
    store.append_label_row(ID, 7, "auto-ok", "c1", now=delivered)
    assert store.read_label_rows(ID) == [
        {"ts": "2026-10-09T08:00:00Z", "issue": 7, "label": "auto-ok", "by": "cycle", "cycle_id": "c1"},
    ]


def test_fingerprint_moves_with_any_state_file_and_not_with_the_lock(env):
    grant(env)
    first = store.state_fingerprint(ID)
    assert store.state_fingerprint(ID) == first
    with store.cycle_lock(ID):
        assert store.state_fingerprint(ID) == first
    store.append_label_row(ID, 1, "auto-ok", "c1")
    second = store.state_fingerprint(ID)
    assert second != first
    store.record_digest(ID, "c1", ok=True, notifier="telegram")
    assert store.state_fingerprint(ID) != second


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
