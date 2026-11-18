"""`cmd_reset` reopening a RESOLVED task reports a session evidence directory that is gone.

The `core-session-state` reaper removes the evidence of a resolved session after its retention, so
the reopen of such a session meets a directory the earlier stages' logs lived in no longer there.
The reopen still proceeds; the Directive says so and names the path, instead of the loss showing up
later as a missing log.
"""
import os
import shutil
from pathlib import Path

from agentctl import cli
from agentctl.state import Node
from agentctl.store import safe_session_id
from test_resolved_reentry_gate import _reset_ns, _to_resolved

REASON = "the confirmed fix missed the cron path"


def session_evidence_dir(sid):
    return Path(os.environ["AGENTCTL_EVIDENCE_ROOT"]) / safe_session_id(sid)


def test_a_reopen_whose_evidence_directory_is_gone_reports_the_path_and_still_reopens(store, venued_fixtures_dir):
    sid = "ev1"
    _to_resolved(store, venued_fixtures_dir, sid)
    evidence = session_evidence_dir(sid)
    shutil.rmtree(evidence, ignore_errors=True)
    result = cli.cmd_reset(_reset_ns(sid, reopen_reason=REASON), store=store)
    assert result.ok is True and result.action == "classify"
    assert result.data["evidence_absent"] == str(evidence)
    assert str(evidence) in result.detail
    assert result.data["resolved_reentry_count"] == 1
    assert store.load(sid).node == Node.CLASSIFIED.value


def test_a_reopen_whose_evidence_directory_exists_reports_nothing_about_it(store, venued_fixtures_dir):
    sid = "ev2"
    _to_resolved(store, venued_fixtures_dir, sid)
    session_evidence_dir(sid).mkdir(parents=True, exist_ok=True)
    result = cli.cmd_reset(_reset_ns(sid, reopen_reason=REASON), store=store)
    assert result.ok is True
    assert "evidence_absent" not in result.data
    assert "evidence" not in result.detail


def test_a_reset_that_is_not_a_reopen_does_not_look_for_evidence(store, venued_fixtures_dir):
    sid = "ev3"
    _to_resolved(store, venued_fixtures_dir, sid)
    shutil.rmtree(session_evidence_dir(sid), ignore_errors=True)
    result = cli.cmd_reset(_reset_ns(sid, task="another-task"), store=store)
    assert result.ok is True
    assert "evidence_absent" not in result.data


def test_a_refused_reopen_changes_nothing_and_reports_no_evidence(store, venued_fixtures_dir):
    sid = "ev4"
    _to_resolved(store, venued_fixtures_dir, sid)
    shutil.rmtree(session_evidence_dir(sid), ignore_errors=True)
    result = cli.cmd_reset(_reset_ns(sid), store=store)
    assert result.ok is False
    assert "evidence_absent" not in result.data
    assert store.load(sid).node == Node.RESOLVED.value
