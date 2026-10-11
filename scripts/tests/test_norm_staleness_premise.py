"""Stage 2 of norm-staleness: one norm delta read by every consumer of "what changed".

The difficulty: a plan edit that left a stage's deliverable alone still re-opened work
keyed on the whole stage definition -- a covered order element went stale on a change to
HOW the stage proceeds, a verify_command rewrite that kept its program and script was
judged substantive, and a refinement that changed a command the engine cannot resolve
passed as inside the autonomy boundary. Each case is exercised here against the real
`plan`/`cli`/`premise` code; the pure `plan.norm_delta` API is called at test time (not
imported by name), so this module collects on a tree that predates it. (The enumeration
narrowing this module once also covered went with the standalone enumerator.)"""
from __future__ import annotations

import json
import shutil
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentctl import cli, grants, order_approvals, plan, plan_resources, plugins, plugins_premise, premise, tool_contracts
from agentctl.plan import diff_plans, load_plan
from agentctl.state import Node, SessionState
from agentctl.text_shape import WHOLE_STAGE_ELEMENT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LAND_BRANCH = Path(__file__).resolve().parents[1] / "land-branch.py"


_STAGE_TMPL = """\
[[stage]]
index = {i}
title = "Stage {i}"
executor = "spawn:developer"
expected_result_image = "{img}"
criterion_type = "measurable"
done_criterion = "stage {i} done"
means = "tool"
method = "{method}"
depends_on = {deps}
output_artifacts = ["s{i}.py"]
{verify}"""


def _write_plan(path, stages, *, verify=None, final_checks=(), functional_place=None, repo_root=None):
    """`stages`: (index, result image, method) triples. `verify` maps a stage index to its
    verify_command; `final_checks` are commands; `functional_place` declares an order."""
    body = [
        "[meta]",
        'task_id = "demo-norm-staleness"',
        'goal = "exercise the norm delta"',
        'done_criterion = "all stages PASSED"',
        'criterion_type = "measurable"',
    ]
    if repo_root is not None:
        body.append(f"repo_root = {json.dumps(str(repo_root))}")
    if functional_place is not None:
        body += ["", "[meta.order]", 'customer_id = "acme"', 'customer = "the asker"',
                 f"functional_place = {json.dumps(functional_place)}"]
    body.append("")
    prev = None
    for i, img, method in stages:
        command = (verify or {}).get(i)
        extra = f"verify_command = {json.dumps(command)}\nexpected_exit = 0\n" if command else ""
        deps = "[]" if prev is None else f"[{prev}]"
        body.append(_STAGE_TMPL.format(i=i, img=img, method=method, deps=deps, verify=extra))
        prev = i
    for n, command in enumerate(final_checks, 1):
        body.append(f'[[final_check]]\nlabel = "check {n}"\ncommand = {json.dumps(command)}\nexpected_exit = 0\n')
    path.write_text("\n".join(body), encoding="utf-8")
    return path


BASE = [(1, "img-one", "do it by hand"), (2, "img-two", "do it by hand")]
EDITED_RESULT = [(1, "img-one-EDITED", "do it by hand"), BASE[1]]
EDITED_METHOD = [(1, "img-one", "do it differently"), BASE[1]]


def _state(store, plan_path, sid="s"):
    state = SessionState(session_id=sid, task_id="t")
    plugins.activate(state, "premise")
    state.plan_path = str(plan_path)
    store.save(state)
    return state


# --- the norm delta itself ---------------------------------------------------------

def _delta(tmp_path, new_stages, **new_kwargs):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    new = load_plan(_write_plan(tmp_path / "b.toml", new_stages, **new_kwargs))
    return plan.norm_delta(old, new)


def test_norm_delta_names_the_moved_elements_only(tmp_path):
    delta = _delta(tmp_path, EDITED_RESULT)

    assert delta.moved_stages == frozenset({1})
    assert "result" in delta.question_elements(1)
    assert "means" not in delta.question_elements(1)
    assert 1 in delta.interface_moved


def test_method_edit_moves_the_stage_but_not_its_interface(tmp_path):
    delta = _delta(tmp_path, EDITED_METHOD)

    assert delta.moved_stages == frozenset({1})
    assert delta.interface_moved == frozenset()
    assert "method" in delta.question_elements(1)


def test_identical_plans_have_no_delta(tmp_path):
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    assert not plan.norm_delta(doc, doc).any_moved


def test_order_only_edit_moves_the_order_and_no_stage(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE, functional_place="one place"))
    new = load_plan(_write_plan(tmp_path / "b.toml", BASE, functional_place="another place"))
    delta = plan.norm_delta(old, new)

    assert delta.order_moved and delta.any_moved
    assert not delta.moved_stages
    assert not delta.final_check_moved


def test_final_check_only_edit_moves_the_final_check_and_nothing_else(tmp_path):
    delta = _delta(tmp_path, BASE, final_checks=["git status"])

    assert delta.final_check_moved and delta.any_moved
    assert not delta.moved_stages
    assert not delta.meta_moved


# --- O-items bound to the covering stage's deliverable ---------------------------------

def _covered(stamp):
    return premise.OrderElement(id="O1", element="the ask", disposition="covered", stage=1,
                                content_digest=stamp)


def _stamp(plan_path):
    return cli._bound_order_stage_key(SimpleNamespace(), _covered(""), str(plan_path))


def _norm_keys(doc):
    build = getattr(plan, "stage_norm_keys", plan.stage_element_keys)
    return {s.index: build(s) for s in doc.stages}


def _stale_note_after_edit(tmp_path, edited_stages):
    """The stale note of an element stamped against BASE once the plan became `edited_stages`."""
    old_path = _write_plan(tmp_path / "a.toml", BASE)
    new = load_plan(_write_plan(tmp_path / "b.toml", edited_stages))
    bag = {"order_elements": premise.order_elements_to_dicts([_covered(_stamp(old_path))])}

    premise.invalidate_stale_order_dispositions(bag, _norm_keys(new))
    return premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note


def test_covered_element_is_not_stale_after_a_method_only_edit(tmp_path):
    assert _stale_note_after_edit(tmp_path, EDITED_METHOD) == ""


def test_covered_element_is_stale_after_the_result_image_moves(tmp_path):
    assert _stale_note_after_edit(tmp_path, EDITED_RESULT)


def test_order_stamp_survives_a_method_only_edit(tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    before = _stamp(plan_path)
    _write_plan(plan_path, EDITED_METHOD)

    assert before
    assert _stamp(plan_path) == before


def test_order_stamp_moves_with_the_result_image(tmp_path):
    plan_path = _write_plan(tmp_path / "plan.toml", BASE)
    before = _stamp(plan_path)
    _write_plan(plan_path, EDITED_RESULT)

    assert _stamp(plan_path) != before


def test_legacy_whole_stage_stamp_stays_accepted(tmp_path):
    """An element stamped by the older engine carries the whole-stage key; it keeps
    discharging until that whole definition moves."""
    doc = load_plan(_write_plan(tmp_path / "a.toml", BASE))
    keys = _norm_keys(doc)
    bag = {"order_elements": premise.order_elements_to_dicts([_covered(keys[1][WHOLE_STAGE_ELEMENT])])}

    assert not premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note == ""


def test_foreign_stamp_is_stale(tmp_path):
    keys = _norm_keys(load_plan(_write_plan(tmp_path / "a.toml", BASE)))
    bag = {"order_elements": premise.order_elements_to_dicts([_covered("iface:not-this-stage")])}

    assert premise.invalidate_stale_order_dispositions(bag, keys)
    assert premise.order_elements_from_dicts(bag["order_elements"])[0].stale_note


# --- classification from the norm delta ------------------------------------------------

def _grantgrowth(command):
    doc = load_plan(str(FIXTURES / "plan_two_stage_verifyfix_grantgrowth.toml"))
    doc.stages[0].criterion.verify_command = command
    return doc


def _verify_kind(old, new):
    """The kind of a verify_command rewrite with the relaxation admitted, as for a
    ledgered order."""
    return diff_plans(_grantgrowth(old), _grantgrowth(new), relax_verify_identity=True)


@pytest.mark.parametrize("old, new", [
    ("python3 mod.py", "python3 mod.py --strict"),
    ("python3 -m pytest a", "python3 -m pytest a --x"),
    ("python3 mod.py", "python3 mod.py -m other"),
])
def test_same_program_and_script_with_other_arguments_is_a_refinement(old, new):
    assert _verify_kind(old, new) == "refinement"


@pytest.mark.parametrize("old, new", [
    ("python3 mod.py", "python3 other.py"),
    ("python3 -m pytest a", "python3 -m b"),
    ("python3 mod.py", "python3 -m mod"),
    ("python3 mod.py", "python3.11 -W ignore mod.py"),
    ("python3 mod.py", "python3 -u mod.py"),
    ("python3 mod.py", "python3 -W ignore mod.py"),
    ("node -r ./hook.js a.js", "node -r ./evil.js a.js"),
    ("git status", "git push origin HEAD:main"),
    ("python3 mod.py", "env python3 mod.py"),
    ("python3 mod.py", "timeout 5 python3 mod.py"),
    ("bash x.sh", "bash -o pipefail x.sh"),
])
def test_a_new_program_script_module_or_uncovered_flag_is_growth(old, new):
    assert _verify_kind(old, new) == "substantive"


@pytest.mark.parametrize("rule, identity", [
    ("Bash(python3 mod.py:*)", ("python3", "mod.py")),
    ("Bash(python3 mod.py --strict:*)", ("python3", "mod.py")),
    ("Bash(python3 -W ignore mod.py:*)", None),
    ("Bash(node -r ./hook.js a.js:*)", None),
    ("Bash(perl -M Evil a.pl:*)", None),
    ("Bash(git status:*)", None),
    ("Bash(git push origin HEAD:main:*)", None),
    ("Bash(git -C . status:*)", None),
    ("Bash(python3 mod.py -m a:*)", ("python3", "mod.py")),
    ("Bash(python3 -m pytest:*)", ("python3", "-m", "pytest")),
    ("Bash(python3 -m pytest --x:*)", ("python3", "-m", "pytest")),
    ("Bash(pytest -q tests/a.py:*)", ("pytest",)),
    ("Bash(python3:*)", None),
    ("Bash(python3 -u:*)", None),
    ("Bash(python3 -u mod.py:*)", None),
    ("Bash(python3 -m:*)", None),
    ("Bash(python3 -m a:*)", None),
    ("Bash(python3 -m pip install x:*)", None),
    ("Bash(python3 -m cProfile a.py:*)", None),
    ("Bash(python3 -m coverage run a.py:*)", None),
    ("Bash(python3 -m timeit x:*)", None),
    ("Bash(python3 -c print:*)", None),
    ("Bash(python3.11 -W ignore x.py:*)", None),
    ("Bash(bash -o pipefail x.sh:*)", None),
    ("Bash(env python3 mod.py:*)", None),
    ("Bash(/usr/bin/env python3 mod.py:*)", None),
    ("Bash(/usr/bin/python3 mod.py:*)", None),
    ("Bash(timeout 5 python3 mod.py:*)", None),
    ("Bash(nice python3 mod.py:*)", None),
    ("Bash(strace python3 mod.py:*)", None),
    ("Bash(uv run mod.py:*)", None),
    ("Bash(make -f x:*)", None),
    ("Bash(ksh a.sh:*)", None),
    ("Bash(fish a.fish:*)", None),
    ("Bash(bun a.ts:*)", None),
    ("Bash(deno run a.ts:*)", None),
    ("Bash(php a.php:*)", None),
    ("Bash(ruff check x:*)", None),
    ("Edit(//tmp/x/**)", None),
])
def test_rule_identity_never_hands_a_new_executable_an_approved_identity(rule, identity):
    """Identity is closed-world: a program not positively classified has none."""
    assert grants.bash_rule_identity(rule) == identity


_UNCLASSIFIED_REWRITES = [
    ("/usr/bin/env python3 a.py", "/usr/bin/env rm -rf build"),
    ("ksh a.sh", "ksh b.sh"),
    ("python3 -m cProfile a.py", "python3 -m cProfile b.py"),
]


@pytest.mark.parametrize("old, new", _UNCLASSIFIED_REWRITES)
def test_unclassified_rewrite_is_substantive_with_or_without_the_relaxation(old, new):
    assert diff_plans(_grantgrowth(old), _grantgrowth(new)) == "substantive"
    assert diff_plans(_grantgrowth(old), _grantgrowth(new), relax_verify_identity=True) == "substantive"


def test_unledgered_diff_keeps_the_plain_set_difference():
    """Without the relaxation, a same-identity rewrite that grows the derived rules is
    substantive, as before the relaxation existed."""
    assert diff_plans(_grantgrowth("python3 mod.py"), _grantgrowth("python3 mod.py --strict")) == "substantive"


def _replan_verify(store, tmp_path, old_verify, new_verify, *, ledgered, sid="rv"):
    """Drive the real replan of stage 1's verify_command from `old_verify` to `new_verify`.
    `ledgered`: the plan declares an order and its customer approves it, so the order has
    a user-approved ledger record; otherwise the plan has no order at all."""
    fixture = (FIXTURES / "plan_two_stage_verifyfix_grantgrowth.toml").read_text(encoding="utf-8")
    fixture = fixture.replace('output_artifacts = ["mod.py"]',
                              'output_artifacts = ["a.py", "b.py", "a.sh", "b.sh", "tests/a.py"]')
    if ledgered:
        fixture = fixture.replace("\n[[stage]]\n", '\n[meta.order]\ncustomer_id = "acme"\n'
                                  'customer = "the asker"\nfunctional_place = "one place"\n\n[[stage]]\n', 1)
    old, new = tmp_path / "old.toml", tmp_path / "new.toml"
    for path, command in ((old, old_verify), (new, new_verify)):
        path.write_text(fixture.replace('verify_command = "python3 mod.py"',
                                        f"verify_command = {json.dumps(command)}"), encoding="utf-8")
    cli.cmd_start(Namespace(session=sid, task="demo-norm-staleness", goal="", done_criterion="",
                            criterion_type="measurable", recursion_depth=0), store=store)
    cli.cmd_classify(Namespace(session=sid, chat=False, changed_lines=200, files=5,
                               wall_clock_min=60, tracker_key=None, architectural=True,
                               external_effect=False, new_dependency=False,
                               public_api_change=False), store=store)
    cli.cmd_plan(Namespace(session=sid), store=store)
    submitted = cli.cmd_submit_plan(Namespace(session=sid, plan=str(old)), store=store)
    assert submitted.ok, submitted.detail
    cli.cmd_approve(Namespace(session=sid, by="acme" if ledgered else "user"), store=store)
    cli.cmd_partition(Namespace(session=sid, m1=False, m2=False, m3=False, m4=False,
                                m3_severe=False, m4_severe=False), store=store)
    cli.cmd_next_stage(Namespace(session=sid), store=store)
    state = store.load(sid)
    assert state.node == Node.EXECUTING.value
    assert (cli._ledgered_order_key(state) is not None) is ledgered
    return cli.cmd_replan(Namespace(session=sid, plan=str(new)), store=store)


@pytest.mark.parametrize("old, new", _UNCLASSIFIED_REWRITES)
def test_unledgered_replan_of_an_unclassified_rewrite_rearms_approval(store, tmp_path, monkeypatch, old, new):
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    d = _replan_verify(store, tmp_path, old, new, ledgered=False)

    assert d.marker == "PLAN-READY"


def test_unledgered_replan_of_a_same_identity_rewrite_rearms_approval(store, tmp_path, monkeypatch):
    """The relaxation is admitted only for a ledgered order: with no ledger the plain set
    difference applies, so even a same-identity rewrite re-arms approval."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    d = _replan_verify(store, tmp_path, "pytest -q tests/a.py", "pytest -q tests/a.py -x", ledgered=False)

    assert d.marker == "PLAN-READY"


def test_ledgered_replan_of_a_same_identity_rewrite_stays_a_refinement(store, tmp_path, monkeypatch):
    """The ledgered counterpart: the same criterion-confined, same-identity rewrite resumes
    execution without re-approval."""
    monkeypatch.setenv("AGENTCTL_REPLAN_AUTHORIZATION", "0")
    d = _replan_verify(store, tmp_path, "pytest -q tests/a.py", "pytest -q tests/a.py -x", ledgered=True)

    assert d.action == "continue", d.detail
    assert d.marker is None


def test_rewrite_that_also_moves_the_interface_keeps_the_plain_set_difference():
    old = _grantgrowth("python3 mod.py")
    new = _grantgrowth("python3 mod.py --strict")
    new.stages[0].subject.result = "a different deliverable"

    assert "interface" in plan.norm_delta(old, new).elements[1]
    assert diff_plans(old, new, relax_verify_identity=True) == "substantive"


def test_rewrite_that_also_moves_the_order_keeps_the_plain_set_difference(tmp_path):
    old = load_plan(_write_plan(tmp_path / "a.toml", BASE, verify={1: "python3 mod.py"},
                                functional_place="one place"))
    new = load_plan(_write_plan(tmp_path / "b.toml", BASE, verify={1: "python3 mod.py --strict"},
                                functional_place="another place"))

    assert diff_plans(old, new, relax_verify_identity=True) == "substantive"


# --- the autonomy boundary, through the real ledger -------------------------------------

def _approved_order(store, tmp_path, old_verify, stage2_verify=None):
    """A session whose order has a user-approved version holding `old_verify` (and
    `stage2_verify` on stage 2, whose approved resources the order-wide boundary shares);
    returns the state and the venue the plans declare."""
    venue = tmp_path / "venue"
    (venue / "scripts").mkdir(parents=True)
    shutil.copy(LAND_BRANCH, venue / "scripts" / "land-branch.py")
    (venue / "lb.py").symlink_to("scripts/land-branch.py")
    verify = {1: old_verify} | ({2: stage2_verify} if stage2_verify else {})
    base = _write_plan(tmp_path / "base.toml", BASE, verify=verify, repo_root=venue)
    doc = load_plan(str(base))
    key = plan.order_digest(doc)
    view = plan_resources.compute_boundary_view(doc, key)
    order_approvals.record_approval(
        key, plan_sha256="p1", resources=view.resources,
        unresolved_identities=[item["identity"] for item in view.unresolved],
        stage_effects=[], by="acme", at="2026-10-09T00:00:00Z")
    return _state(store, base), venue


def _replan_verdict(store, tmp_path, old_verify, new_verify, stage2_verify=None):
    """`(diff_plans kind, kind the replan applies as)` of rewriting stage 1's verify_command
    in a ledgered order."""
    state, venue = _approved_order(store, tmp_path, old_verify, stage2_verify)
    old = load_plan(state.plan_path)
    verify = {1: new_verify} | ({2: stage2_verify} if stage2_verify else {})
    new = load_plan(str(_write_plan(tmp_path / "new.toml", BASE, verify=verify, repo_root=venue)))
    return diff_plans(old, new, relax_verify_identity=True), cli._replan_kind(state, old, new)


def test_refinement_to_a_command_the_engine_cannot_resolve_goes_to_approval(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", "python3 mod.py --strict")

    assert bounded == "substantive"
    assert kind == "refinement"


@pytest.mark.parametrize("suffix", ["$(curl x)", "`curl x`"])
def test_nested_execution_in_a_rewritten_command_is_never_inside_the_boundary(store, tmp_path, suffix):
    """Derivation drops a segment it cannot read, so the grant set does not grow and the
    plain diff calls this a refinement; the whole text is unresolved, which the boundary
    escalates."""
    kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", f"python3 mod.py {suffix}")

    assert bounded == "substantive"
    assert kind == "refinement"


@pytest.mark.parametrize("new_verify", ["python3 -u", "python3", "python3 -m pip"])
def test_interpreter_without_a_script_or_with_a_launcher_module_goes_to_approval(
        store, tmp_path, new_verify):
    """Derivation grants no rule to these, so the plain diff sees no growth; the boundary
    still reads the command as a changed unresolved one."""
    _kind, bounded = _replan_verdict(store, tmp_path, "python3 mod.py", new_verify)

    assert bounded == "substantive"


def test_refinement_to_a_resolved_command_inside_the_set_stays_a_refinement(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "pytest -q tests/a.py", "pytest -q tests/a.py -x")

    assert kind == "refinement"
    assert bounded == "refinement"


def test_rewrite_to_a_new_program_is_substantive_before_the_boundary(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "pytest -q tests/a.py", "ruff check tests/a.py")

    assert kind == "substantive"
    assert bounded == "substantive"


def test_command_substitution_is_unresolved(tmp_path):
    """The relaxation never reaches a rewritten command the engine cannot read: nested
    execution resolves to nothing, so the boundary escalates it."""
    resolution = tool_contracts.resolve_command("python3 $(echo mod.py)", str(tmp_path))
    assert resolution.status == "unresolved"


def test_a_push_to_main_never_shares_an_identity_with_another_git_subcommand(store, tmp_path):
    """The ledgered boundary's approved set is order-wide: stage 2 already holds an approved
    push to main, so stage 1's `git status` -> `git push origin HEAD:main` is covered by
    it. The edit still has to be substantive -- stage 1's own verify_command grew a push."""
    push = "git push origin HEAD:main"
    kind, bounded = _replan_verdict(store, tmp_path, "git status", push, stage2_verify=push)

    assert kind == "substantive"
    assert bounded == "substantive"


LAND_CHECK = "python3 scripts/land-branch.py --check"
LAND_PUSH = "python3 scripts/land-branch.py --keep-branch --remote-only --branch norm-staleness"


def test_g1_a_registry_script_push_form_is_substantive_though_its_check_form_is_approved(store, tmp_path):
    """`land-branch.py --check` resolves to nothing and `--keep-branch --remote-only` to a
    push to origin/main: one script, two resolved resources. The order-wide approved set
    already holds the push (stage 2), so only the diff layer can call stage 1's edit
    growth."""
    kind, bounded = _replan_verdict(store, tmp_path, LAND_CHECK, LAND_PUSH, stage2_verify=LAND_PUSH)

    assert kind == "substantive"
    assert bounded == "substantive"


@pytest.mark.parametrize("script", [
    "scripts/land-branch.py", "./scripts/land-branch.py", "scripts//land-branch.py",
    "elsewhere/land-branch.py", "../x/Land-Branch.py",
])
def test_g1_a_registry_script_has_no_identity_by_path_or_basename(script):
    assert grants.bash_rule_identity(f"Bash(python3 {script} --check:*)") is None


def test_g1_a_symlink_alias_of_a_registry_script_has_no_identity(tmp_path):
    """`resolve_script` matches by realpath against the venue, so `lb.py` resolves exactly
    as `scripts/land-branch.py` does; a text/basename match alone would miss the alias."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(LAND_BRANCH, tmp_path / "scripts" / "land-branch.py")
    (tmp_path / "lb.py").symlink_to("scripts/land-branch.py")

    assert grants.bash_rule_identity("Bash(python3 lb.py --check:*)", str(tmp_path)) is None
    assert grants.bash_rule_identity("Bash(python3 other.py --check:*)", str(tmp_path)) == ("python3", "other.py")


def test_g1_a_symlink_alias_check_to_push_is_substantive(store, tmp_path):
    """Through the real ledger: the push is already approved order-wide (stage 2), and the
    alias spelling must not let stage 1's `--check` -> push edit pass as a refinement."""
    alias_check = "python3 lb.py --check"
    alias_push = "python3 lb.py --keep-branch --remote-only --branch norm-staleness"
    kind, bounded = _replan_verdict(store, tmp_path, alias_check, alias_push, stage2_verify=LAND_PUSH)

    assert kind == "substantive"
    assert bounded == "substantive"


def test_g1_an_unregistered_script_keeps_its_identity():
    assert grants.bash_rule_identity("Bash(python3 scripts/other.py --check:*)") == ("python3", "scripts/other.py")


def test_g1_an_unreadable_registry_gives_no_script_an_identity(monkeypatch):
    def broken():
        raise ValueError("duplicate entry")
    monkeypatch.setattr(grants.script_effects, "load_script_effects_table", broken)

    assert grants.bash_rule_identity("Bash(python3 mod.py:*)") is None


def test_g2_a_new_pytest_argument_is_a_refinement_the_boundary_escalates(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "pytest -q tests/a.py", "pytest -q tests/a.py -p evil")

    assert kind == "refinement"
    assert bounded == "substantive"


def test_g3_a_new_argument_of_an_effect_none_program_is_a_refinement_the_boundary_escalates(store, tmp_path):
    kind, bounded = _replan_verdict(store, tmp_path, "rg foo", "rg --pre ./evil foo")

    assert kind == "refinement"
    assert bounded == "substantive"
