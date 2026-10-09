"""Rules of the standing mandate (`agentctl.mandate`): every bound is a pure function.

Each catalogue entry in `mandate_mutation_control.py` names the tests here (or in
test_mandate_cli.py) that must go red when its production line is broken.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone

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


def test_overrun_is_strictly_beyond_either_cap():
    assert not m.check_overrun(15.0, 125.0, usd_cap=15.0, minutes_cap=125.0).overrun
    assert m.check_overrun(15.01, 1.0, usd_cap=15.0, minutes_cap=125.0).reasons == ("item-usd-cap",)
    assert m.check_overrun(1.0, 125.5, usd_cap=15.0, minutes_cap=125.0).reasons == ("item-minutes-cap",)
    both = m.check_overrun(20.0, 200.0, usd_cap=15.0, minutes_cap=125.0)
    assert both.overrun and len(both.reasons) == 2


def test_a_killed_spawn_without_a_cost_row_is_charged_the_whole_cap():
    assert m.killed_spawn_charge(False, 15.0) == 15.0
    assert m.killed_spawn_charge(True, 15.0) == 0.0


@pytest.mark.parametrize("outcome,opens", [
    ("pr-opened", False), ("overrun", False), ("declined", False), ("limit", False),
    ("failed", True), ("surprise", True), ("", True),
])
def test_only_a_failed_or_unknown_outcome_opens_the_breaker(outcome, opens):
    assert m.opens_breaker(outcome) is opens


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
