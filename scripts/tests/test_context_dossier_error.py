"""Path-typed arguments of both spawn wrappers must state their contract when a
caller hands them text instead of a path.

`--context-dossier` and `--plan` are `type=Path` and stay that way — a path-typed
receiver that silently swallows prose is the defect this suite exists to prevent,
not the fix. What was wrong is the DIAGNOSTIC: a long inline dossier reached
`Path.read_text()` and surfaced as a bare `OSError: [Errno 36] File name too long`
traceback, which reports the mechanical symptom of the violation and sends the
reader to investigate the filesystem instead of the argument they got wrong.

Both directions are pinned, per wrapper, because the two scripts share no code but
carry byte-identical copies of this logic — a fix landed in one and forgotten in
the other is the realistic regression here. The short-absent-path case is pinned
alongside the over-long one so the guard cannot degrade into a length special-case:
`Path.is_file()` raises ENAMETOOLONG only past PATH_MAX, and a fix that keys on
length alone would pass the over-long test while leaving every ordinary typo'd path
to fail somewhere less legible.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent

# Past PATH_MAX (4096) so the filesystem itself rejects it — this is what an inline
# dossier looks like to a path-typed argument.
PROSE_NOT_A_PATH = "This is a context dossier, not a file path. " * 200


def _load(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _specialist():
    return _load("spawn-specialist.py", "spawn_specialist")


def _cursor():
    return _load("spawn-cursor-specialist.py", "spawn_cursor_specialist")


def _specialist_prompt(extra_argv: list[str]) -> str:
    mod = _specialist()
    args = mod.build_parser().parse_args(
        ["--kind", "developer", "--done-criterion", "d", "--criterion-type", "measurable"]
        + extra_argv
    )
    return mod.assemble_prompt(args, depth=1, permissions="")


def _cursor_prompt(extra_argv: list[str]) -> str:
    mod = _cursor()
    args = mod.build_parser().parse_args(
        ["--kind", "developer", "--done-criterion", "d", "--criterion-type", "measurable"]
        + extra_argv
    )
    return mod.assemble_prompt(args, 1, "", "skill body", None)


wrappers = pytest.mark.parametrize(
    "assemble",
    [_specialist_prompt, _cursor_prompt],
    ids=["spawn-specialist", "spawn-cursor-specialist"],
)


def _expect_clean_exit(assemble, extra_argv: list[str]) -> str:
    """Return the diagnostic, failing loudly if an OSError leaked instead."""
    try:
        assemble(extra_argv)
    except SystemExit as exc:
        return str(exc)
    except OSError as exc:  # the defect: the caller gets a traceback about the filesystem
        pytest.fail(f"leaked an OSError instead of naming the contract: {exc!r}")
    pytest.fail("a non-file argument was accepted silently")


def _plan_file(tmp_path: Path) -> str:
    plan = tmp_path / "plan.md"
    plan.write_text("the plan body", encoding="utf-8")
    return str(plan)


# --- --context-dossier: the wrong direction -----------------------------------

@wrappers
def test_inline_dossier_names_the_flag_and_the_file_contract(assemble, tmp_path):
    msg = _expect_clean_exit(
        assemble, ["--plan", _plan_file(tmp_path), "--context-dossier", PROSE_NOT_A_PATH]
    )

    assert "--context-dossier" in msg
    assert "file" in msg.lower() and "path" in msg.lower()
    assert "\n" not in msg.strip(), "the diagnostic must be a single line"
    # The payload is echoed as evidence, truncated — not reproduced whole.
    assert PROSE_NOT_A_PATH not in msg
    assert len(msg) < 600


@wrappers
def test_short_absent_dossier_path_gets_the_same_clean_error(assemble, tmp_path):
    # Not a length special-case: an ordinary typo'd path is the common instance.
    msg = _expect_clean_exit(
        assemble,
        ["--plan", _plan_file(tmp_path), "--context-dossier", str(tmp_path / "dosier.md")],
    )

    assert "--context-dossier" in msg
    assert "dosier.md" in msg, "the error must name the path the caller has to fix"


# --- --context-dossier: the right direction still works ------------------------

@wrappers
def test_a_real_dossier_file_is_read_into_the_prompt(assemble, tmp_path):
    dossier = tmp_path / "dossier.md"
    dossier.write_text("the digest of the conversation so far", encoding="utf-8")

    prompt = assemble(["--plan", _plan_file(tmp_path), "--context-dossier", str(dossier)])

    assert "the digest of the conversation so far" in prompt
    assert str(dossier) not in prompt, "the path must be resolved, not pasted"


@wrappers
def test_an_absent_dossier_argument_stays_absent(assemble, tmp_path):
    # The flag is optional; omitting it must not trip the new required-file path.
    assert "the plan body" in assemble(["--plan", _plan_file(tmp_path)])


# --- --plan shares the failure mode, so it shares the fix -----------------------

@wrappers
def test_inline_plan_names_the_flag_and_the_file_contract(assemble):
    msg = _expect_clean_exit(assemble, ["--plan", PROSE_NOT_A_PATH])

    assert "--plan" in msg
    assert "file" in msg.lower() and "path" in msg.lower()
    assert len(msg) < 600


def test_specialist_main_refuses_an_inline_plan_before_launching(monkeypatch, capsys):
    # main()'s pre-check probes the plan path before anything is spawned; the probe
    # itself raised ENAMETOOLONG on an inline plan.
    mod = _specialist()
    monkeypatch.setattr(mod, "log_refused", lambda *a, **k: None)

    rc = mod.main(
        ["--kind", "developer", "--plan", PROSE_NOT_A_PATH,
         "--done-criterion", "d", "--criterion-type", "measurable"]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "--plan" in err
    assert PROSE_NOT_A_PATH not in err
    assert len(err) < 600


def test_cursor_main_refuses_an_inline_plan_before_launching(monkeypatch, capsys, tmp_path):
    mod = _cursor()
    monkeypatch.setattr(mod, "log_refused", lambda *a, **k: None)
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill", encoding="utf-8")
    monkeypatch.setattr(mod, "skill_path", lambda kind: skill)

    rc = mod.main(
        ["--kind", "developer", "--plan", PROSE_NOT_A_PATH,
         "--done-criterion", "d", "--criterion-type", "measurable"]
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "--plan" in err
    assert PROSE_NOT_A_PATH not in err
    assert len(err) < 600
