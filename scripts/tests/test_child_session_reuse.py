"""A re-dispatched linked stage resumes its child's engine session.

Every dispatch of a linked stage used to mint a fresh child session, so what the root
recorded on the child's session between two spawns (an approval of its plan, a review
override, order elements) never reached the next child. The spawn now keeps a record
`<state root>/child-sessions/<parent>-<stage>.json` naming the child's session and its
workdir, and resumes that session (`claude -p --resume <id>`) when the workdir is the
same, it is not RESOLVED, the norm it adopted is the stage's current norm and its
transcript exists; otherwise it mints a fresh one as before. The own channel also admits
`order-raise` / `order-dispose`.

Six tests fail on the base tree at their `BEHAVIOUR:` assertion; the rest pin what must
stay as on base. The spawn is driven up to the child launch through the stubbed launcher of
`test_child_contour_gate`; the launcher writes no transcript, so a test that expects a
resume creates the previous child's `<id>.jsonl` itself."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentctl import norm_handoff
from agentctl.own_contour import OWN_CONTOUR_VERBS
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


def _spawn(monkeypatch, home: Path, workdir: Path, *, stage_index: int | None = STAGE) -> Spawned:
    """One spawn for the parent session, run up to the child launch; linked for stage 1
    unless `stage_index` is None."""
    spawn = _load_spawn()
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")

    def launch(cmd, env=None, cwd=None, **kw):
        raise _Launched(list(cmd), dict(env or {}), cwd)

    monkeypatch.setattr(spawn.proc_tree, "launch_supervised", launch)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        rc = spawn.main(_spawn_argv(home, workdir, stage_index))
    except _Launched as launched:
        return Spawned(launched.cmd, launched.env, launched.cwd)
    raise AssertionError(f"setup: the spawn did not reach the launch (rc={rc})")


def _record_path(home: Path) -> Path:
    return norm_handoff.child_session_path(_state_dir(home), PARENT, STAGE)


def _start_child_engine(monkeypatch, home: Path, first: Spawned) -> FileStateStore:
    """The first child's engine session, started under the handoff it was spawned with."""
    child = first.option("--session-id")
    monkeypatch.setenv(HANDOFF_ENV, first.env[HANDOFF_ENV])
    _cli(_state_dir(home), "start", session=child, task="child", goal="g", done_criterion="dc")
    monkeypatch.delenv(HANDOFF_ENV)
    return FileStateStore(_state_dir(home))


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
        f"the spawn did not mint a fresh child session: {spawned.session_args()}"
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


def test_changed_stage_norm_mints_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    store = _start_child_engine(monkeypatch, home, first)
    stale = store.load(child).to_dict()
    assert stale["parent_link"]["norm_digest"], "setup: the child engine adopted no handoff"
    stale["parent_link"]["norm_digest"] = "0" * 64
    store.path(child).write_text(json.dumps(stale), encoding="utf-8")
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    _assert_fresh(second, child)


def test_dry_run_writes_no_child_session_record(tmp_path, monkeypatch):
    home, workdir = tmp_path / "home", tmp_path / "work"
    _parent_session(home)
    workdir.mkdir()
    spawn = _load_spawn()
    monkeypatch.setattr(spawn.shutil, "which", lambda *a, **k: "/usr/bin/claude")
    assert spawn.main([*_spawn_argv(home, workdir, STAGE), "--dry-run"]) == 0
    assert not _record_path(home).exists()


def test_unlinked_spawn_neither_reads_nor_writes_record(tmp_path, monkeypatch):
    home, workdir = tmp_path / "home", tmp_path / "work"
    _parent_session(home)
    calls: list[str] = []
    monkeypatch.setattr(norm_handoff, "read_child_session", lambda *a, **k: calls.append("read"))
    monkeypatch.setattr(norm_handoff, "write_child_session", lambda *a, **k: calls.append("write"))
    spawned = _spawn(monkeypatch, home, workdir, stage_index=None)
    assert OWN_ENV not in spawned.env, "setup: the spawn without a stage index is linked"
    assert calls == []
    assert not _record_path(home).parent.exists()


def test_corrupt_record_mints_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)
    _record_path(home).write_text("{not json", encoding="utf-8")
    second = _spawn(monkeypatch, home, workdir)
    _assert_fresh(second, child)


def test_corrupt_child_state_mints_fresh_session(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    FileStateStore(_state_dir(home)).path(child).write_text("{not json", encoding="utf-8")
    _write_transcript(workdir, child)
    second = _spawn(monkeypatch, home, workdir)
    _assert_fresh(second, child)


def test_failed_handoff_write_after_resume_decision_spawns_unlinked(tmp_path, monkeypatch):
    home, workdir, first = _first_child(monkeypatch, tmp_path)
    child = first.option("--session-id")
    _write_transcript(workdir, child)

    def refuse(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(norm_handoff, "write_record", refuse)
    second = _spawn(monkeypatch, home, workdir)
    assert "--resume" not in second.cmd and "--session-id" not in second.cmd, second.session_args()
    assert OWN_ENV not in second.env and HANDOFF_ENV not in second.env


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
            f"{argv[0]} with a foreign --session was not refused: {proc.stderr!r}"
        )
        # A tree whose channel does not admit the verb refuses it before reading --session.
        reason = "takes no --session" if argv[0] in OWN_CONTOUR_VERBS else "outside the own contour"
        assert reason in proc.stderr, (
            f"{argv[0]} was not refused with {reason!r}: {proc.stderr!r}"
        )
    assert contour.state_bytes(OTHER) == before


def test_own_channel_still_admits_question_verbs(contour):  # noqa: F811
    raised = contour.own("question-raise", "--id", "q1", "--target", "stage:1",
                         "--question", "which path?", "--control", "the path exists")
    _admitted(raised, "question-raise")
    disposed = contour.own("question-dispose", "--id", "q1", "--to", "assumed",
                           "--basis", "the brief names it", "--risk", "a wrong path")
    _admitted(disposed, "question-dispose")


def test_own_channel_admits_question_research_and_candidate_dispose(contour):  # noqa: F811
    for verb in ("question-research", "question-candidate-dispose"):
        proc = contour.own(verb, "--id", "q1")
        assert "outside the own contour" not in proc.stderr, (
            f"BEHAVIOUR: the own channel refuses `{verb}`: {proc.stderr.strip()}"
        )


def test_own_channel_gains_no_user_authority_verb():
    from lib.widening_targets import AGENTCTL_USER_AUTHORITY_VERBS

    assert {"question-research", "question-candidate-dispose"} <= OWN_CONTOUR_VERBS
    leaked = (OWN_CONTOUR_VERBS & AGENTCTL_USER_AUTHORITY_VERBS) - {
        "plan-review", "code-review", "submit-plan", "dispatch", "record-result", "verify-final",
        "replan", "declare", "investigate", "critique", "normalize", "approve",
        "next-stage", "partition",
    }
    assert not leaked, leaked
