"""Rules of the standing mandate (`agentctl.mandate`): every bound is a pure function.

Each catalogue entry in `mandate_mutation_control.py` names the tests here (or in
test_mandate_cli.py) that must go red when its production line is broken.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentctl import mandate as m
from ast_purity import impure_names

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
OWNER = "the-owner"
LABELS = dict(m.DEFAULT_LABELS)
MANDATE_DIR = "/home/x/.claude-agent/agentctl/mandates/core-debt"


def hours(n: float) -> str:
    return m.format_ts(NOW - timedelta(hours=n))


def issue(number=1, labels=("backlog", "auto-ok"), author=OWNER, created="2026-10-01T00:00:00Z"):
    return {"number": number, "labels": list(labels), "author": author, "created_at": created}


def evaluate(iss, *, label_rows=(), digests=None, pr_refs=frozenset(), window=24.0, now=NOW):
    return m.evaluate_candidate(
        iss, owner=OWNER, labels_cfg=LABELS, label_rows=list(label_rows),
        digests=digests or {}, pr_refs=set(pr_refs), veto_window_hours=window, now=now,
    )


def cycle_label_row(issue_no=1, cycle_id="c1", ts=None, by="cycle", label="auto-ok"):
    return {"ts": ts or hours(100), "issue": issue_no, "label": label, "by": by, "cycle_id": cycle_id}


def delivered(ago_hours, notifier="telegram", ok=True):
    return {"ok": ok, "notifier": notifier, "delivered_at": hours(ago_hours)}


def mandate(**over):
    base = m.new_mandate("core-debt", "alice", NOW)
    return dataclasses.replace(base, **over)


def clear_spend():
    return m.SpendWindows(0.0, 0.0)


# --- purity ---------------------------------------------------------------------------

def test_module_reaches_no_transport_root():
    assert impure_names(m) == set()


def test_module_imports_nothing_that_does_io_and_calls_no_clock():
    tree = ast.parse(inspect.getsource(m))
    allowed = {"__future__", "fnmatch", "posixpath", "re", "dataclasses", "datetime", "typing"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= allowed, imported - allowed
    source = inspect.getsource(m)
    for forbidden in ("open(", ".now(", "utcnow", "time.time", "os.", "Path("):
        assert forbidden not in source, forbidden


# --- the mandate record ---------------------------------------------------------------

def test_new_mandate_defaults_to_a_fourteen_day_term():
    granted = m.new_mandate("core-debt", "alice", NOW)
    assert granted.granted_at == m.format_ts(NOW)
    assert m.parse_ts(granted.expires_at) - m.parse_ts(granted.granted_at) == timedelta(days=14)
    assert (granted.paused, granted.breaker_open) == (False, False)
    assert granted.limits == m.MandateLimits()


@pytest.mark.parametrize("bad", ["", "a b", "../x", "-x", "a/b"])
def test_mandate_id_must_be_a_plain_slug(bad):
    with pytest.raises(m.MandateError):
        m.new_mandate(bad, "alice", NOW)


@pytest.mark.parametrize("name", m.LIMIT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, True, "5"])
def test_every_limit_must_be_a_positive_number(name, value):
    with pytest.raises(m.MandateError):
        m.MandateLimits(**{name: value})


def test_max_items_must_be_whole():
    with pytest.raises(m.MandateError):
        m.MandateLimits(max_items=2.5)


def test_mandate_round_trips_through_a_dict():
    original = mandate(labels={"ok": "yes", "no": "nope", "veto": "stop"}, constitution=("a/", "b.py"))
    assert m.Mandate.from_dict(original.to_dict()) == original


def test_from_dict_fills_default_labels_and_constitution():
    data = mandate().to_dict()
    del data["labels"], data["constitution"]
    loaded = m.Mandate.from_dict(data)
    assert dict(loaded.labels) == m.DEFAULT_LABELS and tuple(loaded.constitution) == m.DEFAULT_CONSTITUTION


def test_from_dict_names_the_missing_field():
    data = mandate().to_dict()
    del data["expires_at"]
    with pytest.raises(m.MandateError, match="expires_at"):
        m.Mandate.from_dict(data)


def test_from_dict_rejects_an_unparsable_timestamp():
    data = mandate().to_dict()
    data["expires_at"] = "tomorrow-ish"
    with pytest.raises(m.MandateError):
        m.Mandate.from_dict(data)


def test_extension_counts_from_the_later_of_now_and_expiry():
    live = mandate()
    assert m.extended(live, NOW, 7).expires_at == m.format_ts(NOW + timedelta(days=21))
    lapsed = mandate(expires_at=hours(48))
    assert m.extended(lapsed, NOW, 3).expires_at == m.format_ts(NOW + timedelta(days=3))
    with pytest.raises(m.MandateError):
        m.extended(live, NOW, 0)


def test_stop_pauses_and_resume_unpauses_and_closes_the_breaker():
    broken = m.breaker_opened(mandate(), "tests failed")
    assert broken.breaker_open and broken.breaker_reason == "tests failed"
    resumed = m.resumed(m.stopped(broken))
    assert (resumed.paused, resumed.breaker_open, resumed.breaker_reason) == (False, False, "")
    assert m.stopped(mandate()).paused


def test_naive_timestamps_are_read_as_utc():
    assert m.parse_ts("2026-10-09T12:00:00") == NOW
    assert m.parse_ts("2026-10-09T15:00:00+03:00") == NOW
    assert m.parse_ts("garbage") is None


# --- the gate -------------------------------------------------------------------------

def test_gate_lets_a_fresh_mandate_start():
    verdict = m.gate(mandate(), NOW, clear_spend())
    assert verdict.may_start and verdict.reasons == ()


def test_gate_refuses_without_a_mandate():
    assert m.gate(None, NOW, clear_spend()).reasons == ("no-mandate",)


def test_gate_refuses_after_expiry():
    lapsed = mandate(expires_at=hours(1))
    verdict = m.gate(lapsed, NOW, clear_spend())
    assert not verdict.may_start and "expired" in verdict.reasons


def test_gate_treats_the_expiry_instant_as_expired():
    exact = mandate(expires_at=m.format_ts(NOW))
    assert "expired" in m.gate(exact, NOW, clear_spend()).reasons
    assert m.gate(exact, NOW - timedelta(seconds=1), clear_spend()).may_start


def test_gate_refuses_a_paused_mandate():
    assert "paused" in m.gate(mandate(paused=True), NOW, clear_spend()).reasons


def test_gate_refuses_with_the_breaker_open():
    verdict = m.gate(mandate(breaker_open=True), NOW, clear_spend())
    assert not verdict.may_start and "breaker-open" in verdict.reasons


def test_gate_refuses_at_the_daily_budget():
    limit = mandate().daily_usd
    assert "daily-budget" in m.gate(mandate(), NOW, m.SpendWindows(limit, limit)).reasons
    assert m.gate(mandate(), NOW, m.SpendWindows(limit - 0.01, limit - 0.01)).may_start


def test_gate_refuses_at_the_weekly_budget():
    spend = m.SpendWindows(0.0, mandate().weekly_usd)
    verdict = m.gate(mandate(), NOW, spend)
    assert not verdict.may_start and verdict.reasons == ("weekly-budget",)


def test_gate_refuses_at_the_cycle_minutes_cap():
    cap = mandate().cycle_minutes_cap
    assert "cycle-minutes-cap" in m.gate(mandate(), NOW, clear_spend(), cap).reasons
    assert m.gate(mandate(), NOW, clear_spend(), cap - 1).may_start


def test_gate_reports_every_reason_at_once():
    broken = mandate(paused=True, breaker_open=True, expires_at=hours(1))
    spend = m.SpendWindows(1e6, 1e6)
    assert set(m.gate(broken, NOW, spend, 1e6).reasons) == {
        "expired", "paused", "breaker-open", "daily-budget", "weekly-budget", "cycle-minutes-cap",
    }


# --- spend ----------------------------------------------------------------------------

def row(cost, ago, plan=MANDATE_DIR + "/plans/p.toml", **extra):
    return {"ts": hours(ago), "cost_usd": cost, "plan_path": plan, **extra}


def test_spend_counts_only_rows_under_the_mandate_directory():
    rows = [
        row(1.0, 1),
        row(2.0, 1, plan=MANDATE_DIR + "-other/plans/p.toml"),
        row(4.0, 1, plan="/elsewhere/plan.toml"),
        row(8.0, 1, plan=None),
    ]
    assert m.compute_spend(rows, [], MANDATE_DIR, NOW) == m.SpendWindows(1.0, 1.0)


def test_spend_windows_are_rolling_day_and_week():
    rows = [row(1.0, 23), row(10.0, 48), row(100.0, 24 * 8)]
    assert m.compute_spend(rows, [], MANDATE_DIR, NOW) == m.SpendWindows(1.0, 11.0)


def test_spend_adds_judge_cost_and_overrun_charge_events():
    events = [
        {"ts": hours(2), "event": "judge-cost", "detail": {"cost_usd": 0.5}},
        {"ts": hours(3), "event": "overrun-charge", "detail": {"cost_usd": 15.0}},
        {"ts": hours(3), "event": "granted", "detail": {"cost_usd": 99.0}},
        {"ts": hours(30), "event": "judge-cost", "detail": {"cost_usd": 2.0}},
    ]
    assert m.compute_spend([], events, MANDATE_DIR, NOW) == m.SpendWindows(15.5, 17.5)


def test_spend_counts_an_unreadable_timestamp_in_both_windows():
    rows = [{"ts": "??", "cost_usd": 3.0, "plan_path": MANDATE_DIR + "/p.toml"}]
    assert m.compute_spend(rows, [], MANDATE_DIR, NOW) == m.SpendWindows(3.0, 3.0)


def test_spend_ignores_refused_rows_and_non_numeric_cost():
    rows = [row(5.0, 1, event="refused"), row("5", 1), row(True, 1), row(None, 1)]
    assert m.compute_spend(rows, [], MANDATE_DIR, NOW) == m.SpendWindows(0.0, 0.0)


# --- issues ---------------------------------------------------------------------------

def test_normalize_issue_reads_the_gh_json_shape():
    raw = {"number": "7", "labels": [{"name": "backlog"}, {"name": "auto-ok"}],
           "author": {"login": OWNER}, "createdAt": "2026-10-02T00:00:00Z"}
    assert m.normalize_issue(raw) == {
        "number": 7, "labels": ["backlog", "auto-ok"], "author": OWNER, "created_at": "2026-10-02T00:00:00Z",
    }


def test_pr_references_come_from_title_body_and_branch():
    prs = [
        {"title": "Fix #12", "body": "", "headRefName": "x"},
        {"title": "t", "body": "closes #13, see also #14.", "headRefName": "x"},
        {"title": "t", "body": None, "headRefName": "mandate/15-fix-thing"},
        {"title": "ref#99 and #1234x and #", "body": "", "headRefName": "mandates/77-x"},
    ]
    assert m.pr_referenced_issues(prs) == {12, 13, 14, 15}


def test_issue_by_another_author_is_not_eligible():
    verdict = evaluate(issue(author="someone-else"))
    assert not verdict.eligible and verdict.reason == "not-owner-authored"


def test_issue_outside_the_domain_labels_is_not_eligible():
    verdict = evaluate(issue(labels=("question", "auto-ok")))
    assert not verdict.eligible and verdict.reason == "out-of-domain"


def test_difficulty_label_is_in_the_domain():
    assert evaluate(issue(labels=("difficulty", "auto-ok"))).eligible


def test_a_veto_label_beats_a_user_set_auto_ok():
    verdict = evaluate(issue(labels=("backlog", "auto-ok", "no-auto")))
    assert not verdict.eligible and verdict.reason == "vetoed"


def test_a_veto_label_beats_a_cycle_set_auto_ok_past_its_window():
    verdict = evaluate(
        issue(labels=("backlog", "auto-ok", "no-auto")),
        label_rows=[cycle_label_row()], digests={"c1": delivered(100)},
    )
    assert not verdict.eligible and verdict.reason == "vetoed"


def test_an_issue_an_open_pr_references_is_not_eligible():
    verdict = evaluate(issue(7), pr_refs={7})
    assert not verdict.eligible and verdict.reason == "open-pr"
    assert evaluate(issue(7), pr_refs={8}).eligible


def test_a_process_umbrella_is_not_eligible():
    verdict = evaluate(issue(labels=("backlog", "auto-ok", "process-umbrella")))
    assert not verdict.eligible and verdict.reason == "process-umbrella"


def test_auto_no_beside_auto_ok_makes_it_ineligible():
    verdict = evaluate(issue(labels=("backlog", "auto-ok", "auto-no")))
    assert not verdict.eligible and verdict.reason == "auto-no"


def test_without_auto_ok_it_is_not_eligible():
    verdict = evaluate(issue(labels=("backlog",)))
    assert not verdict.eligible and verdict.reason == "not-auto-ok"


def test_a_user_set_auto_ok_is_eligible_at_once_even_for_severity_high():
    verdict = evaluate(issue(labels=("backlog", "auto-ok", "severity:high")))
    assert verdict.eligible and verdict.auto_ok_source == "user"


def test_a_label_row_by_someone_other_than_the_cycle_is_a_user_label():
    verdict = evaluate(issue(), label_rows=[cycle_label_row(by="user")])
    assert verdict.eligible and verdict.auto_ok_source == "user"


def test_a_cycle_set_auto_ok_on_severity_high_is_never_eligible():
    verdict = evaluate(
        issue(labels=("backlog", "auto-ok", "severity:high")),
        label_rows=[cycle_label_row()], digests={"c1": delivered(100)},
    )
    assert not verdict.eligible and verdict.reason == "severity-high-cycle-set"


def test_a_cycle_set_auto_ok_waits_for_a_delivered_digest():
    verdict = evaluate(issue(), label_rows=[cycle_label_row()])
    assert not verdict.eligible and verdict.reason == "digest-not-delivered"


def test_a_file_notifier_digest_does_not_count_as_delivered():
    verdict = evaluate(issue(), label_rows=[cycle_label_row()], digests={"c1": delivered(100, notifier="file")})
    assert not verdict.eligible and verdict.reason == "digest-not-delivered"


@pytest.mark.parametrize("record", [
    {"ok": False, "notifier": "telegram", "delivered_at": hours(100)},
    {"ok": True, "notifier": None, "delivered_at": hours(100)},
    {"ok": True, "notifier": "", "delivered_at": hours(100)},
    {"ok": True, "notifier": "telegram", "delivered_at": "never"},
    {"ok": True, "notifier": "telegram"},
])
def test_a_digest_record_that_is_not_a_real_delivery_does_not_count(record):
    verdict = evaluate(issue(), label_rows=[cycle_label_row()], digests={"c1": record})
    assert not verdict.eligible and verdict.reason == "digest-not-delivered"


def test_a_cycle_set_auto_ok_waits_out_the_veto_window_after_the_digest():
    early = evaluate(issue(), label_rows=[cycle_label_row()], digests={"c1": delivered(10)})
    assert not early.eligible and early.reason == "veto-window"
    late = evaluate(issue(), label_rows=[cycle_label_row()], digests={"c1": delivered(25)})
    assert late.eligible and late.auto_ok_source == "cycle"


def test_the_veto_window_is_the_mandates_not_a_constant():
    row_set = dict(label_rows=[cycle_label_row()], digests={"c1": delivered(10)})
    assert not evaluate(issue(), **row_set).eligible
    assert evaluate(issue(), window=6.0, **row_set).eligible


def test_a_digest_counts_as_delivered_only_when_a_real_notifier_reported_ok():
    not_delivered = [
        None,
        "delivered",
        {"ok": False, "notifier": "telegram", "delivered_at": hours(1)},
        {"ok": True, "notifier": "file", "delivered_at": hours(1)},
        {"ok": True, "notifier": "", "delivered_at": hours(1)},
        {"ok": True, "notifier": None, "delivered_at": hours(1)},
        {"ok": True, "notifier": "telegram"},
        {"ok": True, "notifier": "telegram", "delivered_at": "never"},
    ]
    for record in not_delivered:
        assert m.digest_counts_as_delivered(record) is False, record
    assert m.digest_counts_as_delivered({"ok": True, "notifier": "telegram", "delivered_at": hours(1)}) is True


# --- refused-start notice ---------------------------------------------------------------

def notice_event(reasons, ago=1.0, notifier="telegram", ok=True, delivered=True):
    detail = {"reasons": list(reasons), "notifier": notifier, "ok": ok}
    if delivered:
        detail["delivered_at"] = hours(ago)
    return {"ts": hours(ago), "event": "refused-notice", "detail": detail}


def test_refusal_notice_due_once_per_day_per_reason_set():
    assert m.refusal_notice_due(["paused"], [], NOW)
    sent = [notice_event(["paused"], ago=1)]
    assert not m.refusal_notice_due(["paused"], sent, NOW)
    assert not m.refusal_notice_due(["paused"], sent, NOW + timedelta(hours=5))
    assert m.refusal_notice_due(["paused"], sent, NOW + timedelta(hours=12))
    assert not m.refusal_notice_due(["expired", "paused"], [notice_event(["paused", "expired"])], NOW)


def test_refusal_notice_due_again_when_reasons_change():
    sent = [notice_event(["paused"])]
    assert m.refusal_notice_due(["paused", "breaker-open"], sent, NOW)
    assert m.refusal_notice_due(["breaker-open"], sent, NOW)


def test_a_file_only_notice_does_not_suppress():
    for kwargs in ({"notifier": "file"}, {"notifier": ""}, {"notifier": None}, {"ok": False}, {"delivered": False}):
        assert m.refusal_notice_due(["paused"], [notice_event(["paused"], **kwargs)], NOW), kwargs
    assert not m.refusal_notice_due(["paused"], [notice_event(["paused"], notifier="chat")], NOW)


def test_blocked_since_is_the_trailing_refused_run_or_the_breaker_trip():
    rows = [
        {"cycle_id": "a", "status": "refused", "started_at": hours(70)},
        {"cycle_id": "b", "status": "ok", "started_at": hours(60)},
        {"cycle_id": "c", "status": "refused", "started_at": hours(48)},
        {"cycle_id": "d", "status": "refused", "started_at": hours(24)},
    ]
    assert m.blocked_since(rows, [], ["paused"]) == NOW - timedelta(hours=48)
    trip = [{"ts": hours(55), "event": "breaker-open"}, {"ts": hours(30), "event": "breaker-open"}]
    assert m.blocked_since(rows, trip, ["paused"]) == NOW - timedelta(hours=48)
    assert m.blocked_since(rows, trip, ["breaker-open"]) == NOW - timedelta(hours=48)
    old_trip = [{"ts": hours(100), "event": "breaker-open"}, {"ts": hours(90), "event": "breaker-open"}]
    assert m.blocked_since(rows, old_trip, ["breaker-open"]) == NOW - timedelta(hours=90)
    assert m.blocked_since([{"cycle_id": "x", "status": "ok"}], [], ["paused"]) is None


def test_unblock_commands_per_reason():
    granted = mandate()
    commands = m.unblock_commands(
        ["expired", "paused", "breaker-open", "daily-budget", "weekly-budget", "no-mandate", "cycle-minutes-cap"],
        granted, NOW, {"daily-budget": NOW + timedelta(hours=3)},
    )
    assert commands == [
        "expired: agentctl mandate-extend --days <n> --by <you>",
        "paused: agentctl mandate-resume --by <you>",
        "breaker-open: agentctl mandate-resume --by <you>",
        f"daily-budget: clears by itself at {m.format_ts(NOW + timedelta(hours=3))}",
        "weekly-budget: clears by itself when the spend window rolls over",
        "no-mandate: agentctl mandate-grant ... --by <you>",
    ]


def test_spend_clears_when_the_oldest_charges_leave_the_window():
    rows = [row(8.0, 20), row(5.0, 10), row(5.0, 2)]
    assert m.spend_clears_at(rows, [], MANDATE_DIR, NOW, 24, 10.0) == NOW + timedelta(hours=14)
    assert m.spend_clears_at(rows, [], MANDATE_DIR, NOW, 24, 1.0) == NOW + timedelta(hours=22)
    assert m.spend_clears_at([], [], MANDATE_DIR, NOW, 24, 10.0) is None


def test_the_latest_cycle_label_row_picks_which_digest_applies():
    rows = [cycle_label_row(cycle_id="old", ts=hours(200)), cycle_label_row(cycle_id="new", ts=hours(50))]
    digests = {"old": delivered(190), "new": delivered(5)}
    assert evaluate(issue(), label_rows=rows, digests=digests).reason == "veto-window"
    assert evaluate(issue(), label_rows=list(reversed(rows)), digests=digests).reason == "veto-window"


def test_label_rows_of_other_issues_and_labels_are_ignored():
    rows = [cycle_label_row(issue_no=2), cycle_label_row(label="auto-no")]
    assert evaluate(issue(1), label_rows=rows).auto_ok_source == "user"


# --- triage and selection -------------------------------------------------------------

def test_triage_sees_only_unlabelled_owner_authored_domain_issues_oldest_first():
    issues = [
        issue(1, ("backlog",), created="2026-10-05T00:00:00Z"),
        issue(2, ("backlog",), created="2026-10-01T00:00:00Z"),
        issue(3, ("backlog", "auto-ok")),
        issue(4, ("backlog", "auto-no")),
        issue(5, ("backlog", "no-auto")),
        issue(6, ("backlog", "severity:high")),
        issue(7, ("backlog", "process-umbrella")),
        issue(8, ("backlog",), author="stranger"),
        issue(9, ("question",)),
        issue(10, ("difficulty",), created="2026-10-03T00:00:00Z"),
    ]
    seen = m.triage_candidates(issues, owner=OWNER, labels_cfg=LABELS)
    assert [i["number"] for i in seen] == [2, 10, 1]


def candidates(*specs):
    return [
        m.Candidate(n, (), eligible, "x", None, created)
        for n, eligible, created in specs
    ]


def test_selection_takes_the_oldest_eligible_up_to_max_items():
    pool = candidates((1, True, "2026-10-03T00:00:00Z"), (2, True, "2026-10-01T00:00:00Z"),
                      (3, False, "2026-09-01T00:00:00Z"), (4, True, "2026-10-02T00:00:00Z"))
    assert m.select_take(pool, max_items=2) == [2, 4]


def test_selection_follows_a_board_rank_with_unranked_after():
    pool = candidates((1, True, "2026-10-01T00:00:00Z"), (2, True, "2026-10-02T00:00:00Z"),
                      (3, True, "2026-10-03T00:00:00Z"))
    assert m.select_take(pool, max_items=3, rank=[3, 99, 2]) == [3, 2, 1]


def test_selection_of_nothing_eligible_is_empty():
    assert m.select_take(candidates((1, False, "")), max_items=3) == []


# --- the test gate, overrun, outcomes -------------------------------------------------

def test_an_item_passes_when_every_baseline_pass_still_passes():
    assert m.passes({"a", "b"}, {"a", "b", "new"})
    assert m.failing_ids({"a", "b"}, {"a"}) == ["b"]


def test_a_vanished_test_is_a_failure():
    assert not m.passes({"a", "b"}, {"a"})


def test_one_rerun_can_clear_a_first_run_failure():
    assert m.passes_after_rerun({"a", "b"}, {"a"}, {"b"})
    assert not m.passes_after_rerun({"a", "b"}, {"a"}, set())


OLD = "scripts.tests.test_a.TestA"
NEW_SIDE = "scripts.tests.test_b.TestB"
TOUCHED_A = [["scripts/tests/test_a.py"]]
RENAMED_A_TO_B = [["scripts/tests/test_a.py", "scripts/tests/test_b.py"]]
JUNIT_364 = Path(__file__).parent / "fixtures" / "mandate" / "junit-364"


def test_module_of_maps_a_collection_error_and_only_whole_dotted_prefixes():
    groups = [["docs/x.md"], ["scripts/tests/test_a.py"]]
    assert m.test_module_of(f"{OLD}::t1", groups) == 1
    assert m.test_module_of("::scripts.tests.test_a", groups) == 1
    assert m.test_module_of("scripts.tests.test_ab.TestA::t1", groups) is None
    assert m.test_module_of(f"{OLD}::t1", [["docs/x.md"]]) is None


def test_vanished_id_in_a_touched_module_is_excused():
    ids = {f"{OLD}::t1", f"{OLD}::t2"}
    assert m.split_failures(ids, ids, {f"{OLD}::t1"}, {f"{OLD}::t1"}, TOUCHED_A) == ([], [f"{OLD}::t2"])


def test_vanished_id_in_an_untouched_module_still_fails():
    ids = {f"{OLD}::t1", f"{OLD}::t2"}
    untouched = [["scripts/tests/test_c.py"]]
    assert m.split_failures(ids, ids, {f"{OLD}::t1"}, {f"{OLD}::t1"}, untouched) == ([f"{OLD}::t2"], [])


def test_a_new_failure_in_the_touched_module_voids_the_excuse():
    ids = {f"{OLD}::t1", f"{OLD}::t2"}
    item_seen = {f"{OLD}::t1", f"{OLD}::t3"}
    assert m.split_failures(ids, ids, {f"{OLD}::t1"}, item_seen, TOUCHED_A) == ([f"{OLD}::t2"], [])
    already_red = {f"{OLD}::t4"}
    assert m.split_failures(ids, ids | already_red, {f"{OLD}::t1"}, item_seen | already_red, TOUCHED_A) == (
        [f"{OLD}::t2"], [],
    )
    assert m.split_failures(ids, ids | already_red, {f"{OLD}::t1"}, {f"{OLD}::t1"} | already_red, TOUCHED_A) == (
        [], [f"{OLD}::t2"],
    )


def test_a_renamed_module_old_path_excuses_its_ids():
    ids = {f"{OLD}::t1", f"{OLD}::t2"}
    assert m.split_failures(ids, ids, {f"{OLD}::t1"}, {f"{OLD}::t1"}, RENAMED_A_TO_B) == ([], [f"{OLD}::t2"])


def test_a_new_failure_in_the_renamed_to_module_voids_the_excuse():
    ids = {f"{OLD}::t1", f"{OLD}::t2"}
    item_seen = {f"{OLD}::t1", f"{NEW_SIDE}::t2"}
    assert m.split_failures(ids, ids, {f"{OLD}::t1"}, item_seen, RENAMED_A_TO_B) == ([f"{OLD}::t2"], [])


def test_an_excused_id_is_not_held_against_the_item_after_the_rerun_rule():
    assert m.passes_after_rerun({"a", "b"}, {"a"}, set(), excused={"b"})
    assert m.passes_after_rerun({"a", "b", "c"}, {"a"}, {"c"}, excused={"b"})
    assert not m.passes_after_rerun({"a", "b"}, {"a"}, set())


def test_the_recorded_364_junit_excuses_exactly_the_removed_id():
    roots = ("/srv/placeholder-root",)

    def read(name):
        passed, seen = set(), set()
        for case in ET.parse(JUNIT_364 / name).getroot().iter("testcase"):
            ident = m.canonical_test_id(f"{case.get('classname', '')}::{case.get('name', '')}", roots)
            seen.add(ident)
            if all(case.find(tag) is None for tag in ("failure", "error", "skipped")):
                passed.add(ident)
        return passed, seen

    baseline_passed, baseline_seen = read("baseline.xml")
    item_passed, item_seen = read("item.xml")
    groups = m.diff_path_groups(["M\tscripts/tests/test_sync_pull_rewires_hooks.py"])
    removed = "scripts.tests.test_sync_pull_rewires_hooks.TestPullRewiresHooks::test_up_to_date_pull_skips_the_installer"
    assert m.failing_ids(baseline_passed, item_passed) == [removed]
    assert m.split_failures(baseline_passed, baseline_seen, item_passed, item_seen, groups) == ([], [removed])


def test_a_test_id_that_embeds_the_worktree_path_is_the_same_id_in_every_worktree():
    canonical = "scripts.tests.test_b::test_c[Bash(python3 <worktree>/scripts/agentctl-cli.py:*)]"
    item = "/var/tmp/agent-debt-cycle/c-339"
    item_id = f"scripts.tests.test_b::test_c[Bash(python3 {item}/scripts/agentctl-cli.py:*)]"
    assert m.canonical_test_id(item_id, (item, "/place/vartmp/agent-debt-cycle/c-339")) == canonical
    baseline = "/place/vartmp/agent-debt-cycle/c-baseline"
    baseline_id = f"scripts.tests.test_b::test_c[Bash(python3 {baseline}/scripts/agentctl-cli.py:*)]"
    assert m.canonical_test_id(baseline_id, (baseline, baseline + "/")) == canonical
    assert m.canonical_test_id("a::b", (item,)) == "a::b"
    assert m.canonical_test_id(item_id, ()) == item_id


def test_overrun_is_strictly_beyond_either_cap():
    assert not m.check_overrun(15.0, 125.0, usd_cap=15.0, minutes_cap=125.0).overrun
    assert m.check_overrun(15.01, 1.0, usd_cap=15.0, minutes_cap=125.0).reasons == ("item-usd-cap",)
    assert m.check_overrun(1.0, 125.5, usd_cap=15.0, minutes_cap=125.0).reasons == ("item-minutes-cap",)
    both = m.check_overrun(20.0, 200.0, usd_cap=15.0, minutes_cap=125.0)
    assert both.overrun and len(both.reasons) == 2


def test_a_killed_spawn_without_a_cost_row_is_charged_the_whole_cap():
    assert m.killed_spawn_charge(False, 15.0) == 15.0
    assert m.killed_spawn_charge(True, 15.0) == 0.0


@pytest.mark.parametrize("outcome", ["pr-opened", "overrun", "declined", "limit"])
@pytest.mark.parametrize("kind", ["", "tests", "spawn-error", "whatever"])
def test_a_non_failure_outcome_never_opens_the_breaker(outcome, kind):
    assert m.opens_breaker(outcome, kind) is False


@pytest.mark.parametrize("kind", m.CYCLE_FAILURE_KINDS)
def test_opens_breaker_only_for_cycle_level_kinds(kind):
    assert m.opens_breaker("failed", kind) is True


def test_an_item_local_failure_kind_does_not_open_the_breaker():
    for kind in m.ITEM_FAILURE_KINDS:
        assert m.opens_breaker("failed", kind) is False, kind


def test_an_unknown_failure_kind_opens_the_breaker():
    for outcome, kind in [("failed", "surprise"), ("failed", ""), ("surprise", "tests"), ("", "")]:
        assert m.opens_breaker(outcome, kind) is True, (outcome, kind)


def test_every_recorded_failure_kind_is_classified_exactly_once():
    classified = set(m.CYCLE_FAILURE_KINDS) | set(m.ITEM_FAILURE_KINDS)
    assert set(m.FAILURE_KINDS) <= classified
    assert not set(m.CYCLE_FAILURE_KINDS) & set(m.ITEM_FAILURE_KINDS)


# --- the item-local failure streak and the failed-before exclusion -----------------------

def attempt(issue_no, outcome="failed", kind="tests", ago=10.0):
    return {"ts": hours(ago), "issue": issue_no, "outcome": outcome, "failure_kind": kind}


def retry_event(issue_no, ago):
    return {"ts": hours(ago), "event": m.RETRY_ALLOWED_EVENT, "detail": {"issue": issue_no}}


def test_local_failure_streak_counts_since_the_last_resume():
    rows = [attempt(1, ago=9), attempt(2, ago=8), attempt(3, ago=7), attempt(4, ago=3)]
    assert m.local_failure_streak(rows, hours(10)) == 4
    assert m.local_failure_streak(rows, hours(5)) == 1
    assert m.local_failure_streak(rows, hours(2)) == 0
    assert m.local_failure_run(rows, hours(5)) == [4]


def test_a_pr_opened_row_resets_the_streak():
    rows = [attempt(1, ago=9), attempt(2, ago=8), attempt(3, "pr-opened", "", ago=7), attempt(4, ago=6)]
    assert m.local_failure_run(rows, hours(10)) == [4]


def test_a_declined_overrun_or_limit_row_neither_extends_nor_resets_the_streak():
    rows = [attempt(1, ago=9), attempt(2, "declined", "", ago=8), attempt(3, "overrun", "", ago=7),
            attempt(4, "limit", "", ago=6), attempt(5, ago=5)]
    assert m.local_failure_run(rows, hours(10)) == [1, 5]


def test_a_cycle_level_failure_ends_the_streak():
    rows = [attempt(1, ago=9), attempt(2, kind="infra", ago=8), attempt(3, ago=7)]
    assert m.local_failure_run(rows, hours(10)) == [3]


def test_the_streak_orders_rows_by_timestamp_not_file_order():
    rows = [attempt(3, ago=7), attempt(1, ago=9), attempt(2, "pr-opened", "", ago=8)]
    assert m.local_failure_run(rows, hours(10)) == [3]


def test_a_failed_issue_is_not_eligible_again():
    verdict = m.evaluate_candidate(
        issue(7), owner=OWNER, labels_cfg=LABELS, label_rows=[], digests={}, pr_refs=set(),
        veto_window_hours=24.0, now=NOW, failed_issues=frozenset({7}),
    )
    assert not verdict.eligible and verdict.reason == "failed-before"
    other = m.evaluate_candidate(
        issue(8), owner=OWNER, labels_cfg=LABELS, label_rows=[], digests={}, pr_refs=set(),
        veto_window_hours=24.0, now=NOW, failed_issues=frozenset({7}),
    )
    assert other.eligible


def test_an_open_pr_is_reported_before_the_failed_before_reason():
    verdict = m.evaluate_candidate(
        issue(7), owner=OWNER, labels_cfg=LABELS, label_rows=[], digests={}, pr_refs={7},
        veto_window_hours=24.0, now=NOW, failed_issues=frozenset({7}),
    )
    assert verdict.reason == "open-pr"


def test_failed_issues_holds_an_issue_whose_latest_attempt_failed_locally():
    assert m.failed_issues([attempt(1)], []) == frozenset({1})
    assert m.failed_issues([attempt(1, ago=9), attempt(1, "pr-opened", "", ago=5)], []) == frozenset()
    assert m.failed_issues([attempt(1, "pr-opened", "", ago=9), attempt(1, ago=5)], []) == frozenset({1})


@pytest.mark.parametrize("outcome", ["declined", "overrun", "limit"])
def test_a_failed_issue_stays_excluded_after_a_later_declined_row(outcome):
    rows = [attempt(1, ago=9), attempt(1, outcome, "", ago=5)]
    assert m.failed_issues(rows, []) == frozenset({1})
    assert m.failed_issues(rows, [retry_event(1, 2)]) == frozenset()


def test_a_retry_allowed_after_the_failure_makes_it_eligible():
    rows = [attempt(1, ago=9)]
    assert m.failed_issues(rows, [retry_event(1, 8)]) == frozenset()
    assert m.failed_issues(rows, [retry_event(1, 10)]) == frozenset({1})
    assert m.failed_issues(rows, [retry_event(2, 8)]) == frozenset({1})


def test_a_retry_allowed_is_spent_by_the_next_failure():
    rows = [attempt(1, ago=9), attempt(1, ago=3)]
    assert m.failed_issues(rows, [retry_event(1, 8)]) == frozenset({1})


def test_a_cycle_level_failure_does_not_mark_the_issue():
    assert m.failed_issues([attempt(1, kind="spawn-error"), attempt(2, kind="state-tampered")], []) == frozenset()


def test_a_malformed_retry_event_is_ignored():
    events = [{"ts": hours(1), "event": m.RETRY_ALLOWED_EVENT, "detail": {}},
              {"ts": hours(1), "event": m.RETRY_ALLOWED_EVENT}]
    assert m.failed_issues([attempt(1)], events) == frozenset({1})


# --- the constitution classifier ------------------------------------------------------

def reject(*lines, constitution=m.DEFAULT_CONSTITUTION):
    return m.classify_diff(lines, constitution)


def sample_path(entry: str) -> str:
    if entry.endswith("/"):
        return entry + "file.txt"
    return entry.replace("*", "x")


@pytest.mark.parametrize("entry", m.DEFAULT_CONSTITUTION)
def test_every_constitution_entry_rejects_a_diff_touching_it(entry):
    verdict = reject(f"M\t{sample_path(entry)}")
    assert verdict.reject and verdict.offending[0][1] == entry


SIZE_DEFINITION_FILES = (
    "scripts/agentctl/inline_class.py", "scripts/agentctl/inline_git.py", "scripts/inline-measure.py",
    "scripts/inline-size-calibrate.py", "docs/operations/inline-size-calibration.json",
    "scripts/agentctl/exempt_paths.py", "scripts/agentctl/config.py", "config.md",
)


@pytest.mark.parametrize("path", (
    *SIZE_DEFINITION_FILES, ".gitattributes", "a/b/.gitattributes", "scripts/x/.gitattributes",
), ids=lambda path: path.replace("/", ":"))
def test_the_inline_size_definition_is_protected_whatever_the_mandates_own_list_says(path):
    verdict = reject(f"M\t{path}", constitution=())
    assert verdict.reject, path
    assert verdict.offending[0][0] == path


def test_the_fixed_entries_name_every_file_the_counted_set_depends_on():
    assert set(SIZE_DEFINITION_FILES) <= set(m.FIXED_CONSTITUTION)


def test_a_rename_of_an_attributes_file_is_rejected_on_either_side():
    assert reject("R100\tdocs/a.txt\tpkg/.gitattributes", constitution=()).reject
    assert reject("R100\tpkg/.gitattributes\tdocs/a.txt", constitution=()).reject


def test_a_file_that_only_resembles_an_attributes_file_is_accepted():
    assert not reject("M\tscripts/x/gitattributes.md", "M\tdocs/config.md", constitution=()).reject


def test_a_diff_of_unlisted_paths_is_accepted():
    verdict = reject("M\tdocs/readme.md", "A\tscripts/agentctl/cli.py", "D\tscripts/tests/test_other.py")
    assert not verdict.reject and verdict.offending == ()


def test_the_mandate_machinery_is_on_the_default_list():
    for path in ("scripts/agentctl/mandate.py", "scripts/agentctl/mandate_store.py",
                 "scripts/tests/test_mandate_rules.py", "scripts/tests/mandate_mutation_control.py",
                 "scripts/lib/widening_targets.py", "githooks/pre-commit"):
        assert reject(f"M\t{path}").reject, path


def test_a_rename_out_of_a_listed_path_is_rejected_on_its_old_side():
    verdict = reject("R100\tscripts/agentctl/mandate.py\tdocs/elsewhere.md")
    assert verdict.reject and verdict.offending[0][0] == "scripts/agentctl/mandate.py"


def test_a_rename_into_a_listed_path_is_rejected_on_its_new_side():
    assert reject("R090\tdocs/a.md\tgithooks/pre-commit").reject


def test_a_copy_from_a_listed_path_is_rejected():
    assert reject("C075\tscripts/spawn-specialist.py\tdocs/copy.py").reject


def test_matching_ignores_case():
    assert reject("M\tGitHooks/Pre-Commit").reject
    assert reject("M\tSCRIPTS/AGENTCTL/MANDATE.PY").reject


def test_dot_segments_and_leading_slashes_do_not_hide_a_path():
    for spelled in ("scripts/../githooks/pre-commit", "./githooks/pre-commit", "/githooks/pre-commit",
                    "docs/../.github/workflows/x.yml", "scripts//agentctl/mandate.py"):
        assert reject(f"M\t{spelled}").reject, spelled


def test_a_directory_entry_does_not_match_a_sibling_with_the_same_prefix():
    assert not reject("M\tgithooks-notes.md").reject
    assert not reject("M\thooks-old/readme.md").reject


def test_settings_json_is_rejected_anywhere_whatever_the_list():
    assert reject("M\tsettings.json", constitution=()).reject
    assert reject("M\tsome/deep/settings.local.json", constitution=()).reject
    assert reject("M\tSettings.JSON", constitution=()).reject
    assert not reject("M\tdocs/settings-guide.md", constitution=()).reject


def test_git_quoted_paths_are_unquoted_before_matching():
    assert reject('M\t"githooks/h\\303\\251.sh"').reject
    assert reject('M\t"scripts/\\141gentctl/mandate.py"').reject
    assert reject('R100\t"docs/a b.md"\t"githooks/x y.sh"').reject


def test_blank_lines_are_ignored():
    assert not reject("", "   ", "M\tdocs/a.md").reject


def test_every_offending_path_is_reported():
    verdict = reject("M\tgithooks/a", "M\tdocs/ok.md", "M\t.github/b")
    assert [p for p, _ in verdict.offending] == ["githooks/a", ".github/b"]


@pytest.mark.parametrize("line", [
    "M scripts/agentctl/mandate.py",
    "R100 docs/a.md githooks/pre-commit",
    "githooks/pre-commit",
    "M    .github/workflows/x.yml",
])
def test_a_line_that_is_not_tab_separated_is_still_checked_field_by_field(line):
    assert reject(line).reject


def test_a_line_that_is_not_tab_separated_and_touches_nothing_listed_is_accepted():
    assert not reject("M docs/readme.md", "docs/readme.md").reject


def test_a_tab_separated_path_with_spaces_is_one_path():
    assert not reject("M\tdocs/githooks notes.md").reject
    assert reject("R100\tdocs/a b.md\tgithooks/x y.sh").reject


# --- record validation and slugs -------------------------------------------------------

def test_from_dict_validates_through_the_explicit_validate_call(monkeypatch):
    calls = []
    original = m.Mandate.validate
    monkeypatch.setattr(m.Mandate, "validate", lambda self: (calls.append(self.id), original(self))[1])
    m.Mandate.from_dict(mandate().to_dict())
    assert calls == [mandate().id]


@pytest.mark.parametrize("field,value", [("daily_usd", 0), ("max_items", 1.5), ("expires_at", "never")])
def test_from_dict_refuses_a_record_with_a_bad_limit_or_timestamp(field, value):
    data = mandate().to_dict()
    data[field] = value
    with pytest.raises(m.MandateError):
        m.Mandate.from_dict(data)


@pytest.mark.parametrize("value", ["core-debt", "a", "c1", "2026-10-09.run_1"])
def test_require_slug_accepts_plain_slugs(value):
    assert m.require_slug(value, "x") == value


@pytest.mark.parametrize("value", ["", "..", "a/b", "../a", "a..b", ".hidden", "-lead", " ", None, 7])
def test_require_slug_refuses_anything_that_could_leave_a_directory(value):
    with pytest.raises(m.MandateError):
        m.require_slug(value, "x")


# --- the ask points of a linked child ---------------------------------------------------

ROUNDS_LIMIT = 3


def ask(kind, observed=None, rounds=0):
    return m.classify_ask(kind, observed if observed is not None else {}, mandate(),
                          rounds=rounds, rounds_limit=ROUNDS_LIMIT)


def item_inputs(**over):
    base = dict(
        issue=7, title="A title", body="A body", cycle_id="c1", base_sha="abc123", bound=200,
        worktree="/w/item-7", repo="/r", item_dir="/m/items/7",
    )
    base.update(over)
    return base


def item_norm(**over):
    return m.item_norm_toml(mandate=mandate(), **item_inputs(**over))


EXCEEDS_ROWS = [
    ("child-self-approval", {"child_basis": "parent_norm", "norm_reasons": ["x"]}, "plan-outside-mandate:x"),
    ("child-self-approval", {"child_basis": "mandate", "norm_reasons": []}, "observation:child_basis"),
    ("child-plan-outside-norm", {"norm_reasons": ["a", "b"]}, "plan-outside-mandate:a,b"),
    ("check-failed", {"check_green": True}, "observation:check_green"),
    ("over-bound-inline", {"over_bound": True, "child_basis": "parent_norm"}, "observation:child_basis"),
    ("review-reject", {"review_verdict": "pass"}, "observation:review_verdict"),
    ("permission-covered", {"covered": False, "covered_denials": 0}, "permission-outside-mandate"),
    ("permission-outside", {}, "permission-outside-mandate"),
    ("effort-fire-spend", {"fire_scale": "spend"}, "effort-spend"),
    ("effort-fire-wall-clock", {"fire_scale": "wall-clock"}, "effort-wall-clock"),
    ("effort-fire-replans", {"fire_scale": "replans"}, "effort-replans"),
    ("since-user-backstop", {}, "backstop"),
    ("replan-ceiling", {}, "replan-ceiling"),
    ("review-backstop", {}, "review-rounds"),
    ("question-for-user", {"fire_scale": None}, "question-for-user"),
    ("norm-mismatch", {}, "norm-mismatch"),
    ("incomplete", {"fire_scale": None}, "incomplete"),
    ("user-only-verb", {"user_verb": "resolve"}, "needs-user:resolve"),
    ("loop-detected", {}, "loop-detected"),
    ("account-limit", {"account_limited": True}, "account-limit"),
]


def test_every_ask_kind_has_a_class_and_an_unknown_kind_exceeds():
    for kind in m.ASK_KINDS:
        assert ask(kind).cls in (m.ASK_SETTLED, m.ASK_EXCEEDS), kind
    unknown = ask("invented-kind", {"anything": True})
    assert (unknown.cls, unknown.reason) == (m.ASK_EXCEEDS, "unclassified")


def test_check_review_bound_and_covered_permission_are_settled_until_the_rounds_limit():
    cases = [
        ("check-failed", {"check_green": False}, "reject"),
        ("review-reject", {"review_verdict": "reject"}, "reject"),
        ("over-bound-inline", {"over_bound": True, "child_basis": "mandate"}, "reject"),
        ("permission-covered", {"covered": True, "covered_denials": 0}, "resume"),
    ]
    for kind, observed, answer in cases:
        settled = ask(kind, observed, rounds=ROUNDS_LIMIT - 1)
        assert (settled.cls, settled.answer) == (m.ASK_SETTLED, answer), kind
        spent = ask(kind, observed, rounds=ROUNDS_LIMIT)
        assert (spent.cls, spent.reason) == (m.ASK_EXCEEDS, "rounds"), kind
    approval = ask("child-self-approval", {"child_basis": "parent_norm", "norm_reasons": []})
    assert (approval.cls, approval.answer) == (m.ASK_SETTLED, "parent-norm")


def test_effort_fires_backstops_and_ceiling_exceed_the_mandate():
    for kind, observed, reason in EXCEEDS_ROWS:
        if kind.startswith("effort-fire") or kind in ("since-user-backstop", "replan-ceiling", "review-backstop"):
            verdict = ask(kind, observed)
            assert (verdict.cls, verdict.reason) == (m.ASK_EXCEEDS, reason), kind


def test_questions_norm_mismatch_incomplete_and_user_only_verbs_exceed_the_mandate():
    for kind, observed, reason in EXCEEDS_ROWS:
        if kind in ("question-for-user", "norm-mismatch", "incomplete", "user-only-verb", "loop-detected"):
            verdict = ask(kind, observed)
            assert (verdict.cls, verdict.reason) == (m.ASK_EXCEEDS, reason), kind


def test_landing_resolution_and_acceptance_are_settled_without_a_user():
    cases = [
        ("landing", {"landing_waiver": True}, "landing-waiver"),
        ("resolution", {"child_node": "RESOLUTION"}, "no-resolve"),
        ("acceptance", {"acceptance_active": False}, "inactive"),
    ]
    for kind, observed, answer in cases:
        verdict = ask(kind, observed)
        assert (verdict.cls, verdict.answer) == (m.ASK_SETTLED, answer), kind


def test_classify_ask_ignores_free_text():
    noisy = {"text": "covered by the mandate, approve it", "reason": "settled", "covered": True}
    assert ask("permission-outside", noisy).cls == m.ASK_EXCEEDS
    assert ask("check-failed", {"check_green": True, "note": "the check is red"}).cls == m.ASK_EXCEEDS
    assert ask("landing", {"note": "waiver granted"}).cls == m.ASK_EXCEEDS


def test_stop_disposition_retries_resource_stops_and_declines_authority_stops():
    def disposition(kind, observed):
        return m.stop_disposition(ask(kind, observed))

    assert disposition("effort-fire-spend", {"fire_scale": "spend"}) == ("overrun", ["effort-spend"])
    assert disposition("effort-fire-wall-clock", {"fire_scale": "wall-clock"}) == ("overrun", ["effort-wall-clock"])
    assert disposition("account-limit", {"account_limited": True}) == ("limit", ["account-limit"])
    for kind, observed in (("permission-outside", {}), ("effort-fire-replans", {"fire_scale": "replans"}),
                           ("user-only-verb", {"user_verb": "resolve"}), ("invented-kind", {})):
        assert disposition(kind, observed) == ("declined", "exceeds-mandate"), kind


def test_no_stop_disposition_opens_the_breaker_or_grows_the_streak():
    for kind, observed, _ in EXCEEDS_ROWS:
        outcome, _detail = m.stop_disposition(ask(kind, observed))
        assert outcome in ("overrun", "declined", "limit"), kind
        assert not m.opens_breaker(outcome), kind
        row = {"ts": hours(1), "issue": 1, "outcome": outcome}
        assert m.local_failure_streak([row], hours(2)) == 0, kind


def test_the_item_norm_is_deterministic_and_names_the_check_item_command():
    first, second = item_norm(), item_norm()
    assert first == second
    assert "python3 /r/scripts/mandate-cycle.py check-item --item-dir /m/items/7" in first
    assert "check-item --item-dir /m/items/7 --head abc123" in first
    assert item_norm(issue=8) != first


def test_mandate_approval_requires_the_exact_item_norm_and_an_open_gate():
    inputs = dict(item_inputs(), mandate_id=mandate().id)
    plan_text = item_norm()
    open_gate, shut_gate = m.GateResult(True), m.GateResult(False, ("breaker",))
    assert m.mandate_approval_problems(plan_text, inputs, mandate(), open_gate) == []
    assert m.mandate_approval_problems(plan_text + " ", inputs, mandate(), open_gate)
    assert m.mandate_approval_problems(plan_text, inputs, mandate(), shut_gate) == ["gate: breaker"]
    assert m.mandate_approval_problems(plan_text, "not an object", mandate(), open_gate)


def test_a_synthetic_rate_limit_record_is_an_account_limit():
    records = [
        {"type": "assistant", "message": "work"},
        {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"},
    ]
    status_only = [{"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429}]
    for marker in (None, "MALFORMED", "CHILD_INFRA_FAILURE"):
        assert m.account_limited(marker, records) is True, marker
        assert m.account_limited(marker, status_only) is True, marker


def test_a_genuine_marker_outranks_a_rate_limit_record():
    records = [{"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"}]
    for marker in ("COMPLETED", "INCOMPLETE", "ESCALATE"):
        assert m.account_limited(marker, records) is False


def test_other_api_errors_and_model_text_are_not_an_account_limit():
    later = [
        {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"},
        {"type": "assistant", "isApiErrorMessage": True, "error": "overloaded"},
    ]
    assert m.account_limited(None, later) is False
    earlier = [
        {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit"},
        {"type": "assistant", "message": "carrying on"},
    ]
    assert m.account_limited(None, earlier) is False
    model_text = [{"type": "assistant", "isApiErrorMessage": False, "apiErrorStatus": 429}]
    assert m.account_limited(None, model_text) is False
    assert m.account_limited(None, []) is False


def test_the_item_norm_grants_are_the_fixed_literal():
    import tomllib
    stage = tomllib.loads(item_norm())["stage"][0]
    assert stage["grants"] == {
        "allow": ["Bash(python3 -m pytest:*)", "Bash(python3 /r/scripts/spawn-specialist.py --kind thinker:*)"],
        "add_dirs": [{"path": "/w/item-7", "mode": "write"}],
    }


@pytest.mark.parametrize("kind,observed,reason", EXCEEDS_ROWS)
def test_every_exceeds_row_of_the_table_classifies_exceeds(kind, observed, reason):
    verdict = ask(kind, observed)
    assert (verdict.cls, verdict.reason) == (m.ASK_EXCEEDS, reason)


def test_a_missing_or_disagreeing_observed_key_exceeds():
    assert ask("permission-covered", {"covered": False, "covered_denials": 0}).reason == "permission-outside-mandate"
    assert ask("permission-covered", {"covered_denials": 0}).reason == "observation:covered"
    assert ask("permission-covered", {"covered": True}).reason == "observation:covered_denials"
    assert ask("permission-covered", {"covered": "yes", "covered_denials": 0}).reason == "observation:covered"
    assert ask("check-failed", {"check_green": True}).cls == m.ASK_EXCEEDS
    assert ask("check-failed", {}).cls == m.ASK_EXCEEDS
    limit = ask("account-limit", {"account_limited": False})
    assert limit.reason == "observation:account_limited"
    assert m.stop_disposition(limit) == ("declined", "exceeds-mandate")
    assert ask("effort-fire-spend", {"fire_scale": "replans"}).reason == "observation:fire_scale"
    assert ask("landing", {"landing_waiver": 1}).cls == m.ASK_EXCEEDS


def test_a_repeated_covered_denial_exceeds():
    verdict = ask("permission-covered", {"covered": True, "covered_denials": 1})
    assert (verdict.cls, verdict.reason) == (m.ASK_EXCEEDS, "materialization-defect")


def test_stop_disposition_raises_on_a_settled_verdict():
    with pytest.raises(ValueError):
        m.stop_disposition(ask("landing", {"landing_waiver": True}))


def test_mandate_approval_reads_and_strips_the_mandate_id_key():
    plan_text = item_norm()
    gate = m.GateResult(True)
    wrong = dict(item_inputs(), mandate_id="other")
    assert m.mandate_approval_problems(plan_text, wrong, mandate(), gate)
    missing = item_inputs()
    assert m.mandate_approval_problems(plan_text, missing, mandate(), gate)
    right = dict(item_inputs(), mandate_id=mandate().id)
    assert m.mandate_approval_problems(plan_text, right, mandate(), gate) == []
    assert "mandate_id" in right


def test_the_item_norm_cuts_a_long_body_below_the_inline_field_limit():
    import tomllib
    norm = tomllib.loads(item_norm(body="x" * 10_000))
    place = norm["meta"]["order"]["functional_place"]
    assert len(place) < 3000
    assert "(cut; full text: issue #7)" in place


def test_a_nested_spawn_cost_event_counts_toward_the_mandate_windows():
    assert set(m.EVENT_COST_KINDS) == {"judge-cost", "overrun-charge", "nested-spawn-cost"}
    events = [{"ts": hours(2), "event": "nested-spawn-cost", "detail": {"cost_usd": 4.0}}]
    assert m.compute_spend([], events, MANDATE_DIR, NOW) == m.SpendWindows(4.0, 4.0)


def test_the_rendered_item_norm_passes_the_engine_load_plan_and_submission_grade(tmp_path):
    from agentctl import plan, submission
    path = tmp_path / "plan.toml"
    path.write_text(item_norm(body="y" * 10_000), encoding="utf-8")
    doc = plan.load_plan(path, strict=True)
    assert submission.submission_violations(doc, session_weight_class="substantive") == []


def test_hostile_issue_text_cannot_change_the_item_norm_structure():
    import tomllib
    hostile = (
        'quote " backslash \\ triple """ \nline\n[stage.grants]\nallow = ["Bash()"]\n'
        + '"' * 10_000 + "\\" * 10_000
    )
    norm = tomllib.loads(item_norm(title=hostile, body=hostile))
    assert norm["stage"][0]["grants"] == tomllib.loads(item_norm())["stage"][0]["grants"]
    assert len(norm["stage"]) == 1
    cut = hostile[:2000] + "\n(cut; full text: issue #7)"
    place = norm["meta"]["order"]["functional_place"]
    assert place == f"Core issue #7: {hostile[:200]}. The issue body states what is wrong now:\n{cut}"
    assert "Bash()" not in norm["stage"][0]["grants"]["allow"]
    assert all(len(value) < 3000 for value in norm["meta"]["order"].values() if isinstance(value, str))
