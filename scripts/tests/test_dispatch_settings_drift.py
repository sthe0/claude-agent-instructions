"""cmd_dispatch's settings_drift detection, once-scoped runtime-grant consumption,
and the small pure helpers around them (`_parse_transcript_path`, `_rekey_runtime_grants`)
-- all added by the commit that wired grant coverage into dispatch (grants:
fix diff_plans growth escalation; wire dispatch settings_drift/transcript/
once-consumption) with no test coverage of its own.

Covers: a spawned child leaving the live settings*.json documents byte-identical
records no drift; a child that (illegitimately) changes one records exactly that
path; a successful dispatch consumes `scope: "once"` runtime grants but leaves
`scope: "stage"` ones and other stages' entries alone; a `CHILD_INFRA_FAILURE`
marker consumes nothing (the launch never counted); transcript-path parsing off
spawn-specialist.py's own stderr announcement; and runtime-grant re-keying by
stage title across a stage-index renumbering.
"""
from __future__ import annotations

import json
from argparse import Namespace

from agentctl import cli
from agentctl.dispatch import RunResult


def ns(**kw):
    return Namespace(**kw)


def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


# --- settings_drift --------------------------------------------------------------

def test_cmd_dispatch_no_settings_drift_when_child_cwd_settings_unchanged(store, fixtures_dir, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}', encoding="utf-8")

    sid = "settings-drift-none"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = str(tmp_path)
    state.repo_root = str(tmp_path)
    store.save(state)

    def runner(argv, cwd=None):
        return RunResult(0, stdout="COMPLETED: done\n")

    directive = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                                    dry_run=False), store=store, runner=runner)
    assert "settings_drift" not in directive.data
    assert store.load(sid).settings_drift == []


def test_cmd_dispatch_records_settings_drift_when_child_cwd_settings_changes(store, fixtures_dir, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.local.json"
    settings_path.write_text('{"permissions": {"allow": ["Bash(ls:*)"]}}', encoding="utf-8")

    sid = "settings-drift-changed"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.delivery_worktree = str(tmp_path)
    state.repo_root = str(tmp_path)
    store.save(state)

    def runner(argv, cwd=None):
        # A spawned child is never granted write access to a settings document --
        # this simulates the untrusted-write case the drift check exists to catch.
        settings_path.write_text('{"permissions": {"allow": ["Bash(rm -rf /:*)"]}}', encoding="utf-8")
        return RunResult(0, stdout="COMPLETED: done\n")

    directive = cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                                    dry_run=False), store=store, runner=runner)
    assert "settings_drift" in directive.data
    assert str(settings_path) in directive.data["settings_drift"]["changed"]
    persisted = store.load(sid).settings_drift
    assert len(persisted) == 1
    assert persisted[0]["stage_index"] == 1
    assert str(settings_path) in persisted[0]["changed"]


# --- once-scoped runtime grant consumption ---------------------------------------

def test_cmd_dispatch_consumes_once_scoped_runtime_grant_on_completed(store, fixtures_dir):
    sid = "once-grant-consumed"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(rare-cmd:*)", "provenance": "runtime", "scope": "once", "consumed": False},
        {"rule": "Bash(other-cmd:*)", "provenance": "runtime", "scope": "stage", "consumed": False},
    ]
    store.save(state)

    def runner(argv, cwd=None):
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    entries = store.load(sid).runtime_grants["1"]
    once_entry = next(e for e in entries if e["scope"] == "once")
    stage_entry = next(e for e in entries if e["scope"] == "stage")
    assert once_entry["consumed"] is True
    assert stage_entry["consumed"] is False


def test_cmd_dispatch_does_not_consume_once_grant_on_child_infra_failure(store, fixtures_dir):
    # The launch never meaningfully ran against the grant -- burning a
    # once-scoped approval here would waste it on a retry that gets no
    # benefit from having used it.
    sid = "once-grant-not-consumed-infra-failure"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(rare-cmd:*)", "provenance": "runtime", "scope": "once", "consumed": False},
    ]
    store.save(state)

    def runner(argv, cwd=None):
        return RunResult(1, stdout="CHILD_INFRA_FAILURE: lost connection\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    entries = store.load(sid).runtime_grants["1"]
    assert entries[0]["consumed"] is False


# --- _parse_transcript_path -------------------------------------------------------

def test_parse_transcript_path_reads_the_announcer_line():
    stderr = "some preamble\nspawn-specialist: transcript=/home/x/.claude/projects/p/t.jsonl\n"
    assert cli._parse_transcript_path(stderr) == "/home/x/.claude/projects/p/t.jsonl"


def test_parse_transcript_path_treats_not_found_sentinel_as_none():
    stderr = "spawn-specialist: transcript=<not-found-within-10s>\n"
    assert cli._parse_transcript_path(stderr) is None


def test_parse_transcript_path_returns_none_when_line_absent():
    assert cli._parse_transcript_path("nothing relevant here\n") is None
    assert cli._parse_transcript_path("") is None


def test_parse_transcript_path_picks_the_last_announcement_line():
    # A retried/re-announced transcript line must not be shadowed by an
    # earlier, now-stale one -- the function scans in reverse.
    stderr = (
        "spawn-specialist: transcript=/tmp/first.jsonl\n"
        "spawn-specialist: transcript=/tmp/second.jsonl\n"
    )
    assert cli._parse_transcript_path(stderr) == "/tmp/second.jsonl"


# --- _rekey_runtime_grants ---------------------------------------------------------

class _FakeStageDoc:
    def __init__(self, stages):
        self.stages = stages


class _FakeStage:
    def __init__(self, index, title):
        self.index = index
        self.title = title


def test_rekey_runtime_grants_moves_entry_to_the_single_matching_title(store, fixtures_dir):
    sid = "rekey-single-match"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Renamed stage"},
    ]
    doc = _FakeStageDoc([_FakeStage(2, "Renamed stage"), _FakeStage(3, "Other stage")])
    cli._rekey_runtime_grants(state, doc)
    assert "1" not in state.runtime_grants
    assert state.runtime_grants["2"][0]["rule"] == "Bash(x:*)"


def test_rekey_runtime_grants_drops_entry_when_title_now_ambiguous(store, fixtures_dir):
    sid = "rekey-ambiguous"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Shared title"},
    ]
    doc = _FakeStageDoc([_FakeStage(2, "Shared title"), _FakeStage(3, "Shared title")])
    cli._rekey_runtime_grants(state, doc)
    assert state.runtime_grants == {}


def test_rekey_runtime_grants_drops_entry_when_title_no_longer_present(store, fixtures_dir):
    sid = "rekey-gone"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Vanished stage"},
    ]
    doc = _FakeStageDoc([_FakeStage(2, "Some other stage")])
    cli._rekey_runtime_grants(state, doc)
    assert state.runtime_grants == {}


# --- _refresh_runtime_grant_titles -------------------------------------------------
#
# Unlike `_rekey_runtime_grants` (used only on `approve`, after a substantive
# replan may have renumbered stage indices, so it must re-key by title match),
# `_refresh_runtime_grant_titles` is used by the "no_change"/"refinement"
# replan branches, which never renumber -- it just overwrites `stage_title`
# in place for whatever entries already sit at the given index.

def test_refresh_runtime_grant_titles_overwrites_title_at_the_given_index(store, fixtures_dir):
    sid = "refresh-titles-basic"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Old title"},
    ]
    cli._refresh_runtime_grant_titles(state, 1, "New title")
    assert state.runtime_grants["1"][0]["stage_title"] == "New title"


def test_refresh_runtime_grant_titles_updates_every_entry_at_the_index(store, fixtures_dir):
    sid = "refresh-titles-multiple-entries"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Old title"},
        {"rule": "Bash(y:*)", "provenance": "runtime", "stage_title": "Old title"},
    ]
    cli._refresh_runtime_grant_titles(state, 1, "New title")
    assert all(e["stage_title"] == "New title" for e in state.runtime_grants["1"])


def test_refresh_runtime_grant_titles_leaves_other_stage_indices_untouched(store, fixtures_dir):
    sid = "refresh-titles-other-index-untouched"
    _to_executing(store, sid, fixtures_dir)
    state = store.load(sid)
    state.runtime_grants["1"] = [
        {"rule": "Bash(x:*)", "provenance": "runtime", "stage_title": "Stage one"},
    ]
    state.runtime_grants["2"] = [
        {"rule": "Bash(y:*)", "provenance": "runtime", "stage_title": "Stage two"},
    ]
    cli._refresh_runtime_grant_titles(state, 1, "Renamed stage one")
    assert state.runtime_grants["1"][0]["stage_title"] == "Renamed stage one"
    assert state.runtime_grants["2"][0]["stage_title"] == "Stage two"
