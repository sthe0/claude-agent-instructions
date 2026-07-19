"""The runtime negative-end-state proof for the premise (question-provenance) gate.

Every OTHER premise test drives the plugin through the `cmd_*(Namespace, store=)`
seam — a unit surface. This one drives the REAL `cli.main(["--state-root", ...])`
dispatch (argparse, the COMMANDS table, the on-disk StateStore) against a session
NOBODY hand-armed: `premise` is armed only because `classify` routed the session
SUBSTANTIVE, exactly as a production session arms it. It proves the two directions
the gate must satisfy at the plan_approval boundary:

  * approve is REFUSED while a question is open, while a question is escalated with
    no own_research, and while the enumeration cross-check has not run — three
    distinct blocker origins, each surfaced through the real `approve` verb's
    `data["blockers"]` (prefixed `[premise] ...` by plugins.plugin_gate_blockers);
  * approve is ALLOWED once every question is dispositioned AND the enumeration
    cross-check has run against the current plan content.

`question-enumerate` has no `runner=` seam through `cli.main` (the COMMANDS table
calls each verb with only `store=`), so the advisor runner is stubbed at its
module-level fallback — `advisor.subprocess_runner` — never a live `claude -p`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import advisor, cli, plan, plugins_premise
from agentctl.store import FileStateStore


@pytest.fixture(autouse=True)
def _premise_armed(monkeypatch):
    """Override conftest's suite-wide AGENTCTL_PREMISE=0 force-off so the real
    weight_class-alone arming predicate runs (a module-local autouse fixture runs
    after conftest's, so this delenv wins). conftest's AGENTCTL_PLAN_REVIEW=0 stays,
    keeping the unrelated thinker-review gate out of this test's approve path."""
    monkeypatch.delenv("AGENTCTL_PREMISE", raising=False)


@pytest.fixture(autouse=True)
def _stub_advisor_runner(monkeypatch):
    """`question-enumerate` driven through cli.main() falls back to
    advisor.subprocess_runner (no runner= kwarg on the dispatch path). Stub it to a
    healthy, question-less pass: enumerated flips True, zero candidates — enough to
    discharge the mandatory cross-check without a live subprocess. Individual tests
    that never call question-enumerate are unaffected."""
    def run(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(advisor, "subprocess_runner", run)


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "state")


# Absolute, resolved from THIS file — the verify_command drives pytest from repo_root,
# not scripts/, and submit-plan stores plan_path verbatim for approve to re-parse; a
# relative fixture path would resolve only when cwd happens to be scripts/.
_PLAN = str(Path(__file__).resolve().parent / "fixtures" / "plan_two_stage.toml")


def _run(capsys, root, *argv):
    """Drive the real CLI; return (rc, parsed-directive-dict). rc is 0 when the
    directive is ok, else 1; stdout is the directive JSON (warnings go to stderr)."""
    import json
    rc = cli.main(["--state-root", root, *argv])
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def _build_substantive(capsys, root, sid="e2e"):
    """start -> classify (SUBSTANTIVE, deliverable_kind UNSET) -> plan -> submit-plan.
    `--architectural` alone routes SUBSTANTIVE; omitting --deliverable-kind leaves it
    '', proving premise arms on weight_class alone (the gap-2 fix)."""
    _run(capsys, root, "start", "--session", sid, "--task", "t", "--goal", "g",
         "--done-criterion", "dc", "--criterion-type", "measurable")
    _run(capsys, root, "classify", "--session", sid, "--architectural")
    _run(capsys, root, "plan", "--session", sid)
    _run(capsys, root, "submit-plan", "--session", sid, "--plan", _PLAN)
    return sid


def _blockers(directive):
    return (directive.get("data") or {}).get("blockers") or []


# --- arming: SUBSTANTIVE alone, deliverable_kind never named --------------------

def test_fresh_substantive_session_arms_premise_without_deliverable_kind(capsys, root):
    _run(capsys, root, "start", "--session", "e2e", "--task", "t", "--goal", "g",
         "--done-criterion", "dc", "--criterion-type", "measurable")
    rc, d = _run(capsys, root, "classify", "--session", "e2e", "--architectural")
    assert rc == 0

    state = FileStateStore(root).load("e2e")
    assert state.weight_class == "SUBSTANTIVE"
    assert state.deliverable_kind == ""          # never named on the CLI
    assert "premise" in state.plugins            # armed anyway
    assert state.plugins["premise"]["enumerated"] is False


# --- approve REFUSED: an open question ------------------------------------------

def test_approve_refused_with_open_question(capsys, root):
    sid = _build_substantive(capsys, root)
    _run(capsys, root, "question-raise", "--session", sid, "--id", "Q1",
         "--target", "plan.goal", "--question", "is the goal even agreed?")
    _run(capsys, root, "question-enumerate", "--session", sid)  # discharge the cross-check

    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 1
    assert d["ok"] is False
    assert d["action"] == "fix_plan"
    assert any("[premise]" in b and "open" in b for b in _blockers(d))


# --- approve REFUSED: escalated with no own_research ----------------------------

def test_approve_refused_with_escalated_and_empty_own_research(capsys, root):
    sid = _build_substantive(capsys, root)

    # The CLI's question-dispose fast-fail refuses `--to escalated` with empty
    # own_research, so the ONLY way this state reaches approve is a bag that
    # bypassed the CLI (a hand-edited state, a future bug). The gate — not the CLI
    # fast-fail — is the real authority, so inject the state directly and prove the
    # real approve verb still refuses it. Stamp the enumeration against current
    # content so the escalation blocker is the one isolated blocker.
    store = FileStateStore(root)
    state = store.load(sid)
    state.plugins["premise"]["questions"] = [{
        "id": "Q9", "target": "plan.goal", "question": "reachable?",
        "disposition": "escalated", "answer": "ask the user", "own_research": "",
    }]
    state.plugins["premise"]["enumerated"] = True
    state.plugins["premise"]["enumerated_at"] = plugins_premise._plan_content_digest(
        plan.load_plan(state.plan_path))
    store.save(state)

    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 1
    assert d["ok"] is False
    assert any("[premise]" in b and "own_research" in b for b in _blockers(d))


# --- approve REFUSED: the enumeration cross-check has not run -------------------

def test_approve_refused_when_not_enumerated(capsys, root):
    sid = _build_substantive(capsys, root)
    # no open questions, but enumeration never ran — the mandatory cross-check blocks
    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 1
    assert d["ok"] is False
    assert any("[premise]" in b and "enumeration cross-check not run" in b
               for b in _blockers(d))


# --- approve ALLOWED: every question dispositioned AND enumeration run ----------

def test_approve_allowed_when_dispositioned_and_enumerated(capsys, root):
    sid = _build_substantive(capsys, root)
    _run(capsys, root, "question-raise", "--session", sid, "--id", "Q1",
         "--target", "plan.goal", "--question", "is the goal even agreed?")
    _run(capsys, root, "question-research", "--session", sid, "--id", "Q1",
         "--attempted", "read the tracker thread and the two prior runs")
    _run(capsys, root, "question-dispose", "--session", sid, "--id", "Q1",
         "--to", "assumed", "--basis", "confirmed reachable by the reporter",
         "--risk", "the reporter may be wrong")
    _run(capsys, root, "question-enumerate", "--session", sid)

    rc, d = _run(capsys, root, "approve", "--session", sid, "--by", "user")
    assert rc == 0
    assert d["ok"] is True
    assert not _blockers(d)
