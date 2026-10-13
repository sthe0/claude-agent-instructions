"""A re-dispatched linked stage resumes its child's engine session.

Every dispatch of a linked stage used to mint a fresh child session, so what the root
recorded on the child's session between two spawns (an approval of its plan, a review
override, order elements) never reached the next child. The spawn now keeps a record
`<state root>/child-sessions/<parent>-<stage>.json` naming the child's session and its
workdir, and resumes that session (`claude -p --resume <id>`) when it is not RESOLVED,
the workdir is the same and its transcript exists; otherwise it mints a fresh one as
before. The own channel also admits `order-raise` / `order-dispose`.

Six tests fail on the base tree at their `BEHAVIOUR:` assertion; six pin what must stay
as on base. The spawn is driven up to the child launch through the stubbed launcher of
`test_child_contour_gate`; the launcher writes no transcript, so a test that expects a
resume creates the previous child's `<id>.jsonl` itself."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentctl.store import FileStateStore
from test_child_contour_gate import (
    CHILD,
    HANDOFF_ENV,
    OTHER,
    OWN_ENV,
    PARENT,
    REFUSED,
    _AMBIENT_ENV,
    _cli,
    _load_spawn,
    _parent_session,
    _spawn_argv,
    _state_dir,
    contour,  # noqa: F401 - the fixture
)

STAGE = 1


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config"
    skill = config / "skills" / "thinker"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# stub thinker skill for tests\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(config))
    norms = tmp_path / "norms"
    norms.mkdir()
    monkeypatch.setenv("AGENTCTL_NORM_ROOTS", str(norms))
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")


class _Launched(Exception):
    def __init__(self, cmd, env, cwd):
        super().__init__("launched")
        self.cmd, self.env, self.cwd = cmd, env, cwd


class Spawned:
    def __init__(self, cmd, env, cwd):
        self.cmd, self.env, self.cwd = cmd, env, cwd

    def option(self, name: str) -> str | None:
        return self.cmd[self.cmd.index(name) + 1] if name in self.cmd else None

    def session_args(self) -> dict:
        return {name: self.option(name) for name in ("--session-id", "--resume") if name in self.cmd}


def _spawn(monkeypatch, home: Path, workdir: Path) -> Spawned:
    """One linked spawn for stage 1 of the parent session, run up to the child launch."""
    spawn = _load_spawn()
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, cwd=None, **kw):
        raise _Launched(list(cmd), dict(env or {}), cwd)

    monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        rc = spawn.main(_spawn_argv(home, workdir, STAGE))
    except _Launched as launched:
        return Spawned(launched.cmd, launched.env, launched.cwd)
    raise AssertionError(f"setup: the spawn did not reach the launch (rc={rc})")


def _first_child(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Spawned]:
    home, workdir = tmp_path / "home", tmp_path / "work"
    _parent_session(home)
    first = _spawn(monkeypatch, home, workdir)
    child = first.option("--session-id")
    assert child and first.env.get(OWN_ENV) == child, "setup: the first spawn is not linked"
    return home, workdir, first


def _write_transcript(workdir: Path, session: str) -> None:
    """The previous child's transcript, where the harness keeps it for that workdir."""
    project = _load_spawn()._project_dir_name(str(workdir))
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user"}\n', encoding="utf-8")


def _assert_resumed(second: Spawned, child: str) -> None:
    assert second.option("--resume") == child, (
        f"BEHAVIOUR: the re-dispatch did not resume the unresolved child session {child}: "
        f"{second.session_args()}"
    )
    assert "--session-id" not in second.cmd


def _assert_fresh(spawned: Spawned, previous: str | None) -> None:
    fresh = spawned.option("--session-id")
    assert fresh and fresh != previous and "--resume" not in spawned.cmd, (
        f"BEHAVIOUR: the spawn did not mint a fresh child session: {spawned.session_args()}"
    )
    assert spawned.env.get(OWN_ENV) == fresh


# --- session reuse ---------------------------------------------------------------------


def test_redispatch_resumes_unresolved_child_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    _assert_resumed(second, child)


def test_redispatch_reuses_own_session_env(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    monkeypatch.setenv(HANDOFF_ENV, first.env[HANDOFF_ENV])
    _cli(_state_dir(home), "start", session=child, task="child", goal="g", done_criterion="dc")
    monkeypatch.delenv(HANDOFF_ENV)
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    assert second.env.get(OWN_ENV) == child, (
        f"BEHAVIOUR: the re-dispatched child is pinned to {second.env.get(OWN_ENV)!r}, not {child!r}"
    )
    assert second.env.get(HANDOFF_ENV) == first.env[HANDOFF_ENV], (
        "the resumed child is not handed the norm handoff its engine session adopted"
    )


def test_resumed_spawn_runs_in_first_spawn_workdir(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    assert second.option("--resume") == child, (
        f"BEHAVIOUR: the re-dispatch in the first spawn's workdir did not resume {child}: "
        f"{second.session_args()}"
    )
    assert str(second.cwd) == str(workdir), (second.cwd, workdir)


def test_previous_child_record_survives_parent_state_roundtrip(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)
    store = FileStateStore(_state_dir(home))
    store.save(store.load(PARENT))
    assert child not in store.path(PARENT).read_text(encoding="utf-8"), (
        "the parent's session state names the child session"
    )
    second = _spawn(monkeypatch, home, workdir)
    _assert_resumed(second, child)


def test_first_dispatch_mints_fresh_child_session(tmp_path, monkeypatch):
    home, workdir = tmp_path / "home", tmp_path / "work"
    _parent_session(home)
    first = _spawn(monkeypatch, home, workdir)
    _assert_fresh(first, None)
    assert first.option("--session-id") != PARENT
    assert str(first.cwd) == str(workdir)


def test_resolved_previous_child_gets_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    monkeypatch.setenv(HANDOFF_ENV, first.env[HANDOFF_ENV])
    _cli(_state_dir(home), "start", session=child, task="child", goal="g", done_criterion="dc")
    monkeypatch.delenv(HANDOFF_ENV)
    store = FileStateStore(_state_dir(home))
    resolved = store.load(child).to_dict()
    resolved["node"] = "RESOLVED"
    store.path(child).write_text(json.dumps(resolved), encoding="utf-8")
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    _assert_fresh(second, child)


def test_differing_workdir_mints_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)
    other = tmp_path / "other-work"
    second = _spawn(monkeypatch, home, other)
    _assert_fresh(second, child)
    assert str(second.cwd) == str(other)


def test_missing_transcript_mints_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    second = _spawn(monkeypatch, home, workdir)
    _assert_fresh(second, child)


# --- the own channel --------------------------------------------------------------------


def _admitted(proc, verb: str) -> None:
    assert "outside the own contour" not in proc.stderr, (
        f"BEHAVIOUR: the own channel refuses `{verb}`: {proc.stderr.strip()}"
    )
    assert REFUSED not in proc.stderr, (verb, proc.stderr)
    assert proc.stdout.strip(), f"`{verb}` never reached the engine: {proc.stderr!r}"


def test_own_channel_admits_order_raise(contour):  # noqa: F811
    proc = contour.own("order-raise", "--id", "o1", "--element", "an element of the order")
    _admitted(proc, "order-raise")


def test_own_channel_admits_order_dispose(contour):  # noqa: F811
    _cli(contour.root, "order-raise", session=CHILD, id="o1", element="an element of the order")
    proc = contour.own("order-dispose", "--id", "o1", "--as", "cut", "--reason", "out of scope")
    _admitted(proc, "order-dispose")


def test_own_channel_order_verbs_refuse_foreign_session(contour):  # noqa: F811
    _cli(contour.root, "start", session=OTHER, task="other", goal="g", done_criterion="dc")
    before = contour.state_bytes(OTHER)
    for argv in (("order-raise", "--id", "o1", "--element", "e"),
                 ("order-dispose", "--id", "o1", "--as", "cut", "--reason", "r")):
        proc = contour.own(*argv, "--session", OTHER)
        assert proc.returncode == 2 and REFUSED in proc.stderr and proc.stdout == "", (
            f"BEHAVIOUR: {argv[0]} with a foreign --session was not refused: {proc.stderr!r}"
        )
    assert contour.state_bytes(OTHER) == before


def test_own_channel_still_admits_question_verbs(contour):  # noqa: F811
    raised = contour.own("question-raise", "--id", "q1", "--target", "stage:1",
                         "--question", "which path?", "--control", "the path exists")
    _admitted(raised, "question-raise")
    disposed = contour.own("question-dispose", "--id", "q1", "--to", "assumed",
                           "--basis", "the brief names it", "--risk", "a wrong path")
    _admitted(disposed, "question-dispose")
