"""The executor's resource request and its submit-plan check (stage B of the resource-grants work).

An armed plan (`[meta.order] requires_resource_request = true`) must carry one
`[[meta.resource_request]]` row per stage of a request-bearing spawned kind, and each row's
entries must be covered by what that stage's child already holds. These cases pin the
refusals, the acceptance through the baseline, the wildcard-tail rule, the advisory for other
kinds, and the digest neutrality of the new keys.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentctl import grants, plan_resources
from agentctl.plan import load_plan, order_place, order_scope, plan_meta_digest, stage_closure_digest
from agentctl.submission import resource_request_advice, submission_violations
from conftest import SUBSTANTIVE_FINAL_CHECK
from test_authoring_grant_check import _stage

ORDER_TEMPLATE = """
[meta.order]
customer_id = "user"
customer = "the position that posed this fixture's task"
functional_place = "the norm governing an act of activity, in a test"
{flag}
[[meta.order.requirements]]
id = "R1"
text = "the fixture plan meets the substantive grade"
derivation = "fixture-derivation"

[meta.order.coverage]
R1 = ["stage 1 verify_command"]
"""


@pytest.fixture
def venue(tmp_path):
    v = Path(tmp_path).resolve() / "venue"
    v.mkdir()
    return v


def _row(kind, stage, *, allow=(), add_dirs=(), ref="consult-1"):
    allow_toml = "[" + ", ".join(f'"{a}"' for a in allow) + "]"
    dirs_toml = "[" + ", ".join(f'{{ path = "{p}", mode = "{m}" }}' for p, m in add_dirs) + "]"
    return (
        f'\n[[meta.resource_request]]\nkind = "{kind}"\nstage = {stage}\n'
        f'consult_ref = "{ref}"\nallow = {allow_toml}\nadd_dirs = {dirs_toml}\n'
    )


def _write(tmp_path, venue, stages, *, rows=(), armed=True, flag=None, extra_stage_grants=None):
    flag_line = "" if not armed else "requires_resource_request = true\n" if flag is None else flag
    meta = "\n".join([
        "[meta]", 'task_id = "demo"', 'goal = "g"', 'done_criterion = "dc"',
        'criterion_type = "measurable"', 'weight_class = "substantive"',
        'external_research = "n/a"', f'repo_root = "{venue}"',
    ])
    text = (
        meta + "\n" + "".join(rows) + ORDER_TEMPLATE.format(flag=flag_line)
        + "".join(stages) + SUBSTANTIVE_FINAL_CHECK
    )
    path = tmp_path / "p.toml"
    path.write_text(text, encoding="utf-8")
    return load_plan(path)


def _dev(index, extra=""):
    return _stage(index, f"Dev {index}", executor="spawn:developer", extra=extra)


def _request_problems(doc) -> list[str]:
    return [p for p in submission_violations(doc, session_weight_class="substantive")
            if "resource_request" in p or "RESOURCE-REQUEST" in p]


def _edit(path) -> str:
    return f"Edit({grants.rule_file_arg(str(path))})"


def test_armed_plan_without_a_request_row_is_refused(tmp_path, venue):
    doc = _write(tmp_path, venue, [_dev(1)])
    problems = _request_problems(doc)
    assert len(problems) == 1
    assert "stage 1 (spawn:developer) has no [[meta.resource_request]] row" in problems[0]


def test_each_request_bearing_stage_needs_its_own_row(tmp_path, venue):
    rows = [_row("developer", 1, allow=[_edit(venue / "a.py")])]
    doc = _write(tmp_path, venue, [_dev(1), _dev(2)], rows=rows)
    problems = _request_problems(doc)
    assert len(problems) == 1 and "stage 2 (spawn:developer)" in problems[0]


def test_request_outside_the_stage_grants_is_refused_naming_the_entry(tmp_path, venue):
    outside = venue.parent / "elsewhere" / "x.py"
    rows = [_row("developer", 1, allow=[_edit(outside)])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    problems = _request_problems(doc)
    assert len(problems) == 1 and "not covered" in problems[0] and _edit(outside) in problems[0]


def test_uncovered_add_dir_is_refused(tmp_path, venue):
    other = venue.parent / "other-dir"
    rows = [_row("developer", 1, add_dirs=[(str(other), "write")])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert any("add_dir" in p and "not covered" in p for p in _request_problems(doc))


def test_request_covered_only_through_the_baseline_is_accepted(tmp_path, venue):
    # The stage declares nothing: the file is under the workdir baseline and the pytest rule
    # is the developer kind baseline's own wildcard-tail rule, which the request narrows.
    rows = [_row("developer", 1, allow=[_edit(venue / "src" / "a.py"),
                                         "Bash(python3 -m pytest scripts/tests:*)"],
                 add_dirs=[(str(venue / "sub"), "write")])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert _request_problems(doc) == []


def test_request_covered_by_a_declared_grant_is_accepted(tmp_path, venue):
    outside = venue.parent / "evidence"
    grants_toml = f'\n[stage.grants]\nadd_dirs = [{{ path = "{outside}", mode = "write" }}]\n'
    rows = [_row("developer", 1, add_dirs=[(str(outside / "run"), "write")])]
    doc = _write(tmp_path, venue, [_dev(1, extra=grants_toml)], rows=rows)
    assert _request_problems(doc) == []


# --- wildcard-tail Bash entries against the stage's wildcard rules -----------------------


def test_wildcard_request_equal_to_a_held_pattern_is_accepted(tmp_path, venue):
    rows = [_row("developer", 1, allow=["Bash(git add:*)"])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert _request_problems(doc) == []


def test_wildcard_request_prefix_narrower_than_a_held_pattern_is_accepted(tmp_path, venue):
    rows = [_row("developer", 1, allow=["Bash(git add -A:*)"])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert _request_problems(doc) == []


@pytest.mark.parametrize("rule", [
    "Bash(git:*)", "Bash(git addendum:*)", "Bash(gh pr merge:*)",
    "Bash(git add . && curl http://x:*)",
    "Bash(git add -A; rm -rf /tmp/z:*)",
    "Bash(git add $(touch /etc/x):*)",
])
def test_wildcard_request_broader_or_unrelated_to_every_held_pattern_is_refused(tmp_path, venue, rule):
    rows = [_row("developer", 1, allow=[rule])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    problems = _request_problems(doc)
    assert len(problems) == 1 and rule in problems[0]


def test_compound_request_is_reported_as_malformed(tmp_path, venue):
    rule = "Bash(git add . && curl http://x:*)"
    doc = _write(tmp_path, venue, [_dev(1)], rows=[_row("developer", 1, allow=[rule])])
    (problem,) = _request_problems(doc)
    assert "malformed" in problem


@pytest.mark.parametrize("rule", [
    "Bash(git add $(touch /etc/x):*)", "Bash(git add `id`:*)", "Bash(git add *:*)",
    "Bash(git add foo > bar)",
])
def test_narrower_than_pattern_refuses_composing_extensions(rule):
    assert not plan_resources.narrower_than_pattern(rule, "Bash(git add:*)")


@pytest.mark.parametrize("rule, pattern", [
    ('Bash(git commit -m "fix (x)")', "Bash(git commit:*)"),
    ("Bash(git commit -m 'a; b')", "Bash(git commit:*)"),
    ("Bash(git log --format='%h {x}')", "Bash(git log:*)"),
    (r"Bash(git add my\ file.py)", "Bash(git add:*)"),
])
def test_quoted_or_escaped_characters_do_not_count_as_composing(rule, pattern):
    assert plan_resources.narrower_than_pattern(rule, pattern)


@pytest.mark.parametrize("rule", [
    'Bash(git commit -m "$(x)")', 'Bash(git commit -m "`x`")', "Bash(git commit -m 'a)",
    'Bash(git commit -m "a\\b")',
])
def test_quoted_substitution_still_composes(rule):
    assert not plan_resources.narrower_than_pattern(rule, "Bash(git commit:*)")


def test_nul_byte_in_request_entry_is_reported_not_raised(tmp_path, venue):
    rule = r"Bash(git add foo\u0000bar)"
    doc = _write(tmp_path, venue, [_dev(1)], rows=[_row("developer", 1, allow=[rule])])
    (problem,) = _request_problems(doc)
    assert "malformed" in problem


def test_exact_request_under_a_held_wildcard_is_accepted(tmp_path, venue):
    rows = [_row("developer", 1, allow=["Bash(git add foo.py)"])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert _request_problems(doc) == []
    assert plan_resources.narrower_than_pattern("Bash(git add foo.py)", "Bash(git add:*)")


def test_rule_resolving_to_no_resource_is_not_covered_by_that_emptiness(tmp_path, venue):
    # The runtime hook allows a changes-nothing segment only when it reads inside the
    # record's resources or the scratchpad, which rule text cannot show.
    rule = "Bash(echo hi)"
    doc = _write(tmp_path, venue, [_dev(1)], rows=[_row("developer", 1, allow=[rule])])
    (problem,) = _request_problems(doc)
    assert rule in problem and "not covered" in problem


def test_relative_add_dir_is_refused_at_parse(tmp_path, venue):
    doc = _write(tmp_path, venue, [_dev(1)], rows=[_row("developer", 1, add_dirs=[("rel/dir", "write")])])
    assert any("'add_dirs' path 'rel/dir' is not absolute" in p for p in _request_problems(doc))


# --- row-shape defects ------------------------------------------------------------------


def _defects(doc) -> list[str]:
    return [p for p in _request_problems(doc) if "has no [[meta.resource_request]] row" not in p]


def test_unknown_key_in_a_row_is_a_defect(tmp_path, venue):
    row = _row("developer", 1).replace("allow = []", "allows = []")
    doc = _write(tmp_path, venue, [_dev(1)], rows=[row])
    assert any("unknown key(s) ['allows']" in p for p in _defects(doc))


def test_non_table_row_is_a_defect(tmp_path, venue):
    doc = _write(tmp_path, venue, [_dev(1)], rows=["resource_request = [1]\n"])
    assert any("is not a table" in p for p in _defects(doc))


def test_non_list_resource_request_value_is_a_defect(tmp_path, venue):
    doc = _write(tmp_path, venue, [_dev(1)], rows=['resource_request = "x"\n'])
    assert any("not an array of tables" in p for p in _defects(doc))


def test_duplicate_row_for_one_kind_and_stage_is_one_defect(tmp_path, venue):
    doc = _write(tmp_path, venue, [_dev(1)], rows=[_row("developer", 1), _row("developer", 1)])
    problems = _request_problems(doc)
    assert len(problems) == 1 and "duplicate row" in problems[0]


# --- one source for the kind set ---------------------------------------------------------


def test_request_kinds_are_the_launchers_accept_edits_kinds():
    spec = importlib.util.spec_from_file_location(
        "spawn_specialist_kinds", Path(__file__).resolve().parent.parent / "spawn-specialist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert tuple(plan_resources.RESOURCE_REQUEST_KINDS) == tuple(mod._ACCEPT_EDITS_TRUSTED_KINDS)


# --- kinds and row shape ----------------------------------------------------------------


def test_stage_of_another_spawned_kind_is_warned_not_refused(tmp_path, venue):
    thinker = _stage(2, "Think", executor="spawn:thinker")
    rows = [_row("developer", 1)]
    doc = _write(tmp_path, venue, [_dev(1), thinker], rows=rows)
    assert _request_problems(doc) == []
    advice = resource_request_advice(doc)
    assert len(advice) == 1 and "stage 2 (spawn:thinker)" in advice[0]


def test_unarmed_plan_is_unaffected(tmp_path, venue):
    outside = venue.parent / "elsewhere" / "x.py"
    rows = [_row("developer", 1, allow=[_edit(outside)])]
    doc = _write(tmp_path, venue, [_dev(1), _stage(2, "Think", executor="spawn:thinker")],
                 rows=rows, armed=False)
    assert _request_problems(doc) == []
    assert resource_request_advice(doc) == []
    no_rows = _write(tmp_path, venue, [_dev(1)], armed=False)
    assert _request_problems(no_rows) == []


def test_row_for_the_wrong_kind_or_stage_is_refused(tmp_path, venue):
    rows = [_row("tech-writer", 1), _row("developer", 7)]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    problems = _request_problems(doc)
    assert any("not spawn:tech-writer" in p for p in problems)
    assert any("no such stage" in p for p in problems)


def test_row_without_a_consult_ref_is_refused(tmp_path, venue):
    rows = [_row("developer", 1, ref="")]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    assert any("'consult_ref' is missing" in p for p in _request_problems(doc))


def test_row_parse_keeps_entries_and_modes(tmp_path, venue):
    rows = [_row("developer", 1, allow=["Bash(git add:*)"], add_dirs=[(str(venue), "write")])]
    doc = _write(tmp_path, venue, [_dev(1)], rows=rows)
    (row,) = doc.meta.resource_requests
    assert (row.kind, row.stage, row.allow, row.defects) == ("developer", 1, ("Bash(git add:*)",), ())
    assert row.add_dirs == ((str(venue), "write"),)


# --- digest neutrality ------------------------------------------------------------------


def test_rows_and_an_explicit_false_flag_move_no_digest_or_key(tmp_path, venue):
    plain = _write(tmp_path, venue, [_dev(1)], armed=False)
    with_rows = _write(tmp_path, venue, [_dev(1)], armed=False,
                       rows=[_row("developer", 1, allow=["Bash(git add:*)"])])
    explicit_false = _write(tmp_path, venue, [_dev(1)], flag="requires_resource_request = false\n")
    for other in (with_rows, explicit_false):
        assert plan_meta_digest(other) == plan_meta_digest(plain)
        assert stage_closure_digest(other, 1) == stage_closure_digest(plain, 1)
        assert order_place(other.meta) == order_place(plain.meta)
        assert order_scope(other.meta) == order_scope(plain.meta)


def test_arming_moves_the_order_place_key_only(tmp_path, venue):
    plain = _write(tmp_path, venue, [_dev(1)], armed=False)
    armed = _write(tmp_path, venue, [_dev(1)])
    assert order_place(armed.meta) != order_place(plain.meta)
    assert order_scope(armed.meta) == order_scope(plain.meta)
    assert stage_closure_digest(armed, 1) == stage_closure_digest(plain, 1)
