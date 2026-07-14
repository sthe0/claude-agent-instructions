"""advisor.enumerate_claims + the ledger-enumerate CLI surface + the third
resolution blocker (the enumeration cross-check having RUN).

The enumeration pass is model perception (a `claude -p --model sonnet` call), so
every test here STUBS the subprocess with a fake runner returning fixed lines — no
live model call ever fires in CI. Covers:
  - enumerate_claims is fail-open (None runner / non-zero exit -> []) and
    cost-bounded (the argv carries `--model sonnet`);
  - ledger-enumerate ingests each returned line as a 'raised' candidate, flips
    bag['enumerated']=True, and NEVER auto-dispositions;
  - the resolution gate blocks while the cross-check is un-run even when the
    recorded claims/candidates are closed, blocks on the raised candidates it
    produced, and passes once the cross-check has run AND every candidate is
    dispositioned;
  - a missing artifact is a recoverable noop-error, not a crash;
  - an empty enumeration still flips the flag (the pass RAN) with no candidates.
"""
from __future__ import annotations

from argparse import Namespace

from agentctl import advisor, cli, plugins
from agentctl.dispatch import RunResult
from agentctl.state import SessionState


class FakeRunner:
    """Records argv and returns a fixed RunResult — stands in for `claude -p`."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, argv):
        self.calls.append(argv)
        return RunResult(self._returncode, self._stdout, "")


def _new_state(sid="s", **kw):
    return SessionState(session_id=sid, task_id="t", **kw)


def _enumerate(store, sid, *, artifact, runner):
    return cli.cmd_ledger_enumerate(
        Namespace(session=sid, artifact=str(artifact)), store=store, runner=runner
    )


def _artifact(tmp_path, text="Chose approach A because latency spiked 3x."):
    p = tmp_path / "deliverable.md"
    p.write_text(text, encoding="utf-8")
    return p


# --- advisor.enumerate_claims: fail-open + cost bound ---------------------------

def test_enumerate_claims_fail_open_on_none_runner():
    assert advisor.enumerate_claims("some text", None) == []


def test_enumerate_claims_fail_open_on_nonzero_exit():
    assert advisor.enumerate_claims("some text", FakeRunner("a\nb", returncode=1)) == []


def test_enumerate_claims_parses_lines_and_is_cost_bounded():
    runner = FakeRunner("chose approach A\nlatency spiked 3x\n")
    out = advisor.enumerate_claims("text", runner)
    assert out == ["chose approach A", "latency spiked 3x"]
    argv = runner.calls[0]
    assert "--model" in argv and "sonnet" in argv
    # the timeout half of the cost bound lives in advisor.subprocess_runner
    assert advisor._ADVISOR_TIMEOUT_S == 20


# --- ledger-enumerate: guard, ingest, flip flag, never disposition --------------

def test_ledger_enumerate_refused_when_plugin_inactive(store, tmp_path):
    state = _new_state()
    store.save(state)
    d = _enumerate(store, "s", artifact=_artifact(tmp_path), runner=FakeRunner("x"))
    assert d.ok is False
    assert d.action == "noop"
    assert "ledger" not in store.load("s").plugins


def test_ledger_enumerate_ingests_candidates_and_flips_flag(store, tmp_path):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    runner = FakeRunner("chose approach A\nload will grow 2x\n")
    d = _enumerate(store, "s", artifact=_artifact(tmp_path), runner=runner)
    assert d.ok is True
    assert d.data["raised"] == ["enum-1", "enum-2"]
    assert d.data["enumerated"] is True

    bag = store.load("s").plugins["ledger"]
    assert bag["enumerated"] is True
    assert [c["id"] for c in bag["candidates"]] == ["enum-1", "enum-2"]
    assert bag["candidates"][0]["statement"] == "chose approach A"
    # raises only — never auto-dispositions
    assert all(c["disposition"] == "raised" for c in bag["candidates"])


def test_ledger_enumerate_empty_result_flips_flag_with_no_candidates(store, tmp_path):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    d = _enumerate(store, "s", artifact=_artifact(tmp_path), runner=FakeRunner(""))
    assert d.ok is True
    assert d.data["raised"] == []
    bag = store.load("s").plugins["ledger"]
    assert bag["enumerated"] is True  # the pass RAN even though it raised nothing
    assert bag["candidates"] == []


def test_ledger_enumerate_upserts_last_wins_by_id(store, tmp_path):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    _enumerate(store, "s", artifact=_artifact(tmp_path), runner=FakeRunner("first\nsecond"))
    _enumerate(store, "s", artifact=_artifact(tmp_path), runner=FakeRunner("rewritten"))
    bag = store.load("s").plugins["ledger"]
    # enum-1 is upserted (last wins); enum-2 from the first run remains
    assert bag["candidates"][0]["statement"] == "rewritten"


def test_ledger_enumerate_missing_artifact_is_recoverable(store, tmp_path):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)
    d = cli.cmd_ledger_enumerate(
        Namespace(session="s", artifact=str(tmp_path / "nope.md")),
        store=store, runner=FakeRunner("x"),
    )
    assert d.ok is False
    assert d.action == "noop"
    assert "cannot read artifact" in d.detail
    # unchanged: no candidates, flag not flipped
    assert store.load("s").plugins["ledger"]["enumerated"] is False


# --- resolution gate: cross-check mandatory + raised candidates block -----------

def test_gate_blocks_while_cross_check_unrun_even_with_closed_claims():
    state = _new_state()
    plugins.activate(state, "ledger", {
        "claims": [{"id": "c1", "status": "axiom", "statement": "x", "source": "dashboard"}],
    })  # enumerated defaults False
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert any("enumeration cross-check not run" in b for b in blockers)
    state.plugins["ledger"]["enumerated"] = True
    assert plugins.plugin_gate_blockers(state, "resolution") == []


def test_gate_blocks_on_raised_candidate_then_passes_once_run_and_disposed(store, tmp_path):
    state = _new_state()
    plugins.activate(state, "ledger")
    store.save(state)

    # ground one axiom claim so claim-closure is satisfied
    cli.cmd_ledger_add(Namespace(
        session="s", id="c1", status="axiom", statement="measured load",
        source="prod dashboard", premises=None, basis="", load_bearing=True,
    ), store=store)

    # run the cross-check: it raises enum-1
    _enumerate(store, "s", artifact=_artifact(tmp_path), runner=FakeRunner("chose approach A"))

    state = store.load("s")
    blockers = plugins.plugin_gate_blockers(state, "resolution")
    assert any("enum-1" in b for b in blockers)  # raised candidate blocks
    assert not any("enumeration cross-check not run" in b for b in blockers)  # flag flipped

    # disposition it: recorded, linked to the grounded claim
    cli.cmd_ledger_dispose(Namespace(
        session="s", id="enum-1", as_="recorded", reason="", claim="c1",
    ), store=store)

    assert plugins.plugin_gate_blockers(store.load("s"), "resolution") == []
