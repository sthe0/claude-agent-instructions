"""Agreement and divergence gate: the engine holds a substantive root session to what the user
agreed as the order, and the agreement rests on an answer the agent cannot write.

Claims, each pinned in the direction that could fail:

* classify arms the gate from the environment it can observe (an interactive claude host with
  the answer hook registered, a root session, no headless channel) and records WHY otherwise;
  an explicit `--host` or a caller-supplied recursion depth changes neither;
* `order-agree` records an agreement only on a user answer in the session's answer ledger that
  chose the option marked for the presented understanding, given after that presentation was
  delivered — no flag, no `--by`, no transcript read substitutes; with no such answer it refuses
  and records nothing;
* submit-plan, approve, replan and the essence / replan_diff presentations refuse a session
  with no agreement, a plan whose understanding differs from the agreed one in a named part, and
  an open challenge;
* a reviewer's blocking `understanding:` concern is taken up as a challenge keyed by its ledger
  id, stops holding the plan review, and is closed only by the user's `order-challenge-dismiss`
  answer or a new agreement; a re-raise of a challenge already in force opens nothing;
* every assignment of `plan_path` in the engine sits behind the gate or carries a stated reason.

A test earns trust only when it goes red on a real mutation: `order_agreement_mutants` plants
eight defects and one `test_*_is_killed` case per defect re-runs this file under it, asserting
pytest exit 1 and the intended test among the failures. Calls into code a mutant replaces go
through their module (`gates.`, `cli.`), never a by-name import.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import answer_ledger, cli, delivery, delivery_channel, gates, runtime_host
from agentctl.state import (
    CHALLENGE_DISMISSED,
    CONCERN_OPEN,
    GateRecord,
    CHALLENGE_OPEN,
    CHALLENGE_REAGREED,
    ORDER_AGREEMENT_ANCHOR_CONFIRMED,
    ORDER_AGREEMENT_ANCHOR_STAMP,
    ORDER_EXEMPT_CHANNEL,
    ORDER_EXEMPT_HOOK,
    ORDER_EXEMPT_HOST,
    ORDER_EXEMPT_LINKED,
    ORDER_EXEMPT_SPAWNED,
    PLAN_PRESENTATION_KIND_ORDER,
    order_agree_marker,
    order_dismiss_marker,
)
from agentctl.store import FileStateStore
from conftest import declare_venue
from lib import config_root, hook_wiring
from order_agreement_mutants import ENV, MUTANTS, apply_active_mutant
from test_meta_order import _COVERAGE, _FINAL_CHECK, _PLAN
from test_order_phase import (
    RENDERING, _LINES, _delivered_transcript, _draft, _present, _review, _submit, run_hook,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
SESSION = "ord"
STATE_FILE_NAME = f"{SESSION}.json"
HOOK_PATH = SCRIPTS / "hook-ask-answer-record.py"
RESTART_HINT = "register it with scripts/install-reminder-hooks.sh and restart the session"


@pytest.fixture(autouse=True)
def _planted_mutant(monkeypatch):
    apply_active_mutant(monkeypatch)


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    monkeypatch.delenv("AGENT_RECURSION_DEPTH", raising=False)
    return tmp_path


@pytest.fixture
def ostore(home):
    return FileStateStore(home / "agentctl" / "state")


def _wire_answer_hook(monkeypatch, *, wired: bool = True) -> None:
    real = hook_wiring.probe

    def probe(basename, root=None):
        if basename == gates.ANSWER_HOOK_BASENAME:
            return hook_wiring.Wiring(
                basename=basename, root=Path("/nonexistent"),
                status=hook_wiring.WIRED if wired else hook_wiring.ABSENT)
        return real(basename, root)

    monkeypatch.setattr(hook_wiring, "probe", probe)


@pytest.fixture
def armed_env(monkeypatch):
    """What classify observes for an interactive claude root: the host, the registered answer
    hook, no headless delivery channel. Patched below the helpers the mutants replace."""
    monkeypatch.setattr(runtime_host, "detect_host", lambda: runtime_host.HOST_CLAUDE)
    _wire_answer_hook(monkeypatch)
    monkeypatch.setattr(delivery_channel, "load_command", lambda: None)


def _classify(store, *, session=SESSION, **flags):
    args = dict(session=session, chat=False, changed_lines=200, files=5, wall_clock_min=60,
                tracker_key=None, architectural=True, external_effect=False,
                new_dependency=False, public_api_change=False, host="claude")
    args.update(flags)
    return cli.cmd_classify(ns(**args), store=store)


def _start(store, *, session=SESSION, **kw):
    cli.cmd_start(ns(session=session, task="od", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=kw.get("recursion_depth", 0)),
                  store=store)


def _state_file(home: Path) -> Path:
    return home / "agentctl" / "state" / STATE_FILE_NAME


def _hook_module():
    spec = importlib.util.spec_from_file_location("hook_ask_answer_record", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    return module


def _user_answers(marker: str, *, choose_marked: bool = True, at: float | None = None,
                  session: str = SESSION, other: str = "No, that is not what I ordered") -> int:
    """The user's answer to an ask offering `marker` beside an unmarked option, recorded by the
    real hook function. Returns how many ledgers got the row."""
    question = "Is this the order you gave?"
    marked = f"Yes, agreed {marker}"
    payload = {
        "tool_name": "AskUserQuestion", "session_id": session,
        "tool_input": {"questions": [{"question": question,
                                      "options": [{"label": marked}, {"label": other}]}]},
        "tool_response": {"answers": {question: marked if choose_marked else other}},
    }
    return _hook_module().record(payload, now=time.time() + 1 if at is None else at)


def _confirm_delivery(store, session=SESSION):
    d = cli.cmd_confirm_delivery(
        ns(session=session, kind="order", by="fedor", note="hook is not installed here",
           escape_reason=delivery.DELIVERY_ESCAPE_REASONS[0]), store=store)
    assert d.ok, d.detail


def _agree(store, *, note: str = ""):
    return cli.cmd_order_agree(ns(session=SESSION, note=note), store=store)


def _presented(store, tmp_path: Path, **draft_kw) -> Path:
    """An armed, classified session with a submitted, reviewed and presented order draft whose
    delivery is confirmed: everything but the user's answer."""
    _start(store)
    assert _classify(store).ok
    path = _draft(tmp_path, **draft_kw)
    assert _submit(store, path).ok
    assert _review(store).ok
    assert _present(store, tmp_path).ok
    _confirm_delivery(store)
    return path


def _marker_of(path: Path) -> str:
    from agentctl.plan import load_order_draft, understanding_digest
    return order_agree_marker(understanding_digest(load_order_draft(path)[0]))


def _agreed(store, home: Path, tmp_path: Path, **draft_kw) -> Path:
    path = _presented(store, tmp_path, **draft_kw)
    assert _user_answers(_marker_of(path)) == 1
    d = _agree(store)
    assert d.ok, d.detail
    return path


def _plan(tmp_path: Path, *, replace=None, name="plan.toml") -> str:
    """A submittable plan whose order carries the draft's understanding (or `replace`d lines)."""
    lines = {**_LINES, **(replace or {})}
    coverage = "".join(f"{k} = {json.dumps(v)}\n" for k, v in _COVERAGE.items())
    order = "[meta.order]\n" + "\n".join(lines.values()) + "\n\n[meta.order.coverage]\n" + coverage + "\n"
    text = _PLAN.format(
        goal=json.dumps("g"), done_criterion=json.dumps("d"),
        weight_class='weight_class = "substantive"\n', order=order, final_check=_FINAL_CHECK)
    path = tmp_path / name
    path.write_text(declare_venue(text, tmp_path), encoding="utf-8")
    return str(path)


DIVERGED = {"difficulty": 'difficulty = "a release takes a week of hand-work"'}


def _plan_ready(store, home: Path, tmp_path: Path) -> str:
    _agreed(store, home, tmp_path)
    plan = _plan(tmp_path)
    assert cli.cmd_plan(ns(session=SESSION), store=store).ok
    d = cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=store)
    assert d.ok, (d.detail, d.data)
    return plan


def _blockers(d) -> str:
    return " | ".join(d.data.get("blockers", []))


# --- arming at classify --------------------------------------------------------


def test_classify_arms_a_root_claude_session_with_the_hook(ostore, armed_env):
    _start(ostore)
    d = _classify(ostore)

    state = ostore.load(SESSION)
    assert d.ok and state.order_agreement_armed is True and state.order_agreement_exemption == ""
    assert "order_agreement_exemption" not in d.data


def _exempt_host(mp, tmp):
    mp.setattr(runtime_host, "detect_host", lambda: None)


def _exempt_hook(mp, tmp):
    _wire_answer_hook(mp, wired=False)


def _exempt_spawned(mp, tmp):
    mp.setenv("AGENT_RECURSION_DEPTH", "1")


def _exempt_channel(mp, tmp):
    mp.setattr(delivery_channel, "load_command", lambda: "deliver --somewhere")


@pytest.mark.parametrize("reason, plant", [
    (ORDER_EXEMPT_HOST, _exempt_host),
    (ORDER_EXEMPT_HOOK, _exempt_hook),
    (ORDER_EXEMPT_SPAWNED, _exempt_spawned),
    (ORDER_EXEMPT_CHANNEL, _exempt_channel),
])
def test_classify_exempts_with_a_reason(ostore, armed_env, monkeypatch, tmp_path, reason, plant):
    plant(monkeypatch, tmp_path)
    _start(ostore)

    d = _classify(ostore)

    state = ostore.load(SESSION)
    assert state.order_agreement_armed is False and state.order_agreement_exemption == reason
    assert d.data["order_agreement_exemption"] == reason and reason in d.detail
    assert cli.cmd_status(ns(session=SESSION), store=ostore).data["order_agreement_exemption"] == reason


def test_classify_exempts_a_linked_child(ostore, armed_env):
    _start(ostore)
    state = ostore.load(SESSION)
    state.parent_link = {"parent_session": "p", "stage_index": 1, "depth": 1, "norm_digest": "x"}
    ostore.save(state)

    _classify(ostore)

    assert ostore.load(SESSION).order_agreement_exemption == ORDER_EXEMPT_LINKED


def test_classify_does_not_arm_on_an_explicit_host_flag(ostore, armed_env, monkeypatch):
    monkeypatch.setattr(runtime_host, "detect_host", lambda: None)
    _start(ostore)

    _classify(ostore, host="claude")

    state = ostore.load(SESSION)
    assert state.order_agreement_armed is False and state.order_agreement_exemption == ORDER_EXEMPT_HOST


def test_classify_arms_despite_a_recursion_depth_flag(ostore, armed_env):
    _start(ostore, recursion_depth=3)

    _classify(ostore)

    state = ostore.load(SESSION)
    assert state.recursion_depth == 3 and state.order_agreement_armed is True


def test_classify_arms_only_a_substantive_session(ostore, armed_env):
    _start(ostore)

    _classify(ostore, changed_lines=3, files=1, wall_clock_min=1, architectural=False)

    state = ostore.load(SESSION)
    assert state.weight_class != "substantive"
    assert state.order_agreement_armed is False and state.order_agreement_exemption == ""


def test_an_unarmed_session_is_not_held_to_the_gate(ostore, armed_env, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_host, "detect_host", lambda: None)
    _start(ostore)
    _classify(ostore)
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok

    d = cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path)), store=ostore)

    assert d.ok, (d.detail, d.data)
    assert ostore.load(SESSION).order_agreement is None


# --- order-agree: the answer is the evidence --------------------------------------


def test_order_agree_refuses_without_a_recorded_answer(ostore, home, armed_env, tmp_path):
    _presented(ostore, tmp_path)

    d = _agree(ostore)

    assert d.ok is False and "needs the user's own answer" in d.detail
    assert RESTART_HINT in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_order_agree_refuses_an_answer_that_declined(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    _user_answers(_marker_of(path), choose_marked=False)

    d = _agree(ostore)

    assert d.ok is False and "needs the user's own answer" in d.detail
    assert RESTART_HINT not in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_the_restart_hint_names_a_missing_ledger_file(ostore, home, armed_env, tmp_path):
    _presented(ostore, tmp_path)
    assert not answer_ledger.ledger_path_for(_state_file(home)).exists()

    d = _agree(ostore)

    assert "needs the user's own answer" in d.detail and RESTART_HINT in d.detail


def test_the_restart_hint_names_a_ledger_holding_only_answers_before_the_presentation(
        ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    stamp = delivery.read_stamp(_state_file(home))
    _user_answers(_marker_of(path), at=stamp.verified_ts - 60)
    assert answer_ledger.ledger_path_for(_state_file(home)).exists()

    d = _agree(ostore)

    assert "needs the user's own answer" in d.detail and RESTART_HINT in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_order_agree_refuses_an_answer_for_another_understanding(ostore, home, armed_env, tmp_path):
    _presented(ostore, tmp_path)
    _user_answers(order_agree_marker("f" * 64))

    assert _agree(ostore).ok is False
    assert ostore.load(SESSION).order_agreement is None


def test_order_agree_refuses_an_answer_given_before_the_delivery(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    stamp = delivery.read_stamp(_state_file(home))
    _user_answers(_marker_of(path), at=stamp.verified_ts - 60)

    assert _agree(ostore).ok is False
    assert ostore.load(SESSION).order_agreement is None


def test_order_agree_takes_no_by_flag_and_no_escape():
    from agentctl import cli as engine
    parser = engine.build_parser() if hasattr(engine, "build_parser") else None
    if parser is None:
        pytest.skip("no parser factory")
    with pytest.raises(SystemExit):
        parser.parse_args(["order-agree", "--session", "s", "--by", "agent"])


def test_order_agree_records_the_agreement_on_a_confirmed_anchor(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    _user_answers(_marker_of(path))

    d = _agree(ostore, note="as I said")

    state = ostore.load(SESSION)
    agreement = state.order_agreement
    assert d.ok, d.detail
    assert agreement.anchor == ORDER_AGREEMENT_ANCHOR_CONFIRMED and agreement.note == "as I said"
    assert agreement.draft_path == str(path) and agreement.parts
    assert state.order_agreement_history == [agreement]
    status = cli.cmd_status(ns(session=SESSION), store=ostore).data["order_agreement"]
    assert status["anchor"] == ORDER_AGREEMENT_ANCHOR_CONFIRMED
    assert status["understanding_sha256"] == agreement.understanding_sha256


def test_order_agree_anchors_on_a_hook_stamp(ostore, home, armed_env, tmp_path):
    _start(ostore)
    _classify(ostore)
    path = _draft(tmp_path)
    assert _submit(ostore, path).ok and _review(ostore).ok and _present(ostore, tmp_path).ok
    receipt = gates._plan_presentation_for(ostore.load(SESSION), PLAN_PRESENTATION_KIND_ORDER)
    transcript = _delivered_transcript(home, receipt.rendering_text)
    ask = {
        "tool_name": "AskUserQuestion", "session_id": SESSION, "transcript_path": str(transcript),
        "tool_input": {"questions": [{"question": "q", "options": [
            {"label": f"Yes {_marker_of(path)}"}, {"label": "No"}]}]},
    }
    assert run_hook(ask, home).returncode == 0
    stamp = delivery.read_stamp(_state_file(home))
    assert stamp is not None and stamp.source == delivery.SOURCE_HOOK
    _user_answers(_marker_of(path), at=stamp.verified_ts + 1)

    d = _agree(ostore)

    assert d.ok, d.detail
    assert ostore.load(SESSION).order_agreement.anchor == ORDER_AGREEMENT_ANCHOR_STAMP


def test_order_agree_refuses_a_stamp_of_another_rendering(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    assert _present(ostore, tmp_path, text=RENDERING + "\nAnd one more sentence.\n").ok
    _user_answers(_marker_of(path))

    d = _agree(ostore)

    assert d.ok is False and "another rendering" in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_order_agree_refuses_without_a_standing_review(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")
    assert _submit(ostore, path).ok
    _user_answers(_marker_of(path))

    d = _agree(ostore)

    assert d.ok is False and "no order review is bound" in d.detail


def test_order_agree_refuses_without_a_presentation(ostore, home, armed_env, tmp_path):
    _start(ostore)
    _classify(ostore)
    path = _draft(tmp_path)
    assert _submit(ostore, path).ok and _review(ostore).ok

    d = _agree(ostore)

    assert d.ok is False and "no order presentation" in d.detail


def test_agreement_follows_a_changed_understanding_and_closes_open_challenges(
        ostore, home, armed_env, tmp_path):
    path = _agreed(ostore, home, tmp_path)
    state = ostore.load(SESSION)
    first = state.order_agreement
    challenge_id = _challenge(ostore, tmp_path, state)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")
    assert _submit(ostore, path).ok and _review(ostore).ok and _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(path), at=time.time() + 5)

    d = _agree(ostore)

    state = ostore.load(SESSION)
    assert d.ok, d.detail
    assert state.order_agreement.understanding_sha256 != first.understanding_sha256
    assert [a.understanding_sha256 for a in state.order_agreement_history] == [
        first.understanding_sha256, state.order_agreement.understanding_sha256]
    assert {c.id: c.status for c in state.order_challenges}[challenge_id] == CHALLENGE_REAGREED


# --- the gate at each act -------------------------------------------------------------


def test_submit_plan_refuses_a_session_without_an_agreement(ostore, home, armed_env, tmp_path):
    _start(ostore)
    _classify(ostore)
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok

    d = cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path)), store=ostore)

    assert d.ok is False and "no agreed order understanding" in _blockers(d)
    assert ostore.load(SESSION).plan_path in (None, "")


def test_submit_plan_accepts_the_agreed_understanding(ostore, home, armed_env, tmp_path):
    _plan_ready(ostore, home, tmp_path)

    assert ostore.load(SESSION).plan_path


def test_submit_plan_refuses_a_diverging_understanding(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok

    d = cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path, replace=DIVERGED)), store=ostore)

    assert d.ok is False and "diverges from the agreed" in _blockers(d)
    assert ostore.load(SESSION).plan_path in (None, "")


def test_approve_refuses_a_plan_that_diverged_after_submission(ostore, home, armed_env, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)
    Path(plan).write_text(_plan_text(tmp_path, DIVERGED), encoding="utf-8")

    d = cli.cmd_approve(ns(session=SESSION, by="user"), store=ostore)

    assert d.ok is False and "diverges from the agreed" in _blockers(d)
    assert ostore.load(SESSION).node != "APPROVED"


def _plan_text(tmp_path: Path, replace) -> str:
    return Path(_plan(tmp_path, replace=replace, name="scratch.toml")).read_text(encoding="utf-8")


def test_replan_refuses_a_diverging_proposal(ostore, home, armed_env, tmp_path):
    _plan_ready(ostore, home, tmp_path)
    proposal = _plan(tmp_path, replace=DIVERGED, name="proposal.toml")

    d = cli.cmd_replan(ns(session=SESSION, plan=proposal), store=ostore)

    assert d.ok is False and "diverges from the agreed" in _blockers(d)
    assert ostore.load(SESSION).plan_path != proposal


def test_present_essence_refuses_a_diverged_plan(ostore, home, armed_env, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)
    Path(plan).write_text(_plan_text(tmp_path, DIVERGED), encoding="utf-8")
    rendering = tmp_path / "essence.txt"
    rendering.write_text("The plan, in essence.\n", encoding="utf-8")

    d = cli.cmd_present_plan(ns(session=SESSION, kind="essence", rendering_file=str(rendering),
                                emit_skeleton=False, plan=None), store=ostore)

    assert d.ok is False and "diverges from the agreed" in _blockers(d)
    assert ostore.load(SESSION).plan_presentations[-1:] and all(
        p.kind != "essence" for p in ostore.load(SESSION).plan_presentations)


def test_present_replan_diff_refuses_a_diverging_proposal(ostore, home, armed_env, tmp_path):
    _plan_ready(ostore, home, tmp_path)
    proposal = _plan(tmp_path, replace=DIVERGED, name="proposal.toml")
    rendering = tmp_path / "diff.txt"
    rendering.write_text("The proposed change.\n", encoding="utf-8")

    d = cli.cmd_present_plan(ns(session=SESSION, kind="replan_diff", rendering_file=str(rendering),
                                emit_skeleton=False, plan=proposal), store=ostore)

    assert d.ok is False and "diverges from the agreed" in _blockers(d)


# --- challenges -----------------------------------------------------------------------


def _plan_review(store, verdict="revise", concerns=None, *, reviewer="thinker", scope=None,
                 plan_digest=None):
    return cli.cmd_plan_review(
        ns(session=SESSION, verdict=verdict, reviewer=reviewer, concerns=concerns, note="",
           target=None, plan_digest=plan_digest, scope=scope), store=store)


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _challenge(store, tmp_path: Path, state=None, text="the second requirement is not what I ordered") -> str:
    """Make sure a plan is under review and record one blocking understanding concern;
    return the id of the challenge it opened."""
    state = store.load(SESSION)
    if not state.plan_path:
        assert cli.cmd_plan(ns(session=SESSION), store=store).ok
        d = cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path)), store=store)
        assert d.ok, (d.detail, d.data)
    d = _plan_review(store, concerns=[f"blocking: understanding: {text}"])
    assert d.ok, (d.detail, d.data.get("blockers"))
    challenges = [c for c in store.load(SESSION).order_challenges if c.status == CHALLENGE_OPEN]
    assert len(challenges) == 1, challenges
    return challenges[0].id


def test_blocking_understanding_concern_is_recorded_as_a_challenge(
        ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)

    challenge_id = _challenge(ostore, tmp_path)

    state = ostore.load(SESSION)
    [challenge] = state.order_challenges
    assert challenge.id == challenge_id and challenge.status == CHALLENGE_OPEN
    assert challenge.reviewer == "thinker" and "second requirement" in challenge.text
    assert challenge.understanding_sha256 == state.order_agreement.understanding_sha256
    assert challenge_id in state.plan_review.stable_ids


@pytest.fixture
def review_gate(monkeypatch):
    """The plan-review gate on, so a `revise` verdict holds the plan the way it does live."""
    monkeypatch.setenv("AGENTCTL_PLAN_REVIEW", "1")


def _held_by_review(store, plan: str) -> list[str]:
    return gates.plan_review_blockers(store.load(SESSION), plan)


def test_a_challenged_understanding_concern_does_not_hold_the_plan_review(
        ostore, home, armed_env, review_gate, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)

    _challenge(ostore, tmp_path)

    assert _held_by_review(ostore, plan) == []


def test_an_ordinary_blocking_concern_holds_the_plan_review(
        ostore, home, armed_env, review_gate, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)

    d = _plan_review(ostore, concerns=["blocking: stage:1 the verify command proves nothing"])

    assert d.ok is False and "does not clear the gate" in d.detail
    assert any("revise" in b for b in _held_by_review(ostore, plan))


def test_an_open_challenge_blocks_the_gate_until_dismissed(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    state = ostore.load(SESSION)

    blockers = gates.order_agreement_blockers(state, None)
    assert any(challenge_id in b for b in blockers), blockers
    d = cli.cmd_approve(ns(session=SESSION, by="user"), store=ostore)
    assert d.ok is False and challenge_id in _blockers(d)

    _user_answers(order_dismiss_marker(challenge_id))
    dismissed = cli.cmd_order_challenge_dismiss(
        ns(session=SESSION, id=challenge_id, note="the user confirms R2 as written"), store=ostore)
    assert dismissed.ok, dismissed.detail
    state = ostore.load(SESSION)
    assert state.order_challenges[0].status == CHALLENGE_DISMISSED
    assert gates.order_agreement_blockers(state, None) == []


def test_dismiss_needs_a_note(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    _user_answers(order_dismiss_marker(challenge_id))

    d = cli.cmd_order_challenge_dismiss(ns(session=SESSION, id=challenge_id, note="  "), store=ostore)

    assert d.ok is False and "--note" in d.detail
    assert ostore.load(SESSION).order_challenges[0].status == CHALLENGE_OPEN


def test_dismiss_needs_the_users_answer_to_its_own_marker(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    _user_answers(order_dismiss_marker("some-other-id"))
    _user_answers(order_dismiss_marker(challenge_id), choose_marked=False)

    d = cli.cmd_order_challenge_dismiss(ns(session=SESSION, id=challenge_id, note="fine"), store=ostore)

    assert d.ok is False and "needs the user's own answer" in d.detail
    assert ostore.load(SESSION).order_challenges[0].status == CHALLENGE_OPEN


def test_dismiss_refuses_an_answer_given_before_the_challenge(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    challenge = ostore.load(SESSION).order_challenges[0]
    _user_answers(order_dismiss_marker(challenge_id), at=challenge.at - 60)

    d = cli.cmd_order_challenge_dismiss(ns(session=SESSION, id=challenge_id, note="fine"), store=ostore)

    assert d.ok is False
    assert ostore.load(SESSION).order_challenges[0].status == CHALLENGE_OPEN


def test_notes_and_advisory_understanding_concerns_open_nothing(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok
    assert cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path)), store=ostore).ok

    assert _plan_review(ostore, "pass", ["note: understanding: R2 reads thin to me"]).ok

    assert ostore.load(SESSION).order_challenges == []


def test_an_ordinary_concern_opens_no_challenge(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok
    assert cli.cmd_submit_plan(ns(session=SESSION, plan=_plan(tmp_path)), store=ostore).ok

    _plan_review(ostore, concerns=["blocking: stage:1 the verify command proves nothing"])

    assert ostore.load(SESSION).order_challenges == []


def test_a_reraise_of_an_open_challenge_opens_nothing(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)

    d = _plan_review(ostore, concerns=[f"blocking: re:{challenge_id} understanding: still not what I ordered"])

    assert d.ok, d.detail
    state = ostore.load(SESSION)
    assert [c.id for c in state.order_challenges] == [challenge_id]
    assert not any("plan blocked" in b for b in gates.plan_review_blockers(state, state.plan_path))


def test_a_reraise_of_a_dismissed_challenge_opens_nothing_while_the_understanding_stands(
        ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    _user_answers(order_dismiss_marker(challenge_id))
    assert cli.cmd_order_challenge_dismiss(
        ns(session=SESSION, id=challenge_id, note="settled"), store=ostore).ok

    _plan_review(ostore, concerns=[f"blocking: re:{challenge_id} understanding: asking again"])

    state = ostore.load(SESSION)
    assert [c.id for c in state.order_challenges] == [challenge_id]
    assert gates.order_agreement_blockers(state, None) == []


def test_a_reraise_after_a_new_agreement_opens_a_fresh_challenge(ostore, home, armed_env, tmp_path):
    path = _agreed(ostore, home, tmp_path)
    challenge_id = _challenge(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")
    assert _submit(ostore, path).ok and _review(ostore).ok and _present(ostore, tmp_path).ok
    _confirm_delivery(ostore)
    _user_answers(_marker_of(path), at=time.time() + 5)
    assert _agree(ostore).ok
    assert ostore.load(SESSION).order_challenges[0].status == CHALLENGE_REAGREED

    _plan_review(ostore, concerns=[f"blocking: re:{challenge_id} understanding: it still is not right"])

    statuses = {c.id: c.status for c in ostore.load(SESSION).order_challenges}
    assert statuses.pop(challenge_id) == CHALLENGE_REAGREED
    assert list(statuses.values()) == [CHALLENGE_OPEN]


UNDERSTANDING_CONCERN = "blocking: understanding: the second requirement is not what I ordered"


def _understanding_entries(state):
    return [e for e in state.concern_ledger.values() if "second requirement" in e.text]


def test_a_stage_scoped_understanding_concern_opens_no_challenge(ostore, home, armed_env, tmp_path):
    _plan_ready(ostore, home, tmp_path)

    d = _plan_review(ostore, scope="stage:1", concerns=[UNDERSTANDING_CONCERN])

    state = ostore.load(SESSION)
    assert d.ok, d.detail
    [entry] = _understanding_entries(state)
    assert entry.status != CONCERN_OPEN
    assert state.order_challenges == []
    assert gates.order_agreement_blockers(state, None) == []


def test_an_unevidenced_post_pass_understanding_concern_opens_no_challenge(
        ostore, home, armed_env, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)
    assert _plan_review(ostore, "pass", plan_digest=_file_sha256(plan)).ok

    d = _plan_review(ostore, concerns=[UNDERSTANDING_CONCERN])

    state = ostore.load(SESSION)
    assert d.data.get("plan_review_post_pass_unevidenced") is True
    [entry] = _understanding_entries(state)
    assert entry.status != CONCERN_OPEN
    assert state.order_challenges == []


def test_a_challenged_understanding_concern_does_not_hold_a_topo_unit_review(
        ostore, home, armed_env, tmp_path):
    from agentctl.plan import load_plan, review_ids
    plan = _plan_ready(ostore, home, tmp_path)
    unit = next(r for r in review_ids(load_plan(plan)) if r.startswith("unit:"))

    d = _plan_review(ostore, concerns=[UNDERSTANDING_CONCERN], scope=f"topo:{unit}")

    state = ostore.load(SESSION)
    assert d.ok, d.detail
    assert state.plan_pair_reviews[unit].verdict == "revise"
    assert len(state.order_challenges) == 1
    assert gates.pair_status(state, load_plan(plan), plan, unit) == "current"
    assert any(state.order_challenges[0].id in b for b in gates.order_agreement_blockers(state, None))


def test_a_topo_unit_review_with_an_ordinary_concern_beside_the_challenged_one_still_revises(
        ostore, home, armed_env, tmp_path):
    from agentctl.plan import load_plan, review_ids
    plan = _plan_ready(ostore, home, tmp_path)
    unit = next(r for r in review_ids(load_plan(plan)) if r.startswith("unit:"))

    _plan_review(ostore, concerns=[UNDERSTANDING_CONCERN, "blocking: the unit proves nothing"],
                 scope=f"topo:{unit}")

    state = ostore.load(SESSION)
    assert gates.pair_status(state, load_plan(plan), plan, unit) == "revise"


def test_an_order_coverage_concern_still_blocks_as_an_ordinary_revise(
        ostore, home, armed_env, review_gate, tmp_path):
    plan = _plan_ready(ostore, home, tmp_path)

    d = _plan_review(ostore, concerns=["blocking: order: R2 has no stage that covers it"])

    assert d.ok is False and "does not clear the gate" in d.detail
    assert ostore.load(SESSION).order_challenges == []
    assert any("revise" in b for b in _held_by_review(ostore, plan))


def _unarmed_host(mp, store):
    mp.setattr(runtime_host, "detect_host", lambda: None)


def _linked_child(mp, store):
    state = store.load(SESSION)
    state.parent_link = {"parent_session": "p", "stage_index": 1, "depth": 1, "norm_digest": "x"}
    store.save(state)


@pytest.mark.parametrize("plant", [_unarmed_host, _linked_child], ids=["unarmed", "linked-child"])
def test_an_understanding_concern_blocks_the_plan_review_where_the_gate_is_not_armed(
        ostore, home, armed_env, review_gate, monkeypatch, tmp_path, plant):
    _start(ostore)
    plant(monkeypatch, ostore)
    assert _classify(ostore).ok
    assert ostore.load(SESSION).order_agreement_armed is False
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok
    plan = _plan(tmp_path)
    d = cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=ostore)
    assert d.ok, (d.detail, d.data)

    d = _plan_review(ostore, concerns=[UNDERSTANDING_CONCERN])

    assert d.ok is False and "does not clear the gate" in d.detail
    assert ostore.load(SESSION).order_challenges == []
    assert any("revise" in b for b in _held_by_review(ostore, plan))


# --- sub-plans, arming at the root, the staged draft, the status line ----------------


def _push_child(store, tmp_path: Path):
    state = store.load(SESSION)
    state.node = "EXECUTING"
    state.route = "IN_THREAD"
    state.approval = GateRecord("plan_approval", armed=True, passed=True, by="user")
    store.save(state)
    return cli.cmd_push_subplan(
        ns(session=SESSION, plan=str(tmp_path / "child.toml"), task=None, originating_stage=1),
        store=store)


def test_a_pushed_sub_plan_leaves_the_gate_and_the_pop_restores_the_agreement(
        ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    agreed = ostore.load(SESSION).order_agreement
    challenge_id = _challenge(ostore, tmp_path)

    pushed = _push_child(ostore, tmp_path)

    child = ostore.load(SESSION)
    assert pushed.ok, pushed.detail
    assert child.order_agreement is None and child.order_challenges == []
    assert gates.order_agreement_active(child) is False
    assert gates.order_agreement_blockers(child, None) == []
    assert cli.cmd_status(ns(session=SESSION), store=ostore).data["order_agreement"] is None

    child.node = "RESOLVED"
    child.resolution = GateRecord("resolution", armed=True, passed=True, by="user")
    ostore.save(child)
    popped = cli.cmd_pop_subplan(ns(session=SESSION), store=ostore)

    restored = ostore.load(SESSION)
    assert popped.ok, popped.detail
    assert restored.order_agreement == agreed
    assert [c.id for c in restored.order_challenges] == [challenge_id]
    assert gates.order_agreement_active(restored) is True


def test_a_root_session_with_a_parent_norm_and_no_parent_link_is_armed_and_held(
        ostore, home, armed_env, tmp_path):
    _start(ostore)
    assert _classify(ostore).ok
    state = ostore.load(SESSION)
    assert state.parent_link is None and state.order_agreement_armed is True
    assert cli.cmd_plan(ns(session=SESSION), store=ostore).ok
    plan = _plan(tmp_path, replace={"parent_norm": 'parent_norm = "the stage that ordered this"'})
    assert 'parent_norm = "the stage' in Path(plan).read_text(encoding="utf-8")

    d = cli.cmd_submit_plan(ns(session=SESSION, plan=plan), store=ostore)

    assert d.ok is False and "no agreed order understanding" in _blockers(d)


def test_order_submit_refuses_a_staged_draft_before_the_agreement(ostore, home, armed_env, tmp_path):
    _presented(ostore, tmp_path)

    d = _submit(ostore, Path(_plan(tmp_path)))

    assert d.ok is False and "has not agreed this session's order" in d.detail
    assert ostore.load(SESSION).order_draft_path != _plan(tmp_path)


def test_order_submit_accepts_a_staged_draft_after_the_agreement(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    staged = Path(_plan(tmp_path))

    d = _submit(ostore, staged)

    assert d.ok, d.detail
    assert ostore.load(SESSION).order_draft_path == str(staged)


def test_the_agreement_status_is_null_where_the_gate_is_inactive(ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)
    assert cli.cmd_status(ns(session=SESSION), store=ostore).data["order_agreement"] is not None
    state = ostore.load(SESSION)
    state.order_agreement_armed = False
    ostore.save(state)

    status = cli.cmd_status(ns(session=SESSION), store=ostore)

    assert status.data["order_agreement"] is None
    assert ostore.load(SESSION).order_agreement is not None


def test_the_transcript_is_not_evidence_of_the_users_answer(
        ostore, home, armed_env, monkeypatch, tmp_path):
    from lib import ask_evidence

    def read_a_transcript(*_a, **_k):
        raise AssertionError("order-agree read a transcript")

    _presented(ostore, tmp_path)
    monkeypatch.setattr(cli, "_present_plan_transcript", read_a_transcript)
    monkeypatch.setattr(ask_evidence, "ask_after_event", read_a_transcript)

    d = _agree(ostore)

    assert d.ok is False and "needs the user's own answer" in d.detail
    assert ostore.load(SESSION).order_agreement is None


def test_the_agreement_records_the_anchor_time_and_the_rendering_digest(
        ostore, home, armed_env, tmp_path):
    _agreed(ostore, home, tmp_path)

    agreement = ostore.load(SESSION).order_agreement
    stamp = delivery.read_stamp(_state_file(home))

    assert agreement.anchor_at == stamp.verified_ts
    assert agreement.rendering_sha256 == stamp.rendering_sha256 and agreement.rendering_sha256


def test_a_foreign_stamp_refusal_says_a_fresh_ask_is_needed(ostore, home, armed_env, tmp_path):
    path = _presented(ostore, tmp_path)
    assert _present(ostore, tmp_path, text=RENDERING + "\nAnd one more sentence.\n").ok
    _user_answers(_marker_of(path))

    d = _agree(ostore)

    assert "another rendering" in d.detail and "fresh ask" in d.detail


# --- structure ---------------------------------------------------------------------


def test_the_new_verbs_are_user_authority_verbs():
    from lib import widening_targets

    assert {"order-agree", "order-challenge-dismiss"} <= set(widening_targets.AGENTCTL_USER_AUTHORITY_VERBS)


# `plan_path` is where an act changes what the session executes. Every function in the engine
# that assigns it either runs the agreement gate first, or states below why it need not.
_PLAN_PATH_REASONS = {
    "cli.py:cmd_push_subplan": "a sub-plan frame keeps the parent's agreement fields and does not carry the top-level order",
    "cli.py:cmd_pop_subplan": "restores the frame it pushed, agreement fields included",
    "cli.py:cmd_reset": "a reset clears the session, agreement included",
    "cli.py:_renormalize_replan": "reached only from _cmd_replan, after its agreement refusal",
    "carry_forward.py:_restamp": "rebinds a review record's own plan_path, not the session's",
    "carry_forward.py:pair_records": "rebinds a pair record's own plan_path, not the session's",
}


def _plan_path_assigners() -> dict[str, list[ast.AST]]:
    found: dict[str, list[ast.AST]] = {}
    for source in (SCRIPTS / "agentctl").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == "plan_path" for t in targets):
                    found.setdefault(f"{source.name}:{func.name}", []).append(func)
                    break
    return found


def _calls_agreement_gate(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == "_order_agreement_refusal":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "order_agreement_blockers":
            return True
    return False


def test_every_plan_path_assignment_is_behind_the_gate_or_has_a_stated_reason():
    ungated = []
    for where, [func, *_] in _plan_path_assigners().items():
        if where in _PLAN_PATH_REASONS or _calls_agreement_gate(func):
            continue
        ungated.append(where)
    assert ungated == [], (
        "these functions assign plan_path without calling the order-agreement gate: "
        f"{ungated}; gate them or add a reason to _PLAN_PATH_REASONS")


def test_the_assignment_scan_sees_the_gated_acts():
    gated = {w.split(":", 1)[1] for w, [f, *_] in _plan_path_assigners().items() if _calls_agreement_gate(f)}
    assert {"cmd_submit_plan"} <= gated


def test_non_test_sources_never_read_the_mutant_variable():
    offenders = []
    for root in (SCRIPTS, REPO_ROOT / "skills", REPO_ROOT / "hooks"):
        if not root.exists():
            continue
        for source in root.rglob("*"):
            if source.suffix not in {".py", ".sh"} or "tests" in source.parts:
                continue
            if ENV in source.read_text(encoding="utf-8", errors="replace"):
                offenders.append(str(source.relative_to(REPO_ROOT)))
    assert offenders == []


# --- the negative control: each planted defect is caught ---------------------------------

_KILLERS = {
    "agree_without_ask": "test_order_agree_refuses_without_a_recorded_answer",
    "divergence_ignored": "test_submit_plan_refuses_a_diverging_understanding",
    "challenge_not_recorded": "test_blocking_understanding_concern_is_recorded_as_a_challenge",
    "understanding_routes_order_token": "test_a_challenged_understanding_concern_does_not_hold_the_plan_review",
    "reraise_unmatched": "test_a_reraise_of_an_open_challenge_opens_nothing",
    "explicit_host_arms": "test_classify_does_not_arm_on_an_explicit_host_flag",
    "depth_flag_exempts": "test_classify_arms_despite_a_recursion_depth_flag",
    "headless_channel_ignored": "test_classify_exempts_with_a_reason",
}


def _kill(mutant: str) -> None:
    assert mutant in MUTANTS, f"{mutant} is not in the mutant table"
    env = {**os.environ, ENV: mutant}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"{Path(__file__)}::{_KILLERS[mutant]}", "-q",
         "-p", "no:cacheprovider", "--tb=no", "-rf"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900,
    )

    assert result.returncode == 1, (mutant, result.returncode, result.stdout[-800:])
    assert f"::{_KILLERS[mutant]}" in result.stdout, (mutant, result.stdout[-800:])


def test_the_killer_table_names_every_agreement_mutant():
    assert set(_KILLERS) <= set(MUTANTS)


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
@pytest.mark.parametrize("mutant", sorted(_KILLERS))
def test_agreement_mutant_is_killed(mutant):
    _kill(mutant)
