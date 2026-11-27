"""The order part of a spawn's brief and the size line that requires it (delegation by size,
part one).

`spawn-specialist.py` draws the size line through `classify.above_threshold` /
`plan.stage_above_threshold` (`agentctl.brief_order.resolve_size`) and refuses a brief above it
unless the brief carries the five order parts: place, what is missing, product, acceptance
checks, binding conditions. Part one builds two size sources: the executed stage's own
declaration (`--stage-index` of a stage whose executor is `spawn:<kind>`) and the explicit
`--declared-size` / `--size-flags`; the session source is a later part.

Every test drives `spawn-specialist.py` through `main` / `assembled_prompt`; plans are
hand-written TOML in `tmp_path`, so nothing depends on the directory the tests run from and no
model, session or config of the machine is read (the thresholds are patched in, the state root
and the environment are the test's own).
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import brief_order
from agentctl.config import Thresholds
from order_agreement_mutants import MUTANTS, mutant, script_module  # noqa: F401  (autouse fixture)

MOD = script_module("spawn-specialist.py")
PLAN_REVIEW = script_module("plan-review-topological.py")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "brief_order_part"

TIGHT = Thresholds({
    "small-change-max-lines": "50",
    "substantive-wall-clock-min": "10",
    "effort-stage-minutes-small": "5",
    "effort-stage-minutes-medium": "8",
    "effort-stage-minutes-large": "20",
})
LOOSE = Thresholds({
    "small-change-max-lines": "500",
    "substantive-wall-clock-min": "10",
    "effort-stage-minutes-small": "5",
    "effort-stage-minutes-medium": "8",
    "effort-stage-minutes-large": "20",
})

PART_NAMES = ("place", "what is missing", "product", "acceptance checks", "binding conditions")
FULL_BRIEF = (
    "Place: the spawner, relied on by every specialist.\n"
    "What is missing: a brief above the line states no order.\n"
    "Product: the five-part check.\n"
    "Acceptance checks: a brief lacking a part is refused.\n"
    "Binding conditions: none\n"
)
FULL_BRIEF_LINES = FULL_BRIEF.splitlines()
KINDS = ("developer", "tech-writer", "thinker", "planner", "code-reviewer")

META = """[meta]
weight_class = "small_change"
task_id = "brief-order-part"
goal = "check the brief"
done_criterion = "the brief is checked"
criterion_type = "measurable"
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A spawn that reads nothing of the machine: no own engine session, the thresholds the
    test names, the cost log in tmp_path, the state root an empty directory."""
    monkeypatch.delenv(MOD.OWN_SESSION_ENV, raising=False)
    monkeypatch.setattr(MOD, "COST_LOG", tmp_path / "spawn-costs.jsonl")
    monkeypatch.setattr(MOD, "Thresholds", lambda: TIGHT)
    (tmp_path / "state").mkdir()


def _stage(index: int = 1, *, executor: str = "spawn:developer", place: "str | None" = "the engine",
           size: str = "declared_size = 400", conditions: "str | None" = "binding_conditions = []") -> str:
    lines = [
        "[[stage]]", f"index = {index}", f'title = "stage {index}"', f'executor = "{executor}"',
        'expected_result_image = "the product"', 'criterion_type = "measurable"',
        'done_criterion = "the check passes"', "depends_on = []",
    ]
    if place is not None:
        lines.append(f'place = "{place}"')
    if size:
        lines.append(size)
    if conditions:
        lines.append(conditions)
    return "\n".join(lines) + "\n"


def _plan(tmp_path: Path, *stages: str, meta_extra: str = "") -> Path:
    path = tmp_path / "plan.toml"
    path.write_text(META + meta_extra + "\n" + "\n".join(stages or (_stage(),)), encoding="utf-8")
    return path


def _plain_brief(tmp_path: Path) -> Path:
    path = tmp_path / "brief.md"
    path.write_text("A plain brief, not a plan.\n", encoding="utf-8")
    return path


def _argv(plan: Path, *, kind: str = "developer", stage: "int | None" = None, constraints: str = "",
          extra: "list[str] | None" = None) -> list[str]:
    argv = ["--kind", kind, "--plan", str(plan), "--done-criterion", "tests green",
            "--criterion-type", "measurable", "--complexity", "medium", "--effort", "high",
            "--state-root", str(plan.parent / "state")]
    if stage is not None:
        argv += ["--stage-index", str(stage)]
    if constraints:
        argv += ["--constraints", constraints]
    return argv + (extra or [])


def _run(argv: list[str]) -> "tuple[int, str, str]":
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = MOD.main(argv + ["--dry-run"])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def _prompt(argv: list[str]) -> str:
    return MOD.assembled_prompt(argv)


def _todays_prompt(argv: list[str], monkeypatch) -> str:
    """The prompt the spawn assembled before the order part existed: the new section stubbed out."""
    with monkeypatch.context() as mp:
        mp.setattr(MOD, "order_part_section", lambda *a, **k: [])
        return _prompt(argv)


def _without(brief_lines: "list[str]", part: str) -> str:
    head = {"place": "Place:", "what is missing": "What is missing:", "product": "Product:",
            "acceptance checks": "Acceptance checks:", "binding conditions": "Binding conditions:"}[part]
    return "\n".join(line for line in brief_lines if not line.startswith(head)) + "\n"


# --- registration ------------------------------------------------------------------------

def test_mutant_brief_order_part_unchecked_registered(monkeypatch):
    assert "brief_order_part_unchecked" in MUTANTS
    MUTANTS["brief_order_part_unchecked"](monkeypatch)
    assert brief_order.order_part_violations({}) == [], "the planted mutant finds no missing part"


# --- the stage's own declaration ---------------------------------------------------------

def test_stage_dispatch_refused_without_order_part(tmp_path):
    plan = _plan(tmp_path, _stage(size="declared_size = 400", conditions=None))
    rc, _out, err = _run(_argv(plan, stage=1))
    assert rc == 2
    assert "above the size line (the declaration of stage 1)" in err
    assert "what is missing" in err and "binding conditions" in err
    assert "'What is missing:'" in err, "the refusal names the headed section to add"


def test_under_threshold_stage_dispatches_without_check(tmp_path):
    plan = _plan(tmp_path, _stage(size="declared_size = 10\nexpected_minutes = 3", conditions=None))
    rc, out, _err = _run(_argv(plan, stage=1))
    assert rc == 0
    assert "## The order this stage carries" not in out


def test_stage_below_line_raised_by_size_flags_refused(tmp_path):
    plan = _plan(tmp_path, _stage(size="declared_size = 10\nexpected_minutes = 3"))
    assert _run(_argv(plan, stage=1))[0] == 0, "below the line without the flag"
    rc, _out, err = _run(_argv(plan, stage=1, extra=["--size-flags", "architectural"]))
    assert rc == 2
    assert "what is missing" in err
    assert "binding conditions" not in err, "the stage states its conditions itself"
    carried = _argv(plan, stage=1, constraints="What is missing: the line.",
                    extra=["--size-flags", "architectural"])
    assert _run(carried)[0] == 0


def test_override_cannot_lower_stage_declaration(tmp_path):
    plan = _plan(tmp_path, _stage(size="declared_size = 400"))
    for lowering in (["--declared-size", "0"], ["--declared-size", "10"]):
        rc, _out, err = _run(_argv(plan, stage=1, extra=lowering))
        assert rc == 2, lowering
        assert "the declaration of stage 1" in err


def test_new_form_stage_declaring_no_size_refused_without_order_part(tmp_path):
    plan = _plan(tmp_path, _stage(size="", conditions="binding_conditions = []"))
    rc, _out, err = _run(_argv(plan, stage=1))
    assert rc == 2 and "what is missing" in err
    assert _run(_argv(plan, stage=1, constraints="What is missing: the line."))[0] == 0


def test_old_form_plan_stage_keeps_todays_prompt(tmp_path, monkeypatch):
    plan = _plan(tmp_path, _stage(place=None, size="", conditions=None))
    argv = _argv(plan, stage=1)
    assert _prompt(argv) == _todays_prompt(argv, monkeypatch)
    assert "## The order this stage carries" not in _prompt(argv)
    assert _run(argv)[0] == 0


def test_review_spawn_on_other_executor_stage_gets_todays_prompt(tmp_path, monkeypatch):
    plan = _plan(tmp_path, _stage(executor="spawn:developer", size="declared_size = 400"))
    for extra in ([], ["--declared-size", "0"]):
        argv = _argv(plan, kind="thinker", stage=1, extra=extra)
        assert _prompt(argv) == _todays_prompt(argv, monkeypatch), extra
    unscoped = _argv(plan, kind="thinker", extra=["--declared-size", "0"])
    assert _run(unscoped)[0] == 0


def test_binding_conditions_none_accepted_absent_refused(tmp_path):
    stated_none = _plan(tmp_path, _stage(conditions="binding_conditions = []"))
    carried = _argv(stated_none, stage=1, constraints="What is missing: x.")
    assert _run(carried)[0] == 0
    assert "Binding conditions:\nnone" in _prompt(carried)
    absent = _plan(tmp_path, _stage(conditions=None))
    rc, _out, err = _run(_argv(absent, stage=1, constraints="What is missing: x."))
    assert rc == 2 and "binding conditions" in err
    plain = _plain_brief(tmp_path)
    explicit_none = _argv(plain, constraints=FULL_BRIEF, extra=["--declared-size", "400"])
    assert _run(explicit_none)[0] == 0
    no_heading = _argv(plain, constraints=_without(FULL_BRIEF_LINES, "binding conditions"),
                       extra=["--declared-size", "400"])
    rc, _out, err = _run(no_heading)
    assert rc == 2 and "binding conditions" in err


def test_binding_conditions_read_from_description_and_how_kept(tmp_path):
    rows = ('binding_conditions = [{id = "B1", text = "no push to trunk", set_by = "user"}]')
    sub_plan = ('[meta.sub_plan]\nstage = 1\n'
                'how_kept = [{condition = "B2", how = "no push verb is called"}]\n')
    plan = _plan(tmp_path, _stage(conditions=rows), meta_extra=sub_plan)
    argv = _argv(plan, stage=1, constraints="What is missing: x.")
    prompt = _prompt(argv)
    assert "- B1: no push to trunk" in prompt
    assert "B2 (inherited; its text is not readable here): kept by: no push verb is called" in prompt


def test_binding_conditions_inherited_text_is_read_from_the_session(tmp_path, monkeypatch):
    sub_plan = ('[meta.sub_plan]\nstage = 1\n'
                'how_kept = [{condition = "B2", how = "no push verb is called"}]\n')
    plan = _plan(tmp_path, _stage(conditions="binding_conditions = []"), meta_extra=sub_plan)
    from agentctl import cli_common
    monkeypatch.setattr(MOD, "_spawn_session_state", lambda args: object())
    monkeypatch.setattr(cli_common, "inherited_binding_conditions",
                        lambda state: ({"id": "B2", "text": "no trunk push", "set_by": "user"},))
    prompt = _prompt(_argv(plan, stage=1, constraints="What is missing: x.",
                           extra=["--session", "s-1"]))
    assert "- B2 (inherited): no trunk push" in prompt


# --- explicit inputs ---------------------------------------------------------------------

def test_undeclared_spawn_keeps_todays_prompt(tmp_path, monkeypatch):
    plan = _plain_brief(tmp_path)
    argv = _argv(plan)

    def unread():
        raise AssertionError("an undeclared spawn reads no threshold")

    monkeypatch.setattr(MOD, "Thresholds", unread)
    assert _prompt(argv) == _todays_prompt(argv, monkeypatch)
    assert _run(argv)[0] == 0


def test_explicit_zero_outside_stage_is_small(tmp_path, monkeypatch):
    plan = _plain_brief(tmp_path)
    for declared in ("0", "10"):
        argv = _argv(plan, extra=["--declared-size", declared])
        assert _prompt(argv) == _todays_prompt(argv, monkeypatch), declared
        assert _run(argv)[0] == 0
    refused = _run(_argv(plan, extra=["--declared-size", "400"]))
    assert refused[0] == 2 and "the explicit --declared-size / --size-flags" in refused[2]


@pytest.mark.parametrize("flag", sorted(brief_order.size_flag_names()))
def test_size_flags_each_alone_substantive(tmp_path, flag):
    plan = _plain_brief(tmp_path)
    rc, _out, err = _run(_argv(plan, extra=["--size-flags", flag]))
    assert rc == 2, flag
    assert "above the size line" in err
    carried = _argv(plan, constraints=FULL_BRIEF, extra=["--size-flags", flag])
    assert _run(carried)[0] == 0


def test_size_flags_alone_classed_on_zero_lines(tmp_path):
    plan = _plain_brief(tmp_path)
    for extra in (["--size-flags", "architectural"],
                  ["--declared-size", "0", "--size-flags", "architectural"]):
        assert _run(_argv(plan, extra=extra))[0] == 2, extra


@pytest.mark.parametrize("kind", KINDS)
def test_reviewer_above_threshold_refused_without_order_part(tmp_path, kind):
    plan = _plain_brief(tmp_path)
    extra = ["--declared-size", "400"]
    rc, _out, err = _run(_argv(plan, kind=kind, extra=extra))
    assert rc == 2 and "does not carry the order parts" in err, kind
    assert _run(_argv(plan, kind=kind, constraints=FULL_BRIEF, extra=extra))[0] == 0


def test_each_missing_order_part_refused(tmp_path):
    plan = _plain_brief(tmp_path)
    for part in PART_NAMES:
        argv = _argv(plan, constraints=_without(FULL_BRIEF_LINES, part), extra=["--declared-size", "400"])
        rc, _out, err = _run(argv)
        assert rc == 2, part
        listed = err.split("does not carry the order parts:", 1)[1].split(".", 1)[0]
        assert [n.strip() for n in listed.split(",")] == [part], (part, listed)
    blank = _argv(plan, constraints=FULL_BRIEF.replace("Product: the five-part check.", "Product:"),
                  extra=["--declared-size", "400"])
    assert _run(blank)[0] == 2, "a heading with no text is a missing part"


@pytest.mark.parametrize(('binding_section', 'expected_rc'), [
    pytest.param('', 2, id='absent'),
    pytest.param('Binding conditions:', 2, id='blank'),
    pytest.param('Binding conditions: none', 0, id='explicit-none'),
    pytest.param('Binding conditions:\nKeep the existing interface.', 0, id='multiline'),
])
def test_binding_conditions_end_at_the_source_boundary(tmp_path, binding_section, expected_rc):
    plan = _plain_brief(tmp_path)
    dossier = tmp_path / 'dossier.md'
    dossier.write_text('Place: the spawner.\nWhat is missing: a check.', encoding='utf-8')
    constraints = 'Product: a checked brief.\nAcceptance checks: refusals name the missing part.\n' + binding_section
    # _argv supplies the separate, unheaded --done-criterion 'tests green'.
    argv = _argv(plan, constraints=constraints,
                 extra=['--declared-size', '400', '--context-dossier', str(dossier)])
    rc, _out, err = _run(argv)
    assert rc == expected_rc, err
    if expected_rc == 2:
        assert 'does not carry the order parts: binding conditions.' in err


def test_brief_parts_are_read_from_dossier_and_done_criterion(tmp_path):
    plan = _plain_brief(tmp_path)
    dossier = tmp_path / "dossier.md"
    dossier.write_text("Place: the spawner.\nWhat is missing: the check.\n", encoding="utf-8")
    argv = _argv(plan, constraints="Product: the check.\nAcceptance checks: refused.\n"
                 "Binding conditions: none\n", extra=["--declared-size", "400", "--context-dossier",
                                                    str(dossier)])
    assert _run(argv)[0] == 0


def test_threshold_change_moves_line(tmp_path, monkeypatch):
    plan = _plan(tmp_path, _stage(size="declared_size = 80\nexpected_minutes = 3", conditions=None))
    argv = _argv(plan, stage=1)
    assert _run(argv)[0] == 2, "80 lines is above a 50-line line"
    monkeypatch.setattr(MOD, "Thresholds", lambda: LOOSE)
    assert _run(argv)[0] == 0, "and below a 500-line line"
    explicit = _argv(_plain_brief(tmp_path), extra=["--declared-size", "80"])
    assert _run(explicit)[0] == 0
    monkeypatch.setattr(MOD, "Thresholds", lambda: TIGHT)
    assert _run(explicit)[0] == 2


# --- the way on, the exempt flows and the failures of what is read ------------------------

def test_refusal_way_on_respawn_dispatches(tmp_path):
    stage_plan = _plan(tmp_path, _stage(conditions=None))
    argv = _argv(stage_plan, stage=1)
    rc, _out, err = _run(argv)
    assert rc == 2
    for heading in ("'Place:'", "'What is missing:'", "'Product:'", "'Acceptance checks:'",
                    "'Binding conditions:'"):
        assert heading in err
    way_on = _argv(stage_plan, stage=1, constraints="What is missing: x.\nBinding conditions: none")
    assert _run(way_on)[0] == 0
    plain = _plain_brief(tmp_path)
    explicit = _argv(plain, extra=["--declared-size", "400"])
    assert _run(explicit)[0] == 2
    assert "--declared-size 0" in _run(explicit)[2]
    small = _argv(plain, extra=["--declared-size", "0"])
    assert _run(small)[0] == 0


def test_prompt_only_and_dry_run_meet_the_refusal_and_the_way_on(tmp_path):
    plan = _plain_brief(tmp_path)
    argv = _argv(plan, extra=["--declared-size", "400"])
    with pytest.raises(ValueError):
        _prompt(argv)
    assert _run(argv)[0] == 2
    carried = _argv(plan, constraints=FULL_BRIEF, extra=["--declared-size", "400"])
    assert "Binding conditions: none" in _prompt(carried)
    assert _run(carried)[0] == 0


def test_dry_run_and_prompt_only_meet_the_refusal(tmp_path):
    plan = _plan(tmp_path, _stage(conditions=None))
    argv = _argv(plan, stage=1)
    rc, _out, err = _run(argv)
    assert rc == 2 and "does not carry the order parts" in err
    with pytest.raises(ValueError):
        _prompt(argv)


def test_unreadable_config_fails_open_with_a_note(tmp_path, monkeypatch):
    def unreadable():
        raise OSError("no config.md")

    monkeypatch.setattr(MOD, "Thresholds", unreadable)
    plan = _plan(tmp_path, _stage(conditions=None))
    rc, _out, err = _run(_argv(plan, stage=1))
    assert rc == 0
    assert "the size thresholds do not read (OSError)" in err


def test_unreadable_session_state_still_launches(tmp_path):
    sub_plan = ('[meta.sub_plan]\nstage = 1\n'
                'how_kept = [{condition = "B2", how = "no push verb is called"}]\n')
    plan = _plan(tmp_path, _stage(conditions="binding_conditions = []"), meta_extra=sub_plan)
    (tmp_path / "state" / "broken.json").write_text("{not json", encoding="utf-8")
    argv = _argv(plan, stage=1, constraints="What is missing: x.", extra=["--session", "broken"])
    args = MOD.build_parser().parse_args(argv)
    assert MOD._spawn_session_state(args) is None
    lines = MOD.order_part_section(args, None, ["What is missing: x."])
    assert any("B2 (inherited; its text is not readable here)" in line for line in lines)
    absent = _argv(plan, stage=1, constraints="What is missing: x.", extra=["--session", "absent"])
    rc, out, err = _run(absent)
    assert rc == 0, err
    assert "B2 (inherited; its text is not readable here)" in out


def test_launches_with_a_fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    plan = _plan(tmp_path, _stage(conditions="binding_conditions = []"))
    assert _run(_argv(plan, stage=1, constraints="What is missing: x."))[0] == 0


def test_unreadable_condition_session_stops_spawn_readably(tmp_path):
    plan = _plan(tmp_path, _stage(size="declared_size = 10\nexpected_minutes = 3"))
    (tmp_path / "state" / "broken.json").write_text("{not json", encoding="utf-8")
    argv = _argv(plan, stage=1, extra=["--condition-session", "broken"])
    rc, _out, err = _run(argv)
    assert rc == 2
    assert "the session named by --condition-session ('broken') does not read" in err
    assert "Traceback" not in err
    named_ok = _argv(plan, stage=1, extra=["--condition-session", "no-such-session"])
    assert _run(named_ok)[0] == 0, "a named session with no state on record is the plan as written"
    with pytest.raises(ValueError, match="'broken'"):
        MOD.site_condition_decisions(SimpleNamespace(
            condition_session="broken", state_root=tmp_path / "state", session=None))


def test_own_session_unreadable_state_keeps_launching(tmp_path, monkeypatch):
    (tmp_path / "state" / "own.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(MOD.OWN_SESSION_ENV, "own")
    plan = _plan(tmp_path, _stage(size="declared_size = 10\nexpected_minutes = 3"))
    rc, _out, err = _run(_argv(plan, stage=1))
    assert rc == 0
    assert "the session's state does not read" in err


# --- the review spawn ---------------------------------------------------------------------

def test_review_command_passes_declared_size(tmp_path):
    args = SimpleNamespace(model=None, complexity="medium")
    argv = PLAN_REVIEW.build_spawn_argv(args, "1-2", str(tmp_path / "plan.toml"), dry_run=False)
    assert argv[argv.index("--declared-size") + 1] == "0"
    assert "--size-flags" not in argv
    assert MOD.build_parser().parse_args(argv).declared_size == 0


def test_review_spawn_prompt_is_todays_prompt(tmp_path, monkeypatch):
    plan = FIXTURES.parent / "plan_two_stage.toml"
    args = SimpleNamespace(model=None, complexity="medium")
    argv = PLAN_REVIEW.build_spawn_argv(args, "2-1", str(plan), dry_run=True)
    argv += ["--state-root", str(tmp_path / "state"), "--view-root", str(tmp_path / "views")]
    rc, _out, err = _run([a for a in argv if a != "--dry-run"])
    assert rc == 0, err
    assert "does not carry the order parts" not in err


def test_review_suggestion_in_cli_review_carries_declared_size():
    text = (Path(__file__).resolve().parent.parent / "agentctl" / "cli_review.py").read_text(encoding="utf-8")
    assert "--declared-size 0" in text


# --- the parser and the plan ---------------------------------------------------------------

@pytest.mark.parametrize("bad", [["--declared-size", "-1"], ["--declared-size", "many"],
                                 ["--size-flags", "enormous"]])
def test_parser_refuses_a_malformed_size(tmp_path, bad):
    with pytest.raises(SystemExit) as exc:
        MOD.build_parser().parse_args(_argv(_plain_brief(tmp_path), extra=bad))
    assert exc.value.code == 2


def test_parser_reads_a_flag_list(tmp_path):
    parsed = MOD.build_parser().parse_args(
        _argv(_plain_brief(tmp_path), extra=["--size-flags", "architectural,new dependency"]))
    assert parsed.size_flags == ["architectural", "new dependency"]


def test_new_form_plan_with_size_fields_submits():
    from test_stage_order_subplan import _new_form, _problems

    plain = _problems(_new_form())
    sized = _problems(_new_form({"declared_size": 120, "size_flags": ["architectural"],
                                 "expected_minutes": 45}))
    assert sized == plain
    assert not [p for p in sized if "size" in p or "minutes" in p]


def test_base_rev_fixture_is_one_revision_line():
    lines = (FIXTURES / "base_rev").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("^") and len(lines[0]) == 41
