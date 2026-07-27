"""Stage 6: `--constraints` on `agentctl dispatch`, forwarded argv-safely.

`--constraints` lets the coordinator hand a spawned specialist a clarification
that bounds HOW it does the already-approved stage — never a scope or
done-criterion change. `dispatch_stage` never reads this text itself: the spawn
wrappers (`spawn-specialist.py` / `spawn-cursor-specialist.py`) already resolve
`--constraints`/`--done-criterion` via `lib.argv_text` on their own side
(`test_argv_text_call_sites.py`'s `_WRAPPER_RESOLVE`), so dispatch's job is only
to forward the value, argv-safely, without ever inlining a `@`-reference's
contents itself — that would relocate the E2BIG defect one hop later rather
than remove it. This file pins that forwarding contract for both
`--constraints` and the pre-existing `--done-criterion` element, which shares
the same `_normalize_forward_value` treatment.
"""
from __future__ import annotations

import argparse
from argparse import Namespace
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.dispatch import RunResult, _normalize_forward_value, build_argv, dispatch_stage
from agentctl.state import Actor, Criterion, Means, Stage, Subject
from lib import argv_text


def ns(**kw):
    return Namespace(**kw)


def _make_spawn_stage(index: int = 1, done_criterion: str = "tests green") -> Stage:
    return Stage(
        index=index,
        title="test stage",
        subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"),
        actor=Actor(executor="spawn:developer"),
        criterion=Criterion(criterion_type="measurable", done_criterion=done_criterion),
    )


def _capture_runner(seen):
    def runner(argv, cwd=None):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: ok\n")
    return runner


# --- argv presence / omission --------------------------------------------------

def test_build_argv_omits_constraints_when_empty():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml")
    assert "--constraints" not in argv


def test_build_argv_includes_constraints_when_given():
    stage = _make_spawn_stage()
    argv = build_argv(stage, "/tmp/plan.toml", constraints="stay inside module X")
    assert argv[argv.index("--constraints") + 1] == "stay inside module X"


def test_dispatch_stage_omits_constraints_when_unset():
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen))
    assert "--constraints" not in seen[0]


# --- byte-identical pass-through for short values -------------------------------

def test_dispatch_stage_forwards_short_inline_constraints_byte_identical():
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen),
                    constraints="stay inside module X")
    argv = seen[0]
    assert argv[argv.index("--constraints") + 1] == "stay inside module X"


def test_dispatch_stage_forwards_double_at_literal_verbatim_not_deescaped():
    # A small '@@literal' constraint must reach the child's argv WITH its own
    # escaping intact — forwarding the de-escaped '@literal' instead would make
    # the child (which applies the SAME @-convention) misclassify it as a
    # genuine reference and probe a nonexistent file 'literal-not-a-path'.
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen),
                    constraints="@@literal-not-a-path")
    argv = seen[0]
    assert argv[argv.index("--constraints") + 1] == "@@literal-not-a-path"


# --- @ref forwarding: absolute path, for both absolute and relative input ------

def test_dispatch_stage_forwards_absolute_ref_resolved(tmp_path):
    f = tmp_path / "dossier.md"
    f.write_text("dossier body", encoding="utf-8")
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen),
                    constraints=f"@{f}")
    argv = seen[0]
    assert argv[argv.index("--constraints") + 1] == f"@{f.resolve()}"


def test_dispatch_stage_absolutizes_a_relative_ref(tmp_path, monkeypatch):
    f = tmp_path / "dossier.md"
    f.write_text("dossier body", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    stage = _make_spawn_stage()
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen),
                    constraints="@dossier.md")
    argv = seen[0]
    assert argv[argv.index("--constraints") + 1] == f"@{f.resolve()}"


def test_dispatch_stage_raises_loudly_on_missing_ref(tmp_path):
    stage = _make_spawn_stage()
    missing = tmp_path / "absent.md"
    with pytest.raises(SystemExit) as exc:
        dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner([]),
                        constraints=f"@{missing}")
    assert str(missing) in str(exc.value)
    assert "readable file" in str(exc.value)


# --- staging + cleanup for oversized values -------------------------------------

def test_dispatch_stage_stages_oversized_inline_constraints_and_cleans_up():
    big = "x" * 40000  # past _FORWARD_STAGE_THRESHOLD_BYTES (32768)
    stage = _make_spawn_stage()
    staged_path_holder = []

    def runner(argv, cwd=None):
        value = argv[argv.index("--constraints") + 1]
        assert value.startswith("@")
        staged = Path(value[1:])
        assert staged.exists()
        assert staged.read_text(encoding="utf-8") == big
        staged_path_holder.append(staged)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner, constraints=big)
    assert not staged_path_holder[0].exists()  # cleaned up once the runner returns


def test_dispatch_stage_stages_oversized_double_at_literal_deescaped():
    # The staged FILE carries no further '@' meaning (a plain read has no argv
    # to misparse), so it is staged de-escaped: the child recovers the same
    # text '@' + payload either way.
    payload = "x" * 40000
    big_escaped = "@@" + payload
    stage = _make_spawn_stage()
    staged_path_holder = []

    def runner(argv, cwd=None):
        value = argv[argv.index("--constraints") + 1]
        assert value.startswith("@")
        staged = Path(value[1:])
        assert staged.exists()
        assert staged.read_text(encoding="utf-8") == "@" + payload
        staged_path_holder.append(staged)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner, constraints=big_escaped)
    assert not staged_path_holder[0].exists()


def test_dispatch_stage_cleans_up_staged_file_even_when_runner_raises():
    big = "x" * 40000
    stage = _make_spawn_stage()
    staged_path_holder = []

    def runner(argv, cwd=None):
        value = argv[argv.index("--constraints") + 1]
        staged_path_holder.append(Path(value[1:]))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        dispatch_stage(stage, "/tmp/plan.toml", runner=runner, constraints=big)
    assert not staged_path_holder[0].exists()


def test_dispatch_stage_cleans_up_staged_constraints_when_done_criterion_normalize_raises(
    tmp_path, monkeypatch
):
    # constraints (oversized -> staged) normalizes fine; done_criterion normalizes
    # SECOND and raises on a missing @ref. Both normalize calls must live inside
    # the same try/finally so the constraints tempfile staged before the raise is
    # still cleaned up, not leaked.
    missing = tmp_path / "absent.md"
    stage = _make_spawn_stage(done_criterion=f"@{missing}")
    big = "c" * 40000

    staged_paths = []
    real_stage_to_tempfile = argv_text.stage_text_to_tempfile

    def spy_stage_to_tempfile(text):
        path = real_stage_to_tempfile(text)
        staged_paths.append(path)
        return path

    monkeypatch.setattr(argv_text, "stage_text_to_tempfile", spy_stage_to_tempfile)

    def runner(argv, cwd=None):
        raise AssertionError("runner must not be reached: done_criterion ref is missing")

    with pytest.raises(SystemExit):
        dispatch_stage(stage, "/tmp/plan.toml", runner=runner, constraints=big)

    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()


# --- T(v') == T(v): normalizing a forwarded value must not change what the -----
# --- child ultimately resolves via its own read_arg_text -----------------------

@pytest.mark.parametrize("kind", [
    "short-inline",
    "short-escaped",
    "ref",
    "oversized-inline",
    "oversized-escaped",
])
def test_normalize_forward_value_preserves_read_arg_text_round_trip(kind, tmp_path):
    if kind == "short-inline":
        original = "stay inside module X"
    elif kind == "short-escaped":
        original = "@@literal-not-a-path"
    elif kind == "ref":
        f = tmp_path / "dossier.md"
        f.write_text("dossier body", encoding="utf-8")
        original = f"@{f}"
    elif kind == "oversized-inline":
        original = "x" * 40000
    else:  # oversized-escaped
        original = "@@" + "x" * 40000

    normalized, staged = _normalize_forward_value(original, "--constraints")
    try:
        assert argv_text.read_arg_text(normalized) == argv_text.read_arg_text(original)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


# --- MAX_ARG_STRLEN bound on every forwarded element ----------------------------

def test_every_argv_element_stays_under_max_arg_strlen():
    stage = _make_spawn_stage(done_criterion="d" * 200_000)
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen),
                    constraints="c" * 200_000)
    for element in seen[0]:
        assert len(element.encode("utf-8")) < argv_text.MAX_ARG_STRLEN


# --- the same treatment applies to the pre-existing --done-criterion element ---

def test_dispatch_stage_forwards_short_done_criterion_byte_identical():
    stage = _make_spawn_stage(done_criterion="pytest tests/ green")
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen))
    argv = seen[0]
    assert argv[argv.index("--done-criterion") + 1] == "pytest tests/ green"


def test_dispatch_stage_absolutizes_a_relative_done_criterion_ref(tmp_path, monkeypatch):
    f = tmp_path / "criterion.md"
    f.write_text("pytest tests/ green", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    stage = _make_spawn_stage(done_criterion="@criterion.md")
    seen = []
    dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner(seen))
    argv = seen[0]
    assert argv[argv.index("--done-criterion") + 1] == f"@{f.resolve()}"


def test_dispatch_stage_raises_loudly_on_missing_done_criterion_ref(tmp_path):
    missing = tmp_path / "absent.md"
    stage = _make_spawn_stage(done_criterion=f"@{missing}")
    with pytest.raises(SystemExit) as exc:
        dispatch_stage(stage, "/tmp/plan.toml", runner=_capture_runner([]))
    assert str(missing) in str(exc.value)


def test_dispatch_stage_stages_oversized_done_criterion_and_cleans_up():
    big = "d" * 40000
    stage = _make_spawn_stage(done_criterion=big)
    staged_path_holder = []

    def runner(argv, cwd=None):
        value = argv[argv.index("--done-criterion") + 1]
        assert value.startswith("@")
        staged = Path(value[1:])
        assert staged.exists()
        assert staged.read_text(encoding="utf-8") == big
        staged_path_holder.append(staged)
        return RunResult(0, stdout="COMPLETED: ok\n")

    dispatch_stage(stage, "/tmp/plan.toml", runner=runner)
    assert not staged_path_holder[0].exists()


# --- CLI wiring: cli.py threads args.constraints into dispatch_stage -----------

def _to_executing(store, sid, fixtures_dir):
    plan = str(fixtures_dir / "plan_two_stage.toml")
    cli.cmd_start(ns(session=sid, task="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(ns(session=sid, chat=False, changed_lines=200, files=5,
                        wall_clock_min=60, tracker_key=None, architectural=True,
                        external_effect=False, new_dependency=False,
                        public_api_change=False), store=store)
    cli.cmd_plan(ns(session=sid), store=store)
    cli.cmd_submit_plan(ns(session=sid, plan=plan), store=store)
    cli.cmd_approve(ns(session=sid, by="user"), store=store)
    cli.cmd_partition(ns(session=sid, m1=False, m2=False, m3=False, m4=False,
                         m3_severe=False, m4_severe=False), store=store)
    return cli.cmd_next_stage(ns(session=sid), store=store)


def test_cmd_dispatch_threads_constraints_into_argv(store, fixtures_dir):
    sid = "constraints-wiring"
    _to_executing(store, sid, fixtures_dir)
    seen = []

    def runner(argv, cwd=None):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False, constraints="only touch module X"),
                     store=store, runner=runner)
    argv = seen[0]
    assert argv[argv.index("--constraints") + 1] == "only touch module X"


def test_cmd_dispatch_omits_constraints_flag_when_args_lack_it(store, fixtures_dir):
    """Pre-existing callers that never set args.constraints
    (test_dispatch_semantics.py, test_dispatch_cwd.py) must keep working
    unmodified: getattr(args, "constraints", "") defaults to omission."""
    sid = "constraints-backcompat"
    _to_executing(store, sid, fixtures_dir)
    seen = []

    def runner(argv, cwd=None):
        seen.append(argv)
        return RunResult(0, stdout="COMPLETED: done\n")

    cli.cmd_dispatch(ns(session=sid, budget="medium", complexity="medium",
                        dry_run=False), store=store, runner=runner)
    assert "--constraints" not in seen[0]


# --- help text bounds --constraints to non-scope-changing clarification --------

def _dispatch_constraints_action() -> argparse.Action:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            dispatch_parser = action.choices["dispatch"]
            for sub in dispatch_parser._actions:
                if sub.dest == "constraints":
                    return sub
    raise AssertionError("dispatch subparser has no --constraints action")


def test_constraints_help_text_bounds_scope():
    help_text = _dispatch_constraints_action().help.lower()
    assert "never" in help_text
    assert "scope" in help_text or "done-criterion" in help_text


def test_constraints_defaults_to_empty_string():
    assert _dispatch_constraints_action().default == ""
