"""The `@<path>` convention is applied at every call site that should have it —
and at none that should not.

test_argv_text.py pins the RULE (lib.argv_text's four branches). This file pins
its COVERAGE, and coverage is the part that rots: the rule is written once, while
arguments keep being added. So the control here does not read a list of argument
names written by a human. It walks `agentctl`'s parser — the root parser AND every
subparser — computes the set of arguments that could plausibly carry prose, and
requires each one to appear in EXACTLY ONE of cli.py's three declared classes.

A new argument that nobody classified fails RED. A classification left behind by a
rename fails RED too — the check runs in both directions, because a table that
accumulates dead entries stops being evidence of anything.

The candidate rule is STRUCTURAL on purpose (action kind, `choices`, `type`), never
a guess from the argument's NAME. Deciding "is this classified?" is a rule a machine
can own; deciding "is this prose or a token?" is perception, and that judgment lives
in the human-written reason attached to every DO-NOT-WRAP entry.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from agentctl import cli
from agentctl.store import FileStateStore

SCRIPTS = Path(__file__).resolve().parent.parent


# --- the structural candidate rule ---------------------------------------------

def _is_candidate(action: argparse.Action) -> bool:
    """Whether `action` could carry free text, decided from its declaration alone.

    A flag with `choices=` takes one of a fixed vocabulary; a non-str `type=`
    (`Path`, `int`) already names a non-prose receiver; `store_true` carries no
    value at all. Everything else could be prose, so everything else must be
    classified.
    """
    if not isinstance(action, (argparse._StoreAction, argparse._AppendAction)):
        return False
    if action.choices is not None:
        return False
    return action.type is None or action.type is str


def _candidates() -> set[tuple[str, str]]:
    """Every (subcommand, dest) the parser declares that could carry free text.

    The ROOT parser is part of the walk, not an afterthought: `--state-root` lives
    there today, and a subparser-only walk would let the next top-level narrative
    option through the gate unseen. Root arguments are keyed under cli._ROOT.
    """
    parser = cli.build_parser()
    found: set[tuple[str, str]] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                found.update(
                    (name, sub.dest) for sub in subparser._actions if _is_candidate(sub)
                )
        elif _is_candidate(action):
            found.add((cli._ROOT, action.dest))
    return found


def _classes() -> dict[str, set[tuple[str, str]]]:
    return {
        "_ARG_RESOLVE": set(cli._ARG_RESOLVE),
        "_ARG_FORWARD": set(cli._ARG_FORWARD),
        "_ARG_DO_NOT_WRAP": set(cli._ARG_DO_NOT_WRAP),
    }


# --- exhaustiveness: the partition covers the parser, and only the parser -------

def test_every_candidate_argument_is_classified():
    unclassified = _candidates() - set().union(*_classes().values())
    assert not unclassified, (
        "arguments the parser declares but the @<path> partition does not classify "
        f"(add each to _ARG_RESOLVE, _ARG_FORWARD or _ARG_DO_NOT_WRAP in cli.py): "
        f"{sorted(unclassified)}"
    )


def test_no_argument_is_classified_twice():
    classes = _classes()
    overlaps = {
        f"{a} & {b}": sorted(classes[a] & classes[b])
        for a, b in (
            ("_ARG_RESOLVE", "_ARG_FORWARD"),
            ("_ARG_RESOLVE", "_ARG_DO_NOT_WRAP"),
            ("_ARG_FORWARD", "_ARG_DO_NOT_WRAP"),
        )
        if classes[a] & classes[b]
    }
    assert not overlaps, f"an argument in two classes has no defined behaviour: {overlaps}"


def test_no_classification_names_an_argument_the_parser_lost():
    """The reverse direction — a rename or removal must not leave a stale entry.

    Without this the table degrades into folklore: it keeps asserting coverage of
    arguments that no longer exist, while the check that new ones are covered still
    passes.
    """
    candidates = _candidates()
    stale = {name: sorted(members - candidates) for name, members in _classes().items()}
    stale = {name: entries for name, entries in stale.items() if entries}
    assert not stale, f"classified but no longer declared by the parser: {stale}"


def test_every_do_not_wrap_entry_states_a_reason():
    unexplained = sorted(k for k, reason in cli._ARG_DO_NOT_WRAP.items() if not reason.strip())
    assert not unexplained, (
        f"a DO-NOT-WRAP entry without a reason is an unreviewable exemption: {unexplained}"
    )


# --- behaviour: RESOLVE arguments honour the convention -------------------------

def _resolved(*argv: str) -> argparse.Namespace:
    """Parse argv and apply the same normalization `main()` applies before dispatch."""
    args = cli.build_parser().parse_args(list(argv))
    cli.resolve_arg_text(args)
    return args


def _file(tmp_path: Path, body: str = "the body, too large for argv") -> str:
    path = tmp_path / "text.md"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_required_single_value_resolves_a_reference(tmp_path):
    args = _resolved("critique", "--session", "s", "--functional-ground", "fg",
                     "--replanning-task", f"@{_file(tmp_path)}")
    assert args.replanning_task == "the body, too large for argv"


def test_optional_single_value_resolves_a_reference(tmp_path):
    args = _resolved("record-result", "--session", "s", "--status", "passed",
                     "--observation", f"@{_file(tmp_path)}")
    assert args.observation == "the body, too large for argv"


def test_append_argument_resolves_element_wise(tmp_path):
    args = _resolved("plan-review", "--session", "s", "--verdict", "pass", "--reviewer", "thinker",
                     "--concern", f"@{_file(tmp_path)}", "--concern", "inline concern")
    assert args.concerns == ["the body, too large for argv", "inline concern"]


def test_append_argument_never_given_stays_none():
    args = _resolved("plan-review", "--session", "s", "--verdict", "pass", "--reviewer", "thinker")
    assert args.concerns is None


def test_previously_omitted_narrative_argument_resolves(tmp_path):
    """`replan --normalization-waiver` — one of the arguments an eye-read of the
    parser missed, which is why the partition is computed rather than listed."""
    args = _resolved("replan", "--session", "s", "--plan", "/tmp/p.toml",
                     "--normalization-waiver", f"@{_file(tmp_path)}")
    assert args.normalization_waiver == "the body, too large for argv"


def test_double_at_stays_a_literal():
    args = _resolved("record-result", "--session", "s", "--status", "passed", "--observation", "@@notafile")
    assert args.observation == "@notafile"


def test_missing_reference_exits_cleanly_naming_the_contract(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _resolved("record-result", "--session", "s", "--status", "passed",
                  "--observation", f"@{tmp_path / 'absent.md'}")
    assert "does not name a readable file" in str(exc.value)


def test_value_not_starting_with_at_is_untouched():
    args = _resolved("record-result", "--session", "s", "--status", "passed", "--observation", "plain prose")
    assert args.observation == "plain prose"


# --- behaviour: DO-NOT-WRAP arguments are left alone ----------------------------

def test_do_not_wrap_argument_does_not_resolve_a_reference(tmp_path):
    """`--session` is an id. A value that happens to start with '@' must survive
    verbatim — not be read as a file, and not exit because no such file exists.
    """
    args = _resolved("record-result", "--session", f"@{_file(tmp_path)}", "--status", "passed")
    assert args.session == f"@{_file(tmp_path)}"


def test_root_parser_entry_resolves_under_any_subcommand(tmp_path, monkeypatch):
    """A _ROOT-keyed RESOLVE entry is not scoped to one subcommand.

    No root option is RESOLVE today, so this pins the dispatch RULE rather than
    current coverage: without it, the first narrative top-level option would sit
    classified RESOLVE and never actually be resolved — classified and inert.
    """
    monkeypatch.setattr(cli, "_ARG_RESOLVE", frozenset({(cli._ROOT, "state_root")}))
    args = _resolved("--state-root", f"@{_file(tmp_path, '/tmp/elsewhere')}", "status", "--session", "s")
    assert args.state_root == "/tmp/elsewhere"


# --- wiring: main() applies the normalization before the command body sees it ----

def test_main_resolves_before_the_command_records_state(tmp_path, capsys):
    """The one end-to-end proof that the pass is WIRED, not merely written: a
    reference handed to `start` reaches persisted state as the file's contents.
    """
    goal = tmp_path / "goal.md"
    goal.write_text("resolve the difficulty", encoding="utf-8")
    root = tmp_path / "state"

    cli.main(["--state-root", str(root), "start", "--session", "wired", "--task", "t",
              "--goal", f"@{goal}", "--done-criterion", "dc",
              "--criterion-type", "measurable"])
    capsys.readouterr()

    assert FileStateStore(root).load("wired").goal == "resolve the difficulty"


# --- the spawn wrappers ---------------------------------------------------------

def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _specialist_prompt(extra_argv: list[str], plan: str) -> str:
    mod = _load("spawn-specialist.py", "spawn_specialist")
    args = mod.build_parser().parse_args(
        ["--kind", "developer", "--criterion-type", "measurable", "--plan", plan,
         "--complexity", "medium"]
        + extra_argv
    )
    return mod.assemble_prompt(args, depth=1, permissions="")


def _cursor_prompt(extra_argv: list[str], plan: str) -> str:
    mod = _load("spawn-cursor-specialist.py", "spawn_cursor_specialist")
    args = mod.build_parser().parse_args(
        ["--kind", "developer", "--criterion-type", "measurable", "--plan", plan]
        + extra_argv
    )
    return mod.assemble_prompt(args, 1, "", "skill body", None)


wrappers = pytest.mark.parametrize(
    "assemble",
    [_specialist_prompt, _cursor_prompt],
    ids=["spawn-specialist", "spawn-cursor-specialist"],
)


# --- the spawn wrappers: mechanical exhaustiveness, mirroring the cli.py walk ---
#
# The cli.py walk above computes its candidate set from `agentctl`'s OWN parser,
# so a new subcommand option can never slip by unclassified. A wrapper is a
# separate process with its own parser, so it needs its own walk: a hardcoded
# `parametrize("flag", [...])` here would be exactly the prose list the cli.py
# side exists to abolish — a new narrative wrapper flag would be silently
# uncovered. Each wrapper gets a small per-wrapper partition (RESOLVE / DO-NOT-
# WRAP; no FORWARD member exists on the wrapper side — both narrative flags are
# consumed locally into the assembled prompt, never handed on unresolved) and
# the same three exhaustiveness assertions the cli.py side makes.

_WRAPPER_FILES = (
    ("spawn-specialist.py", "spawn_specialist"),
    ("spawn-cursor-specialist.py", "spawn_cursor_specialist"),
)

wrapper_modules = pytest.mark.parametrize(
    "filename,module_name", _WRAPPER_FILES, ids=[f for f, _ in _WRAPPER_FILES]
)

# Both wrappers declare the same two narrative flags, consumed via
# argv_text.read_arg_text into the assembled prompt.
_WRAPPER_RESOLVE = frozenset({"done_criterion", "constraints"})

_WRAPPER_DO_NOT_WRAP: dict[str, dict[str, str]] = {
    "spawn-specialist.py": {
        "kind": "specialization name — an id from a fixed catalog (SKILL.md directory names), not prose",
        "model": "model alias/id (e.g. sonnet, opus) — a token, not prose",
        "continue_worktree": "a worktree path — a filesystem reference, not prose",
    },
    "spawn-cursor-specialist.py": {
        "kind": "specialization name — an id from a fixed catalog (SKILL.md directory names), not prose",
        "model": "model alias/id (e.g. composer-2.5) — a token, not prose",
        "continue_worktree": "a worktree path — a filesystem reference, not prose",
    },
}


def _wrapper_candidates(filename: str, module_name: str) -> set[str]:
    """Every dest `filename`'s parser declares that could carry free text,
    via the SAME structural `_is_candidate` predicate the cli.py walk uses."""
    mod = _load(filename, module_name)
    return {a.dest for a in mod.build_parser()._actions if _is_candidate(a)}


@wrapper_modules
def test_wrapper_every_candidate_argument_is_classified(filename, module_name):
    classified = _WRAPPER_RESOLVE | set(_WRAPPER_DO_NOT_WRAP[filename])
    unclassified = _wrapper_candidates(filename, module_name) - classified
    assert not unclassified, (
        f"{filename}: arguments the parser declares but the wrapper partition does "
        f"not classify (add each to _WRAPPER_RESOLVE or _WRAPPER_DO_NOT_WRAP): "
        f"{sorted(unclassified)}"
    )


@wrapper_modules
def test_wrapper_no_argument_is_classified_twice(filename, module_name):
    overlap = _WRAPPER_RESOLVE & set(_WRAPPER_DO_NOT_WRAP[filename])
    assert not overlap, (
        f"{filename}: an argument in both RESOLVE and DO-NOT-WRAP has no defined "
        f"behaviour: {sorted(overlap)}"
    )


@wrapper_modules
def test_wrapper_no_classification_names_an_argument_the_parser_lost(filename, module_name):
    candidates = _wrapper_candidates(filename, module_name)
    stale_resolve = sorted(_WRAPPER_RESOLVE - candidates)
    stale_do_not_wrap = sorted(set(_WRAPPER_DO_NOT_WRAP[filename]) - candidates)
    assert not stale_resolve and not stale_do_not_wrap, (
        f"{filename}: classified but no longer declared by the parser: "
        f"resolve={stale_resolve} do_not_wrap={stale_do_not_wrap}"
    )


@wrapper_modules
def test_wrapper_every_do_not_wrap_entry_states_a_reason(filename, module_name):
    unexplained = sorted(
        k for k, reason in _WRAPPER_DO_NOT_WRAP[filename].items() if not reason.strip()
    )
    assert not unexplained, (
        f"{filename}: a DO-NOT-WRAP entry without a reason is an unreviewable "
        f"exemption: {unexplained}"
    )


# --- behaviour: the two RESOLVE flags honour the convention (both wrappers) -----


def _plan_file(tmp_path: Path) -> str:
    plan = tmp_path / "plan.md"
    plan.write_text("the plan body", encoding="utf-8")
    return str(plan)


@wrappers
@pytest.mark.parametrize("flag", ["--constraints", "--done-criterion"])
def test_wrapper_narrative_flag_resolves_a_reference(assemble, flag, tmp_path):
    prompt = assemble(
        ["--done-criterion", "dc", flag, f"@{_file(tmp_path, 'text from a file')}"],
        _plan_file(tmp_path),
    )
    assert "text from a file" in prompt


@wrappers
def test_wrapper_double_at_stays_a_literal(assemble, tmp_path):
    prompt = assemble(["--done-criterion", "@@literal criterion"], _plan_file(tmp_path))
    assert "@literal criterion" in prompt
    assert "@@literal criterion" not in prompt


@wrappers
def test_wrapper_missing_reference_exits_cleanly(assemble, tmp_path):
    with pytest.raises(SystemExit) as exc:
        assemble(["--done-criterion", f"@{tmp_path / 'absent.md'}"], _plan_file(tmp_path))
    assert "does not name a readable file" in str(exc.value)
