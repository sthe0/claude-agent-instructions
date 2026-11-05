"""`classify --title` titles the tmux pane of a top-level session with the task it works on.

Pure: no tmux server runs. CLI-level tests replace `agentctl.pane_title.read_env` and
`agentctl.pane_title.RUNNER` -- the two seams cmd_classify reads through module attributes --
and assert on the state reloaded from disk. Test names are fixed: the negative-control mutant
(owns_pane reduced to "TMUX_PANE is set") must fail exactly branches 07-10."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli, config, pane_title

REPO = Path(__file__).resolve().parent.parent.parent
PANE = "%7"
# Captured at import: the negative-control plugin replaces pane_title.owns_pane per test, and the
# direct unit test of the rule must keep seeing the real one.
REAL_OWNS_PANE = pane_title.owns_pane
OWNED_ENV = {"TMUX_PANE": PANE}


class Recorder:
    def __init__(self, *, returncode=0, raises=None):
        self.calls = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if self.raises:
            raise self.raises
        return Namespace(returncode=self.returncode)


def ns(**kw):
    return Namespace(**kw)


def _start(store, sid):
    cli.cmd_start(ns(session=sid, task=f"t-{sid}", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)


def _classify(store, sid, monkeypatch, env, recorder, *, title=None, chat=False):
    monkeypatch.setattr("agentctl.pane_title.read_env", lambda: env)
    monkeypatch.setattr("agentctl.pane_title.RUNNER", recorder)
    return cli.cmd_classify(
        ns(session=sid, chat=chat, changed_lines=10, wall_clock_min=1, tracker_key=None,
           architectural=False, external_effect=False, new_dependency=False,
           public_api_change=False, title=title),
        store=store,
    )


def _setup(store, sid="s1"):
    _start(store, sid)
    return sid


def _not_owned_case(store, monkeypatch, env, *, mutate=None):
    sid = _setup(store)
    if mutate:
        state = store.load(sid)
        mutate(state)
        store.save(state)
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, env, rec)
    assert d.ok, d.detail
    assert rec.calls == []
    assert store.load(sid).task_title == ""
    return sid, rec


def test_branch01_owned_no_title(store, monkeypatch):
    sid = _setup(store)
    before = store.load(sid).to_json()
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, OWNED_ENV, rec)
    assert not d.ok
    assert d.action == "classify"
    assert "classify needs --title" in d.detail
    assert rec.calls == []
    assert store.load(sid).to_json() == before


def test_branch02_owned_with_title(store, monkeypatch):
    sid = _setup(store)
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, OWNED_ENV, rec, title="Probe title")
    assert d.ok, d.detail
    assert rec.calls == [["tmux", "select-pane", "-t", PANE, "-T", "✳ Probe title"]]
    assert store.load(sid).task_title == "Probe title"


def test_branch03_second_classify_after_reset(store, monkeypatch):
    sid = _setup(store)
    rec = Recorder()
    assert _classify(store, sid, monkeypatch, OWNED_ENV, rec, title="First title").ok
    cli.cmd_reset(ns(session=sid, task="t-second", goal="", done_criterion="",
                     criterion_type="measurable", recursion_depth=0), store=store)
    assert _classify(store, sid, monkeypatch, OWNED_ENV, rec, title="Second title").ok
    assert [c[-1] for c in rec.calls] == ["✳ First title", "✳ Second title"]
    assert store.load(sid).task_title == "Second title"


@pytest.mark.parametrize("bad", ["x" * 49, "two\nlines", "two\rlines", "   ", "\x07\x1b"])
def test_branch04_malformed_title(store, monkeypatch, bad):
    sid = _setup(store)
    before = store.load(sid).to_json()
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, OWNED_ENV, rec, title=bad)
    assert not d.ok
    assert rec.calls == []
    assert store.load(sid).to_json() == before


def test_branch04_malformed_title_refused_when_not_owned(store, monkeypatch):
    sid = _setup(store)
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, {}, rec, title="x" * 49)
    assert not d.ok
    assert store.load(sid).task_title == ""


def test_branch04_title_at_limit_and_control_chars_stripped(store, monkeypatch):
    sid = _setup(store)
    rec = Recorder()
    title = "x" * 48
    assert _classify(store, sid, monkeypatch, OWNED_ENV, rec, title=title).ok
    assert store.load(sid).task_title == title
    assert pane_title.normalize_title("a\x07b\x1b[c") == ("ab[c", "")
    assert pane_title.normalize_title("a\nb")[0] == ""


def test_branch05_chat_never_retitles_or_refuses(store, monkeypatch):
    sid = _setup(store)
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, OWNED_ENV, rec, chat=True)
    assert d.ok, d.detail
    assert rec.calls == []
    assert store.load(sid).task_title == ""


def test_branch06_tmux_pane_unset(store, monkeypatch):
    _not_owned_case(store, monkeypatch, {})


def test_branch07_depth_one_env(store, monkeypatch):
    _not_owned_case(store, monkeypatch, {**OWNED_ENV, "AGENT_RECURSION_DEPTH": "1"})


def test_branch08_parent_link_set(store, monkeypatch):
    def link(state):
        state.parent_link = {"parent_session": "p", "stage_index": 1, "depth": 1}

    _not_owned_case(store, monkeypatch, OWNED_ENV, mutate=link)


def test_branch09_pytest_current_test_set(store, monkeypatch):
    _not_owned_case(store, monkeypatch, {**OWNED_ENV, "PYTEST_CURRENT_TEST": "t::x (call)"})


def test_branch10_switch_off(store, monkeypatch):
    env = {**OWNED_ENV, "AGENTCTL_TASK_TITLE": "0"}
    _not_owned_case(store, monkeypatch, env)
    sid = _setup(store, "s2")
    rec = Recorder()
    d = _classify(store, sid, monkeypatch, env, rec, title="Given anyway")
    assert d.ok, d.detail
    assert rec.calls == []
    assert store.load(sid).task_title == "Given anyway"


@pytest.mark.parametrize("runner", [
    Recorder(returncode=1),
    Recorder(raises=OSError("no tmux")),
    Recorder(raises=TimeoutError("slow")),
], ids=["nonzero", "oserror", "timeout"])
def test_branch11_failure_is_logged_and_fail_open(store, monkeypatch, runner):
    sid = _setup(store)
    d = _classify(store, sid, monkeypatch, OWNED_ENV, runner, title="Stale pane")
    assert d.ok, d.detail
    assert len(runner.calls) == 1
    state = store.load(sid)
    assert state.task_title == "Stale pane"
    failures = [e for e in state.history if e["event"] == "pane_title_failed"]
    assert len(failures) == 1
    assert failures[0]["reason"]


def test_branch12_config():
    on, off = {"task-title": "on"}, {"task-title": "off"}
    enabled = config.task_title_enabled
    assert enabled({"AGENTCTL_TASK_TITLE": "1"}, off) is True
    assert enabled({"AGENTCTL_TASK_TITLE": "0"}, on) is False
    assert enabled({"AGENTCTL_TASK_TITLE": "yes"}, on) is True
    assert enabled({"AGENTCTL_TASK_TITLE": "yes"}, off) is False
    assert enabled({}, on) is True
    assert enabled({}, off) is False
    assert enabled({}, {}) is False


def test_missing_config_file_is_off(monkeypatch, tmp_path):
    def unreadable(path=None):
        raise OSError("gone")

    monkeypatch.setattr(config, "parse_config_md", unreadable)
    assert config.task_title_enabled({}) is False


def test_owns_pane_reports_each_reason():
    owns = REAL_OWNS_PANE
    state = ns(parent_link=None, recursion_depth=0)
    on = {"task-title": "on"}
    assert owns(OWNED_ENV, state, on) == (True, "")
    assert owns({}, state, on)[0] is False
    assert owns(OWNED_ENV, ns(parent_link={"x": 1}, recursion_depth=0), on)[0] is False
    assert owns(OWNED_ENV, ns(parent_link=None, recursion_depth=2), on)[0] is False
    assert owns({**OWNED_ENV, "AGENT_RECURSION_DEPTH": "0"}, state, on)[0] is True
    reasons = {
        owns(env, st, on)[1]
        for env, st in [({}, state), (OWNED_ENV, ns(parent_link={"x": 1}, recursion_depth=0)),
                        (OWNED_ENV, ns(parent_link=None, recursion_depth=2)),
                        ({**OWNED_ENV, "AGENT_RECURSION_DEPTH": "3"}, state),
                        ({**OWNED_ENV, "PYTEST_CURRENT_TEST": "x"}, state)]
    }
    assert len(reasons) == 5 and "" not in reasons


def test_set_pane_title_never_raises_and_logs_once():
    logged = []
    ok = pane_title.set_pane_title(OWNED_ENV, "T", run=Recorder(raises=RuntimeError("boom")),
                                   log=logged.append)
    assert ok is False and len(logged) == 1
    assert pane_title.set_pane_title(OWNED_ENV, "T", run=Recorder(), log=logged.append) is True
    assert len(logged) == 1


def test_docs_row():
    assert config.parse_config_md().get("task-title") == "on"
    assert "--title" in (REPO / "scripts/agentctl/README.md").read_text(encoding="utf-8")
    assert "classify --title" in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
