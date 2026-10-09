"""The resolve-time resolution-ask gate: `resolve --quality` needs an answered
AskUserQuestion carrying RESOLUTION_ASK_MARKER that was asked AFTER the session's
verify-final stamp.

Both ends of the ordering are machine records — the engine's `verify_final` event
(`at`, an epoch float) and the harness transcript — so every test builds a synthetic
transcript at a known offset from the real stamp and resolves against it. The transcript
is found the way production finds it (session id under a projects root), never handed in.
"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentctl import cli, gates, state as state_module
from agentctl.state import Node
from conftest import STAGE_OBSERVATIONS
from lib import transcript_turns

SID = "ask-sess"
# The literal is pinned here, and the engine's constant is asserted equal to it below.
RESOLUTION_ASK_MARKER = "[resolution-ask]"


def ns(**kw):
    return Namespace(**kw)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _gate_on_with_isolated_transcripts(_resolution_ask_gate_off_by_default, monkeypatch, tmp_path):
    """Runs after the suite-wide force-off fixture so it can undo it, and points the
    transcript lookup at an empty projects root under tmp_path."""
    monkeypatch.delenv("AGENTCTL_RESOLUTION_ASK_GATE", raising=False)
    home = tmp_path / "home"
    (home / "projects" / "proj").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)


def transcript_path(tmp_path: Path, sid: str = SID) -> Path:
    return tmp_path / "home" / "projects" / "proj" / f"{sid}.jsonl"


def ask_entry(call_id: str, ts: float, *, label: str = "Resolve", description: str = "",
              question: str = "Rate 1-5 and resolve?") -> dict:
    return {
        "type": "assistant", "timestamp": iso(ts),
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": call_id, "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": question, "header": "Resolution",
                "options": [{"label": label, "description": description},
                            {"label": "Not yet", "description": "keep working"}],
            }]},
        }]},
    }


def answer_entry(call_id: str, ts: float, *, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "Resolve"}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "timestamp": iso(ts),
            "message": {"role": "user", "content": [block]}}


def write_transcript(tmp_path: Path, entries: list[dict], sid: str = SID) -> Path:
    path = transcript_path(tmp_path, sid)
    path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return path


def to_resolution(store, sid: str = SID):
    fixtures = Path(__file__).parent / "fixtures"
    cli.cmd_start(ns(session=sid, task="demo", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(
        session=sid, chat=False, changed_lines=200, files=5, wall_clock_min=60,
        tracker_key=None, architectural=False, external_effect=False,
        new_dependency=False, public_api_change=False,
    ), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=str(fixtures / "plan_two_stage.toml")), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    for observation in STAGE_OBSERVATIONS[:2]:
        cli.cmd_next_stage(ns(session=sid), store=store)
        cli.cmd_record_result(ns(session=sid, status="passed", actual="ok",
                                 control="reviewed: ok", observation=observation), store=store)
    directive = cli.cmd_verify_final(ns(session=sid), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="searched", note=""), store=store)
    cli.cmd_plugin_record(ns(session=sid, plugin="experience", phase="skipped",
                             note="test fixture, nothing to record"), store=store)
    state = store.load(sid)
    assert state.node == Node.RESOLUTION.value
    return directive


def verified_at(store, sid: str = SID) -> float:
    stamps = [h for h in store.load(sid).history if h["event"] == "verify_final"]
    return stamps[-1]["at"]


def resolve(store, sid: str = SID, **kw):
    return cli.cmd_resolve(ns(session=sid, by="user", quality=5, quality_by="user-confirmed",
                              quality_note=None, **kw), store=store)


def ask_blockers(d):
    return [b for b in d.data["blockers"] if b.startswith(cli.RESOLUTION_ASK_BLOCKER_PREFIX)]


def quality_rows():
    path = cli.TASK_QUALITY_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_verify_final_directive_names_the_marker_and_logs_a_precise_stamp(store):
    d = to_resolution(store)
    assert state_module.RESOLUTION_ASK_MARKER == RESOLUTION_ASK_MARKER
    assert d.data["resolution_ask_marker"] == RESOLUTION_ASK_MARKER
    assert RESOLUTION_ASK_MARKER in d.detail
    at = verified_at(store)
    assert isinstance(at, float) and at > 1e9


def test_no_ask_at_all_is_refused(store, tmp_path):
    to_resolution(store)
    write_transcript(tmp_path, [{"type": "user", "timestamp": iso(verified_at(store) + 1),
                                 "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}])

    d = resolve(store)
    assert d.ok is False
    assert any("no AskUserQuestion carrying the marker" in b for b in ask_blockers(d))
    assert quality_rows() == []
    assert store.load(SID).node == Node.RESOLUTION.value


def test_marked_ask_before_verify_final_is_refused(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at - 5, label=f"Resolve {RESOLUTION_ASK_MARKER}"),
                                answer_entry("a1", at - 4)])

    d = resolve(store)
    assert d.ok is False
    assert any("predates the latest verify-final" in b for b in ask_blockers(d))


def test_the_latest_verify_final_stamp_is_the_one_an_ask_must_follow(store, tmp_path):
    to_resolution(store)
    first = verified_at(store)
    state = store.load(SID)
    state.log("verify_final", at=first + 10)
    store.save(state)
    write_transcript(tmp_path, [ask_entry("a1", first + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}"),
                                answer_entry("a1", first + 6)])

    d = resolve(store)
    assert d.ok is False
    assert any("predates the latest verify-final" in b for b in ask_blockers(d))


def test_marked_ask_with_an_unreadable_timestamp_is_refused_with_its_own_message(store, tmp_path):
    to_resolution(store)
    ask = ask_entry("a1", verified_at(store) + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}")
    ask.pop("timestamp")
    write_transcript(tmp_path, [ask, answer_entry("a1", verified_at(store) + 6)])

    d = resolve(store)
    assert d.ok is False
    blockers = ask_blockers(d)
    assert any("missing or unparsable timestamp" in b for b in blockers)
    assert not any("predates" in b for b in blockers)


def test_marked_ask_after_verify_final_but_unanswered_is_refused(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}")])

    d = resolve(store)
    assert d.ok is False
    assert any("has not been answered" in b for b in ask_blockers(d))


def test_errored_tool_result_is_not_an_answer(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}"),
                                answer_entry("a1", at + 6, is_error=True)])

    d = resolve(store)
    assert d.ok is False
    assert any("has not been answered" in b for b in ask_blockers(d))


def test_marked_answered_ask_after_verify_final_resolves(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}"),
                                answer_entry("a1", at + 9)])

    d = resolve(store)
    assert d.ok is True, d.data
    row = quality_rows()[-1]
    assert row["resolution_ask_gate_override"] is None
    assert row["resolution_ask_unverifiable"] is None
    assert store.load(SID).node == Node.RESOLVED.value


def test_unmarked_answered_ask_after_verify_final_is_refused(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, label="Resolve (rating 5)"),
                                answer_entry("a1", at + 6)])

    d = resolve(store)
    assert d.ok is False
    assert any("no AskUserQuestion carrying the marker" in b for b in ask_blockers(d))


def test_marker_in_a_description_counts(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, description=f"{RESOLUTION_ASK_MARKER} rate it"),
                                answer_entry("a1", at + 6)])

    assert resolve(store).ok is True


def test_marker_only_in_the_question_stem_does_not_count(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at + 5, question=f"{RESOLUTION_ASK_MARKER} rate?"),
                                answer_entry("a1", at + 6)])

    assert resolve(store).ok is False


def test_an_earlier_marked_ask_does_not_cover_a_later_unanswered_one_but_any_answered_after_does(store, tmp_path):
    to_resolution(store)
    at = verified_at(store)
    marked = f"Resolve {RESOLUTION_ASK_MARKER}"
    write_transcript(tmp_path, [
        ask_entry("old", at - 5, label=marked), answer_entry("old", at - 4),
        ask_entry("new", at + 5, label=marked), answer_entry("new", at + 6),
    ])

    assert resolve(store).ok is True


def test_missing_transcript_is_refused_and_resolves_with_the_logged_escape(store):
    to_resolution(store)

    d = resolve(store)
    assert d.ok is False
    assert any("cannot locate or read the transcript" in b and "--resolution-ask-unverifiable" in b
               for b in ask_blockers(d))

    d = resolve(store, resolution_ask_unverifiable="  harness wrote no transcript  ")
    assert d.ok is True
    assert d.data["resolution_ask_unverifiable"] == "harness wrote no transcript"
    assert quality_rows()[-1]["resolution_ask_unverifiable"] == "harness wrote no transcript"
    logged = [h for h in store.load(SID).history if h["event"] == "resolution_ask_gate"]
    assert [(h["override"], h["unverifiable"]) for h in logged] == [(None, "harness wrote no transcript")]


def test_empty_escape_reason_is_refused(store):
    to_resolution(store)

    d = resolve(store, resolution_ask_unverifiable="   ")
    assert d.ok is False
    assert any("must be non-empty" in b for b in ask_blockers(d))


def test_escape_is_refused_when_the_transcript_is_readable_but_lacks_the_ask(store, tmp_path):
    to_resolution(store)
    write_transcript(tmp_path, [])

    d = resolve(store, resolution_ask_unverifiable="pretend it is unreadable")
    assert d.ok is False
    assert any("refused: the transcript is readable" in b for b in ask_blockers(d))
    assert quality_rows() == []


def test_state_without_a_precise_stamp_must_rerun_verify_final(store, tmp_path):
    to_resolution(store)
    state = store.load(SID)
    for h in state.history:
        if h["event"] == "verify_final":
            h.pop("at", None)
    store.save(state)
    write_transcript(tmp_path, [ask_entry("a1", 4e9, label=RESOLUTION_ASK_MARKER),
                                answer_entry("a1", 4e9 + 1)])

    d = resolve(store)
    assert d.ok is False
    assert any("re-run verify-final" in b for b in ask_blockers(d))


def test_override_off_resolves_and_is_logged(store, monkeypatch):
    to_resolution(store)
    monkeypatch.setenv("AGENTCTL_RESOLUTION_ASK_GATE", "0")

    d = resolve(store)
    assert d.ok is True
    assert d.data["resolution_ask_gate_override"] == "0"
    assert quality_rows()[-1]["resolution_ask_gate_override"] == "0"
    logged = [h for h in store.load(SID).history if h["event"] == "resolution_ask_gate"]
    assert [(h["override"], h["unverifiable"]) for h in logged] == [("0", None)]


def test_quality_row_always_carries_the_two_keys_even_when_the_gate_is_inactive(store, monkeypatch):
    to_resolution(store)
    monkeypatch.setenv("AGENTCTL_RESOLUTION_ASK_GATE", "0")
    resolve(store)
    row = quality_rows()[-1]
    assert "resolution_ask_unverifiable" in row and row["resolution_ask_unverifiable"] is None


def test_activation_follows_weight_class_and_env(store, monkeypatch):
    to_resolution(store)
    state = store.load(SID)
    assert gates.resolution_ask_gate_active(state) is True
    state.weight_class = "small_change"
    assert gates.resolution_ask_gate_active(state) is False
    monkeypatch.setenv("AGENTCTL_RESOLUTION_ASK_GATE", "1")
    assert gates.resolution_ask_gate_active(state) is True


def test_close_probe_reports_the_ask_blocker_only_with_a_confirmer_and_threads_the_escape(store):
    to_resolution(store)

    waiting = cli.cmd_close(ns(session=SID), store=store)
    assert waiting.action == "await_user_confirmation"

    blocked = cli.cmd_close(ns(session=SID, confirmed_by="user", quality=5), store=store)
    assert blocked.ok is False
    assert any(b.startswith(cli.RESOLUTION_ASK_BLOCKER_PREFIX) for b in blocked.data["blockers"])

    done = cli.cmd_close(ns(session=SID, confirmed_by="user", quality=5,
                            resolution_ask_unverifiable="no transcript on this host"), store=store)
    assert done.ok is True
    assert quality_rows()[-1]["resolution_ask_unverifiable"] == "no transcript on this host"


def test_scanner_orders_asks_and_distinguishes_unreadable_from_empty(tmp_path):
    path = write_transcript(tmp_path, [
        ask_entry("a1", 100.0, label="plain"), answer_entry("a1", 101.0),
        ask_entry("a2", 200.0, label=RESOLUTION_ASK_MARKER),
    ])
    calls = transcript_turns.ask_user_question_calls(path)
    assert [(ts, answered) for _, ts, answered in calls] == [(100.0, True), (200.0, False)]
    assert [transcript_turns.has_marker_option(i, RESOLUTION_ASK_MARKER) for i, _, _ in calls] == [False, True]

    assert transcript_turns.ask_user_question_calls(tmp_path / "missing.jsonl") is None
    assert transcript_turns.ask_user_question_calls(write_transcript(tmp_path, [], sid="empty")) == []


def _predates(store, tmp_path):
    at = verified_at(store)
    write_transcript(tmp_path, [ask_entry("a1", at - 5, label=f"Resolve {RESOLUTION_ASK_MARKER}"),
                                answer_entry("a1", at - 4)])


def _missing_timestamp(store, tmp_path):
    ask = ask_entry("a1", verified_at(store) + 5, label=f"Resolve {RESOLUTION_ASK_MARKER}")
    ask.pop("timestamp")
    write_transcript(tmp_path, [ask, answer_entry("a1", verified_at(store) + 6)])


def _no_stamp(store, tmp_path):
    state = store.load(SID)
    for h in state.history:
        if h["event"] == "verify_final":
            h.pop("at", None)
    store.save(state)
    write_transcript(tmp_path, [ask_entry("a1", 4e9, label=RESOLUTION_ASK_MARKER),
                                answer_entry("a1", 4e9 + 1)])


@pytest.mark.parametrize("arrange", [_predates, _missing_timestamp, _no_stamp])
def test_refusals_state_the_marker_rule(store, tmp_path, arrange):
    to_resolution(store)
    arrange(store, tmp_path)

    d = resolve(store)
    assert d.ok is False
    blockers = ask_blockers(d)
    assert blockers
    for b in blockers:
        assert "option label or description" in b
        assert "land -> verify-final -> ask -> resolve" in b
        assert cli.RESOLUTION_ASK_RULE_HINT in b
