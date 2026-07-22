"""question-candidate-dispose: the CLI verb that disposes ONE question-enumeration
candidate (bag['candidates'], written by question-enumerate) as 'recorded' (linked
to an existing Question via --question) or 'dismissed' (with --reason). Mirrors
cmd_ledger_dispose's shape and validation but writes premise.QuestionCandidate
entries, not ledger.Candidate — the two stores have different entry shapes and
disposition referents, which is why question-dispose (which only ever touches
bag['questions']) cannot be overloaded to do this job.

Before this command existed, question-enumerate's own success message advised
`question-dispose --id <qenum-N>` — that command searches only bag['questions']
and returns "no such question" for a qenum-N id, so every raised candidate was a
permanent, CLI-unreachable plan_approval blocker (bag['candidates'] has no
disposing verb at all). Covers: guard on unknown id, --question required AND
resolvable for --as recorded, --reason required for --as dismissed, both success
paths, and that a fully-dispositioned candidate bag no longer trips
premise.validate_question_candidates at the plan_approval gate.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import cli, plugins, plugins_premise
from agentctl.state import SessionState


# --- helpers -------------------------------------------------------------------

def _state(store, sid="s"):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    store.save(state)
    return state


def _raise_question(store, sid, *, id, target="plan.goal", question="?"):
    return cli.cmd_question_raise(
        Namespace(session=sid, id=id, target=target, question=question), store=store)


def _seed_candidate(store, sid, *, id, statement="q?"):
    """Seed a bare 'raised' QuestionCandidate directly into the bag — exactly the
    shape question-enumerate writes (id, statement, disposition='raised',
    reason='', question=''). There is no CLI verb that raises a bare candidate
    (only the advisor-backed enumerate pass does), so tests seed it directly,
    mirroring how test_ledger_candidates exercises ledger-dispose via a
    directly-seeded bag entry."""
    state = store.load(sid)
    bag = state.plugins["premise"]
    bag.setdefault("candidates", []).append(
        {"id": id, "statement": statement, "disposition": "raised", "reason": "", "question": ""})
    store.save(state)


def _dispose(store, sid, *, id, as_, reason="", question=""):
    return cli.cmd_question_candidate_dispose(
        Namespace(session=sid, id=id, as_=as_, reason=reason, question=question), store=store)


def _candidates(store, sid):
    return store.load(sid).plugins["premise"]["candidates"]


# --- the plugin-inactive guard (mirrors ledger-dispose's) -----------------------

def test_dispose_refused_when_plugin_inactive(store):
    state = SessionState(session_id="s", task_id="t")
    store.save(state)
    d = _dispose(store, "s", id="qenum-1", as_="dismissed", reason="n/a")
    assert d.ok is False
    assert d.action == "noop"
    assert "premise" not in store.load("s").plugins


# --- error on unknown id ---------------------------------------------------------

def test_dispose_refused_when_candidate_unknown(store):
    _state(store)
    d = _dispose(store, "s", id="ghost", as_="dismissed", reason="n/a")
    assert d.ok is False
    assert "no such candidate" in d.detail


# --- recorded requires --question, and --question must resolve -----------------

def test_dispose_recorded_requires_question(store):
    _state(store)
    _seed_candidate(store, "s", id="qenum-1")
    d = _dispose(store, "s", id="qenum-1", as_="recorded")
    assert d.ok is False
    assert "--question" in d.detail
    # refused before mutating the candidate's disposition
    assert _candidates(store, "s")[0]["disposition"] == "raised"


def test_dispose_recorded_requires_resolvable_question(store):
    _state(store)
    _seed_candidate(store, "s", id="qenum-1")
    d = _dispose(store, "s", id="qenum-1", as_="recorded", question="Q-ghost")
    assert d.ok is False
    assert "does not resolve" in d.detail
    assert _candidates(store, "s")[0]["disposition"] == "raised"


# --- dismissed requires --reason -------------------------------------------------

def test_dispose_dismissed_requires_reason(store):
    _state(store)
    _seed_candidate(store, "s", id="qenum-1")
    d = _dispose(store, "s", id="qenum-1", as_="dismissed")
    assert d.ok is False
    assert "--reason" in d.detail


# --- success paths ---------------------------------------------------------------

def test_dispose_recorded_sets_question(store):
    _state(store)
    _raise_question(store, "s", id="Q1", target="plan.goal")
    _seed_candidate(store, "s", id="qenum-1")
    d = _dispose(store, "s", id="qenum-1", as_="recorded", question="Q1")
    assert d.ok is True
    candidate = _candidates(store, "s")[0]
    assert candidate["disposition"] == "recorded"
    assert candidate["question"] == "Q1"


def test_dispose_dismissed_sets_reason(store):
    _state(store)
    _seed_candidate(store, "s", id="qenum-1")
    d = _dispose(store, "s", id="qenum-1", as_="dismissed", reason="out of scope")
    assert d.ok is True
    candidate = _candidates(store, "s")[0]
    assert candidate["disposition"] == "dismissed"
    assert candidate["reason"] == "out of scope"


# --- gate-unblock: dispositioning every candidate clears the blocker -----------

def test_dispositioning_all_candidates_clears_gate(store):
    _state(store)
    _raise_question(store, "s", id="Q1", target="plan.goal")
    _seed_candidate(store, "s", id="qenum-1")
    _seed_candidate(store, "s", id="qenum-2")

    live = store.load("s")
    blockers = plugins_premise.premise_blockers(live, live.plugins["premise"])
    assert any("undispositioned enumeration candidate 'qenum-1'" in b for b in blockers)
    assert any("undispositioned enumeration candidate 'qenum-2'" in b for b in blockers)

    d1 = _dispose(store, "s", id="qenum-1", as_="recorded", question="Q1")
    d2 = _dispose(store, "s", id="qenum-2", as_="dismissed", reason="not applicable here")
    assert d1.ok is True and d2.ok is True

    live = store.load("s")
    blockers = plugins_premise.premise_blockers(live, live.plugins["premise"])
    assert not any("qenum-1" in b or "qenum-2" in b for b in blockers)
