"""question-enumerate: the CLI verbs are retired (amendments-2.md E3); the advisor's
whole-plan question pass is gone too.

  * `question-enumerate`, `question-enumerate-worker`, `question-enumerate-escape` —
    the verbs the enumeration used to run through. Their names stay in the parser so
    a hand-typed or scripted call meets a message pointing at the review route; each
    exits 2 BEFORE reading or writing any state and never reaches the runner. The
    questions now arrive with every review act (test_norm_staleness_review_questions.py).
"""
from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from agentctl import cli, plugins
from agentctl.state import SessionState


# --- stub runner ---------------------------------------------------------------

def _runner(stdout, *, returncode=0):
    """A stub advisor runner: returns a fixed RunResult-shaped object, records the
    argv it was handed so a test can assert no call was made."""
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls
    return run


def _state(store, sid="s"):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    store.save(state)
    return state


# --- the retired verbs ----------------------------------------------------------

_RETIRED = [
    ("question-enumerate", cli.cmd_question_enumerate, {"plan": None}),
    ("question-enumerate-worker", cli.cmd_question_enumerate_worker,
     {"plan": "p.toml", "digest": "d"}),
    ("question-enumerate-escape", cli.cmd_question_enumerate_escape,
     {"plan": None, "reason": "runner-unavailable", "note": "n"}),
]


@pytest.mark.parametrize("command,handler,extra", _RETIRED)
def test_retired_verb_exits_2_without_calling_the_runner(
        store, capsys, command, handler, extra):
    _state(store)
    run = _runner("plan.goal\tq?")
    with pytest.raises(SystemExit) as exc:
        handler(Namespace(session="s", command=command, **extra), store=store, runner=run)
    assert exc.value.code == 2
    assert run.calls == []
    err = capsys.readouterr().err
    assert command in err and "--customer-question" in err


@pytest.mark.parametrize("command,handler,extra", _RETIRED)
def test_retired_verb_leaves_state_unchanged(store, command, handler, extra):
    _state(store)
    before = store.load("s")
    with pytest.raises(SystemExit):
        handler(Namespace(session="s", command=command, **extra), store=store)
    after = store.load("s")
    assert after.plugins["premise"] == before.plugins["premise"]
    assert after.history == before.history


def test_retired_verb_is_dispatched_through_main_with_exit_2(tmp_path, capsys):
    root = str(tmp_path / "state")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--state-root", root, "question-enumerate", "--session", "s"])
    assert exc.value.code == 2
    assert "retired" in capsys.readouterr().err
