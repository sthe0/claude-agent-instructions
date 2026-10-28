"""The order phase of the agentctl engine: an order draft is submitted, reviewed and shown
to the user BEFORE any plan exists, each act bound to the exact understanding it is about.

Claims, each pinned in the direction that could fail:

* `order-submit` records the draft path and the understanding digest, needs no plan and does
  not move the node; an incomplete, ill-formed or staged draft is refused and records nothing;
* `plan-review --scope order` records a review bound to the ENGINE's digest of the draft (not
  to the reviewer's word) and touches neither the plan review nor the stage reviews;
  `gates.order_review_blockers` clears only for a review of the understanding now in force;
* `present-plan --kind order` shows exactly the order's items as `[R1]`/`[P1]`/`[K1]` anchors,
  only after a passing review of the submitted, still-current, still-complete draft, and
  binds the receipt to the understanding digest;
* the delivery hook stamps the order receipt by the same final-text test as the essence, and
  only for an ask carrying the marker of that very understanding.

A test earns trust only when it goes red on a real mutation, so `order_agreement_mutants`
plants four defects and one `test_order_*_is_killed` case per defect re-runs this file under
it and asserts pytest exit 1 and the intended test among the failures. Calls into code a
mutant replaces go through their module (`gates.`, `cli.`), never a by-name import.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, delivery, gates
from agentctl.plan import load_order_draft, understanding_digest
from agentctl.state import (
    PLAN_PRESENTATION_KIND_ORDER,
    SCHEMA_VERSION,
    SessionState,
    order_agree_marker,
)
from agentctl.store import FileStateStore
from order_agreement_mutants import ENV, MUTANTS, apply_active_mutant
from test_meta_order import _write_plan
from test_plan_delivery_gate_presentation import (
    run_hook,
    text_only_entry,
    text_then_tool_use_entry,
    user_prompt_entry,
    write_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION = "ord"


@pytest.fixture(autouse=True)
def _planted_mutant(monkeypatch):
    apply_active_mutant(monkeypatch)


def ns(**kw) -> Namespace:
    return Namespace(**kw)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A config root the delivery hook (a subprocess) and this process both resolve."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_AGENT_HOME", raising=False)
    return tmp_path


@pytest.fixture
def ostore(home):
    return FileStateStore(home / "agentctl" / "state")


_LINES = {
    "customer_id": 'customer_id = "user"',
    "customer": 'customer = "the position that posed the task"',
    "functional_place": 'functional_place = "the norm governing releases"',
    "requires_traceability": "requires_traceability = true",
    "activity": 'activity = "release engineering"',
    "difficulty": 'difficulty = "a release takes a day of hand-work"',
    "requirements": (
        'requirements = [\n'
        '  { id = "R1", text = "releases are tagged by a command", derivation = "removes the hand-work" },\n'
        '  { id = "R2", text = "a tag names its commit", derivation = "a release must be traceable" },\n'
        ']'
    ),
    "proposals": (
        'proposals = [\n'
        '  { id = "P1", text = "tag releases automatically", verdict = "adopt", '
        'critique = "cheap, reversible", against = ["R1"] },\n'
        ']'
    ),
    "constraints": 'constraints = [{ id = "K1", text = "no new dependency" }]',
}


def _draft(tmp_path: Path, *, omit=(), replace=None, name="draft.toml") -> Path:
    lines = {**_LINES, **(replace or {})}
    body = "\n".join(text for key, text in lines.items() if key not in omit)
    path = tmp_path / name
    path.write_text(
        '[meta]\ntask_id = "od"\ngoal = "g"\ndone_criterion = "d"\n\n[meta.order]\n' + body + "\n",
        encoding="utf-8",
    )
    return path


def _digest(path: Path) -> str:
    return understanding_digest(load_order_draft(path)[0])


def _start(store, session=SESSION):
    cli.cmd_start(ns(session=session, task="od", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)


def _submit(store, path: Path, session=SESSION):
    return cli.cmd_order_submit(ns(session=session, plan=str(path)), store=store)


def _review(store, verdict="pass", *, reviewer="thinker", target=None, note="", concerns=None,
            plan_digest=None, session=SESSION):
    return cli.cmd_plan_review(
        ns(session=session, scope="order", target=target, verdict=verdict, reviewer=reviewer,
           concerns=concerns, note=note, plan_digest=plan_digest), store=store)


RENDERING = (
    "The order, as I understand it.\n\n"
    "[R1] releases are tagged by a command.\n[R2] a tag names its commit.\n"
    "[P1] tag releases automatically (adopted).\n[K1] no new dependency.\n"
)


def _present(store, tmp_path: Path, text=RENDERING, *, session=SESSION, **kw):
    rendering = tmp_path / "order-rendering.txt"
    rendering.write_text(text, encoding="utf-8")
    return cli.cmd_present_plan(
        ns(session=session, kind="order", rendering_file=str(rendering), emit_skeleton=False,
           plan=kw.get("plan")), store=store)


def _ready(store, tmp_path: Path, **draft_kw) -> Path:
    """A started session with a submitted, passing-reviewed order draft; no plan."""
    _start(store)
    path = _draft(tmp_path, **draft_kw)
    assert _submit(store, path).ok
    assert _review(store).ok
    return path


# --- order-submit ------------------------------------------------------------


@pytest.mark.parametrize("part", [
    "customer_id", "customer", "functional_place", "requirements", "requires_traceability",
    "activity", "difficulty",
])
def test_order_submit_refuses_incomplete_draft(ostore, tmp_path, part):
    _start(ostore)
    path = _draft(tmp_path, omit=(part,))

    d = _submit(ostore, path)

    assert d.ok is False, part
    assert d.data["problems"], part
    state = ostore.load(SESSION)
    assert not (state.order_draft_path or state.order_draft_sha256)


def test_order_submit_refuses_malformed_proposal(ostore, tmp_path):
    _start(ostore)
    bad = _LINES["proposals"].replace('verdict = "adopt"', 'verdict = "maybe"')

    d = _submit(ostore, _draft(tmp_path, replace={"proposals": bad}))

    assert d.ok is False
    assert any("verdict" in p for p in d.data["problems"]), d.data["problems"]
    assert not ostore.load(SESSION).order_draft_path


def test_order_submit_refuses_staged_draft(ostore, tmp_path):
    _start(ostore)
    staged = Path(_write_plan(tmp_path / "staged.toml"))
    text = staged.read_text(encoding="utf-8")
    staged.write_text(
        text.replace("[meta.order]\n", "[meta.order]\n" + _LINES["activity"] + "\n" + _LINES["difficulty"] + "\n", 1),
        encoding="utf-8")
    assert load_order_draft(staged)[1] is True

    d = _submit(ostore, staged)

    assert d.ok is False
    assert "carries stages" in d.detail
    assert not ostore.load(SESSION).order_draft_path


def test_order_submit_records_without_plan_and_keeps_node(ostore, tmp_path):
    _start(ostore)
    before = ostore.load(SESSION)
    path = _draft(tmp_path)

    d = _submit(ostore, path)

    assert d.ok is True, d.detail
    state = ostore.load(SESSION)
    assert state.order_draft_path == str(path)
    assert state.order_draft_sha256 == _digest(path)
    assert d.data["understanding_sha256"] == state.order_draft_sha256
    assert not state.plan_path
    assert state.node == before.node


def test_order_submit_refuses_an_unloadable_draft(ostore, tmp_path):
    _start(ostore)

    assert _submit(ostore, tmp_path / "absent.toml").ok is False
    assert not ostore.load(SESSION).order_draft_path


def test_state_additions_default_empty_and_schema_is_bumped():
    state = SessionState(session_id="x", task_id="t")

    assert (state.order_draft_path, state.order_draft_sha256, state.order_reviews) == ("", "", [])
    assert SCHEMA_VERSION >= 60
    assert SessionState.from_dict(state.to_dict()).order_reviews == []


# --- plan-review --scope order -----------------------------------------------


def test_order_review_records_a_review_bound_to_the_engines_digest(ostore, tmp_path):
    path = _ready(ostore, tmp_path)

    state = ostore.load(SESSION)
    review = state.order_reviews[-1]
    assert (review.verdict, review.reviewer, review.target) == ("pass", "thinker", str(path))
    assert review.understanding_sha256 == _digest(path)
    assert state.plan_review is None and state.plan_stage_reviews == {}
    assert gates.order_review_blockers(state, str(path)) == []


def test_order_review_attestation_is_cross_checked_against_the_file(ostore, tmp_path):
    _start(ostore)
    path = _draft(tmp_path)
    assert _submit(ostore, path).ok

    wrong = _review(ostore, plan_digest="0" * 64)
    right = _review(ostore, plan_digest=hashlib.sha256(path.read_bytes()).hexdigest())

    assert wrong.ok is False
    assert right.ok is True
    assert len(ostore.load(SESSION).order_reviews) == 1


def test_order_review_refuses_a_target_that_is_not_an_order_draft(ostore, tmp_path):
    _start(ostore)
    junk = tmp_path / "junk.toml"
    junk.write_text('[meta]\ntask_id = "x"\n', encoding="utf-8")

    d = _review(ostore, target=str(junk))

    assert d.ok is False
    assert ostore.load(SESSION).order_reviews == []


def test_order_review_without_a_draft_or_target_is_refused(ostore):
    _start(ostore)

    assert _review(ostore).ok is False


def test_order_review_goes_stale_when_the_understanding_changes(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")

    assert gates.order_review_blockers(ostore.load(SESSION), str(path))


def test_order_review_is_not_stale_for_bytes_outside_the_understanding(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace('goal = "g"', 'goal = "another goal"'),
                    encoding="utf-8")

    assert gates.order_review_blockers(ostore.load(SESSION), str(path)) == []


def test_a_revise_does_not_clear_the_gate(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    assert _review(ostore, "revise", concerns=["blocking: the difficulty is vague"]).ok

    assert gates.order_review_blockers(ostore.load(SESSION), str(path))


def test_an_override_needs_a_distinct_reviewer_and_a_note(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    assert _review(ostore, "revise", concerns=["blocking: vague"]).ok

    same = _review(ostore, "override", reviewer="thinker", note="my call")
    no_note = _review(ostore, "override", reviewer="user", note="")
    assert gates.order_review_blockers(ostore.load(SESSION), str(path))
    ok = _review(ostore, "override", reviewer="user", note="accepted as is")

    assert same.ok is False
    assert no_note.data["blockers"]
    assert ok.ok is True
    assert gates.order_review_blockers(ostore.load(SESSION), str(path)) == []


# --- present-plan --kind order -----------------------------------------------


def test_present_order_records_a_receipt_bound_to_the_understanding(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    node = ostore.load(SESSION).node

    d = _present(ostore, tmp_path)

    assert d.ok is True, d.detail
    state = ostore.load(SESSION)
    receipt = gates._plan_presentation_for(state, PLAN_PRESENTATION_KIND_ORDER)
    assert receipt.plan_path == str(path)
    assert receipt.understanding_sha256 == _digest(path)
    assert receipt.rendering_sha256 == hashlib.sha256(RENDERING.encode()).hexdigest()
    assert d.data["order_agree_marker"] == order_agree_marker(_digest(path))
    assert not state.plan_path and state.node == node
    assert gates.order_presentation_stale_reason(state, receipt) is None


def test_present_order_supersedes_its_own_receipt(ostore, tmp_path):
    _ready(ostore, tmp_path)

    assert _present(ostore, tmp_path).ok
    assert _present(ostore, tmp_path, RENDERING + "\nOne more line.\n").ok

    kinds = [p.kind for p in ostore.load(SESSION).plan_presentations]
    assert kinds == [PLAN_PRESENTATION_KIND_ORDER]


def test_present_order_refuses_without_a_submitted_draft(ostore, tmp_path):
    _start(ostore)

    d = _present(ostore, tmp_path)

    assert d.ok is False and "order-submit" in d.detail
    assert ostore.load(SESSION).plan_presentations == []


def test_present_order_refuses_without_a_passing_review(ostore, tmp_path):
    _start(ostore)
    assert _submit(ostore, _draft(tmp_path)).ok

    d = _present(ostore, tmp_path)

    assert d.ok is False and d.data.get("blockers")
    assert ostore.load(SESSION).plan_presentations == []


def test_present_order_refuses_a_plan_argument(ostore, tmp_path):
    _ready(ostore, tmp_path)

    assert _present(ostore, tmp_path, plan=str(tmp_path / "other.toml")).ok is False


def test_present_order_refuses_a_draft_changed_after_submit(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")
    assert _review(ostore).ok  # a fresh review of the edited draft; order-submit is not re-run

    d = _present(ostore, tmp_path)

    assert d.ok is False
    assert "changed since order-submit" in d.detail, d.detail
    assert ostore.load(SESSION).plan_presentations == []


def test_present_order_refuses_when_the_review_is_of_an_older_understanding(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")
    assert _submit(ostore, path).ok  # re-submitted, but not re-reviewed

    d = _present(ostore, tmp_path)

    assert d.ok is False and d.data.get("blockers"), d.detail
    assert ostore.load(SESSION).plan_presentations == []


def test_present_order_refuses_a_draft_that_is_no_longer_complete(ostore, tmp_path):
    path = _ready(ostore, tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("requires_traceability = true\n", ""),
                    encoding="utf-8")
    assert _digest(path) == ostore.load(SESSION).order_draft_sha256

    d = _present(ostore, tmp_path)

    assert d.ok is False
    assert any("requires_traceability" in p for p in d.data["problems"]), d.data
    assert ostore.load(SESSION).plan_presentations == []


@pytest.mark.parametrize("text, missing, extra", [
    (RENDERING.replace("[K1] no new dependency.\n", "no new dependency.\n"), ["K1"], []),
    (RENDERING.replace("[P1]", "P1"), ["P1"], []),
    (RENDERING + "[R9] something the order does not state.\n", [], ["R9"]),
    (RENDERING.replace("[R2]", "[R2](https://example.org/x)"), ["R2"], []),
])
def test_present_order_shows_exactly_the_orders_items_as_anchors(ostore, tmp_path, text, missing, extra):
    _ready(ostore, tmp_path)

    d = _present(ostore, tmp_path, text)

    assert d.ok is False
    assert d.data == {"missing": missing, "extra": extra}
    assert ostore.load(SESSION).plan_presentations == []


def test_other_bracketed_text_is_not_an_anchor(ostore, tmp_path):
    _ready(ostore, tmp_path)

    d = _present(ostore, tmp_path, RENDERING + "See [note] and [order-agree: later] and [S1].\n")

    assert d.ok is True, d.detail


def test_present_order_keeps_the_writer_gate(ostore, tmp_path, monkeypatch):
    _ready(ostore, tmp_path)
    monkeypatch.setattr(cli, "_present_plan_transcript", lambda sid: tmp_path / "no-witness.jsonl")
    (tmp_path / "no-witness.jsonl").write_text("", encoding="utf-8")
    monkeypatch.delenv("AGENTCTL_TECH_WRITER_GATE", raising=False)

    d = _present(ostore, tmp_path)

    assert d.ok is False and "tech-writer" in d.detail
    assert ostore.load(SESSION).plan_presentations == []


# --- the delivery hook and confirm-delivery -----------------------------------


def _ask(session: str, marker: str, transcript: Path) -> dict:
    return {
        "tool_name": "AskUserQuestion",
        "session_id": session,
        "transcript_path": str(transcript),
        "tool_input": {"questions": [{
            "question": "Do you agree this is what you ordered?",
            "options": [{"label": f"Yes, agreed {marker}"}, {"label": "No"}],
        }]},
    }


def _stamp(home: Path):
    return delivery.read_stamp(home / "agentctl" / "state" / f"{SESSION}.json")


def _delivered_transcript(home: Path, rendering: str, *, final: bool = True) -> Path:
    base = time.time() + 5
    landing = text_only_entry(base + 1, rendering) if final else text_then_tool_use_entry(base + 1, rendering)
    return write_transcript(home / "t.jsonl", [
        user_prompt_entry(base), landing, user_prompt_entry(base + 2),
    ])


def test_hook_stamps_a_delivered_order_receipt(ostore, home, tmp_path):
    path = _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    receipt = gates._plan_presentation_for(ostore.load(SESSION), PLAN_PRESENTATION_KIND_ORDER)
    transcript = _delivered_transcript(home, receipt.rendering_text)

    proc = run_hook(_ask(SESSION, order_agree_marker(_digest(path)), transcript), home)

    assert proc.returncode == 0 and not proc.stdout.strip(), proc.stdout
    stamp = _stamp(home)
    assert stamp is not None
    assert (stamp.plan_sha256, stamp.rendering_sha256) == (receipt.plan_sha256, receipt.rendering_sha256)
    assert stamp.source == delivery.SOURCE_HOOK


def test_hook_does_not_stamp_pre_tool_call_text(ostore, home, tmp_path):
    path = _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    receipt = gates._plan_presentation_for(ostore.load(SESSION), PLAN_PRESENTATION_KIND_ORDER)
    transcript = _delivered_transcript(home, receipt.rendering_text, final=False)

    run_hook(_ask(SESSION, order_agree_marker(_digest(path)), transcript), home)

    assert _stamp(home) is None


def test_hook_does_not_stamp_an_ask_for_another_understanding(ostore, home, tmp_path):
    _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    receipt = gates._plan_presentation_for(ostore.load(SESSION), PLAN_PRESENTATION_KIND_ORDER)
    transcript = _delivered_transcript(home, receipt.rendering_text)

    run_hook(_ask(SESSION, order_agree_marker("f" * 64), transcript), home)
    run_hook(_ask(SESSION, "", transcript), home)

    assert _stamp(home) is None


def test_hook_does_not_stamp_a_receipt_whose_draft_has_changed(ostore, home, tmp_path):
    path = _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok
    marker = order_agree_marker(_digest(path))
    receipt = gates._plan_presentation_for(ostore.load(SESSION), PLAN_PRESENTATION_KIND_ORDER)
    transcript = _delivered_transcript(home, receipt.rendering_text)
    path.write_text(path.read_text(encoding="utf-8").replace("hand-work", "toil"), encoding="utf-8")

    run_hook(_ask(SESSION, marker, transcript), home)

    assert _stamp(home) is None


def test_confirm_delivery_accepts_the_order_kind(ostore, home, tmp_path):
    _ready(ostore, tmp_path)
    assert _present(ostore, tmp_path).ok

    d = cli.cmd_confirm_delivery(
        ns(session=SESSION, kind="order", by="fedor", note="hook is not installed here",
           escape_reason=delivery.DELIVERY_ESCAPE_REASONS[0]), store=ostore)

    assert d.ok is True, d.detail
    assert _stamp(home) is not None


def test_order_submit_is_a_user_authority_verb():
    from lib import widening_targets

    assert "order-submit" in widening_targets.AGENTCTL_USER_AUTHORITY_VERBS


# --- the negative control: each planted defect is caught ---------------------

_KILLERS = {
    "order_review_unbound": "test_order_review_goes_stale_when_the_understanding_changes",
    "order_anchor_unchecked": "test_present_order_shows_exactly_the_orders_items_as_anchors",
    "order_receipt_unbound": "test_present_order_records_a_receipt_bound_to_the_understanding",
    "order_stale_draft_presented": "test_present_order_refuses_a_draft_changed_after_submit",
}


def _kill(mutant: str) -> None:
    assert mutant in MUTANTS, f"{mutant} is not in the mutant table"
    env = {**os.environ, ENV: mutant}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-p", "no:cacheprovider",
         "--tb=no", "-rf", "-k", "not _is_killed"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900,
    )

    assert result.returncode == 1, (mutant, result.returncode, result.stdout[-800:])
    assert f"::{_KILLERS[mutant]}" in result.stdout, (mutant, result.stdout[-800:])


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_order_review_unbound_is_killed():
    _kill("order_review_unbound")


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_order_anchor_unchecked_is_killed():
    _kill("order_anchor_unchecked")


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_order_receipt_unbound_is_killed():
    _kill("order_receipt_unbound")


@pytest.mark.skipif(bool(os.environ.get(ENV)), reason="the file is already running under a mutant")
def test_order_stale_draft_presented_is_killed():
    _kill("order_stale_draft_presented")
